from django.db import migrations
from django.utils import timezone


def publish_report_formatting_prompts(apps, schema_editor):
    PromptDefinition = apps.get_model("ai_orchestrator", "AIPromptDefinition")
    PromptRevision = apps.get_model("ai_orchestrator", "AIPromptRevision")

    from ai_orchestrator.prompt_contracts import IC_SECTION_TITLES
    from ai_orchestrator.services.bulk_prompt_contracts import (
        IC_REPORT_SECTION_STAGE_KEYS,
        IC_REPORT_SECTION_SYSTEM_PROMPT,
        build_ic_report_section_user_template,
    )

    for title in IC_SECTION_TITLES:
        definition_key = f"ic_report_section_{IC_REPORT_SECTION_STAGE_KEYS[title]}"
        definition = PromptDefinition.objects.filter(key=definition_key).first()
        if not definition:
            continue
        user_template = build_ic_report_section_user_template(title)
        current = definition.revisions.filter(status="published").order_by("-revision").first()
        if (
            current
            and current.system_template == IC_REPORT_SECTION_SYSTEM_PROMPT
            and current.user_template == user_template
        ):
            continue

        definition.revisions.filter(status="published").update(status="archived")
        latest = definition.revisions.order_by("-revision").first()
        PromptRevision.objects.create(
            definition=definition,
            revision=(latest.revision + 1) if latest else 1,
            status="published",
            system_template=IC_REPORT_SECTION_SYSTEM_PROMPT,
            user_template=user_template,
            input_schema=(current.input_schema if current else {}),
            output_schema=(current.output_schema if current else {}),
            published_at=timezone.now(),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("ai_orchestrator", "0039_deal_field_synthesis_skill"),
    ]

    operations = [
        migrations.RunPython(publish_report_formatting_prompts, migrations.RunPython.noop),
    ]
