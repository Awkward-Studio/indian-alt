from __future__ import annotations

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from deals.models import DealAnalysis, DealDocument

from ..models import AIAuditLog, AISkill, DealIndustrySkillAssignment
from .ai_processor import AIProcessorService
from .runtime import AIRuntimeService


class IndustrySkillService:
    @staticmethod
    def context_hash(assignment: DealIndustrySkillAssignment) -> str:
        deal = assignment.deal
        latest_analysis = DealAnalysis.objects.filter(deal=deal).order_by(
            "-version", "-created_at"
        ).first()
        documents = DealDocument.objects.filter(
            deal=deal,
            id__in=assignment.source_document_ids or [],
        ).order_by("id")
        payload = {
            "deal": {
                "id": str(deal.id),
                "title": deal.title,
                "sector": deal.sector,
                "industry": deal.industry,
                "city": deal.city,
                "summary": deal.deal_summary,
                "updated_at": deal.updated_at.isoformat(),
            },
            "analysis": (
                {
                    "id": str(latest_analysis.id),
                    "version": latest_analysis.version,
                    "created_at": latest_analysis.created_at.isoformat(),
                }
                if latest_analysis
                else None
            ),
            "documents": [
                {
                    "id": str(document.id),
                    "created_at": document.created_at.isoformat(),
                    "content_hash": hashlib.sha256(
                        (document.normalized_text or document.extracted_text or "").encode("utf-8")
                    ).hexdigest(),
                }
                for document in documents
            ],
            "inputs": assignment.inputs or {},
            "skill_id": str(assignment.skill_id),
            "skill_version": assignment.skill.version,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _documents(assignment):
        document_ids = assignment.source_document_ids or []
        documents = list(
            DealDocument.objects.filter(
                deal=assignment.deal,
                id__in=document_ids,
            ).order_by("created_at")
        )
        if len(documents) != len(set(str(value) for value in document_ids)):
            raise ValueError("Every source document must belong to the selected deal.")
        return documents

    @classmethod
    def run(cls, assignment_id, *, requested_by=None, trigger="manual") -> AIAuditLog:
        with transaction.atomic():
            assignment = (
                DealIndustrySkillAssignment.objects.select_for_update()
                .select_related("deal", "skill")
                .get(id=assignment_id)
            )
            skill = assignment.skill
            if not assignment.enabled:
                raise ValueError("This industry skill assignment is disabled.")
            if (
                skill.status != AISkill.Status.APPROVED
                or not skill.is_industry_overview_eligible
            ):
                raise PermissionError("The selected skill is not approved for Industry Overview.")
            inputs = AIRuntimeService.validate_skill_inputs(skill, assignment.inputs or {})
            documents = cls._documents(assignment)
            context_hash = cls.context_hash(assignment)
            if (
                trigger == "automatic"
                and assignment.last_context_hash == context_hash
                and assignment.last_audit_log_id
                and assignment.last_run_status
                in {
                    DealIndustrySkillAssignment.RunStatus.QUEUED,
                    DealIndustrySkillAssignment.RunStatus.PROCESSING,
                    DealIndustrySkillAssignment.RunStatus.COMPLETED,
                    DealIndustrySkillAssignment.RunStatus.FAILED,
                }
            ):
                return assignment.last_audit_log

            source_metadata = {
                "run_kind": "industry_skill",
                "run_trigger": trigger,
                "assignment_id": str(assignment.id),
                "context_hash": context_hash,
                "skill_version": skill.version,
                "input_scope": {
                    "deal_id": str(assignment.deal_id),
                    "input_keys": sorted(inputs),
                    "source_document_ids": [str(document.id) for document in documents],
                },
                "sources": [
                    {
                        "document_id": str(document.id),
                        "title": document.title,
                        "document_type": document.document_type,
                    }
                    for document in documents
                ],
            }
            audit_log = AIRuntimeService.create_audit_log(
                source_type="industry_skill",
                source_id=str(assignment.deal_id),
                context_label=f"Industry skill: {skill.name} — {assignment.deal.title}",
                skill=skill,
                status="PENDING",
                is_success=False,
                requested_by=requested_by,
                skill_version=skill.version,
                source_metadata=source_metadata,
            )
            assignment.last_context_hash = context_hash
            assignment.last_run_status = DealIndustrySkillAssignment.RunStatus.PROCESSING
            assignment.last_run_trigger = trigger
            assignment.last_audit_log = audit_log
            assignment.save(update_fields=[
                "last_context_hash", "last_run_status", "last_run_trigger",
                "last_audit_log", "updated_at",
            ])

        content = AIRuntimeService.build_industry_skill_context(
            assignment.deal, inputs, documents
        )
        try:
            AIProcessorService().process_content(
                content=content,
                skill_name=skill.name,
                metadata={
                    **inputs,
                    "audit_log_id": str(audit_log.id),
                    "_source_metadata": source_metadata,
                    "context_label": audit_log.context_label,
                },
                source_id=str(assignment.deal_id),
                source_type="industry_skill",
            )
            audit_log.refresh_from_db()
            if audit_log.status != "COMPLETED":
                raise RuntimeError(audit_log.error_message or "Skill execution did not complete.")
        except Exception as exc:
            audit_log.status = "FAILED"
            audit_log.is_success = False
            audit_log.error_message = str(exc)
            audit_log.completed_at = audit_log.completed_at or timezone.now()
            audit_log.save(update_fields=[
                "status", "is_success", "error_message", "completed_at",
            ])
        finally:
            assignment.refresh_from_db()
            assignment.last_run_status = (
                DealIndustrySkillAssignment.RunStatus.COMPLETED
                if audit_log.status == "COMPLETED"
                else DealIndustrySkillAssignment.RunStatus.FAILED
            )
            assignment.save(update_fields=["last_run_status", "updated_at"])
        return audit_log

    @classmethod
    def enqueue_automatic(cls, assignment: DealIndustrySkillAssignment) -> bool:
        if not assignment.enabled or not assignment.auto_run:
            return False
        if (
            assignment.skill.status != AISkill.Status.APPROVED
            or not assignment.skill.is_industry_overview_eligible
        ):
            return False
        context_hash = cls.context_hash(assignment)
        with transaction.atomic():
            locked = DealIndustrySkillAssignment.objects.select_for_update().get(
                id=assignment.id
            )
            if locked.last_context_hash == context_hash:
                return False
            locked.last_context_hash = context_hash
            locked.last_run_status = DealIndustrySkillAssignment.RunStatus.QUEUED
            locked.last_run_trigger = "automatic"
            locked.save(update_fields=[
                "last_context_hash", "last_run_status", "last_run_trigger", "updated_at",
            ])
            from ..tasks import run_industry_skill_assignment_task

            transaction.on_commit(
                lambda: run_industry_skill_assignment_task.delay(str(locked.id))
            )
        return True

    @staticmethod
    def serialize(assignment: DealIndustrySkillAssignment) -> dict:
        skill = assignment.skill
        audit = assignment.last_audit_log
        metadata = audit.source_metadata if audit and audit.source_metadata else {}
        current_hash = IndustrySkillService.context_hash(assignment)
        return {
            "id": str(assignment.id),
            "deal_id": str(assignment.deal_id),
            "skill": {
                "id": str(skill.id),
                "name": skill.name,
                "description": skill.description,
                "version": skill.version,
                "status": skill.status,
                "input_schema": skill.input_schema,
            },
            "enabled": assignment.enabled,
            "auto_run": assignment.auto_run,
            "inputs": assignment.inputs,
            "source_document_ids": assignment.source_document_ids,
            "status": assignment.last_run_status,
            "trigger": assignment.last_run_trigger or None,
            "stale": bool(assignment.last_context_hash and assignment.last_context_hash != current_hash),
            "updated_at": assignment.updated_at.isoformat(),
            "latest_run": (
                {
                    "audit_log_id": str(audit.id),
                    "status": audit.status,
                    "skill_version": audit.skill_version,
                    "trigger": metadata.get("run_trigger"),
                    "output": audit.raw_response,
                    "parsed_output": audit.parsed_json,
                    "sources": metadata.get("sources", []),
                    "error": audit.error_message,
                    "created_at": audit.created_at.isoformat(),
                    "completed_at": audit.completed_at.isoformat() if audit.completed_at else None,
                }
                if audit
                else None
            ),
        }
