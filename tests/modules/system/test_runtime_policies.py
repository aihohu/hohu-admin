from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.exceptions import BusinessRuleException
from app.modules.ai.core.generation_settings import generation_settings
from app.modules.ai.service.model_authorization_service import (
    model_authorization_service,
)
from app.modules.system.service.file_policy_service import file_policy_service
from app.modules.system.service.settings_service import settings_service
from tests.modules.system.test_settings_service import TENANT
from tests.tenant_helpers import create_test_tenant, tenant_context


def test_model_settings_are_optional_and_reject_invalid_values():
    assert generation_settings(None) == {}
    assert generation_settings(
        {"generation": {"max_tokens": 123, "temperature": 0}}
    ) == {
        "max_tokens": 123,
        "temperature": 0,
    }
    for value in [
        {"max_tokens": True},
        {"max_tokens": -1},
        {"temperature": float("nan")},
        {"unknown": 1},
    ]:
        with pytest.raises(BusinessRuleException):
            generation_settings({"generation": value})


async def test_file_policy_applies_tenant_and_processing_limits(db_session):
    initial = await settings_service.get_group(db_session, "files", tenant=TENANT)
    await settings_service.save_group(
        db_session,
        "files",
        {"upload:max_bytes": 2048},
        revision=initial["revision"],
        tenant=TENANT,
    )
    policy = await file_policy_service.resolve(db_session, "import", tenant=TENANT)
    assert policy.max_bytes == 2048
    assert policy.extensions == frozenset({".csv", ".xlsx"})
    with pytest.raises(BusinessRuleException):
        policy.validate_size(2049)


async def test_ai_file_scenario_accepts_markdown_and_json(db_session):
    other = await create_test_tenant(db_session, prefix="aifile")
    policy = await file_policy_service.resolve(
        db_session,
        "ai_file",
        tenant=tenant_context(tenant_id=other.tenant_id, actor_user_id=1),
    )
    assert {".md", ".json"} <= policy.extensions


async def test_model_factory_receives_generation_settings():

    selected = SimpleNamespace(
        model=SimpleNamespace(
            name="example", base_url=None, config={"generation": {"max_tokens": 128}}
        ),
        provider=SimpleNamespace(
            provider_code="openai", api_key="encrypted", base_url=None
        ),
    )
    with (
        patch(
            "app.modules.ai.service.model_authorization_service.decrypt_value",
            return_value="key",
        ),
        patch(
            "app.modules.ai.service.model_authorization_service.create_model"
        ) as create,
    ):
        model_authorization_service.create_model_instance(selected)
        assert create.call_args.kwargs["model_settings"] == {"max_tokens": 128}
