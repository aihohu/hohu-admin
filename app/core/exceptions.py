"""业务异常定义和全局异常处理器"""

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.base_response import ResponseModel
from app.utils.validators import PWD_ERROR_CODE, PWD_ERROR_MSG

logger = logging.getLogger(__name__)


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

    def __init__(self, message: str = "参数错误", error_code: str = ""):
        super().__init__(message=message, error_code=error_code)


class UnprocessableEntityException(BusinessRuleException):
    """业务规则不允许的操作（HTTP 422）。

    继承 ``BusinessRuleException`` 让 ``pytest.raises(BusinessRuleException)``
    兼容既有 service 单测（只校验 error_code，不关心 code）。
    单独覆写 ``self.code = 422`` 让全局 handler 走 422 而非 400。

    用于「请求格式合法但业务语义拒绝」的场景，与 400（请求字段格式错）区分：
    - AI_IMPORT_PREVIEW_INVALID — preview_token 三重校验失败
    - AI_IMPORT_BATCH_RUNNING — 并发 execute 同 batch
    - AI_IMPORT_ALREADY_EXECUTED — 终态 batch 不能重放
    - AI_IMPORT_ILLEGAL_TRANSITION — 状态机非法转换
    - AI_IMPORT_EMPLOYEE_NO_EXISTS — sync_mode=CREATE_ONLY 时 employee_no 已存在
    - AI_IMPORT_BATCH_NOT_FOUND — batch_id 不存在
    - AI_IMPORT_BATCH_NOT_CANCELLABLE — 终态 batch 不能取消
    - AI_EXPORT_ASYNC_REQUIRED — 行数 > 5000，需走异步通道
    """

    def __init__(self, message: str, error_code: str = ""):
        super().__init__(message=message, error_code=error_code)
        self.code = 422


class SSRFBlockedException(BusinessRuleException):
    """SSRF 防护拦截异常。

    error_code: SSRF_BLOCKED — 前端可映射为「请求被安全策略拦截」
    """

    def __init__(
        self, message: str = "请求被 SSRF 防护拦截", error_code: str = "SSRF_BLOCKED"
    ):
        super().__init__(message=message, error_code=error_code)


# ============ 全局异常处理器 ============


def setup_exception_handlers(app: FastAPI):
    """
    配置全局异常处理器

    Args:
        app: FastAPI 应用实例
    """

    # 捕获 FastAPI 原生 HTTPException（如 OAuth2PasswordBearer 自动抛出的 401）
    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException):
        """将 FastAPI 默认的 HTTPException 统一为标准响应格式"""
        content = ResponseModel(code=exc.status_code, msg=str(exc.detail)).model_dump()
        if exc.status_code == 401:
            content["errorCode"] = "UNAUTHORIZED"
        return JSONResponse(status_code=exc.status_code, content=content)

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
    async def all_exception_handler(request: Request, _exc: Exception):
        """处理未捕获的系统异常"""
        logger.exception(
            "Unhandled exception on %s %s",
            request.method,
            request.url.path,
        )
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
        errors = exc.errors()
        first_error = errors[0]
        field_name = first_error["loc"][-1]
        msg = f"参数错误: {field_name} {first_error['msg']}"

        content = ResponseModel(code=422, msg=msg).model_dump()

        # 密码格式错误附加 errorCode
        if PWD_ERROR_MSG in first_error.get("msg", ""):
            content["errorCode"] = PWD_ERROR_CODE

        return JSONResponse(status_code=422, content=content)
