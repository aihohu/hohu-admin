from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.exceptions import setup_exception_handlers
from app.core.redis import close_redis
from app.modules.auth.api import router as auth_router
from app.modules.system.api.menu import router as menu_router
from app.modules.system.api.role import router as role_router
from app.modules.system.api.user import router as user_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期管理"""
    yield
    await close_redis()


app = FastAPI(lifespan=lifespan)

# 注册异常处理器
setup_exception_handlers(app)

app.include_router(auth_router, prefix="/auth", tags=["认证模块"])
app.include_router(user_router, prefix="/system/user", tags=["用户管理"])
app.include_router(role_router, prefix="/system/role", tags=["角色管理"])
app.include_router(menu_router, prefix="/system/menu", tags=["菜单管理"])


@app.get("/")
def read_root():
    return {"Hello": "PancakeAdmin"}
