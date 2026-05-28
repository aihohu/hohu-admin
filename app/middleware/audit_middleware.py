# hohu-admin/app/middleware/audit_middleware.py
import json
import logging
import time

from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.modules.system.models.operation_log import SysOperationLog

logger = logging.getLogger(__name__)

# 需要脱敏的字段名（归一化后的形式，用于匹配任意 case 风格）
SENSITIVE_FIELDS = {
    "password",
    "oldpassword",
    "newpassword",
    "confirmpassword",
    "token",
    "accesstoken",
    "refreshtoken",
    "secret",
    "apikey",
}

# HTTP 方法到操作类型的映射
METHOD_ACTION_MAP = {
    "POST": "create",
    "PUT": "update",
    "DELETE": "delete",
    "PATCH": "update",
}

# 不记录的路径前缀
EXCLUDED_PATHS = (
    "/docs",
    "/redoc",
    "/openapi.json",
    "/health",
    "/system/operation-log",
    "/system/login-log",
    "/ai/chat",
)


def _get_user_info(request) -> tuple[int, str] | None:
    """从请求头中解析 JWT 获取 user_id 和 username"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        user_id = int(payload.get("sub"))
        username = payload.get("username", "")
        return user_id, username
    except (JWTError, ValueError):
        return None


def _extract_module(path: str) -> str:
    """从 URL 路径提取业务模块名，如 /system/user -> system"""
    parts = path.strip("/").split("/")
    if len(parts) >= 1:
        return parts[0]
    return "unknown"


def _mask_sensitive(data: dict) -> dict:
    """脱敏敏感字段（归一化 key：去除下划线后小写比较）"""
    masked = {}
    for key, value in data.items():
        normalized = key.replace("_", "").replace("-", "").lower()
        if normalized in SENSITIVE_FIELDS:
            masked[key] = "***"
        elif isinstance(value, dict):
            masked[key] = _mask_sensitive(value)
        else:
            masked[key] = value
    return masked


def _truncate_params(params_str: str, max_length: int = 2000) -> str:
    """截断参数字符串"""
    if len(params_str) > max_length:
        return params_str[:max_length] + "...(truncated)"
    return params_str


class AuditLogMiddleware(BaseHTTPMiddleware):
    """操作审计中间件"""

    async def dispatch(self, request, call_next):
        # 只拦截写操作
        if request.method not in METHOD_ACTION_MAP:
            return await call_next(request)

        path = request.url.path

        # 跳过排除路径
        if any(path.startswith(prefix) for prefix in EXCLUDED_PATHS):
            return await call_next(request)

        # 获取用户信息
        user_info = _get_user_info(request)
        if not user_info:
            return await call_next(request)

        user_id, username = user_info

        # 读取并缓存 request body
        request_params = None
        if request.method in ("POST", "PUT", "PATCH"):
            content_type = request.headers.get("content-type", "")
            is_json = "application/json" in content_type
            try:
                body = await request.body()
                if body:
                    # 缓存回 request，使后续处理器可读
                    async def receive():
                        return {"type": "http.request", "body": body}

                    request._receive = receive

                    if is_json:
                        parsed = json.loads(body)
                        if isinstance(parsed, dict):
                            parsed = _mask_sensitive(parsed)
                        request_params = _truncate_params(
                            json.dumps(parsed, ensure_ascii=False)
                        )
            except json.JSONDecodeError:
                request_params = None
            except Exception:
                logger.warning(
                    "Failed to read request body for audit log", exc_info=True
                )
                request_params = None

        # 记录开始时间
        start_time = time.perf_counter()

        # 执行业务处理
        response = await call_next(request)

        # 计算耗时
        duration = int((time.perf_counter() - start_time) * 1000)

        # 异步写入操作日志（使用独立 session）
        try:
            async with AsyncSessionLocal() as session:
                log = SysOperationLog(
                    user_id=user_id,
                    username=username,
                    module=_extract_module(path),
                    action=METHOD_ACTION_MAP[request.method],
                    method=request.method,
                    path=path,
                    request_params=request_params,
                    status_code=response.status_code,
                    ip=request.client.host if request.client else None,
                    duration=duration,
                )
                session.add(log)
                await session.commit()
        except Exception:
            logger.exception("Failed to write operation log")

        return response
