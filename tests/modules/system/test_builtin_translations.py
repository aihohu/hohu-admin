from types import SimpleNamespace

from app.core.builtin_i18n import clear_changed_translations, initialize_translations


def test_translation_metadata_preserves_explicit_customization():
    row = SimpleNamespace(name="Default", description="Custom", i18n_keys=None)
    initialize_translations(
        row,
        "builtin.agent.user",
        {"name": "Default", "description": "Default description"},
    )
    assert row.i18n_keys == {"name": "builtin.agent.user.name"}
    clear_changed_translations(row, {"name": "My assistant"})
    assert row.i18n_keys == {}
    row.name = "Default"
    initialize_translations(row, "builtin.agent.user", {"name": "Default"})
    assert row.i18n_keys == {}


def test_unchanged_form_submission_keeps_translation():
    row = SimpleNamespace(
        role_name="User", i18n_keys={"roleName": "builtin.role.user.name"}
    )
    clear_changed_translations(row, {"role_name": "User", "status": "2"})
    assert row.i18n_keys == {"roleName": "builtin.role.user.name"}
