"""An unknown delivery surface cannot reach process admission or database IO."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import main
from app.core.exceptions import BusinessException
from app.core.field_ingress import field_ingress_inventory
from app.core.field_ingress_manifest import INGRESS_BINDINGS, INGRESS_CONTRACTS
from app.core.public_uploads import PublicUploadStaticFiles


@pytest.mark.parametrize(
    "surface",
    [
        "new-route",
        "replaced-handler",
        "private-mount",
        "unverified-public-mount",
        "private-root",
    ],
)
async def test_undeclared_surface_is_rejected_before_process_admission(
    monkeypatch, tmp_path, surface
):
    application = FastAPI(lifespan=main.lifespan)

    async def unknown_handler():
        return {"private_value": "must never be served"}

    if surface in {"unverified-public-mount", "private-root"}:
        application.mount(
            "/uploads",
            PublicUploadStaticFiles(
                directory=tmp_path,
                is_public=main.is_public_upload if surface == "private-root" else None,
            ),
            name="uploads",
        )
    elif surface == "private-mount":
        application.mount("/uploads", StaticFiles(directory=tmp_path), name="uploads")
    else:
        application.add_api_route(
            "/new-field-copy" if surface == "new-route" else "/health",
            unknown_handler,
            methods=["GET"],
        )

    attempted = []

    async def forbidden_start(*_args, **_kwargs):
        attempted.append(True)
        pytest.fail("Unclassified ingress reached process admission")

    monkeypatch.setattr(main.ProcessAdmission, "start", forbidden_start)
    monkeypatch.setattr(main.ProcessAdmission, "stop", AsyncMock())
    monkeypatch.setattr(main, "close_redis", AsyncMock())
    for name in ("close_provider_http_clients", "_unregister_active_worker"):
        if hasattr(main, name):
            monkeypatch.setattr(main, name, AsyncMock())
    with pytest.raises(BusinessException) as failure:
        async with application.router.lifespan_context(application):
            pytest.fail("Unknown ingress entered the serving phase")
    assert failure.value.error_code == "FIELD_INGRESS_UNDECLARED"
    assert attempted == []


def test_actual_app_registrations_exactly_match_the_closed_inventory():
    enabled = main._FIELD_INGRESS_AI_ENABLED
    rows = field_ingress_inventory(main.app, ai_enabled=enabled)
    expected = {
        entry[:4]
        for entry in INGRESS_BINDINGS
        if entry[5] in {"always", "on" if enabled else "off"}
    }
    assert {
        (x["methods"], x["path"], x["kind"], x["binding"]) for x in rows
    } == expected
    assert all(x["contract"] in INGRESS_CONTRACTS for x in rows)
    assert all(x["owner"] and x["source"] and x["domain"] for x in rows)


def test_a_subset_does_not_require_registering_missing_business_surfaces():
    application = FastAPI()
    application.add_api_route("/health", main.health_check, methods=["GET"])
    rows = field_ingress_inventory(application, ai_enabled=False)
    assert len(rows) == 5  # Four framework descriptions and one fixed health endpoint.


def test_duplicate_known_handler_cannot_shadow_another_registration():
    application = FastAPI()
    application.add_api_route("/health", main.health_check, methods=["GET"])
    application.add_api_route("/health", main.health_check, methods=["GET"])
    with pytest.raises(BusinessException) as failure:
        field_ingress_inventory(application, ai_enabled=True)
    assert failure.value.error_code == "FIELD_INGRESS_UNDECLARED"


@pytest.mark.parametrize("enabled", [True, False])
def test_mode_cannot_be_selected_by_a_truthy_string(enabled):
    with pytest.raises(BusinessException):
        field_ingress_inventory(FastAPI(), ai_enabled=str(enabled))


def test_undeclared_websocket_is_also_an_ingress():
    application = FastAPI()

    async def private_stream(socket):
        await socket.accept()

    application.add_api_websocket_route("/private-stream", private_stream)
    with pytest.raises(BusinessException) as failure:
        field_ingress_inventory(application, ai_enabled=True)
    assert failure.value.error_code == "FIELD_INGRESS_UNDECLARED"


def test_registration_budget_is_checked_before_iterating():
    application = FastAPI()
    application.routes.extend([application.routes[0]] * 513)
    with pytest.raises(BusinessException) as failure:
        field_ingress_inventory(application, ai_enabled=True)
    assert failure.value.error_code == "FIELD_INGRESS_LIMIT"
