"""422 校验错误响应携带结构化 fieldErrors（前端表单内联定位依赖）。"""

import pytest
from fastapi import FastAPI
from pydantic import BaseModel, EmailStr, field_validator

from app.core.exceptions import setup_exception_handlers
from app.modules.job.schemas.job import JobCreate


class _Body(BaseModel):
    user_email: EmailStr
    user_phone: str

    @field_validator("user_phone")
    @classmethod
    def _valid_phone(cls, value: str) -> str:
        if not value.isdigit() or len(value) != 11:
            raise ValueError("手机号格式不正确")
        return value


def _build_app() -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    from fastapi import Body  # noqa: PLC0415

    @app.post("/echo")
    async def echo(_body: _Body = Body(...)):  # pragma: no cover - never reached
        return {"ok": True}

    @app.post("/job-schema")
    async def job_schema(_body: JobCreate = Body(...)):
        return {"ok": True}

    return app


@pytest.fixture
def client():
    from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

    app = _build_app()
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def test_validation_error_returns_structured_field_errors(client):
    resp = await client.post(
        "/echo",
        json={"user_email": "bad@example.test", "user_phone": "123"},
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == 422
    fields = {item["field"] for item in body["data"]["fieldErrors"]}
    assert fields == {"user_email", "user_phone"}
    # msg stays backward compatible: first field error in text form.
    assert body["msg"].startswith("参数错误: ")
    email_error = next(
        item for item in body["data"]["fieldErrors"] if item["field"] == "user_email"
    )
    assert "valid email" in email_error["message"]


async def test_omitted_cron_returns_422_field_errors(client):
    response = await client.post("/job-schema", json={"jobName": "t", "jobKey": "t"})
    assert response.status_code == 422
    assert response.json()["code"] == 422
    assert response.json()["data"]["fieldErrors"][0]["field"] == "cron_expression"
