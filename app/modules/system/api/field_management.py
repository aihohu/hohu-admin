"""Fixed management routes shared by normal and closed-admission HTTP transports."""

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import ResponseModel
from app.core.exceptions import BusinessException
from app.core.field_policy import Catalog
from app.core.field_policy.documents import read_json
from app.db.authorization_http import get_authorization_runtime, release_request_reads
from app.db.session import get_db
from app.modules.auth.service import access_identity, get_current_user
from app.modules.system.field_policy.generic_compat import user_compat_object
from app.modules.system.models.user import User
from app.modules.system.schemas.field_management import (
    Build,
    Freeze,
    ManifestDraft,
    ObjectDraft,
    Publish,
)


def request_contract(model):
    """Expose the exact closed schema without FastAPI buffering an unlimited body."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(definitions[value["$ref"].rsplit("/", 1)[-1]])
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": expand(schema)}},
        }
    }


async def bounded_body(request, maximum=524288):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise BusinessException(
                413, "请求超出预算", error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
            )
        body.extend(chunk)
    try:
        return read_json(body)
    except (ValueError, TypeError, BusinessException):
        raise BusinessException(
            422, "字段策略请求无效", error_code="FIELD_POLICY_INVALID"
        ) from None


async def regular_context(
    db: AsyncSession = Depends(get_db), actor: User = Depends(get_current_user)
):
    identity = access_identity(actor)
    await release_request_reads(db)

    async def execute(*, operation, payload, **target):
        return await get_authorization_runtime().field_management(
            tenant=identity.tenant,
            actor_user_version=identity.user_version,
            catalog=Catalog((user_compat_object(),), ()),
            operation=operation,
            payload=payload,
            **target,
        )

    return execute


def create_management_router(context=regular_context):
    router = APIRouter()

    async def response(execute, operation, payload=None, **target):
        return ResponseModel.success(
            data=await execute(operation=operation, payload=payload or {}, **target)
        )

    @router.get("/catalog")
    async def catalog(
        cursor: str = "0",
        limit: int = Query(default=100, ge=1, le=100),
        execute=Depends(context),
    ):
        return await response(execute, "catalog", {"cursor": cursor, "limit": limit})

    @router.get("/roles")
    async def roles(
        cursor: str = "0",
        limit: int = Query(default=100, ge=1, le=100),
        execute=Depends(context),
    ):
        return await response(execute, "roles", {"cursor": cursor, "limit": limit})

    @router.get("/roles/{role_id}/objects/{object_id}")
    async def object_policy(
        role_id: int = Path(gt=0, lt=2**63),
        object_id: str = Path(max_length=96),
        execute=Depends(context),
    ):
        return await response(execute, "object", role_id=role_id, object_id=object_id)

    @router.post(
        "/roles/{role_id}/objects/{object_id}/preview",
        openapi_extra=request_contract(ObjectDraft),
    )
    async def preview(
        request: Request,
        role_id: int = Path(gt=0, lt=2**63),
        object_id: str = Path(max_length=96),
        execute=Depends(context),
    ):
        return await response(
            execute,
            "preview",
            await bounded_body(request),
            role_id=role_id,
            object_id=object_id,
        )

    @router.post(
        "/roles/{role_id}/objects/{object_id}/revisions",
        openapi_extra=request_contract(Publish),
    )
    async def publish(
        request: Request,
        role_id: int = Path(gt=0, lt=2**63),
        object_id: str = Path(max_length=96),
        execute=Depends(context),
    ):
        return await response(
            execute,
            "publish",
            await bounded_body(request),
            role_id=role_id,
            object_id=object_id,
        )

    @router.get("/manifests/current")
    async def manifest(execute=Depends(context)):
        return await response(execute, "manifest")

    @router.post("/manifests/preview", openapi_extra=request_contract(ManifestDraft))
    async def manifest_preview(request: Request, execute=Depends(context)):
        return await response(execute, "manifest-preview", await bounded_body(request))

    @router.post("/manifests", openapi_extra=request_contract(Build))
    async def build(request: Request, execute=Depends(context)):
        return await response(execute, "build", await bounded_body(request))

    @router.post("/manifests/{manifest_id}/ready")
    async def ready(
        request: Request,
        _manifest_id: int = Path(alias="manifest_id", gt=0, lt=2**63),
        execute=Depends(context),
    ):
        return await response(execute, "ready", await bounded_body(request))

    @router.post("/manifests/{manifest_id}/activate")
    async def activate(
        request: Request,
        _manifest_id: int = Path(alias="manifest_id", gt=0, lt=2**63),
        execute=Depends(context),
    ):
        return await response(execute, "activate", await bounded_body(request))

    @router.post("/freeze", openapi_extra=request_contract(Freeze))
    async def freeze(request: Request, execute=Depends(context)):
        return await response(execute, "freeze", await bounded_body(request))

    @router.get("/requests/{request_id}")
    async def receipt(
        request_id: str = Path(min_length=36, max_length=36), execute=Depends(context)
    ):
        return await response(execute, "receipt", {"requestId": request_id})

    return router


router = create_management_router()
