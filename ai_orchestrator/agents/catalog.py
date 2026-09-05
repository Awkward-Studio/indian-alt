from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.cache import cache
from django.db import transaction
from packaging.version import InvalidVersion, Version

from ai_orchestrator.models import AIAuditLog, AISkill, AISkillRevision

from .contracts import AgentDependencies


RUNTIME_VERSION = Version("2.38.0")


@dataclass(frozen=True)
class SkillSummary:
    slug: str
    description: str
    revision_id: UUID
    digest: str
    risk: str


@dataclass(frozen=True)
class LoadedSkill:
    summary: SkillSummary
    instructions: str
    references: dict[str, str]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


class SkillCatalog:
    def __init__(self, actor, dependencies: AgentDependencies, revisions: dict[str, AISkillRevision]):
        self.actor = actor
        self.dependencies = dependencies
        self._revisions = revisions

    @classmethod
    def for_run(
        cls,
        *,
        actor,
        dependencies: AgentDependencies,
        requested_capabilities: set[str] | frozenset[str],
        requested_task: str = "",
    ) -> "SkillCatalog":
        if actor.id != dependencies.requested_by_id or not actor.is_authenticated:
            return cls(actor, dependencies, {})
        allowed_caps = set(requested_capabilities) & set(dependencies.capability_ids)
        is_admin = bool(
            actor.is_staff or actor.is_superuser
            or (getattr(actor, "profile", None) and actor.profile.is_admin and not actor.profile.is_disabled)
        )
        revisions: dict[str, AISkillRevision] = {}
        queryset = AISkillRevision.objects.select_related("skill").filter(
            status=AISkillRevision.Status.PUBLISHED,
            skill_format=AISkill.Format.AGENT_SKILL_V1,
            compatibility_status=AISkillRevision.CompatibilityStatus.COMPATIBLE,
        )
        task_terms = set(requested_task.lower().split())
        for revision in queryset:
            manifest = revision.package_manifest
            capabilities = set(manifest.get("capabilities", []))
            risk = manifest.get("risk")
            if not capabilities <= allowed_caps:
                continue
            if risk != "read_only" and not is_admin:
                continue
            if risk == "high_risk":
                continue
            if not _compatible(manifest.get("compatibility", {})):
                continue
            slug = manifest.get("slug", "")
            searchable = f"{slug} {manifest.get('description', '')}".lower()
            if task_terms and not any(term in searchable for term in task_terms):
                continue
            revisions[slug] = revision
        catalog = cls(actor, dependencies, revisions)
        catalog._record("discovered", [str(row.id) for row in revisions.values()])
        return catalog

    def summaries(self) -> tuple[SkillSummary, ...]:
        return tuple(self._summary(row) for _, row in sorted(self._revisions.items()))

    def load(self, slug: str) -> LoadedSkill:
        expected = self._revisions.get(slug)
        if expected is None:
            raise PermissionError("Skill is not available for this run.")
        current = AISkillRevision.objects.filter(
            id=expected.id,
            status=AISkillRevision.Status.PUBLISHED,
            package_digest=expected.package_digest,
        ).first()
        if current is None:
            raise PermissionError("Skill revision changed and must be rediscovered.")
        key = f"agent-skill-package:{current.package_digest}"
        payload = cache.get(key)
        if payload is None:
            payload = {
                "instructions": current.package_files.get("SKILL.md", ""),
                "references": {
                    item["path"]: current.package_files[item["path"]]
                    for item in current.package_manifest.get("references", [])
                },
                "input_schema": current.package_manifest.get("input_schema", {}),
                "output_schema": current.package_manifest.get("output_schema", {}),
            }
            cache.set(key, payload, timeout=3600)
        self._record("loaded", [str(current.id)])
        return LoadedSkill(summary=self._summary(current), **payload)

    @staticmethod
    def _summary(revision: AISkillRevision) -> SkillSummary:
        manifest = revision.package_manifest
        return SkillSummary(
            slug=manifest["slug"],
            description=manifest["description"],
            revision_id=revision.id,
            digest=revision.package_digest,
            risk=manifest["risk"],
        )

    def _record(self, key: str, revision_ids: list[str]) -> None:
        with transaction.atomic():
            audit = AIAuditLog.objects.select_for_update().filter(
                id=self.dependencies.audit_log_id,
                requested_by_id=self.dependencies.requested_by_id,
            ).first()
            if audit is None:
                return
            metadata = dict(audit.source_metadata or {})
            provenance = dict(metadata.get("agent_skills") or {})
            provenance[key] = revision_ids
            metadata["agent_skills"] = provenance
            audit.source_metadata = metadata
            audit.save(update_fields=["source_metadata"])


def _compatible(value: dict[str, Any]) -> bool:
    try:
        minimum = Version(str(value["min_runtime_version"]))
        maximum = Version(str(value["max_runtime_version"])) if value.get("max_runtime_version") else None
    except (KeyError, InvalidVersion):
        return False
    return minimum <= RUNTIME_VERSION and (maximum is None or RUNTIME_VERSION <= maximum)
