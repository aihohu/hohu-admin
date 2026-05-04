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
    error_code: 机器可读的错误编码，用于前端 i18n 映射
    """

    def __init__(self, code: int, message: str, data: Any = None, error_code: str = ""):
        self.code = code
        self.message = message
        self.data = data
        self.error_code = error_code
        super().__init__(self.message)


class NotFoundException(BusinessException):
    """资源不存在异常"""

    def __init__(self, resource_type: str = "资源", error_code: str = ""):
        super().__init__(
            code=404,
            message=f"{resource_type}不存在",
            data={"resource_type": resource_type},
            error_code=error_code,
        )


class DuplicateException(BusinessException):
    """资源重复异常"""

    def __init__(self, field: str, value: str, error_code: str = ""):
        super().__init__(
            code=400,
            message=f"{field}已存在",
            data={"field": field, "value": value},
            error_code=error_code,
        )


class AuthenticationException(BusinessException):
    """认证失败异常"""

    def __init__(self, message: str = "账号或密码错误", error_code: str = ""):
        super().__init__(code=401, message=message, error_code=error_code)


class AuthorizationException(BusinessException):
    """授权失败异常"""

    def __init__(self, message: str = "权限不足", error_code: str = ""):
        super().__init__(code=403, message=message, error_code=error_code)


class BusinessRuleException(BusinessException):
    """业务规则异常"""

    def __init__(self, message: str, error_code: str = ""):
        super().__init__(code=400, message=message, error_code=error_code)


class InvalidParameterException(BusinessRuleException):
    """无效参数异常"""

    def __init__(self, message: str = "参数错误"):
        super().__init__(message=message)


# ============ 全局异常处理器 ============


def setup_exception_handlers(app: FastAPI):
    """
    配置全局异常处理器

    Args:
        app: FastAPI 应用实例
    """

    # 捕获业务异常
    @app.exception_handler(BusinessException)
    async def business_exception_handler(_request: Request, exc: BusinessException):
        """处理所有业务异常"""
        content = ResponseModel(
            code=exc.code, msg=exc.message, data=exc.data
        ).model_dump()
        if exc.error_code:
            content["errorCode"] = exc.error_code
        return JSONResponse(status_code=exc.code, content=content)

    # 捕获所有未知的系统异常
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

    # 捕获 Pydantic 参数校验错误 (422 错误)
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
