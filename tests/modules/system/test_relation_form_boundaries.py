"""Menu form collection limit and batching, independent of other owner budgets."""

from types import SimpleNamespace

import pytest

from app.core.exceptions import BusinessException
from app.modules.system.service.relation_form_context import menu_buttons


class ButtonProjection:
    def __init__(self, count):
        self.ids = list(range(1, count + 1))
        self.batches = []
        self.references = []
        self.tenant = SimpleNamespace(tenant_id=0)

    async def execute(self, statement):
        assert statement._limit_clause.value == 8193
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.ids))

    async def fetch(self, _db, _model, _anchor, *, maximum, **_kwargs):
        start = sum(self.batches)
        ids = self.ids[start : start + maximum]
        self.batches.append(len(ids))
        return [
            (i, {"menu_name": f"Button {i}", "permission": f"demo:item:{i}"}, None)
            for i in ids
        ]

    def source(self, record, _names):
        self.references.append(record)


@pytest.mark.parametrize(
    "count, batches", [(256, [256]), (257, [256, 1]), (8192, [256] * 32)]
)
async def test_complete_buttons_are_projected_in_bounded_batches(count, batches):
    projection = ButtonProjection(count)
    complete, buttons = await menu_buttons(projection, projection, 100)
    assert complete
    assert projection.batches == batches
    assert len(buttons) == count
    assert projection.references == projection.ids
    assert buttons[-1] == {"desc": f"Button {count}", "code": f"demo:item:{count}"}


async def test_button_limit_plus_one_rejects_before_source_projection():
    projection = ButtonProjection(8193)
    with pytest.raises(BusinessException) as error:
        await menu_buttons(projection, projection, 100)
    assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
    assert projection.batches == [] and projection.references == []
