"""Both transports use a bounded closed DTO and server-owned identity."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import BusinessException
from app.modules.system.api.field_management import create_management_router


@pytest.mark.parametrize(
    "suffix,method,payload,operation",
    [
        ("/catalog", "get", None, "catalog"),
        ("/roles/201/objects/system.user", "get", None, "object"),
        (
            "/roles/201/objects/system.user/preview",
            "post",
            {"unexpected": "safe-invalid"},
            "preview",
        ),
        ("/manifests/current", "get", None, "manifest"),
    ],
)
async def test_management_route_dispatch(suffix, method, payload, operation):
    executor = AsyncMock(return_value={"safe": True})
    app = FastAPI()
    app.include_router(
        create_management_router(lambda: executor), prefix="/system/field-policy"
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.request(
            method, "/system/field-policy" + suffix, json=payload
        )
    assert result.status_code == 200
    assert result.json() == {"code": 200, "msg": "success", "data": {"safe": True}}
    assert executor.call_args.kwargs["operation"] == operation


async def test_stream_limit_does_not_call_owner():
    executor = AsyncMock()
    app = FastAPI()
    app.include_router(create_management_router(lambda: executor))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(BusinessException) as error:
            await client.post("/manifests", content=b" " * 524289)
    assert error.value.code == 413
    executor.assert_not_called()


def test_openapi_documents_closed_camel_case_body_and_string_cas():
    app = FastAPI()
    app.include_router(create_management_router(lambda: AsyncMock()))
    operation = app.openapi()["paths"][
        "/roles/{role_id}/objects/{object_id}/revisions"
    ]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["expectedVersion"]["type"] == "string"
    rule = schema["properties"]["rules"]["items"]
    assert rule["additionalProperties"] is False
    assert "fieldId" in rule["properties"]
