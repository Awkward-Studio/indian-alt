from django.db import migrations
from django.utils import timezone


def publish_deal_title_evidence_contract(apps, schema_editor):
    AISkill = apps.get_model("ai_orchestrator", "AISkill")
    AISkillRevision = apps.get_model("ai_orchestrator", "AISkillRevision")

    from ai_orchestrator.prompt_contracts import (
        DEAL_FIELD_SYNTHESIS_JSON_SCHEMA,
        DEAL_FIELD_SYNTHESIS_PROMPT_TEMPLATE,
        DEAL_FIELD_SYNTHESIS_SYSTEM_TEMPLATE,
        EMAIL_TITLE_EVIDENCE_PROMPT_RULES,
    )

    skill = AISkill.objects.filter(name="deal_field_synthesis").first()
    if skill:
        skill.system_template = DEAL_FIELD_SYNTHESIS_SYSTEM_TEMPLATE
        skill.prompt_template = DEAL_FIELD_SYNTHESIS_PROMPT_TEMPLATE
        skill.output_schema = DEAL_FIELD_SYNTHESIS_JSON_SCHEMA
        skill.save(update_fields=["system_template", "prompt_template", "output_schema", "updated_at"])

        current = skill.revisions.filter(status="published").order_by("-revision").first()
        field_contract_is_current = bool(
            current
            and current.system_template == DEAL_FIELD_SYNTHESIS_SYSTEM_TEMPLATE
            and current.prompt_template == DEAL_FIELD_SYNTHESIS_PROMPT_TEMPLATE
            and current.output_schema == DEAL_FIELD_SYNTHESIS_JSON_SCHEMA
        )
        if not field_contract_is_current:
            skill.revisions.filter(status="published").update(status="archived")
            latest = skill.revisions.order_by("-revision").first()
            AISkillRevision.objects.create(
                skill=skill,
                revision=(latest.revision + 1) if latest else 1,
                status="published",
                system_template=DEAL_FIELD_SYNTHESIS_SYSTEM_TEMPLATE,
                prompt_template=DEAL_FIELD_SYNTHESIS_PROMPT_TEMPLATE,
                input_schema=(current.input_schema if current else {}),
                output_schema=DEAL_FIELD_SYNTHESIS_JSON_SCHEMA,
                skill_format=skill.skill_format,
                package_manifest={},
                package_files={},
                package_digest="",
                validation_report={},
                compatibility_status="not_applicable",
                published_at=timezone.now(),
            )

    email_skill = AISkill.objects.filter(name="email_thread_synthesis").first()
    if not email_skill:
        return

    marker = "[DEAL TITLE EVIDENCE]"
    email_prompt = email_skill.prompt_template or "{{ content }}"
    if marker not in email_prompt:
        email_prompt = f"{email_prompt.rstrip()}\n\n{EMAIL_TITLE_EVIDENCE_PROMPT_RULES}"
        email_skill.prompt_template = email_prompt
        email_skill.save(update_fields=["prompt_template", "updated_at"])

    current_email = email_skill.revisions.filter(status="published").order_by("-revision").first()
    current_prompt = current_email.prompt_template if current_email else email_prompt
    if marker in current_prompt:
        return

    published_prompt = f"{current_prompt.rstrip()}\n\n{EMAIL_TITLE_EVIDENCE_PROMPT_RULES}"
    email_skill.revisions.filter(status="published").update(status="archived")
    latest_email = email_skill.revisions.order_by("-revision").first()
    AISkillRevision.objects.create(
        skill=email_skill,
        revision=(latest_email.revision + 1) if latest_email else 1,
        status="published",
        system_template=(current_email.system_template if current_email else email_skill.system_template),
        prompt_template=published_prompt,
        input_schema=(current_email.input_schema if current_email else {}),
        output_schema=(current_email.output_schema if current_email else email_skill.output_schema),
        skill_format=email_skill.skill_format,
        package_manifest={},
        package_files={},
        package_digest="",
        validation_report={},
        compatibility_status="not_applicable",
        published_at=timezone.now(),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ai_orchestrator", "0041_enforce_report_table_axes"),
    ]

    operations = [
        migrations.RunPython(
            publish_deal_title_evidence_contract,
            migrations.RunPython.noop,
        ),
    ]
