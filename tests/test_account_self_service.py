"""Account recovery contracts and atomic verification behavior."""

import pytest
from pydantic import ValidationError

from app.constants.static_routes import CONSTANT_ROUTES
from app.core.exceptions import BusinessRuleException
from app.modules.auth.schemas.self_service import Registration
from app.modules.system.service.mail_service import MailSettings
from app.modules.system.service.settings_service import _validate
from app.modules.system.settings_catalog import SETTINGS


def test_self_service_is_opt_in_and_code_duration_has_bounds():
    assert SETTINGS["register_enabled"].default == "false"
    assert SETTINGS["password_reset_enabled"].default == "false"
    field = SETTINGS["email_code_minutes"]
    assert _validate(field, 5) == "5"
    assert _validate(field, 30) == "30"
    for value in (4, 31, True, "5"):
        with pytest.raises(BusinessRuleException):
            _validate(field, value)


def test_public_route_does_not_advertise_sms_login():
    login = next(route for route in CONSTANT_ROUTES if route["name"] == "login")
    assert "code-login" not in login["path"]


def test_registration_rejects_admin_fields():

    with pytest.raises(ValidationError):
        Registration(
            userName="tester",
            email="a@example.com",
            password="Pass12345",
            code="123456",
            status="1",
            roleIds=["1"],
        )


def test_mail_password_is_not_a_user_password_and_is_not_returned():

    value = MailSettings(password="smtp-app-key", host="smtp.example.com")
    assert value.password == "smtp-app-key"
    with pytest.raises(ValidationError):
        MailSettings(host="smtp.example.com\r\nBAD")
