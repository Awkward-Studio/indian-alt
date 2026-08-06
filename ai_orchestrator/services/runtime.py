from __future__ import annotations

import json
from typing import Any, Optional

from django.conf import settings

from ..models import AIAuditLog, AIPersonality, AISkill


class AIRuntimeService:
    """Central runtime resolver for the current vLLM-only deployment."""

    PROVIDER_VLLM = "vllm"
    INDUSTRY_CONTEXT_MAX_CHARS = 120_000
    RESERVED_SKILL_INPUT_KEYS = {
        "content",
        "audit_log_id",
        "context_label",
        "chat_template_kwargs",
        "prompt_template_override",
        "response_mode",
        "_source_metadata",
    }

    @classmethod
    def get_default_personality(cls) -> Optional[AIPersonality]:
        return AIPersonality.objects.filter(is_default=True).first()

    @classmethod
    def get_personality(cls, personality_name: Optional[str] = None) -> Optional[AIPersonality]:
        if personality_name and personality_name != "default":
            personality = AIPersonality.objects.filter(name=personality_name).first()
            if personality:
                return personality
        return cls.get_default_personality()

    @classmethod
    def get_skill(cls, skill_name: Optional[str]) -> Optional[AISkill]:
        if not skill_name:
            return None
        return AISkill.objects.filter(name=skill_name).first()

    @classmethod
    def validate_skill_inputs(cls, skill: AISkill, inputs: Any) -> dict:
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise ValueError("inputs must be an object.")
        if len(json.dumps(inputs, ensure_ascii=False, default=str)) > 20_000:
            raise ValueError("inputs exceed the 20,000 character limit.")

        schema = skill.input_schema or {}
        if not isinstance(schema, dict):
            raise ValueError("The skill input schema is malformed.")
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        if not isinstance(required, list) or not all(
            isinstance(key, str) for key in required
        ):
            raise ValueError("The skill input schema has an invalid required list.")
        if not isinstance(properties, dict):
            raise ValueError("The skill input schema has invalid properties.")

        missing = [key for key in required if key not in inputs]
        if missing:
            raise ValueError(
                f"Missing required skill inputs: {', '.join(sorted(missing))}."
            )
        if schema.get("additionalProperties") is False:
            unexpected = sorted(set(inputs) - set(properties))
            if unexpected:
                raise ValueError(
                    f"Unexpected skill inputs: {', '.join(unexpected)}."
                )

        reserved = sorted(set(inputs) & cls.RESERVED_SKILL_INPUT_KEYS)
        if reserved:
            raise ValueError(
                f"Reserved skill input names are not allowed: {', '.join(reserved)}."
            )

        expected_python_types = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        for key, value in inputs.items():
            definition = properties.get(key)
            if not isinstance(definition, dict) or value is None:
                continue
            expected_name = definition.get("type")
            expected_type = expected_python_types.get(expected_name)
            if expected_type and (
                not isinstance(value, expected_type)
                or expected_name in {"number", "integer"}
                and isinstance(value, bool)
            ):
                raise ValueError(
                    f"Skill input '{key}' must be of type {expected_name}."
                )
        return inputs

    @classmethod
    def build_industry_skill_context(cls, deal, inputs: dict, documents) -> str:
        sections = [
            "# Deal scope",
            f"Company: {deal.title or 'Unknown'}",
            f"Sector: {deal.sector or 'Not recorded'}",
            f"Industry: {deal.industry or 'Not recorded'}",
            f"City: {deal.city or 'Not recorded'}",
            f"Deal summary: {deal.deal_summary or 'Not recorded'}",
            "",
            "# Analyst-supplied inputs",
            json.dumps(inputs, ensure_ascii=False, sort_keys=True, default=str),
        ]
        remaining = cls.INDUSTRY_CONTEXT_MAX_CHARS - len("\n".join(sections))
        for index, document in enumerate(documents, start=1):
            if remaining <= 0:
                break
            text = document.normalized_text or document.extracted_text or ""
            excerpt = text[:remaining]
            section = (
                f"\n# Source document {index}\n"
                f"Document ID: {document.id}\n"
                f"Title: {document.title}\n"
                f"Type: {document.document_type}\n"
                f"Content:\n{excerpt}"
            )
            sections.append(section)
            remaining -= len(section)
        return "\n".join(sections)

    @classmethod
    def get_provider(cls, personality: Optional[AIPersonality] = None) -> str:
        provider = getattr(personality, "model_provider", None) or cls.PROVIDER_VLLM
        return cls.PROVIDER_VLLM if provider != cls.PROVIDER_VLLM else provider

    @classmethod
    def get_text_model(cls, personality: Optional[AIPersonality] = None) -> str:
        personality_model = getattr(personality, "text_model_name", None)
        # This deployment resolves all text traffic through vLLM. Legacy
        # personality rows may still carry old Ollama-era model overrides,
        # which should not supersede the configured vLLM model.
        if (
            cls.get_provider(personality) == cls.PROVIDER_VLLM
            and personality_model
            and personality_model != "default"
        ):
            return personality_model
        return getattr(settings, "VLLM_TEXT_MODEL", "")

    @classmethod
    def get_vision_model(cls, personality: Optional[AIPersonality] = None) -> str:
        personality_model = getattr(personality, "vision_model_name", None)
        if (
            cls.get_provider(personality) == cls.PROVIDER_VLLM
            and personality_model
            and personality_model != "default"
        ):
            return personality_model
        return getattr(settings, "VLLM_VISION_MODEL", "")

    @classmethod
    def get_embedding_model(cls) -> str:
        return getattr(settings, "EMBEDDING_MODEL", "") or getattr(settings, "VLLM_EMBEDDING_MODEL", "")

    @classmethod
    def get_reranker_model(cls) -> str:
        return getattr(settings, "RERANKER_MODEL", "")

    @classmethod
    def get_planner_model(cls, personality: Optional[AIPersonality] = None) -> str:
        return getattr(settings, "VLLM_PLANNER_MODEL", "") or cls.get_text_model(personality)

    @classmethod
    def create_audit_log(
        cls,
        *,
        source_type: str,
        source_id: Optional[str],
        context_label: Optional[str] = None,
        personality: Optional[AIPersonality] = None,
        skill: Optional[AISkill] = None,
        status: str = "PENDING",
        is_success: bool = False,
        model_used: Optional[str] = None,
        system_prompt: str = "",
        user_prompt: str = "",
        source_metadata: Optional[dict] = None,
        celery_task_id: Optional[str] = None,
        requested_by=None,
        skill_version: Optional[int] = None,
    ) -> AIAuditLog:
        personality = personality or cls.get_default_personality()
        return AIAuditLog.objects.create(
            source_type=source_type,
            source_id=source_id,
            context_label=context_label,
            personality=personality,
            skill=skill,
            requested_by=requested_by,
            skill_version=skill_version or getattr(skill, "version", None),
            model_provider=cls.get_provider(personality),
            model_used=model_used or cls.get_text_model(personality),
            status=status,
            is_success=is_success,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            source_metadata=source_metadata,
            celery_task_id=celery_task_id,
        )
