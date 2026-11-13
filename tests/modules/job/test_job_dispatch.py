"""A signed notification is short lived input, never execution authority."""

import base64
import hashlib
import hmac
import json
from dataclasses import FrozenInstanceError

import pytest

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.modules.job import dispatch
from app.modules.job.dispatch import issue_dispatch, job_config_digest, verify_dispatch


def job():
    return {
        "tenant_id": 0,
        "job_id": 123,
        "job_key": "test_task",
        "cron_expression": "* * * * *",
        "trigger_type": "cron",
        "interval_value": None,
        "interval_unit": None,
        "job_args": "{}",
        "status": "1",
        "concurrent": "2",
        "timeout_seconds": 30,
        "max_retries": 0,
        "run_on_enable": False,
        "job_name": "name",
        "remark": None,
    }


def issue(**changes):
    values = {
        "tenant_id": 0,
        "job_id": 123,
        "actor_id": 456,
        "actor_version": 1,
        "g_epoch": 1,
        "b_epoch": 2,
        "job_digest": job_config_digest(job()),
        "catalog_digest": "a" * 64,
        "now": 100,
    }
    return issue_dispatch(**(values | changes))


def test_signed_closed_claims_are_detached_without_roles_or_args():
    token = issue()
    claims = verify_dispatch(token, now=101)
    assert (claims.tenant_id, claims.job_id, claims.actor_id, claims.actor_version) == (
        0,
        123,
        456,
        1,
    )
    assert (claims.g_epoch, claims.b_epoch, claims.issued_at, claims.expires_at) == (
        1,
        2,
        100,
        130,
    )
    assert not hasattr(claims, "roles") and not hasattr(claims, "args")
    assert not hasattr(claims, "execute") and not hasattr(claims, "tenant")
    assert len(token.encode("ascii")) <= 2048
    assert verify_dispatch(issue(), now=101).dispatch_id != claims.dispatch_id
    with pytest.raises(FrozenInstanceError):
        claims.actor_id = 1


@pytest.mark.parametrize(
    "raw", [None, {}, "", "tenantId=0&jobId=123", "a.b.c", "x" * 2049, "é"]
)
def test_old_or_unbounded_notifications_never_become_claims(raw):
    with pytest.raises(BusinessException) as error:
        verify_dispatch(raw, now=101)
    assert error.value.error_code == "JOB_DISPATCH_INVALID"


def test_tampering_expiry_future_and_key_rotation_fail_closed(monkeypatch):
    token = issue()
    body, signature = token.split(".")
    with pytest.raises(BusinessException):
        verify_dispatch(body + "x." + signature, now=101)
    with pytest.raises(BusinessException) as expired:
        verify_dispatch(token, now=130)
    assert expired.value.error_code == "JOB_DISPATCH_EXPIRED"
    with pytest.raises(BusinessException):
        verify_dispatch(token, now=97)
    assert verify_dispatch(token, now=98).issued_at == 100

    monkeypatch.setattr(dispatch.settings, "SECRET_KEY", "rotated-test-key")
    with pytest.raises(BusinessException):
        verify_dispatch(token, now=101)


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": -1},
        {"job_id": 0},
        {"actor_id": True},
        {"actor_version": 0},
        {"g_epoch": True},
        {"b_epoch": 0},
        {"job_digest": "unknown"},
        {"catalog_digest": "A" * 64},
        {"now": True},
    ],
)
def test_signer_rejects_malformed_internal_facts(changes):
    with pytest.raises(BusinessException):
        issue(**changes)


def test_digest_binds_execution_configuration_but_not_legacy_labels():
    current = job()
    digest = job_config_digest(current)
    assert (
        job_config_digest(current | {"job_name": "renamed", "remark": "changed"})
        == digest
    )
    for changes in (
        {"job_args": '{"x":1}'},
        {"tenant_id": 1},
        {"job_id": 124},
        {"status": "2"},
        {"timeout_seconds": None},
        {"run_on_enable": True},
    ):
        assert job_config_digest(current | changes) != digest
    with pytest.raises(BusinessException):
        job_config_digest({"job_key": "test_task"})


def test_digest_does_not_accept_oversize_or_non_text_arguments():
    for args in ("x" * 8193, "界" * 2731, {}):
        with pytest.raises(BusinessException):
            job_config_digest(job() | {"job_args": args})


def test_legacy_absent_args_keep_their_own_digest_without_rewriting_to_empty_json():
    assert job_config_digest(job() | {"job_args": None}) != job_config_digest(job())


@pytest.mark.parametrize(
    "fault",
    [
        "extra",
        "duplicate",
        "ttl",
        "boolean",
        "leading-zero",
        "uuid",
        "purpose",
        "domain",
    ],
)
def test_even_signed_malformed_claims_cannot_become_authority(fault):

    original = issue().split(".")[0]
    raw = base64.urlsafe_b64decode(original + "=" * ((-len(original)) % 4)).decode()
    data = json.loads(raw)
    if fault == "extra":
        data["roles"] = ["R_SUPER"]
    if fault == "ttl":
        data["expiresAt"] = 131
    if fault == "boolean":
        data["actorVersion"] = True
    if fault == "leading-zero":
        data["jobId"] = "0123"
    if fault == "uuid":
        data["dispatchId"] = data["dispatchId"].upper()
    if fault == "purpose":
        data["footprint"] = "job.schedule/v1"
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
    if fault == "duplicate":
        encoded = encoded.replace('"v":1', '"v":1,"v":1')
    body = base64.urlsafe_b64encode(encoded.encode()).rstrip(b"=").decode()
    domain = (
        b"hohu.form.receipt/v1\x00"
        if fault == "domain"
        else b"hohu.job.dispatch/v1\x00"
    )
    key = hashlib.sha256(domain + settings.SECRET_KEY.encode()).digest()
    signature = (
        base64.urlsafe_b64encode(hmac.new(key, body.encode(), hashlib.sha256).digest())
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(BusinessException) as rejected:
        verify_dispatch(body + "." + signature, now=101)
    assert rejected.value.error_code == "JOB_DISPATCH_INVALID"
