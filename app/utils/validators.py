"""通用校验工具函数"""

from re import match

# ============ 密码 ============
PWD_PATTERN = r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{6,20}$"
PWD_ERROR_CODE = "INVALID_PASSWORD_FORMAT"
PWD_ERROR_MSG = "密码必须为6-20位，且包含大写字母、小写字母和数字"


def validate_password(v: str) -> str:
    """校验密码强度，不通过则抛出 ValueError"""
    if not match(PWD_PATTERN, v):
        raise ValueError(PWD_ERROR_MSG)
    return v


# ============ 手机号 ============
PHONE_PATTERN = r"^1[3-9]\d{9}$"
PHONE_ERROR_MSG = "手机号格式不正确"


def validate_phone(v: str | None) -> str | None:
    """校验手机号，空值转 None"""
    if not v:
        return None
    if not match(PHONE_PATTERN, v):
        raise ValueError(PHONE_ERROR_MSG)
    return v


# ============ 用户名 ============
USER_NAME_ERROR_MSG = "用户名只能包含字母和数字"


def validate_user_name(v: str) -> str:
    """校验用户名，只允许字母和数字"""
    if not v.isalnum():
        raise ValueError(USER_NAME_ERROR_MSG)
    return v


# ============ 性别 ============
GENDER_ALLOWED = ("0", "1", "2")
GENDER_ERROR_MSG = "性别必须是 0(未知)、1(男) 或 2(女)"


def validate_gender(v: str | None) -> str | None:
    """校验性别，空值转 None"""
    if not v:
        return None
    if v not in GENDER_ALLOWED:
        raise ValueError(GENDER_ERROR_MSG)
    return v


# ============ 可选字符串 ============
def empty_to_none(v: str | None) -> str | None:
    """空字符串转 None"""
    if not v:
        return None
    return v


# ============ 状态 ============
STATUS_ALLOWED = ("1", "2")
STATUS_ERROR_MSG = "状态必须是 1(启用) 或 2(禁用)"


def validate_status(v: str) -> str:
    """校验启用/禁用状态"""
    if v not in STATUS_ALLOWED:
        raise ValueError(STATUS_ERROR_MSG)
    return v
