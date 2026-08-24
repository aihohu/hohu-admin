"""Phase 3 shared scoped department read contracts."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import DATA_SCOPE_CUSTOM, STATUS_DISABLED, STATUS_ENABLED
from app.core.exceptions import NotFoundException
from app.core.id_generator import next_id
from app.modules.system.api import dept as dept_api
from app.modules.system.models.dept import Dept
from app.modules.system.schemas.dept import DeptQuery
from app.utils.data_scope import DataScopeResolution


def _department(
    name: str,
    *,
    parent: Dept | None = None,
    status: str = STATUS_ENABLED,
) -> Dept:
    dept_id = next_id()
    return Dept(
        dept_id=dept_id,
        parent_id=parent.dept_id if parent is not None else None,
        ancestors=("0" if parent is None else f"{parent.ancestors},{parent.dept_id}"),
        dept_name=f"{name}-{dept_id}",
        order_num=0,
        status=status,
    )


def _resolution(*dept_ids: int) -> DataScopeResolution:
    return DataScopeResolution(
        scope_kinds=frozenset({DATA_SCOPE_CUSTOM}),
        accessible_dept_ids=frozenset(dept_ids),
        accessible_user_scope=None,
        include_self=False,
        unbounded=False,
    )


async def test_tree_reads_project_visible_nodes_as_local_roots(
    db_session: AsyncSession,
) -> None:
    hidden_root = _department("phase3-hidden-root")
    visible_parent = _department("phase3-visible-parent", parent=hidden_root)
    visible_child = _department("phase3-visible-child", parent=visible_parent)
    hidden_sibling = _department("phase3-hidden-sibling", parent=hidden_root)
    disabled_visible = _department(
        "phase3-disabled-visible",
        parent=visible_parent,
        status=STATUS_DISABLED,
    )
    db_session.add_all(
        [hidden_root, visible_parent, visible_child, hidden_sibling, disabled_visible]
    )
    await db_session.flush()
    resolution = _resolution(
        visible_parent.dept_id,
        visible_child.dept_id,
        disabled_visible.dept_id,
    )
    actor = MagicMock()

    with patch.object(
        dept_api,
        "resolve_data_scope",
        AsyncMock(return_value=resolution),
    ):
        tree_response = await dept_api.get_dept_tree(
            query=DeptQuery(),
            db=db_session,
            current_user=actor,
        )
        option_response = await dept_api.get_dept_tree_option(
            db=db_session,
            current_user=actor,
        )

    tree = tree_response.data
    assert [item["dept_id"] for item in tree] == [str(visible_parent.dept_id)]
    assert tree[0]["parent_id"] is None
    assert tree[0]["ancestors"] == "0"
    assert [item["dept_id"] for item in tree[0]["children"]] == [
        str(visible_child.dept_id),
        str(disabled_visible.dept_id),
    ]
    assert hidden_root.dept_name not in str(tree)
    assert hidden_sibling.dept_name not in str(tree)

    options = option_response.data
    assert [item.id for item in options] == [visible_parent.dept_id]
    assert options[0].p_id == ""
    assert [item.id for item in options[0].children] == [visible_child.dept_id]
    assert hidden_root.dept_name not in str(options)


async def test_list_reads_share_the_same_visible_set_and_totals(
    db_session: AsyncSession,
) -> None:
    visible_a = _department("phase3-visible-a")
    visible_b = _department("phase3-visible-b", parent=visible_a)
    hidden = _department("phase3-hidden")
    db_session.add_all([visible_a, visible_b, hidden])
    await db_session.flush()
    resolution = _resolution(visible_a.dept_id, visible_b.dept_id)
    actor = MagicMock()

    with patch.object(
        dept_api,
        "resolve_data_scope",
        AsyncMock(return_value=resolution),
    ):
        page_response = await dept_api.get_dept_list(
            query=DeptQuery(current=1, size=100),
            db=db_session,
            current_user=actor,
        )
        tree_page_response = await dept_api.get_dept_tree_list(
            db=db_session,
            current_user=actor,
        )

    page = page_response.data
    assert page.total == 2
    assert {item.dept_id for item in page.records} == {
        visible_a.dept_id,
        visible_b.dept_id,
    }

    tree_page = tree_page_response.data
    assert tree_page.total == 1
    assert [item["dept_id"] for item in tree_page.records] == [str(visible_a.dept_id)]
    assert hidden.dept_name not in str(tree_page.records)


async def test_detail_uses_the_missing_surface_for_out_of_scope_ids(
    db_session: AsyncSession,
) -> None:
    visible = _department("phase3-visible-detail")
    hidden = _department("phase3-hidden-detail")
    db_session.add_all([visible, hidden])
    await db_session.flush()
    resolution = _resolution(visible.dept_id)

    with patch.object(
        dept_api,
        "resolve_data_scope",
        AsyncMock(return_value=resolution),
    ):
        visible_response = await dept_api.get_dept_detail(
            dept_id=visible.dept_id,
            db=db_session,
            current_user=MagicMock(),
        )
        with pytest.raises(NotFoundException):
            await dept_api.get_dept_detail(
                dept_id=hidden.dept_id,
                db=db_session,
                current_user=MagicMock(),
            )

    assert visible_response.data.dept_id == visible.dept_id
