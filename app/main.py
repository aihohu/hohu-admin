import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.exceptions import setup_exception_handlers
from app.core.redis import close_redis
from app.core.scheduler import scheduler_manager
from app.middleware.rate_limit_middleware import RateLimitMiddleware
from app.modules.auth.api import router as auth_router
from app.modules.job.api.job import router as job_router
from app.modules.job.api.job_log import router as job_log_router
from app.modules.system.api.dept import router as dept_router
from app.modules.system.api.dict_data import router as dict_data_router
from app.modules.system.api.dict_type import router as dict_type_router
from app.modules.system.api.file import router as file_router
from app.modules.system.api.menu import router as menu_router
from app.modules.system.api.role import router as role_router
from app.modules.system.api.user import router as user_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期管理"""
    # 启动调度器
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await scheduler_manager.load_jobs_from_db(db)
    scheduler_manager.start()

    yield

    # 关闭调度器和Redis
    scheduler_manager.shutdown()
    await close_redis()


if os.getenv("ENV") == "prod":
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
else:
    app = FastAPI(lifespan=lifespan)

# 添加频率限制中间件
app.add_middleware(RateLimitMiddleware)

# 注册异常处理器
setup_exception_handlers(app)

app.include_router(auth_router, prefix="/auth", tags=["认证模块"])
app.include_router(user_router, prefix="/system/user", tags=["用户管理"])
app.include_router(role_router, prefix="/system/role", tags=["角色管理"])
app.include_router(dept_router, prefix="/system/dept", tags=["部门管理"])
app.include_router(menu_router, prefix="/system/menu", tags=["菜单管理"])
app.include_router(dict_type_router, prefix="/system/dict-type", tags=["字典类型管理"])
app.include_router(dict_data_router, prefix="/system/dict-data", tags=["字典数据管理"])
app.include_router(file_router, prefix="/system/file", tags=["文件管理"])
app.include_router(job_router, prefix="/system/job", tags=["定时任务"])
app.include_router(job_log_router, prefix="/system/job-log", tags=["任务日志"])

# 确保上传目录存在
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="uploads")


# 健康检查
@app.get("/health", include_in_schema=False)
async def health_check():
    return {"status": "ok"}
