"""Regression tests for data-scope demo login identities."""

from app.utils.validators import validate_user_name
from scripts.seed_demo_data_scope import USERS


def test_seeded_demo_usernames_match_the_login_contract() -> None:
    for _user_id, user_name, *_rest in USERS:
        assert validate_user_name(user_name) == user_name
