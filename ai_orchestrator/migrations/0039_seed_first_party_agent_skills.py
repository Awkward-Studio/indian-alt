import hashlib
import json
from django.db import migrations


def seed_packages(apps, schema_editor):
    from ai_orchestrator.agents.first_party_skills import FIRST_PARTY_SKILL_PACKAGES
    from ai_orchestrator.agents.skill_packages import AgentSkillManifest

    AISkill = apps.get_model("ai_orchestrator", "AISkill")
    AISkillRevision = apps.get_model("ai_orchestrator", "AISkillRevision")
    for slug, package in FIRST_PARTY_SKILL_PACKAGES.items():
        manifest = AgentSkillManifest.model_validate(package["manifest"]).model_dump(mode="json")
        files = package["files"]
        canonical = json.dumps(
            {"manifest": manifest, "files": {key: files[key] for key in sorted(files)}},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        skill, _ = AISkill.objects.get_or_create(
            name=slug,
            defaults={
                "description": manifest["description"],
                "prompt_template": files["SKILL.md"],
                "input_schema": manifest["input_schema"],
                "output_schema": manifest["output_schema"],
                "status": "approved",
                "skill_format": "agent_skill_v1",
            },
        )
        if skill.revisions.filter(package_digest=digest).exists():
            continue
        skill.revisions.filter(status="published").update(status="archived")
        next_revision = (skill.revisions.order_by("-revision").values_list("revision", flat=True).first() or 0) + 1
        AISkillRevision.objects.create(
            skill=skill,
            revision=next_revision,
            status="published",
            prompt_template=files["SKILL.md"],
            input_schema=manifest["input_schema"],
            output_schema=manifest["output_schema"],
            skill_format="agent_skill_v1",
            package_manifest=manifest,
            package_files=files,
            package_digest=digest,
            validation_report={"schema_version": "agent_skill_v1", "valid": True, "errors": []},
            compatibility_status="compatible",
        )
        skill.version = next_revision
        skill.status = "approved"
        skill.skill_format = "agent_skill_v1"
        skill.save(update_fields=["version", "status", "skill_format"])


class Migration(migrations.Migration):
    dependencies = [("ai_orchestrator", "0038_aiskilllifecycleevent")]
    operations = [migrations.RunPython(seed_packages, migrations.RunPython.noop)]
