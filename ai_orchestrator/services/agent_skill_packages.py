from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ai_orchestrator.agents.skill_packages import AgentSkillManifest, validate_skill_package
from ai_orchestrator.models import AISkill, AISkillLifecycleEvent, AISkillRevision


class SkillPackageLifecycleError(ValueError):
    pass


def _require_admin(actor) -> None:
    profile = getattr(actor, "profile", None)
    if not getattr(actor, "is_authenticated", False) or not (
        getattr(actor, "is_staff", False) or getattr(actor, "is_superuser", False)
        or bool(profile and profile.is_admin and not profile.is_disabled)
    ):
        raise PermissionError("Administrator access is required.")


def invalidate_skill_cache(digest: str = "") -> None:
    from django.core.cache import cache

    cache.delete("agent-skill-catalog-generation")
    if digest:
        cache.delete(f"agent-skill-package:{digest}")


class AgentSkillPackageService:
    @staticmethod
    def validate(manifest: Any, files: Any) -> dict[str, Any]:
        result = validate_skill_package(manifest, files)
        return {
            **asdict(result),
            "normalized_manifest": (
                AgentSkillManifest.model_validate(manifest).model_dump(mode="json")
                if result.valid else None
            ),
        }

    @classmethod
    @transaction.atomic
    def publish(
        cls, *, skill_id, expected_version: int, actor, manifest: Any, files: Any,
    ) -> AISkillRevision:
        _require_admin(actor)
        skill = AISkill.objects.select_for_update().get(id=skill_id)
        if skill.version != expected_version:
            raise SkillPackageLifecycleError(
                f"Skill version changed from {expected_version} to {skill.version}. Refresh and retry."
            )
        result = validate_skill_package(manifest, files)
        if not result.valid:
            raise SkillPackageLifecycleError("; ".join(result.errors))
        next_revision = (skill.revisions.aggregate(value=Max("revision"))["value"] or 0) + 1
        skill.revisions.filter(status=AISkillRevision.Status.PUBLISHED).update(
            status=AISkillRevision.Status.ARCHIVED,
        )
        revision = AISkillRevision.objects.create(
            skill=skill,
            revision=next_revision,
            status=AISkillRevision.Status.PUBLISHED,
            skill_format=AISkill.Format.AGENT_SKILL_V1,
            system_template="",
            prompt_template=str(files.get("SKILL.md", "")),
            input_schema=manifest.get("input_schema", {}),
            output_schema=manifest.get("output_schema", {}),
            package_manifest=deepcopy(manifest),
            package_files=deepcopy(files),
            package_digest=result.digest,
            validation_report=result.report,
            compatibility_status=AISkillRevision.CompatibilityStatus.COMPATIBLE,
            created_by=actor,
            published_by=actor,
            published_at=timezone.now(),
        )
        skill.version = next_revision
        skill.status = AISkill.Status.APPROVED
        skill.skill_format = AISkill.Format.AGENT_SKILL_V1
        skill.approved_by = actor
        skill.approved_at = timezone.now()
        skill.save(update_fields=["version", "status", "skill_format", "approved_by", "approved_at", "updated_at"])
        cls._event(skill, revision, "published", actor)
        transaction.on_commit(lambda: invalidate_skill_cache(revision.package_digest))
        return revision

    @classmethod
    @transaction.atomic
    def rollback(cls, *, skill_id, revision_id, expected_version: int, actor) -> AISkillRevision:
        _require_admin(actor)
        skill = AISkill.objects.select_for_update().get(id=skill_id)
        if skill.version != expected_version:
            raise SkillPackageLifecycleError("Skill changed before rollback. Refresh and retry.")
        source = skill.revisions.get(id=revision_id, skill_format=AISkill.Format.AGENT_SKILL_V1)
        restored = cls.publish(
            skill_id=skill.id,
            expected_version=skill.version,
            actor=actor,
            manifest=source.package_manifest,
            files=source.package_files,
        )
        cls._event(
            skill, restored, "rolled_back", actor,
            metadata={"source_revision_id": str(source.id), "source_digest": source.package_digest},
        )
        return restored

    @staticmethod
    @transaction.atomic
    def archive(*, skill_id, revision_id, actor) -> AISkillRevision:
        _require_admin(actor)
        revision = AISkillRevision.objects.select_for_update().get(id=revision_id, skill_id=skill_id)
        revision.status = AISkillRevision.Status.ARCHIVED
        revision.save(update_fields=["status", "updated_at"])
        AgentSkillPackageService._event(revision.skill, revision, "archived", actor)
        transaction.on_commit(lambda: invalidate_skill_cache(revision.package_digest))
        return revision

    @staticmethod
    def export(revision: AISkillRevision) -> dict[str, Any]:
        return {
            "manifest": deepcopy(revision.package_manifest),
            "files": deepcopy(revision.package_files),
            "digest": revision.package_digest,
            "revision_id": str(revision.id),
        }

    @staticmethod
    def _event(skill, revision, action, actor, metadata=None) -> None:
        AISkillLifecycleEvent.objects.create(
            skill=skill,
            revision=revision,
            action=action,
            actor=actor,
            package_digest=revision.package_digest,
            validation_report=revision.validation_report,
            metadata=metadata or {},
        )
