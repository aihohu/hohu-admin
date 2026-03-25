"""业务异常定义和全局异常处理器"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.base_response import ResponseModel

# ============ 业务异常类定义 ============


class BusinessException(Exception):
    """
    业务异常基类

    所有业务异常都应该继承此类
    """

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(self.message)


class NotFoundException(BusinessException):
    """资源不存在异常"""

    def __init__(self, resource_type: str = "资源"):
        super().__init__(
            code=404,
            msg=f"{resource_type}不存在",
            data={"resource_type": resource_type},
        )


class UserNotFoundException(NotFoundException):
    """用户不存在异常"""

    def __init__(self):
        super().__init__(resource_type="用户")


class RoleNotFoundException(NotFoundException):
    """角色不存在异常"""

    def __init__(self):
        super().__init__(resource_type="角色")


class MenuNotFoundException(NotFoundException):
    """菜单不存在异常"""

    def __init__(self):
        super().__init__(resource_type="菜单")


class DuplicateException(BusinessException):
    """资源重复异常"""

    def __init__(self, field: str, value: str):
        super().__init__(
            code=400, msg=f"{field}已存在", data={"field": field, "value": value}
        )


class DuplicateUserException(DuplicateException):
    """用户名已存在异常"""

    def __init__(self, username: str = ""):
        super().__init__(field="用户名", value=username)


class DuplicateRoleException(DuplicateException):
    """角色编码已存在异常"""

    def __init__(self, role_code: str = ""):
        super().__init__(field="角色编码", value=role_code)


class ValidationException(BusinessException):
    """参数验证异常"""

    def __init__(self, message: str, field: str = None):
        super().__init__(
            code=400, msg=message, data={"field": field} if field else None
        )


class AuthenticationException(BusinessException):
    """认证失败异常"""

    def __init__(self, message: str = "账号或密码错误"):
        super().__init__(code=401, msg=message)


class AuthorizationException(BusinessException):
    """授权失败异常"""

    def __init__(self, message: str = "权限不足"):
        super().__init__(code=403, msg=message)


class AccountDisabledException(AuthorizationException):
    """账号已被禁用异常"""

    def __init__(self):
        super().__init__(msg="账号已被禁用")


class BusinessRuleException(BusinessException):
    """业务规则异常"""

    def __init__(self, message: str):
        super().__init__(code=400, msg=message)


class CannotDeleteAdminException(BusinessRuleException):
    """不能删除管理员异常"""

    def __init__(self, admin_type: str = "系统管理员"):
        super().__init__(msg=f"不能删除{admin_type}")


class CannotDeleteSelfException(BusinessRuleException):
    """不能删除当前登录账号异常"""

    def __init__(self):
        super().__init__(msg="不能删除当前登录的账号")


class HasChildrenException(BusinessRuleException):
    """存在子节点异常"""

    def __init__(self, resource_type: str = "菜单"):
        super().__init__(msg=f"请先删除{resource_type}的子节点")


class InvalidParameterException(BusinessRuleException):
    """无效参数异常"""

    def __init__(self, message: str = "参数错误"):
        super().__init__(msg=message)


class UnsupportedLoginMethodException(BusinessRuleException):
    """不支持的登录方式异常"""

    def __init__(self):
        super().__init__(msg="不支持的登录方式")


class DictTypeNotFoundException(NotFoundException):
    """字典类型不存在异常"""

    def __init__(self):
        super().__init__(resource_type="字典类型")


class DictDataNotFoundException(NotFoundException):
    """字典数据不存在异常"""

    def __init__(self):
        super().__init__(resource_type="字典数据")


class DuplicateDictTypeException(DuplicateException):
    """字典类型已存在异常"""

    def __init__(self, dict_type: str = ""):
        super().__init__(field="字典类型", value=dict_type)


class InvalidDictTypeException(BusinessRuleException):
    """无效的字典类型异常"""

    def __init__(self, dict_type: str = ""):
        super().__init__(msg=f"字典类型 {dict_type} 不存在")


class HasDictDataException(BusinessRuleException):
    """字典类型下有数据异常"""

    def __init__(self):
        super().__init__(msg="该字典类型下存在数据，请先删除数据")


# ============ 全局异常处理器 ============


def setup_exception_handlers(app: FastAPI):
    """
    配置全局异常处理器

    Args:
        app: FastAPI 应用实例
    """

    # 1. 捕获业务异常
    @app.exception_handler(BusinessException)
    async def business_exception_handler(_request: Request, exc: BusinessException):
        """处理所有业务异常"""
        return JSONResponse(
            status_code=exc.code,
            content=ResponseModel(
                code=exc.code, msg=exc.message, data=exc.data
            ).model_dump(),
        )

    # 2. 捕获所有未知的系统异常
    @app.exception_handler(Exception)
    async def all_exception_handler(_request: Request, _exc: Exception):
        """处理未捕获的系统异常"""
        # 生产环境中应该记录日志
        # import logging
        # logger = logging.getLogger(__name__)
        # logger.exception("Unhandled exception occurred")
        return JSONResponse(
            status_code=500,
            content=ResponseModel.error(msg="服务器内部错误").model_dump(),
        )

    # 3. 捕获 Pydantic 参数校验错误 (422 错误)
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ):
        """处理参数验证错误"""
        # 提取具体的错误字段和原因
        errors = exc.errors()
        msg = f"参数错误: {errors[0]['loc'][-1]} {errors[0]['msg']}"
        return JSONResponse(
            status_code=422,
            content=ResponseModel(code=422, msg=msg).model_dump(),
        )
