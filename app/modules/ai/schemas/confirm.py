"""HITL 确认请求、响应与安全展示 schema。"""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_serializer,
    field_validator,
)
from pydantic.alias_generators import to_camel

_PRESENTATION_SENSITIVE_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "path",
    "url",
    "html",
)

_PRESENTATION_SENSITIVE_VALUE_MARKERS = (
    "preview_token",
    "access_token",
    "refresh_token",
    "private_uploads",
    "authorization:",
    "bearer ",
    "password=",
    "password:",
    "secret=",
    "secret:",
)


def _reject_sensitive_text(value: str, *, field_name: str) -> str:
    lowered = value.lower()
    stripped = value.strip()
    contains_path = (
        stripped.startswith(("/", "\\"))
        or "../" in value
        or "..\\" in value
        or (
            len(stripped) >= 3
            and stripped[0].isalpha()
            and stripped[1] == ":"
            and stripped[2] in {"/", "\\"}
        )
    )
    if (
        "://" in lowered
        or contains_path
        or any(marker in lowered for marker in _PRESENTATION_SENSITIVE_VALUE_MARKERS)
    ):
        raise ValueError(f"confirmation {field_name} is sensitive")
    return value


class ConfirmationPresentationField(BaseModel):
    """One ordered, scalar-only confirmation field."""

    label: str = Field(..., min_length=1, max_length=64)
    value: StrictStr | StrictInt | StrictFloat
    tone: Literal["default", "info", "success", "warning", "danger"] | None = None

    @field_validator("value")
    @classmethod
    def validate_value_length(cls, value):  # noqa: ANN001
        if len(str(value)) > 256:
            raise ValueError("confirmation field value is too long")
        return value

    @field_validator("label")
    @classmethod
    def reject_sensitive_label(cls, value: str) -> str:
        lowered = value.lower()
        if any(fragment in lowered for fragment in _PRESENTATION_SENSITIVE_FRAGMENTS):
            raise ValueError("confirmation field label is sensitive")
        return value

    @field_validator("value")
    @classmethod
    def reject_sensitive_value(cls, value):  # noqa: ANN001
        if isinstance(value, str):
            _reject_sensitive_text(value, field_name="field value")
        return value

    model_config = ConfigDict(extra="forbid")


class ConfirmationPresentation(BaseModel):
    """Safe DTO persisted by Gateway and rendered by every client."""

    title: str = Field(..., min_length=1, max_length=120)
    summary: str | None = Field(default=None, max_length=500)
    summary_key: str | None = Field(default=None, min_length=1, max_length=160)
    summary_params: dict[str, StrictStr | StrictInt | StrictFloat] = Field(
        default_factory=dict, max_length=20
    )
    fields: list[ConfirmationPresentationField] = Field(
        default_factory=list, max_length=20
    )
    warnings: list[str] = Field(default_factory=list, max_length=10)
    warning_keys: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("title", "summary")
    @classmethod
    def reject_sensitive_text(cls, value: str | None, info):  # noqa: ANN001
        if value is not None:
            _reject_sensitive_text(value, field_name=info.field_name)
        return value

    @field_validator("warnings")
    @classmethod
    def validate_warnings(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 500 for value in values):
            raise ValueError("confirmation warning is invalid")
        for value in values:
            _reject_sensitive_text(value, field_name="warning")
        return values

    @field_validator("summary_key")
    @classmethod
    def validate_summary_key(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("page.ai.chat."):
            raise ValueError("confirmation summary key is outside the AI chat locale")
        return value

    @field_validator("warning_keys")
    @classmethod
    def validate_warning_keys(cls, values: list[str]) -> list[str]:
        if any(not value.startswith("page.ai.chat.") for value in values):
            raise ValueError("confirmation warning key is outside the AI chat locale")
        return values

    @field_validator("summary_params")
    @classmethod
    def validate_summary_params(
        cls, values: dict[str, StrictStr | StrictInt | StrictFloat]
    ) -> dict[str, StrictStr | StrictInt | StrictFloat]:
        for key, value in values.items():
            if not key or len(key) > 64 or len(str(value)) > 256:
                raise ValueError("confirmation summary parameter is invalid")
            if isinstance(value, str):
                _reject_sensitive_text(value, field_name="summary parameter")
        return values

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ConfirmRequest(BaseModel):
    """``POST /ai/confirm`` 请求体。"""

    confirmation_id: str = Field(
        ..., min_length=10, description="HITL 抽屉拿到的 confirmation_id"
    )
    action: Literal["approve", "reject"] = Field(..., description="用户点确认或拒绝")

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ConfirmResponse(BaseModel):
    """``/ai/confirm`` 响应的 data 字段。"""

    action_id: int | None = Field(
        default=None, description="Durable action ID；legacy Redis-only HITL 为 null"
    )
    tool_call_id: str = Field(..., description="对应 ai_operation_log.tool_call_id")
    status: Literal[
        "queued",
        "stream_gone",
        "running",
        "succeeded",
        "failed",
        "rejected",
        "expired",
    ] = Field(
        default="queued",
        description=(
            "queued = 唤醒成功，业务将正常执行（前端启动 30s SSE 断流轮询兜底）；"
            "stream_gone = 流已断（服务重启 / 单 worker 切换 / SSE 已中断），"
            "tool 不会执行，前端应立即停止轮询并提示用户重新发起（修订 S-14）"
        ),
    )

    @field_serializer("action_id")
    def serialize_action_id(self, value: int | None, _info) -> str | None:
        return str(value) if value is not None else None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
