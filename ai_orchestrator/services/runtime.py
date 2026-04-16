from __future__ import annotations

from typing import Optional

from django.conf import settings

from ..models import AIAuditLog, AIPersonality, AISkill


class AIRuntimeService:
    """Central runtime resolver for the current vLLM-only deployment."""

    PROVIDER_VLLM = "vllm"

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
    def get_provider(cls, personality: Optional[AIPersonality] = None) -> str:
        provider = getattr(personality, "model_provider", None) or cls.PROVIDER_VLLM
        return cls.PROVIDER_VLLM if provider != cls.PROVIDER_VLLM else provider

    @classmethod
    def get_text_model(cls, personality: Optional[AIPersonality] = None) -> str:
        personality_model = getattr(personality, "text_model_name", None)
        return (
            personality_model if personality_model and personality_model != "default"
            else getattr(settings, "VLLM_TEXT_MODEL", "")
        )

    @classmethod
    def get_vision_model(cls, personality: Optional[AIPersonality] = None) -> str:
        personality_model = getattr(personality, "vision_model_name", None)
        return (
            personality_model if personality_model and personality_model != "default"
            else getattr(settings, "VLLM_VISION_MODEL", "")
        )

    @classmethod
    def get_embedding_model(cls) -> str:
        return getattr(settings, "VLLM_EMBEDDING_MODEL", "")

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
    ) -> AIAuditLog:
        personality = personality or cls.get_default_personality()
        return AIAuditLog.objects.create(
            source_type=source_type,
            source_id=source_id,
            context_label=context_label,
            personality=personality,
            skill=skill,
            model_provider=cls.get_provider(personality),
            model_used=model_used or cls.get_text_model(personality),
            status=status,
            is_success=is_success,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            source_metadata=source_metadata,
            celery_task_id=celery_task_id,
        )
