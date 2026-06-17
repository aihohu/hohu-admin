from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class PermissionOut(BaseModel):
    """应用权限输出"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )

    type: str
    detail: dict[str, Any]
