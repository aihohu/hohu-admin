"""Explicit candidate composition: external proof, protected DB, ACK, effects.

These Python entries are not routed by the running application. A caller must
provide a fresh AuthorizationPlanSession and may not retry an unknown commit.
"""

import asyncio

from jose import JWTError

from app.core.authorization_io import hash_password, smtp_worker, verify_password
from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.config import settings
from app.core.exceptions import AuthenticationException, BusinessException
from app.core.id_generator import next_id
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
)
from app.core.tenant import (
    DEFAULT_TENANT_CODE,
    normalize_tenant_code,
    require_tenant_runtime_enabled,
)
from app.db.authorization_owner import (
    QUARANTINE_KEY,
    PostCommitEffects,
    commit_authorization_session,
    rollback_authorization_session,
)
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.modules.auth.candidate_service import auth_candidate_service
from app.modules.auth.email_verification import email_verification
from app.modules.auth.service import (
    _DUMMY_PASSWORD_HASH,
    _is_blacklisted,
    _parse_token_identity,
    _try_blacklist_token,
    auth_service,
)
from app.modules.system.service.account_service import account_service
from app.modules.system.service.auth_candidate_account_service import (
    auth_candidate_accounts,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.mail_service import _send


async def _external(awaitable):
    try:
        async with asyncio.timeout(2):
            return await awaitable
    except TimeoutError:
        raise BusinessException(
            503, "External proof timed out", error_code="AUTHORIZATION_EXTERNAL_TIMEOUT"
        ) from None


class AuthCandidateOwner:
    def _require(self, db):
        if not isinstance(db, AuthorizationPlanSession) or db.info.get(QUARANTINE_KEY):
            raise _order_error()
        journal = db._authorization_journal
        if (
            db.in_transaction()
            or db.new
            or db.dirty
            or db.deleted
            or journal.phase != "idle"
            or journal.unsafe
            or not journal.valid_bind
        ):
            raise _order_error()

    async def _read(self, db, tenant_id, **kwargs):
        try:
            async with asyncio.timeout(5):
                result = await auth_candidate_accounts.read(db, tenant_id, **kwargs)
        except BaseException as exc:
            try:
                await rollback_authorization_session(db)
            except Exception:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                raise
            raise
        else:
            await rollback_authorization_session(db)
            return result

    async def _commit(self, db, effects):
        await commit_authorization_session(db, effects)

    async def _protected(
        self,
        db,
        prepared,
        operation,
        *,
        payload=None,
        password_hash=None,
        new_user_id=None,
        effects=None,
    ):
        effects = effects or PostCommitEffects()
        write = operation in {"register", "reset", "bind"}
        try:
            async with authorization_transactions.scope(
                db,
                budget=TransactionBudget(timeout_ms=15000 if write else 8000),
                tenants=(
                    TenantLock(
                        prepared.tenant_id,
                        LockMode.EXCLUSIVE if write else LockMode.SHARED,
                    ),
                ),
            ) as tx:
                await tx.lock(
                    auth_candidate_accounts.plan(prepared, new_user_id=new_user_id)
                )
                result = await auth_candidate_service.apply(
                    db,
                    tx,
                    prepared,
                    operation,
                    payload=payload,
                    password_hash=password_hash,
                    new_user_id=new_user_id,
                )
        except BaseException as exc:
            try:
                await rollback_authorization_session(db, effects)
            except Exception:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                raise
            raise
        await self._commit(db, effects)
        return result

    def _tokens(self, identity):
        values = {
            "subject": str(identity.user_id),
            "tenant_id": identity.tenant_id,
            "tenant_version": identity.tenant_version,
            "user_version": identity.user_version,
        }
        return create_access_token(**values), create_refresh_token(**values)

    async def login(self, db, credentials, *, host=None):
        self._require(db)
        if (
            credentials.login_type != "password"
            or not isinstance(credentials.password, str)
            or not credentials.password
        ):
            raise AuthenticationException(error_code="INVALID_CREDENTIALS")
        body = normalize_tenant_code(credentials.tenant_code)
        if credentials.tenant_code is not None and body is None:
            raise AuthenticationException(error_code="INVALID_CREDENTIALS")
        hosted = auth_service._host_tenant_code(host)
        if settings.TENANT_MODE == "single":
            if body not in {None, DEFAULT_TENANT_CODE}:
                raise AuthenticationException(error_code="INVALID_CREDENTIALS")
            locator = DEFAULT_TENANT_CODE
        else:
            if body and hosted and body != hosted:
                raise AuthenticationException(error_code="INVALID_CREDENTIALS")
            locator = body or hosted or DEFAULT_TENANT_CODE
            if (
                locator != DEFAULT_TENANT_CODE
                and not settings.TENANT_HOSTED_LOGIN_ENABLED
            ):
                raise AuthenticationException(error_code="INVALID_CREDENTIALS")
        try:
            async with asyncio.timeout(5):
                tenant_id = await auth_candidate_accounts.tenant_id(db, locator)
        except BaseException as exc:
            try:
                await rollback_authorization_session(db)
            except Exception:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                raise
            raise
        else:
            await rollback_authorization_session(db)
        require_tenant_runtime_enabled(tenant_id, surface="login")
        state = await self._read(db, tenant_id, user_name=credentials.user_name)
        matched = await verify_password(
            credentials.password,
            state.account.hashed_password if state.account else _DUMMY_PASSWORD_HASH,
        )
        if not matched or state.account is None:
            raise AuthenticationException(error_code="INVALID_CREDENTIALS")
        state.identity()
        current = await self._protected(db, state, "login")
        return self._tokens(current.identity())

    async def _token(self, token, *, refresh=False):
        try:
            payload = (decode_refresh_token if refresh else decode_access_token)(token)
            if payload.get("type") != ("refresh" if refresh else "access"):
                raise ValueError("token type")
            uid, tid, tver, uver = _parse_token_identity(payload)
            if any(v >= 2**63 for v in (uid, tid, tver, uver)):
                raise ValueError("token range")
        except (JWTError, ValueError, TypeError):
            raise AuthenticationException(error_code="TOKEN_EXPIRED") from None
        require_tenant_runtime_enabled(tid, surface="refresh" if refresh else "access")
        if refresh:
            valid = await _external(
                _try_blacklist_token(token, int(payload.get("exp", 0)))
            )
        else:
            valid = not await _external(_is_blacklisted(token))
        if not valid:
            raise AuthenticationException(error_code="TOKEN_EXPIRED")
        return uid, tid, tver, uver

    def _identity(self, state, token_identity):
        current = state.identity()
        if (
            current.user_id,
            current.tenant_id,
            current.tenant_version,
            current.user_version,
        ) != token_identity:
            raise AuthenticationException(error_code="TOKEN_EXPIRED")
        return current

    async def current(self, db, token):
        self._require(db)
        proof = await self._token(token)
        state = await self._read(db, proof[1], user_id=proof[0])
        self._identity(state, proof)
        return (await self._protected(db, state, "current")).identity()

    async def refresh(self, db, token):
        self._require(db)
        proof = await self._token(token, refresh=True)
        state = await self._read(db, proof[1], user_id=proof[0])
        self._identity(state, proof)
        return self._tokens((await self._protected(db, state, "refresh")).identity())

    async def register(self, db, tenant_id, payload):
        self._require(db)
        require_tenant_runtime_enabled(tenant_id, surface="login")
        state = await self._read(
            db, tenant_id, user_name=payload.user_name, policy=True
        )
        auth_candidate_accounts.policy(state, "register")
        encoded = await hash_password(payload.password)
        await _external(
            email_verification.consume(
                tenant_id,
                "register",
                f"{payload.user_name}:{payload.email}",
                payload.code,
            )
        )
        return await self._protected(
            db,
            state,
            "register",
            payload=payload,
            password_hash=encoded,
            new_user_id=next_id(),
        )

    async def reset(self, db, tenant_id, payload):
        self._require(db)
        require_tenant_runtime_enabled(tenant_id, surface="login")
        state = await self._read(
            db, tenant_id, user_name=payload.user_name, policy=True
        )
        auth_candidate_accounts.policy(state, "reset")
        identity = account_service.recovery_identity(state.account, str(payload.email))
        if identity is None:
            raise BusinessException(
                400, "Invalid or expired email code", error_code="EMAIL_CODE_INVALID"
            )
        encoded = await hash_password(payload.password)
        await _external(
            email_verification.consume(tenant_id, "reset", identity, payload.code)
        )
        await self._protected(
            db, state, "reset", payload=payload, password_hash=encoded
        )
        await self._notify(
            state.mail,
            str(payload.email),
            "HoHu password changed",
            "Your password was reset. Contact your administrator if this was not you.",
        )

    async def _binding(self, db, token, payload):
        proof = await self._token(token)
        state = await self._read(db, proof[1], user_id=proof[0], policy=True)
        self._identity(state, proof)
        auth_candidate_accounts.policy(state, "bind")
        if not await verify_password(payload.password, state.account.hashed_password):
            raise BusinessException(
                400,
                "Current password is incorrect",
                error_code="INCORRECT_OLD_PASSWORD",
            )
        account = state.account
        identity = f"{account.user_id}:{account.auth_version}:{account.user_email}:{account.email_verified_at}:{payload.email}"
        return state, identity

    async def bind(self, db, token, payload):
        self._require(db)
        state, identity = await self._binding(db, token, payload)
        await _external(
            email_verification.consume(
                state.tenant_id, "bind", identity, payload.code or ""
            )
        )
        await self._protected(db, state, "bind", payload=payload)
        old = state.account.user_email if state.account.email_verified_at else None
        if old and old != str(payload.email):
            await self._notify(
                state.mail,
                old,
                "HoHu recovery email changed",
                "Your recovery email changed. Contact your administrator if this was not you.",
            )

    async def prepare_email(self, db, tenant_id, payload, *, ip, token=None):
        self._require(db)
        if token is not None:
            state, identity = await self._binding(db, token, payload)
            if state.tenant_id != tenant_id:
                raise AuthenticationException(error_code="INVALID_CREDENTIALS")
            purpose, eligible = "bind", True
        else:
            require_tenant_runtime_enabled(tenant_id, surface="login")
            purpose = payload.purpose
            state = await self._read(
                db, tenant_id, user_name=payload.user_name, policy=True
            )
            auth_candidate_accounts.policy(state, purpose)
            identity, eligible = f"{payload.user_name}:{payload.email}", True
            if purpose == "reset":
                recovery = account_service.recovery_identity(
                    state.account, str(payload.email)
                )
                eligible = recovery is not None
                identity = recovery or "unknown:" + identity
        await self._protected(db, state, "email", payload=payload)
        minutes = int(state.value("email_code_minutes"))

        async def issue():
            await email_verification.throttle(tenant_id, str(payload.email), ip)
            return await email_verification.issue(tenant_id, purpose, identity, minutes)

        code = await _external(issue())
        if eligible:
            # Recovery is enumeration resistant even when transport fails.
            await self._notify(
                state.mail,
                str(payload.email),
                "HoHu verification",
                f"Verification code: {code}\nExpires in {minutes} minutes.",
                strict=purpose != "reset",
            )
        return {"expiresIn": minutes * 60, "retryAfter": 60}

    async def _notify(self, config, email, subject, body, *, strict=False):
        try:
            await smtp_worker.run(_send, config, email, subject, body)
        except Exception:
            if strict:
                raise BusinessException(
                    503,
                    "Mail acknowledgement unavailable; delivery may have occurred",
                    error_code="MAIL_SEND_FAILED",
                ) from None


auth_candidate_owner = AuthCandidateOwner()
