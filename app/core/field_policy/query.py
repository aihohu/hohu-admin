"""Build a pre-filter counterexample; owners supply trusted tenant/role scopes."""

from sqlalchemy import Boolean, and_, false, not_, or_, true
from sqlalchemy.sql.elements import ColumnElement

from .contracts import READS, WRITES, FieldUse, require, sequence
from .evaluation import action_context


def query_counterexample(
    catalog, rules, context, action_id, dependencies, base_scope, role_scopes
):
    action = action_context(catalog, rules, context, action_id)
    sequence(dependencies, 256, FieldUse)
    require(bool(dependencies) and len(set(dependencies)) == len(dependencies))
    require(type(role_scopes) is dict and set(role_scopes) == context.role_ids)

    def boolean(value):
        require(isinstance(value, ColumnElement) and isinstance(value.type, Boolean))

    boolean(base_scope)
    for scope in role_scopes.values():
        boolean(scope)
    coverage = []
    for dependency in dependencies:
        require(
            dependency in action.uses and dependency.ref.object_id == context.object_id
        )
        require(dependency.access.operation not in WRITES | READS)
        allow, deny = [], []
        for role, scope in role_scopes.items():
            grant = rules.index.get(
                (context.tenant_id, role, dependency.ref, dependency.access)
            )
            if grant is not None:
                # NULL is outside that role's scope, never a missing denial
                # which can turn NOT(coverage) into SQL UNKNOWN.
                (allow if grant.effect == "allow" else deny).append(
                    scope.is_(True)
                    if not scope.compare(true()) and not scope.compare(false())
                    else scope
                )
        coverage.append(and_(or_(false(), *allow), not_(or_(false(), *deny))))
    return and_(base_scope, not_(and_(true(), *coverage)))
