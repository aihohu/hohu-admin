"""Scoped department invariants through the real protected HTTP owner."""

from sqlalchemy import insert, update

from app.db.base import role_depts
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def scoped_departments(plain, rows, visible):
    async with plain() as db:
        for did, parent, name, status, ancestors in rows:
            await db.execute(
                insert(Dept).values(
                    tenant_id=0,
                    dept_id=did,
                    parent_id=parent,
                    dept_name=name,
                    order_num=0,
                    status=status,
                    ancestors=ancestors,
                )
            )
        await db.execute(
            update(Role)
            .where(Role.tenant_id == 0, Role.role_id == 201)
            .values(role_code="R_SCOPED", data_scope="2")
        )
        await db.execute(
            insert(role_depts),
            [{"tenant_id": 0, "role_id": 201, "dept_id": did} for did in visible],
        )
        await db.commit()


async def test_tree_reads_project_visible_nodes_as_local_roots(http_system):
    client, plain = http_system
    await scoped_departments(
        plain,
        [
            (400, None, "hidden-root", "1", "0"),
            (401, 400, "visible-parent", "1", "0,400"),
            (402, 401, "visible-child", "1", "0,400,401"),
            (403, 400, "hidden-sibling", "1", "0,400"),
            (404, 401, "disabled-visible", "2", "0,400,401"),
        ],
        [401, 402, 404],
    )
    tree_response = await client.get("/dept/tree")
    assert tree_response.status_code == 200, tree_response.text
    tree = tree_response.json()["data"]
    assert [r["deptId"] for r in tree] == ["401"]
    assert tree[0]["parentId"] is None and tree[0]["ancestors"] == "0"
    # The real custom-scope resolver excludes disabled departments. The old
    # API-only mock supplied an impossible custom resolution containing 404.
    assert [r["deptId"] for r in tree[0]["children"]] == ["402"]
    assert all(r["ancestors"] == "0,401" for r in tree[0]["children"])
    assert "hidden-root" not in str(tree) and "hidden-sibling" not in str(tree)
    options_response = await client.get("/dept/tree-option")
    assert options_response.status_code == 200, options_response.text
    options = options_response.json()["data"]
    assert [r["id"] for r in options] == ["401"] and options[0]["pId"] == ""
    assert [r["id"] for r in options[0]["children"]] == ["402"]
    assert "hidden-root" not in str(options) and "hidden-sibling" not in str(options)
    async with plain() as db:
        await db.execute(
            update(Role).where(Role.role_id == 201).values(role_code="R_SUPER")
        )
        await db.commit()
    full = (await client.get("/dept/tree")).json()["data"]
    parent = next(r for r in full[0]["children"] if r["deptId"] == "401")
    assert [r["deptId"] for r in parent["children"]] == ["402", "404"]
    full_options = (await client.get("/dept/tree-option")).json()["data"]
    parent_option = next(r for r in full_options[0]["children"] if r["id"] == "401")
    assert [r["id"] for r in parent_option["children"]] == ["402"]


async def test_list_reads_share_the_same_visible_set_and_totals(http_system):
    client, plain = http_system
    await scoped_departments(
        plain,
        [
            (410, None, "visible-a", "1", "0"),
            (411, 410, "visible-b", "1", "0,410"),
            (412, None, "hidden", "1", "0"),
        ],
        [410, 411],
    )
    response = await client.get("/dept/list", params={"current": 1, "size": 100})
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert page["total"] == 2 and {r["deptId"] for r in page["records"]} == {
        "410",
        "411",
    }
    response = await client.get("/dept/tree-list")
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert page["total"] == 1 and [r["deptId"] for r in page["records"]] == ["410"]
    assert "hidden" not in str(page["records"])


async def test_detail_uses_the_missing_surface_for_out_of_scope_ids(http_system):
    client, plain = http_system
    await scoped_departments(
        plain,
        [(420, None, "visible", "1", "0"), (421, None, "hidden", "1", "0")],
        [420],
    )
    visible = await client.get("/dept/420")
    assert visible.status_code == 200 and visible.json()["data"]["deptId"] == "420"
    hidden = await client.get("/dept/421")
    assert hidden.status_code == 404 and "hidden" not in hidden.text
