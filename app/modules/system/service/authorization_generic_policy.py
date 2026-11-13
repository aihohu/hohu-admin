"""Fixed generic policy operations inside the existing strict transaction handle."""

from contextlib import asynccontextmanager

from sqlalchemy import select, update

from app.core.authorization_plan import ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.db.authorization_plan_session import _order_error
from app.modules.system.models.field_policy import FieldPolicyObjectState as ObjectState
from app.modules.system.models.user_field_policy import UserFieldPolicyGlobalGuard as G
from app.modules.system.models.user_field_policy import UserFieldPolicyState as B

GENERIC_POLICY_KINDS = frozenset(
    {
        K.FIELD_POLICY_HEAD,
        K.FIELD_POLICY_OBJECT,
        K.FIELD_POLICY_REVISION,
        K.FIELD_POLICY_BINDING,
        K.FIELD_POLICY_MANIFEST,
        K.FIELD_POLICY_ITEM,
        K.FIELD_POLICY_AUDIT,
        K.FIELD_POLICY_REQUEST,
    }
)
GENERIC_MUTABLE = {
    K.FIELD_POLICY_HEAD: frozenset(
        {
            "head_version",
            "rollout_state",
            "ever_enforced",
            "active_manifest_id",
            "active_manifest_digest",
            "candidate_manifest_id",
            "candidate_manifest_digest",
            "ready_manifest_digest",
            "qualification_digest",
            "ready_g_epoch",
            "ready_b_epoch",
            "ready_until",
        }
    ),
    K.FIELD_POLICY_OBJECT: frozenset(
        {"mode", "ever_enforced", "object_version", "active_object_digest"}
    ),
    K.FIELD_POLICY_BINDING: frozenset(
        {"revision_id", "binding_version", "archived_at"}
    ),
    K.FIELD_POLICY_MANIFEST: frozenset({"status"}),
}


class GenericPolicyTransaction:
    @asynccontextmanager
    async def generic_policy_change(
        self, tenant_id, *, action, actor_user_id, request_id
    ):
        try:
            self._check(locked=True)
            self._require_exclusive_tenant(tenant_id)
            if (
                self._generic is not None
                or self._policy is not None
                or self._aggregate_tenant is not None
                or self._aggregate_global
                or action
                not in {"publish", "archive", "build", "ready", "activate", "freeze"}
            ):
                raise _order_error()
            self.assert_held(
                ResourceKey(K.FIELD_POLICY_HEAD, tenant_id, tenant_id),
                ResourceKey(K.USER, tenant_id, actor_user_id),
            )
            self._generic = {
                "tenant_id": tenant_id,
                "action": action,
                "actor": actor_user_id,
                "request_id": request_id,
                "epochs": None,
                "audit": 0,
                "receipt": 0,
            }
            yield
            self._check(locked=True)
            if (
                self._generic["epochs"] is None
                or self._generic["audit"] != 1
                or self._generic["receipt"] != 1
            ):
                raise _order_error()
        except BaseException:
            self._journal.fail()
            raise
        finally:
            self._generic = None

    def _generic_context(self, key, values=None):
        frame = self._generic
        if (
            frame is None
            or key.tenant_id != frame["tenant_id"]
            or key.kind not in GENERIC_POLICY_KINDS
            or frame["receipt"]
        ):
            raise _order_error()
        self._require_exclusive_tenant(key.tenant_id)
        if key.kind in {K.FIELD_POLICY_AUDIT, K.FIELD_POLICY_REQUEST}:
            if frame["epochs"] is None or type(values) is not dict:
                raise _order_error()
            if key.kind is K.FIELD_POLICY_AUDIT:
                if (
                    frame["audit"]
                    or values.get("actor_user_id") != frame["actor"]
                    or values.get("action") != frame["action"]
                    or values.get("request_id") != frame["request_id"]
                    or (values.get("g_epoch"), values.get("b_epoch")) != frame["epochs"]
                ):
                    raise _order_error()
            elif not frame["audit"] or key.identity != (
                frame["actor"],
                frame["request_id"],
            ):
                raise _order_error()
        elif frame["audit"]:
            raise _order_error()

    async def generic_epoch(self, *, expected_epoch, user_mirror=None):
        """Advance the original B once. Ready is written afterwards at this watermark."""
        try:
            self._check(locked=True)
            frame = self._generic
            if (
                frame is None
                or frame["epochs"] is not None
                or type(expected_epoch) is not int
                or not 1 <= expected_epoch < 2**63 - 1
            ):
                raise _order_error()
            values = {"epoch": B.epoch + 1}
            if user_mirror is not None:
                if type(user_mirror) is not dict or set(user_mirror) != {
                    "mode",
                    "ever_enforced",
                    "active_manifest_digest",
                    "candidate_manifest_digest",
                }:
                    raise _order_error()
                self.assert_held(
                    ResourceKey(
                        K.FIELD_POLICY_OBJECT, frame["tenant_id"], "system.user"
                    )
                )
                state = (
                    (
                        await self._execute(
                            select(ObjectState.__table__).where(
                                ObjectState.tenant_id == frame["tenant_id"],
                                ObjectState.object_id == "system.user",
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if user_mirror != {
                    "mode": state["mode"],
                    "ever_enforced": state["ever_enforced"],
                    "active_manifest_digest": state["active_object_digest"],
                    "candidate_manifest_digest": state["active_object_digest"],
                }:
                    raise _order_error()
                values.update(user_mirror)
            await self._timeouts()
            epoch = (
                await self._execute(
                    update(B.__table__)
                    .where(B.tenant_id == frame["tenant_id"], B.epoch == expected_epoch)
                    .values(**values)
                    .returning(B.epoch)
                )
            ).scalar_one_or_none()
            if epoch is None:
                raise _order_error()
            g_epoch = (
                await self._execute(select(G.epoch).where(G.guard_id == 1))
            ).scalar_one()
            frame["epochs"] = (g_epoch, epoch)
            return g_epoch, epoch
        except BaseException:
            self._journal.fail()
            raise
