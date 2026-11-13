"""Strict dormant config commands; the current HTTP schemas remain unchanged."""

from pydantic import ConfigDict, model_validator

from app.core.exceptions import BusinessRuleException
from app.modules.system.field_policy.control_values import prepare_value
from app.modules.system.schemas.config import ConfigCreate, ConfigUpdate
from app.modules.system.settings_catalog import is_builtin_key


def import_boolean(value):
    if value is None:
        return False
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    if type(value) is str and value in {"true", "false", "1", "0"}:
        return value in {"true", "1"}
    raise BusinessRuleException(
        "Invalid import boolean", error_code="CONFIGURATION_INPUT_INVALID"
    )


def _public(value):
    if value is True:
        raise BusinessRuleException(
            "Use the publication action", error_code="CONFIG_PUBLICATION_REQUIRED"
        )


def _custom(key):
    if is_builtin_key(key):
        raise BusinessRuleException(
            "Use the setting protocol for built-in keys",
            error_code="BUILTIN_SETTING_PROTECTED",
        )


class ControlConfigCreate(ConfigCreate):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def closed_content(self):
        _public(self.is_public)
        _custom(self.config_key)
        value = prepare_value(
            key=self.config_key,
            config_type=self.config_type,
            replacement=self.config_value,
        )
        if not value.write_value:
            raise BusinessRuleException(
                "A new configuration needs an explicit value",
                error_code="CONFIGURATION_INPUT_INVALID",
            )
        return self


class ControlConfigUpdate(ConfigUpdate):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def closed_changes(self):
        values = self.model_dump(exclude_unset=True)
        if not values or any(
            value is None and key != "remark" for key, value in values.items()
        ):
            raise BusinessRuleException(
                "Invalid sparse configuration update",
                error_code="CONFIGURATION_INPUT_INVALID",
            )
        _public(values.get("is_public"))
        if "config_key" in values:
            _custom(self.config_key)
        if "config_type" in values and self.config_type not in {"text", "richtext"}:
            raise BusinessRuleException(
                "Unsupported configuration type",
                error_code="CONFIG_VALUE_SCHEMA_UNREGISTERED",
            )
        if "config_value" in values:
            # Final key/type/sensitivity and field grants are rechecked by the
            # future owner against the actual current row, never this fallback.
            prepare_value(
                key=values.get("config_key", "unresolved"),
                config_type=values.get("config_type", "text"),
                replacement=self.config_value,
            )
        return self
