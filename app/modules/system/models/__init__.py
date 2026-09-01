from .config import Config
from .data_scope_demo import DataScopeDemo
from .dept import Dept
from .dict_data import DictData
from .dict_type import DictType
from .file import File
from .login_log import SysLoginLog
from .menu import Menu
from .operation_log import SysOperationLog
from .role import Role
from .tenant import Tenant
from .user import User

__all__ = [
    "User",
    "Role",
    "Menu",
    "Dept",
    "DictType",
    "DictData",
    "File",
    "Config",
    "SysOperationLog",
    "SysLoginLog",
    "DataScopeDemo",
    "Tenant",
]
