from app.middleware.audit_middleware import _mask_sensitive


def test_settings_password_is_redacted_inside_values():
    value = {"values": {"auth:default_password": "Secret123", "register_enabled": True}}
    assert _mask_sensitive(value) == {
        "values": {"auth:default_password": "***", "register_enabled": True}
    }
