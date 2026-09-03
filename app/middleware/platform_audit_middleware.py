import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware

from app.core.exceptions import BusinessException
from app.modules.platform.audit import persist_platform_completion

logger = logging.getLogger(__name__)


class PlatformAuditMiddleware(BaseHTTPMiddleware):
    """Append a completion event linked to the pre-business authorization intent."""

    async def dispatch(self, request, call_next):
        started = time.perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        except Exception:
            raise
        finally:
            authorization = getattr(request.state, "platform_authorization", None)
            if authorization is not None:
                context = authorization.context
                status_code = 500 if response is None else response.status_code
                duration_ms = int((time.perf_counter() - started) * 1000)
                result_summary = {"statusCode": status_code}
                extra_summary = getattr(request.state, "platform_result_summary", None)
                if isinstance(extra_summary, dict):
                    result_summary.update(extra_summary)
                values = {
                    "actor_principal_id": context.actor_principal_id,
                    "actor_name": context.actor_name,
                    "permission": request.state.platform_permission,
                    "method": request.method,
                    "path": authorization.audit_path,
                    "reason": context.reason,
                    "ticket_id": context.ticket_id,
                    "correlation_id": context.correlation_id,
                    "ip": getattr(request.state, "platform_ip", None),
                    "target_tenant_id": context.target_tenant_id,
                    "authorization_audit_id": authorization.authorization_audit_id,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                    "result_summary": result_summary,
                }
                completion_error = None
                for attempt in range(2):
                    try:
                        await persist_platform_completion(**values)
                        completion_error = None
                        break
                    except BusinessException as exc:
                        completion_error = exc
                        if (
                            exc.error_code == "PLATFORM_AUDIT_UNAVAILABLE"
                            and attempt == 0
                        ):
                            continue
                        break
                    except Exception as exc:
                        completion_error = exc
                        if attempt == 0:
                            continue
                        break
                if completion_error is not None:
                    logger.error(
                        "Failed to append platform completion audit (%s)",
                        type(completion_error).__name__,
                        extra={
                            "authorization_audit_id": (
                                authorization.authorization_audit_id
                            ),
                        },
                    )
