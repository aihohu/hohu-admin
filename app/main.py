import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import app.tasks  # noqa: F401  # pyright: ignore[reportUnusedImport]
from app.core.config import settings
from app.core.exceptions import setup_exception_handlers
from app.core.redis import close_redis
from app.core.scheduler import scheduler_manager
from app.db.session import AsyncSessionLocal
from app.middleware.audit_middleware import AuditLogMiddleware
from app.middleware.rate_limit_middleware import RateLimitMiddleware
from app.modules.ai.api.chat import router as ai_chat_router
from app.modules.ai.api.conversation import router as ai_conversation_router
from app.modules.ai.api.provider import router as ai_provider_router
from app.modules.auth.api import router as auth_router
from app.modules.job.api.job import router as job_router
from app.modules.job.api.job_log import router as job_log_router
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
from app.modules.system.api.dept import router as dept_router
from app.modules.system.api.dict_data import router as dict_data_router
from app.modules.system.api.dict_type import router as dict_type_router
from app.modules.system.api.file import router as file_router
from app.modules.system.api.login_log import router as login_log_router
from app.modules.system.api.menu import router as menu_router
from app.modules.system.api.operation_log import router as operation_log_router
from app.modules.system.api.role import router as role_router
from app.modules.system.api.user import router as user_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期管理"""
    # 仅在嵌入式（开发）模式下随 API 启停调度器。
    # 生产模式下调度器由独立的 `app.scheduler_worker` 进程承担，
    # 通过 Redis pub/sub 与本进程通信。
    if settings.APP_ROLE == "all":
        async with AsyncSessionLocal() as db:
            await scheduler_manager.reload_jobs(db)
        await scheduler_manager.start_with_pubsub()

    yield

    if settings.APP_ROLE == "all":
        scheduler_manager.shutdown()
    await close_redis()


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
app.include_router(ai_chat_router, prefix="/ai/chat", tags=["AI对话"])
app.include_router(ai_conversation_router, prefix="/ai/conversation", tags=["AI会话"])
app.include_router(ai_provider_router, prefix="/ai/provider", tags=["AI提供商"])
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

# 确保上传目录存在
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")


# 健康检查
@app.get("/health", include_in_schema=False)
async def health_check():
    return {"status": "ok"}
