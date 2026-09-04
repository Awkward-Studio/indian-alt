from __future__ import annotations

from collections.abc import Callable, Iterable

from pydantic_ai.toolsets import AbstractToolset

from .contracts import AgentDependencies

ToolsetFactory = Callable[[AgentDependencies], AbstractToolset[AgentDependencies]]


class AgentCapabilityRegistry:
    """Allowlisted toolset factories. Installed skills will select from this registry."""

    def __init__(self):
        self._factories: dict[str, ToolsetFactory] = {}

    def register(self, capability_id: str, factory: ToolsetFactory) -> None:
        if capability_id in self._factories:
            raise ValueError(f"Capability {capability_id!r} is already registered.")
        self._factories[capability_id] = factory

    def resolve(
        self,
        capability_ids: Iterable[str],
        dependencies: AgentDependencies,
    ) -> list[AbstractToolset[AgentDependencies]]:
        requested = set(capability_ids)
        unauthorized = requested - dependencies.capability_ids
        if unauthorized:
            raise PermissionError(
                f"Capabilities are outside the server-created scope: {', '.join(sorted(unauthorized))}."
            )
        unknown = requested - set(self._factories)
        if unknown:
            raise LookupError(f"Unknown capabilities: {', '.join(sorted(unknown))}.")
        return [self._factories[value](dependencies) for value in sorted(requested)]
