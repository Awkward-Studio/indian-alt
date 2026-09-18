from django.db import migrations, models
from django.utils import timezone


def ensure_skill_revision_package_columns(apps, schema_editor):
    """Reconcile databases that retained packaged-skill columns from an older deploy."""
    skill_revision = apps.get_model("ai_orchestrator", "AISkillRevision")
    table_name = skill_revision._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        existing_columns = {
            column.name
            for column in schema_editor.connection.introspection.get_table_description(
                cursor, table_name,
            )
        }

    fields = {
        "package_manifest": models.JSONField(default=dict, blank=True),
        "package_files": models.JSONField(default=dict, blank=True),
        "package_digest": models.CharField(
            max_length=64, blank=True, default="", db_index=True,
        ),
        "validation_report": models.JSONField(default=dict, blank=True),
        "compatibility_status": models.CharField(
            max_length=20,
            choices=[
                ("not_applicable", "Not applicable"),
                ("unverified", "Unverified"),
                ("compatible", "Compatible"),
                ("incompatible", "Incompatible"),
            ],
            default="not_applicable",
        ),
    }
    for name, field in fields.items():
        if name in existing_columns:
            continue
        field.set_attributes_from_name(name)
        field.model = skill_revision
        schema_editor.add_field(skill_revision, field)


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
            package_manifest={},
            package_files={},
            package_digest="",
            validation_report={},
            compatibility_status="not_applicable",
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
    # Column reconciliation may create a deferred PostgreSQL index. Commit it
    # before the seed writes rows that produce pending constraint triggers.
    atomic = False

    dependencies = [
        ("ai_orchestrator", "0038_aiauditlog_token_breakdown"),
    ]

    operations = [
        migrations.RunPython(
            ensure_skill_revision_package_columns,
            migrations.RunPython.noop,
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name="aiskillrevision",
                    name="package_manifest",
                    field=models.JSONField(blank=True, default=dict),
                ),
                migrations.AddField(
                    model_name="aiskillrevision",
                    name="package_files",
                    field=models.JSONField(blank=True, default=dict),
                ),
                migrations.AddField(
                    model_name="aiskillrevision",
                    name="package_digest",
                    field=models.CharField(
                        blank=True, db_index=True, default="", max_length=64,
                    ),
                ),
                migrations.AddField(
                    model_name="aiskillrevision",
                    name="validation_report",
                    field=models.JSONField(blank=True, default=dict),
                ),
                migrations.AddField(
                    model_name="aiskillrevision",
                    name="compatibility_status",
                    field=models.CharField(
                        choices=[
                            ("not_applicable", "Not applicable"),
                            ("unverified", "Unverified"),
                            ("compatible", "Compatible"),
                            ("incompatible", "Incompatible"),
                        ],
                        default="not_applicable",
                        max_length=20,
                    ),
                ),
            ],
        ),
        migrations.RunPython(add_deal_field_synthesis, remove_deal_field_synthesis),
    ]
