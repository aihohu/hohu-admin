import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import app.tasks  # noqa: F401  # pyright: ignore[reportUnusedImport]
from app.core.config import settings
from app.core.exceptions import setup_exception_handlers
from app.core.file_storage import validate_private_storage_roots
from app.core.public_uploads import PublicUploadStaticFiles
from app.core.redis import close_redis
from app.core.scheduler import scheduler_manager
from app.db.session import AsyncSessionLocal
from app.middleware.audit_middleware import AuditLogMiddleware
from app.middleware.rate_limit_middleware import RateLimitMiddleware
from app.modules.auth.api import router as auth_router
from app.modules.job.api.job import router as job_router
from app.modules.job.api.job_log import router as job_log_router
from app.modules.job.job_runner import RUNNER_ID
from app.modules.job.log_monitor import JobLogMonitor
from app.modules.marketplace.api.admin import router as marketplace_admin_router
from app.modules.marketplace.api.app_data import router as app_data_router
from app.modules.marketplace.api.contributes import (
    router as contributes_router,
)
from app.modules.marketplace.api.developer import (
    router as marketplace_developer_router,
)
from app.modules.marketplace.api.marketplace import router as marketplace_router
from app.modules.system.api.config import router as config_router
from app.modules.system.api.data_scope_demo import router as data_scope_demo_router
from app.modules.system.api.dept import router as dept_router
from app.modules.system.api.dict_data import router as dict_data_router
from app.modules.system.api.dict_type import router as dict_type_router
from app.modules.system.api.file import router as file_router
from app.modules.system.api.login_log import router as login_log_router
from app.modules.system.api.menu import router as menu_router
from app.modules.system.api.operation_log import router as operation_log_router
from app.modules.system.api.role import router as role_router
from app.modules.system.api.user import router as user_router

if settings.AI_MODULE_ENABLED:
    from app.modules.ai.agents.tools import load_builtin_tools
    from app.modules.ai.agents.tools.registry import ToolRegistry, ToolRegistryError
    from app.modules.ai.api.agent import admin_router as ai_agent_admin_router
    from app.modules.ai.api.agent import router as ai_agent_router
    from app.modules.ai.api.chat import router as ai_chat_router
    from app.modules.ai.api.confirm import router as ai_confirm_router
    from app.modules.ai.api.conversation import router as ai_conversation_router
    from app.modules.ai.api.download import router as ai_download_router
    from app.modules.ai.api.operation_log import router as ai_operation_log_router
    from app.modules.ai.api.provider import router as ai_provider_router
    from app.modules.ai.api.query_cache import router as ai_query_cache_router
    from app.modules.ai.api.resume import router as ai_resume_router
    from app.modules.ai.api.role_agent import router as ai_role_agent_router
    from app.modules.ai.api.routing_feedback import (
        query_router as ai_routing_feedback_query_router,
    )
    from app.modules.ai.api.routing_feedback import (
        router as ai_routing_feedback_router,
    )
    from app.modules.ai.core.provider_egress import close_provider_http_clients
    from app.modules.ai.lifecycle import (
        cleanup_durable_prepared_actions_on_startup,
        cleanup_orphaned_pending_on_startup,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期管理"""
    # 保留实例引用供 shutdown 清理；启动中途失败时也不能遗失已创建的监控器。
    job_log_monitor: JobLogMonitor | None = None

    if settings.AI_MODULE_ENABLED:
        # memory 模式必须实测单 worker；uvicorn --workers 启动时环境变量并不可靠，
        # 必须用 Redis SADD 实测活跃 worker 数。
        if settings.AI_HITL_MODE == "memory" and settings.AI_REQUIRE_SINGLE_WORKER:
            worker_count = await _detect_actual_worker_count()
            if worker_count > 1:
                raise RuntimeError(
                    "AI HITL memory mode requires single worker, "
                    f"detected {worker_count}. Set AI_HITL_MODE=redis_pubsub or "
                    "scale workers down to 1. See docs/AI-DEPLOYMENT.md."
                )

        # 导入内置工具以触发装饰器注册，并校验 Agent 与权限引用存在。
        load_builtin_tools()
        try:
            async with AsyncSessionLocal() as db:
                await ToolRegistry.get().validate_on_startup(db)
        except ToolRegistryError as e:
            # 业务迭代中的引用漂移仅告警，不阻断非安全相关启动。
            logging.getLogger("app.ai").error("AI Tool Registry 启动校验失败: %s", e)

        # 重启后进程内事件已丢失；清理或恢复持久化确认。
        if settings.AI_HITL_MODE == "memory":
            await cleanup_orphaned_pending_on_startup()
        else:
            await cleanup_durable_prepared_actions_on_startup()

    # 仅在嵌入式（开发）模式下随 API 启停调度器。
    # 生产模式下调度器由独立的 `app.scheduler_worker` 进程承担，
    # 通过 Redis pub/sub 与本进程通信。
    if settings.APP_ROLE == "all":
        async with AsyncSessionLocal() as db:
            await scheduler_manager.reload_jobs(db)
        await scheduler_manager.start_with_pubsub()

        # 启动时扫描一次孤儿任务日志，随后进入周期监控。
        job_log_monitor = JobLogMonitor(runner_id=RUNNER_ID)
        await job_log_monitor.start()

    yield

    if job_log_monitor is not None:
        await job_log_monitor.stop()
    if settings.APP_ROLE == "all":
        scheduler_manager.shutdown()
    # 修订 S-6：lifespan 结束时从 Redis active worker 集合移除自己，让下次启动
    # 拿到准确计数（不依赖 30s EXPIRE 自然过期）
    if (
        settings.AI_MODULE_ENABLED
        and settings.AI_HITL_MODE == "memory"
        and settings.AI_REQUIRE_SINGLE_WORKER
    ):
        await _unregister_active_worker()
    if settings.AI_MODULE_ENABLED:
        await close_provider_http_clients()
    await close_redis()


# ============ 修订 S-6: Redis-based worker count 实测 ============

_WORKER_ACTIVE_KEY = "ai:workers:active"
_WORKER_TTL_SEC = 30  # 30s 内未续期的 worker 视为挂掉


def _worker_uid() -> str:
    """本进程的稳定唯一标识（pid + 启动时随机 hex）"""
    import os  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    return f"{os.getpid()}:{uuid.uuid4().hex}"


# 进程级 uid（lifespan 内不变，便于 unregister）
_CURRENT_WORKER_UID: str | None = None


async def _detect_actual_worker_count() -> int:
    """修订 S-6: Redis SADD 实测活跃 worker 数。

    各 worker 启动时：
      1. SADD ai:workers:active <uid>
      2. EXPIRE 30s（防止 worker 崩溃后 key 永远残留）
      3. SCARD 查当前活跃数

    Returns:
        当前 Redis 集合中的活跃 worker 数
    """
    global _CURRENT_WORKER_UID
    _CURRENT_WORKER_UID = _worker_uid()

    from app.core.redis import redis_client  # noqa: PLC0415

    await redis_client.sadd(_WORKER_ACTIVE_KEY, _CURRENT_WORKER_UID)
    await redis_client.expire(_WORKER_ACTIVE_KEY, _WORKER_TTL_SEC)
    return await redis_client.scard(_WORKER_ACTIVE_KEY)


async def _unregister_active_worker() -> None:
    """lifespan 结束时从 Redis 活跃 worker 集合移除自己"""
    global _CURRENT_WORKER_UID
    if _CURRENT_WORKER_UID is None:
        return
    try:
        from app.core.redis import redis_client  # noqa: PLC0415

        await redis_client.srem(_WORKER_ACTIVE_KEY, _CURRENT_WORKER_UID)
    except Exception:
        # Redis down 不阻断 shutdown
        pass
    _CURRENT_WORKER_UID = None


if os.getenv("ENV") == "prod":
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
else:
    app = FastAPI(lifespan=lifespan)

# 添加频率限制中间件
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuditLogMiddleware)

# 注册异常处理器
setup_exception_handlers(app)

app.include_router(auth_router, prefix="/auth", tags=["认证模块"])
app.include_router(user_router, prefix="/system/user", tags=["用户管理"])
app.include_router(
    data_scope_demo_router,
    prefix="/system/data-scope-demo",
    tags=["数据权限演示"],
)
app.include_router(config_router, prefix="/system/config", tags=["系统配置管理"])
app.include_router(role_router, prefix="/system/role", tags=["角色管理"])
app.include_router(dept_router, prefix="/system/dept", tags=["部门管理"])
app.include_router(menu_router, prefix="/system/menu", tags=["菜单管理"])
app.include_router(dict_type_router, prefix="/system/dict-type", tags=["字典类型管理"])
app.include_router(dict_data_router, prefix="/system/dict-data", tags=["字典数据管理"])
app.include_router(file_router, prefix="/system/file", tags=["文件管理"])
app.include_router(job_router, prefix="/system/job", tags=["定时任务"])
app.include_router(job_log_router, prefix="/system/job-log", tags=["任务日志"])
app.include_router(
    operation_log_router, prefix="/system/operation-log", tags=["操作日志"]
)
app.include_router(login_log_router, prefix="/system/login-log", tags=["登录日志"])

# 关闭 AI 模块时不注册任何 AI 路由。
if settings.AI_MODULE_ENABLED:
    app.include_router(ai_agent_router, prefix="/ai/agents", tags=["AI Agent"])
    app.include_router(
        ai_agent_admin_router, prefix="/ai/admin/agents", tags=["AI Agent 管理"]
    )
    app.include_router(ai_chat_router, prefix="/ai/chat", tags=["AI对话"])
    app.include_router(ai_resume_router, prefix="/ai/chat", tags=["AI对话"])
    app.include_router(ai_download_router, prefix="/ai/download", tags=["AI结果下载"])
    app.include_router(ai_confirm_router, prefix="/ai/confirm", tags=["AI HITL 确认"])
    app.include_router(
        ai_conversation_router, prefix="/ai/conversation", tags=["AI会话"]
    )
    app.include_router(ai_provider_router, prefix="/ai/provider", tags=["AI提供商"])
    app.include_router(
        ai_operation_log_router, prefix="/ai/operation-log", tags=["AI 操作日志"]
    )
    app.include_router(
        ai_query_cache_router, prefix="/ai/query-cache", tags=["AI chip 跳转回放"]
    )
    app.include_router(
        ai_routing_feedback_router,
        prefix="/ai/messages",
        tags=["AI 路由反馈"],
    )
    # Agent 路由反馈指标和明细。
    app.include_router(
        ai_routing_feedback_query_router,
        prefix="/ai/routing-feedback",
        tags=["AI 路由反馈"],
    )
    # Role 与 Agent 的管理绑定。
    app.include_router(
        ai_role_agent_router, prefix="/ai/role-agent", tags=["AI Role-Agent 绑定"]
    )
else:
    _AI_DISABLED_METHODS = [
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "HEAD",
        "TRACE",
        "CONNECT",
    ]

    @app.api_route(
        "/ai",
        methods=_AI_DISABLED_METHODS,
        include_in_schema=False,
    )
    @app.api_route(
        "/ai/{path:path}",
        methods=_AI_DISABLED_METHODS,
        include_in_schema=False,
    )
    async def ai_module_disabled(path: str = "") -> JSONResponse:
        """紧急熔断入口：全部 AI 路径使用同一稳定拒绝面。"""
        _ = path
        return JSONResponse(
            status_code=503,
            content={
                "code": 503,
                "msg": "AI 模块已关闭",
                "data": None,
                "errorCode": "AI_MODULE_DISABLED",
            },
        )


# Marketplace（注册顺序：developer/admin 先注册，避免被 marketplace 抢匹配）
app.include_router(
    marketplace_developer_router,
    prefix="/marketplace/developer",
    tags=["开发者中心"],
)
app.include_router(
    marketplace_admin_router, prefix="/marketplace/admin", tags=["市场管理"]
)
app.include_router(marketplace_router, prefix="/marketplace", tags=["应用市场"])
# 低代码动态数据 CRUD（app_data_* 表）
app.include_router(app_data_router, prefix="/api/v1/app-data", tags=["应用数据"])
# 前端初始化加载 contributes 缓存（menu + pages）
app.include_router(
    contributes_router, prefix="/api/v1/contributes", tags=["contributes"]
)

# 公共上传目录可静态访问；私有上传目录只创建，禁止 StaticFiles mount。
validate_private_storage_roots()
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.PRIVATE_UPLOAD_DIR, exist_ok=True)
app.mount(
    "/uploads",
    PublicUploadStaticFiles(directory=settings.UPLOAD_DIR),
    name="uploads",
)


# 健康检查
@app.get("/health", include_in_schema=False)
async def health_check():
    return {"status": "ok"}


# 不进 ResponseModel 包装；生产用 nginx/ingress 限制 /metrics 只允许内网 / Prometheus scrape IP。
@app.get("/metrics", include_in_schema=False)
async def metrics():
    return PlainTextResponse(
        generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
