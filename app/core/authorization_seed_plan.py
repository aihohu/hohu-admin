"""Internal fixed-resource seed plan data, never an arbitrary SQL program."""

from dataclasses import dataclass, field

from app.core.authorization_plan import NewResource, ResourceKey


@dataclass(frozen=True, slots=True)
class SeedChange:
    action: str
    key: ResourceKey
    values: dict = field(repr=False)


@dataclass(frozen=True, slots=True)
class SeedPlan:
    existing: tuple[ResourceKey, ...]
    changes: tuple[SeedChange, ...]

    @property
    def new(self):
        return tuple(
            NewResource(change.key)
            for change in self.changes
            if change.action == "insert"
        )
