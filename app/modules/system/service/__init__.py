"""Service 层 - 业务逻辑"""

from app.modules.system.service.dict_data_service import dict_data_service
from app.modules.system.service.dict_type_service import dict_type_service
from app.modules.system.service.menu_service import menu_service
from app.modules.system.service.role_service import role_service
from app.modules.system.service.user_service import user_service

__all__ = [
    "user_service",
    "role_service",
    "menu_service",
    "dict_type_service",
    "dict_data_service",
]
