from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.exceptions import setup_exception_handlers
from app.core.redis import close_redis
from app.middleware.rate_limit_middleware import RateLimitMiddleware
from app.modules.auth.api import router as auth_router
from app.modules.system.api.dept import router as dept_router
from app.modules.system.api.dict_data import router as dict_data_router
from app.modules.system.api.dict_type import router as dict_type_router
from app.modules.system.api.menu import router as menu_router
from app.modules.system.api.role import router as role_router
from app.modules.system.api.user import router as user_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期管理"""
    yield
    await close_redis()


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


@app.get("/")
def read_root():
    return {"Hello": "PancakeAdmin"}
