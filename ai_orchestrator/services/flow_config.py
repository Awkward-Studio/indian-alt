import copy
from typing import Any, Dict, List, Tuple

from django.db import transaction

from ..models import AIFlowDefinition, AIFlowVersion, AISkill


FLOW_KEY = "universal_chat"


STAGE_CATALOG = [
    {
        "id": "query_planner",
        "label": "Query Planner",
        "description": "Classifies the query and extracts exact terms, metrics, and retrieval intent.",
        "required": True,
        "kind": "prompt",
    },
    {
        "id": "deal_filtering",
        "label": "Deal Filtering",
        "description": "Narrows candidate deals before document retrieval.",
        "required": True,
        "kind": "settings",
    },
    {
        "id": "chunk_retrieval",
        "label": "Chunk Retrieval",
        "description": "Controls the candidate pool and hybrid retrieval parameters.",
        "required": True,
        "kind": "settings",
    },
    {
        "id": "chunk_rerank",
        "label": "Chunk Rerank",
        "description": "Boosts exact entity and metric-bearing chunks above broad semantic matches.",
        "required": True,
        "kind": "settings",
    },
    {
        "id": "stats_block",
        "label": "Stats Block",
        "description": "Adds aggregate pipeline stats when the query calls for them.",
        "required": False,
        "kind": "settings",
    },
    {
        "id": "context_assembly",
        "label": "Context Assembly",
        "description": "Budgets and truncates the final evidence payload before answer generation.",
        "required": True,
        "kind": "settings",
    },
    {
        "id": "answer_generation",
        "label": "Answer Generation",
        "description": "Final user-facing answer prompt used by the universal chat skill.",
        "required": True,
        "kind": "prompt",
    },
]


DEFAULT_PLANNER_PROMPT = """Translate the user query into retrieval JSON.

Return only JSON with this shape:
{
  "query_type": "exact_lookup|comparison|stats|pipeline_search|timeline|narrative",
  "deal_filters": {
    "title": null,
    "industry": null,
    "sector": null,
    "city": null,
    "priority": null,
    "current_phase": null,
    "is_female_led": null,
    "management_meeting": null
  },
  "exact_terms": ["exact company names or phrases"],
  "keywords": ["important keywords"],
  "metric_terms": ["ARR", "revenue", "CM1"],
  "rag_queries": ["semantic search query variants"],
  "needs_stats": false,
  "deal_limit": 8,
  "chunks_per_deal": 2
}

Conversation ID: {{conversation_id}}
User query: {{user_message}}
"""


DEFAULT_ANSWER_PROMPT = """You are answering a user query against the firm-wide deal database.

Use the retrieved evidence below. Answer directly, stay precise, and avoid inventing values that are not present in the evidence.
If retrieval is inconclusive, say so clearly.

[CHAT HISTORY]
{{ history_context }}

[QUERY PLAN]
{{ query_plan }}

[RETRIEVED CONTEXT]
{{ context_data }}

[USER QUERY]
{{ content }}
"""


class UniversalChatFlowService:
    @classmethod
    def get_stage_catalog(cls) -> List[Dict[str, Any]]:
        return copy.deepcopy(STAGE_CATALOG)

    @classmethod
    def build_default_config(cls) -> Dict[str, Any]:
        skill = AISkill.objects.filter(name=FLOW_KEY).first()
        answer_prompt = skill.prompt_template if skill and skill.prompt_template else DEFAULT_ANSWER_PROMPT
        return {
            "stages": [
                {
                    "id": "query_planner",
                    "enabled": True,
                    "settings": {
                        "prompt_template": DEFAULT_PLANNER_PROMPT,
                        "fallback_query_type": "pipeline_search",
                        "default_deal_limit": 20,
                        "default_chunks_per_deal": 8,
                        "max_deal_limit": 30,
                        "max_chunks_per_deal": 12,
                    },
                },
                {
                    "id": "deal_filtering",
                    "enabled": True,
                    "settings": {
                        "candidate_pool_limit": 250,
                        "result_limit": 20,
                    },
                },
                {
                    "id": "chunk_retrieval",
                    "enabled": True,
                    "settings": {
                        "vector_limit": 300,
                        "sqlite_candidate_limit": 600,
                        "fallback_candidate_limit": 400,
                        "default_chunks_per_deal": 8,
                    },
                },
                {
                    "id": "chunk_rerank",
                    "enabled": True,
                    "settings": {
                        "deal_title_exact_boost": 100,
                        "deal_context_exact_boost": 40,
                        "deal_title_keyword_boost": 30,
                        "deal_context_keyword_boost": 10,
                        "deal_metric_boost": 20,
                        "chunk_title_exact_boost": 120,
                        "chunk_content_exact_boost": 60,
                        "chunk_metric_boost": 50,
                        "chunk_title_keyword_boost": 25,
                        "chunk_content_keyword_boost": 12,
                        "timeline_bonus": 25,
                    },
                },
                {
                    "id": "stats_block",
                    "enabled": True,
                    "settings": {
                        "auto_include_for_stats_queries": True,
                    },
                },
                {
                    "id": "context_assembly",
                    "enabled": True,
                    "settings": {
                        "max_total_chunks": 80,
                        "soft_max_total_chunks": 60,
                        "fallback_max_total_chunks": 80,
                        "max_context_chars": 180000,
                        "chunk_excerpt_chars": 2200,
                        "deal_summary_excerpt_chars": 1400,
                        "min_chunks_per_selected_deal": 3,
                        "max_chunks_per_selected_deal": 24,
                        "few_deal_chunk_boost_threshold": 4,
                        "few_deal_chunk_boost": 4,
                        "single_deal_chunk_boost": 12,
                        "overflow_reporting_enabled": True,
                    },
                },
                {
                    "id": "answer_generation",
                    "enabled": True,
                    "settings": {
                        "prompt_template": answer_prompt,
                    },
                },
            ]
        }

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        stages = config.get("stages")
        if not isinstance(stages, list) or not stages:
            raise ValueError("Flow config must include a non-empty 'stages' array.")

        allowed_ids = {stage["id"] for stage in STAGE_CATALOG}
        required_ids = {stage["id"] for stage in STAGE_CATALOG if stage["required"]}
        seen_ids = []
        normalized = []

        for stage in stages:
            stage_id = stage.get("id")
            if stage_id not in allowed_ids:
                raise ValueError(f"Unknown flow stage: {stage_id}")
            if stage_id in seen_ids:
                raise ValueError(f"Duplicate flow stage: {stage_id}")
            seen_ids.append(stage_id)
            normalized.append(
                {
                    "id": stage_id,
                    "enabled": bool(stage.get("enabled", True)),
                    "settings": stage.get("settings") or {},
                }
            )

        missing = required_ids.difference(seen_ids)
        if missing:
            raise ValueError(f"Missing required stages: {', '.join(sorted(missing))}")

        if normalized[-1]["id"] != "answer_generation":
            raise ValueError("The answer_generation stage must remain last.")

        for stage in normalized:
            settings = stage["settings"]
            for key, value in list(settings.items()):
                if isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)):
                    if value < 0:
                        raise ValueError(f"{stage['id']}.{key} cannot be negative.")
                elif value is not None and not isinstance(value, str):
                    raise ValueError(f"{stage['id']}.{key} must be a string, number, boolean, or null.")

        answer_stage = next(stage for stage in normalized if stage["id"] == "answer_generation")
        if "{{ content }}" not in answer_stage["settings"].get("prompt_template", "") and "{{content}}" not in answer_stage["settings"].get("prompt_template", ""):
            raise ValueError("The answer_generation prompt must include {{ content }}.")

        planner_stage = next(stage for stage in normalized if stage["id"] == "query_planner")
        if "{{user_message}}" not in planner_stage["settings"].get("prompt_template", ""):
            raise ValueError("The query_planner prompt must include {{user_message}}.")

        return {"stages": normalized}

    @classmethod
    def ensure_flow(cls) -> Tuple[AIFlowDefinition, AIFlowVersion, AIFlowVersion | None]:
        with transaction.atomic():
            flow, _ = AIFlowDefinition.objects.get_or_create(
                key=FLOW_KEY,
                defaults={
                    "name": "Universal Chat Flow",
                    "description": "Stage-based retrieval and answer pipeline for cross-deal chat.",
                },
            )
            published = flow.versions.filter(status=AIFlowVersion.Status.PUBLISHED).order_by("-version").first()
            if not published:
                published = AIFlowVersion.objects.create(
                    flow=flow,
                    version=1,
                    status=AIFlowVersion.Status.PUBLISHED,
                    config=cls.build_default_config(),
                )
                answer_prompt = cls.stage_settings(published.config, "answer_generation").get("prompt_template")
                if answer_prompt:
                    AISkill.objects.update_or_create(
                        name=FLOW_KEY,
                        defaults={
                            "description": "Firm-wide hybrid retrieval and answer orchestration.",
                            "prompt_template": answer_prompt,
                        },
                    )
            draft = flow.versions.filter(status=AIFlowVersion.Status.DRAFT).order_by("-version").first()
            return flow, published, draft

    @classmethod
    def get_runtime_config(cls) -> Tuple[Dict[str, Any], AIFlowVersion]:
        _, published, _ = cls.ensure_flow()
        return cls.validate_config(copy.deepcopy(published.config or cls.build_default_config())), published

    @classmethod
    def create_draft_from_published(cls) -> AIFlowVersion:
        flow, published, draft = cls.ensure_flow()
        if draft:
            return draft
        return AIFlowVersion.objects.create(
            flow=flow,
            version=(flow.versions.order_by("-version").first().version + 1),
            status=AIFlowVersion.Status.DRAFT,
            config=copy.deepcopy(published.config),
        )

    @classmethod
    def update_draft(cls, config: Dict[str, Any]) -> AIFlowVersion:
        draft = cls.create_draft_from_published()
        draft.config = cls.validate_config(copy.deepcopy(config))
        draft.save(update_fields=["config", "updated_at"])
        return draft

    @classmethod
    def publish_draft(cls) -> AIFlowVersion:
        flow, _, draft = cls.ensure_flow()
        if not draft:
            raise ValueError("No draft flow exists to publish.")
        draft.config = cls.validate_config(copy.deepcopy(draft.config))
        flow.versions.filter(status=AIFlowVersion.Status.PUBLISHED).update(status=AIFlowVersion.Status.ARCHIVED)
        draft.status = AIFlowVersion.Status.PUBLISHED
        draft.save(update_fields=["config", "status", "updated_at"])
        flow.versions.filter(status=AIFlowVersion.Status.DRAFT).exclude(id=draft.id).delete()

        answer_prompt = cls.stage_settings(draft.config, "answer_generation").get("prompt_template")
        if answer_prompt:
            skill, _ = AISkill.objects.get_or_create(
                name=FLOW_KEY,
                defaults={
                    "description": "Firm-wide hybrid retrieval and answer orchestration.",
                    "prompt_template": answer_prompt,
                },
            )
            skill.prompt_template = answer_prompt
            if not skill.description:
                skill.description = "Firm-wide hybrid retrieval and answer orchestration."
            skill.save(update_fields=["prompt_template", "description", "updated_at"])
        return draft

    @classmethod
    def serialize_version(cls, version: AIFlowVersion | None) -> Dict[str, Any] | None:
        if not version:
            return None
        return {
            "id": str(version.id),
            "version": version.version,
            "status": version.status,
            "config": version.config,
            "created_at": version.created_at,
            "updated_at": version.updated_at,
        }

    @classmethod
    def serialize_state(cls) -> Dict[str, Any]:
        flow, published, draft = cls.ensure_flow()
        return {
            "definition": {
                "id": str(flow.id),
                "key": flow.key,
                "name": flow.name,
                "description": flow.description,
            },
            "published_version": cls.serialize_version(published),
            "draft_version": cls.serialize_version(draft),
            "stage_catalog": cls.get_stage_catalog(),
        }

    @classmethod
    def stage_settings(cls, config: Dict[str, Any], stage_id: str) -> Dict[str, Any]:
        for stage in config.get("stages", []):
            if stage.get("id") == stage_id:
                return stage.get("settings") or {}
        return {}
