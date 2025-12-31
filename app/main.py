from fastapi import FastAPI

from app.modules.auth.api import router as auth_router
from app.modules.system.api.menu import router as menu_router
from app.modules.system.api.role import router as role_router
from app.modules.system.api.user import router as user_router

app = FastAPI()

app.include_router(auth_router, prefix="/auth", tags=["认证模块"])
app.include_router(user_router, prefix="/system/user", tags=["用户管理"])
app.include_router(role_router, prefix="/system/role", tags=["角色管理"])
app.include_router(menu_router, prefix="/system/menu", tags=["菜单管理"])


@app.get("/")
def read_root():
    return {"Hello": "PancakeAdmin"}
