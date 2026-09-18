from django.db import migrations
from django.utils import timezone


def add_deal_field_synthesis(apps, schema_editor):
    from ai_orchestrator.prompt_contracts import (
        DEAL_FIELD_SYNTHESIS_JSON_SCHEMA,
        DEAL_FIELD_SYNTHESIS_PROMPT_TEMPLATE,
        DEAL_FIELD_SYNTHESIS_SYSTEM_TEMPLATE,
    )

    AISkill = apps.get_model("ai_orchestrator", "AISkill")
    AISkillRevision = apps.get_model("ai_orchestrator", "AISkillRevision")
    AIPipelineDefinition = apps.get_model("ai_orchestrator", "AIPipelineDefinition")
    AIPipelineStage = apps.get_model("ai_orchestrator", "AIPipelineStage")

    skill, _ = AISkill.objects.update_or_create(
        name="deal_field_synthesis",
        defaults={
            "description": "Fills deal ledger fields after every source document is indexed.",
            "system_template": DEAL_FIELD_SYNTHESIS_SYSTEM_TEMPLATE,
            "prompt_template": DEAL_FIELD_SYNTHESIS_PROMPT_TEMPLATE,
            "output_schema": DEAL_FIELD_SYNTHESIS_JSON_SCHEMA,
            "status": "approved",
        },
    )
    revision = AISkillRevision.objects.filter(skill=skill, status="published").order_by("-revision").first()
    if not revision:
        revision = AISkillRevision.objects.create(
            skill=skill,
            revision=1,
            status="published",
            system_template=skill.system_template,
            prompt_template=skill.prompt_template,
            input_schema=skill.input_schema or {},
            output_schema=skill.output_schema or {},
            skill_format=skill.skill_format,
            published_at=timezone.now(),
        )
    pipeline, _ = AIPipelineDefinition.objects.get_or_create(
        key="deal_ingestion",
        defaults={"name": "Deal ingestion", "description": "VDR and folder document analysis"},
    )
    AIPipelineStage.objects.update_or_create(
        pipeline=pipeline,
        key="field_synthesis",
        defaults={
            "name": "Deal field synthesis",
            "description": "Populate deal fields after indexed evidence is complete.",
            "position": 5,
            "kind": "skill",
            "skill": skill,
            "prompt_definition": None,
            "required_variables": [],
            "depends_on": ["evidence"],
        },
    )


def remove_deal_field_synthesis(apps, schema_editor):
    AIPipelineStage = apps.get_model("ai_orchestrator", "AIPipelineStage")
    AISkill = apps.get_model("ai_orchestrator", "AISkill")
    AIPipelineStage.objects.filter(
        pipeline__key="deal_ingestion", key="field_synthesis",
    ).delete()
    AISkill.objects.filter(name="deal_field_synthesis").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ai_orchestrator", "0038_aiauditlog_token_breakdown"),
    ]

    operations = [
        migrations.RunPython(add_deal_field_synthesis, remove_deal_field_synthesis),
    ]
