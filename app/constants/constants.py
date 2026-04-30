"""应用级别常量"""

# 用户名常量
ADMIN_USERNAME = "admin"
SUPER_ADMIN_USERNAME = "super_admin"

# 角色编码常量
SUPER_ADMIN_ROLE_CODE = "R_SUPER"
ADMIN_ROLE_CODE = "admin"
USER_ROLE_CODE = "user"

# 菜单类型常量
MENU_TYPE_DIRECTORY = "M"  # 目录
MENU_TYPE_MENU = "C"  # 菜单
MENU_TYPE_BUTTON = "F"  # 按钮

# 状态常量
STATUS_ENABLED = "1"
STATUS_DISABLED = "2"

# 默认值常量
DEFAULT_PAGE_SIZE = 10
DEFAULT_PAGE_CURRENT = 1

# 时间常量
DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 7 * 24 * 60  # 7天
DEFAULT_REFRESH_TOKEN_EXPIRE_DAYS = 7

# Redis 常量
REDIS_BLACKLIST_PREFIX = "blacklist:"
REDIS_BLACKLIST_TTL = 7 * 24 * 3600  # 7天

# 部门常量
DEPT_MAX_LEVEL = 5  # 部门最大层级
IS_PRIMARY_YES = "Y"  # 主部门标识
IS_PRIMARY_NO = "N"  # 非主部门标识

# 数据权限范围常量
DATA_SCOPE_ALL = "1"  # 全部数据权限
DATA_SCOPE_CUSTOM = "2"  # 自定义数据权限
DATA_SCOPE_DEPT = "3"  # 本部门数据权限
DATA_SCOPE_DEPT_AND_SUB = "4"  # 本部门及以下数据权限
DATA_SCOPE_SELF = "5"  # 仅本人数据权限
