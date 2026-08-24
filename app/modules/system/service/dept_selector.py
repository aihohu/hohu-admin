"""Shared data-scoped department read selector."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import Select, Text, cast, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.constants import STATUS_ENABLED
from app.core.base_response import PageResult
from app.core.exceptions import NotFoundException
from app.modules.system.models.dept import Dept


class DepartmentReadScope(Protocol):
    """Minimal materialized scope accepted by every department read path."""

    accessible_dept_ids: Collection[int] | None


@dataclass(frozen=True)
class DepartmentLookupMatch:
    """One visible department lookup row with a locally scoped path."""

    dept_id: int
    dept_name: str
    path: str


@dataclass(frozen=True)
class DepartmentLookupResult:
    """Bounded lookup rows plus every contributor to the scoped match count."""

    matches: tuple[DepartmentLookupMatch, ...]
    match_count: int
    matched_dept_ids: tuple[int, ...]


class DepartmentSelector:
    """Select departments from one canonical materialized visible set."""

    @staticmethod
    def _scope_filter(scope: DepartmentReadScope) -> ColumnElement[bool] | None:
        accessible_ids = scope.accessible_dept_ids
        if accessible_ids is None:
            return None
        return Dept.dept_id.in_(tuple(sorted(int(value) for value in accessible_ids)))

    def _filters(
        self,
        scope: DepartmentReadScope,
        filters: Sequence[ColumnElement[bool]],
    ) -> tuple[ColumnElement[bool], ...]:
        scope_filter = self._scope_filter(scope)
        if scope_filter is None:
            return tuple(filters)
        return (*filters, scope_filter)

    async def rows(
        self,
        db: AsyncSession,
        *,
        scope: DepartmentReadScope,
        filters: Sequence[ColumnElement[bool]] = (),
    ) -> list[Dept]:
        """Return all visible rows in stable tree display order."""
        stmt = (
            select(Dept)
            .where(*self._filters(scope, filters))
            .order_by(Dept.order_num.asc(), Dept.dept_id.asc())
        )
        return list((await db.execute(stmt)).scalars().all())

    async def page(
        self,
        db: AsyncSession,
        *,
        scope: DepartmentReadScope,
        current: int,
        size: int,
        filters: Sequence[ColumnElement[bool]] = (),
    ) -> PageResult[Any]:
        """Return a stable page and total from the same visible row set."""
        scoped_filters = self._filters(scope, filters)
        total = int(
            await db.scalar(select(func.count(Dept.dept_id)).where(*scoped_filters))
            or 0
        )
        stmt = (
            select(Dept)
            .where(*scoped_filters)
            .order_by(Dept.order_num.asc(), Dept.dept_id.asc())
            .offset((current - 1) * size)
            .limit(size)
        )
        records = list((await db.execute(stmt)).scalars().all())
        return PageResult(
            records=records,
            total=total,
            current=current,
            size=size,
        )

    async def count(
        self,
        db: AsyncSession,
        *,
        scope: DepartmentReadScope,
        filters: Sequence[ColumnElement[bool]] = (),
    ) -> int:
        """Count only rows in the materialized visible set."""
        return int(
            await db.scalar(
                select(func.count(Dept.dept_id)).where(*self._filters(scope, filters))
            )
            or 0
        )

    async def get_by_id(
        self,
        db: AsyncSession,
        *,
        scope: DepartmentReadScope,
        dept_id: int,
    ) -> Dept:
        """Use the missing-object surface for absent and hidden departments."""
        dept = await db.scalar(
            select(Dept).where(
                Dept.dept_id == dept_id,
                *self._filters(scope, ()),
            )
        )
        if dept is None:
            raise NotFoundException("部门")
        return dept

    @staticmethod
    def build_lookup_statement(
        *,
        accessible_dept_ids: Collection[int] | None,
        normalized_query: str,
        limit: int,
        enabled_only: bool = True,
    ) -> Select[Any]:
        """Build locally rooted paths without reading hidden ancestors."""
        filters: list[ColumnElement[bool]] = []
        if enabled_only:
            filters.append(Dept.status == STATUS_ENABLED)
        if accessible_dept_ids is not None:
            filters.append(
                Dept.dept_id.in_(
                    tuple(sorted(int(value) for value in accessible_dept_ids))
                )
            )
        visible = (
            select(
                Dept.dept_id,
                Dept.parent_id,
                Dept.dept_name,
            )
            .where(*filters)
            .cte("visible_depts")
        )
        folded_query = normalized_query.casefold()
        leaf_query = normalized_query.rsplit("/", maxsplit=1)[-1].strip().casefold()
        candidates = select(
            visible.c.dept_id,
            visible.c.parent_id.label("next_parent_id"),
            visible.c.dept_name,
            cast(visible.c.dept_name, Text).label("path"),
            (literal(",") + cast(visible.c.dept_id, Text) + literal(",")).label(
                "visited"
            ),
        ).where(
            func.lower(visible.c.dept_name).contains(
                leaf_query,
                autoescape=True,
            )
        )
        paths = candidates.cte("scoped_dept_paths", recursive=True)
        parent = visible.alias("visible_dept_parent")
        parent_marker = literal(",") + cast(parent.c.dept_id, Text) + literal(",")
        paths = paths.union_all(
            select(
                paths.c.dept_id,
                parent.c.parent_id.label("next_parent_id"),
                paths.c.dept_name,
                (parent.c.dept_name + literal(" / ") + paths.c.path).label("path"),
                (paths.c.visited + cast(parent.c.dept_id, Text) + literal(",")).label(
                    "visited"
                ),
            )
            .join(paths, parent.c.dept_id == paths.c.next_parent_id)
            .where(~paths.c.visited.contains(parent_marker))
        )
        next_parent = visible.alias("visible_dept_next_parent")
        matched = (
            select(
                paths.c.dept_id,
                paths.c.dept_name,
                paths.c.path,
                func.count().over().label("match_count"),
                func.array_agg(paths.c.dept_id).over().label("matched_dept_ids"),
            )
            .where(
                ~exists(
                    select(1)
                    .select_from(next_parent)
                    .where(next_parent.c.dept_id == paths.c.next_parent_id)
                ),
                func.lower(paths.c.path).contains(
                    folded_query,
                    autoescape=True,
                ),
            )
            .cte("scoped_dept_matches")
        )
        return (
            select(
                matched.c.dept_id,
                matched.c.dept_name,
                matched.c.path,
                matched.c.match_count,
                matched.c.matched_dept_ids,
            )
            .order_by(func.lower(matched.c.path), matched.c.dept_id)
            .limit(limit)
        )

    async def lookup(
        self,
        db: AsyncSession,
        *,
        scope: DepartmentReadScope,
        normalized_query: str,
        limit: int,
        enabled_only: bool = True,
    ) -> DepartmentLookupResult:
        """Resolve visible enabled candidates and freeze all match contributors."""
        rows = (
            (
                await db.execute(
                    self.build_lookup_statement(
                        accessible_dept_ids=scope.accessible_dept_ids,
                        normalized_query=normalized_query,
                        limit=limit,
                        enabled_only=enabled_only,
                    )
                )
            )
            .mappings()
            .all()
        )
        matches = tuple(
            DepartmentLookupMatch(
                dept_id=int(row["dept_id"]),
                dept_name=str(row["dept_name"]),
                path=str(row["path"]),
            )
            for row in rows
        )
        match_count = int(rows[0]["match_count"]) if rows else 0
        matched_dept_ids = (
            tuple(sorted(int(value) for value in rows[0]["matched_dept_ids"]))
            if rows
            else ()
        )
        return DepartmentLookupResult(
            matches=matches,
            match_count=match_count,
            matched_dept_ids=matched_dept_ids,
        )


department_selector = DepartmentSelector()
