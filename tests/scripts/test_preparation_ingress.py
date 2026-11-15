"""Preparation is a separate closed application, not a normal-business fallback."""

from types import SimpleNamespace

import pytest
from fastapi.staticfiles import StaticFiles

from app.core.exceptions import BusinessException
from tools.ops.policy_preparation_assets import PreparationAssets
from tools.ops.policy_prepare import create_app


@pytest.mark.parametrize("unknown", ["handler", "duplicate", "static"])
async def test_preparation_refuses_unreviewed_surface_before_serving(tmp_path, unknown):
    application = create_app(SimpleNamespace())
    if unknown == "static":
        application.mount("/business-copy", StaticFiles(directory=tmp_path))
    elif unknown == "duplicate":
        application.router.routes.append(application.routes[0])
    else:

        async def business_copy():
            return {"confidential": "must never be served"}

        application.add_api_route("/business-copy", business_copy, methods=["GET"])
    with pytest.raises(BusinessException) as error:
        async with application.router.lifespan_context(application):
            pytest.fail("Unreviewed preparation route reached serving")
    assert error.value.error_code == "FIELD_INGRESS_UNDECLARED"


@pytest.mark.parametrize("assets_present", [False, True])
async def test_preparation_known_complete_surface_starts(assets_present):
    # Registration only. This empty byte fixture is not a qualified Web manifest.
    assets = PreparationAssets.__new__(PreparationAssets)
    assets.files = {}
    application = create_app(SimpleNamespace(), assets if assets_present else None)
    assert len(application.routes) == (19 if assets_present else 17)
    async with application.router.lifespan_context(application):
        pass


async def test_preparation_missing_known_route_is_not_a_valid_subset():
    application = create_app(SimpleNamespace())
    application.router.routes.pop()
    with pytest.raises(BusinessException) as error:
        async with application.router.lifespan_context(application):
            pytest.fail("Missing preparation contract was accepted")
    assert error.value.error_code == "FIELD_INGRESS_UNDECLARED"
