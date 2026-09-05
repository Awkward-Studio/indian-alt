from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_orchestrator", "0036_aipipelinestage_depends_on")]

    operations = [
        migrations.AlterField(
            model_name="aiskill",
            name="skill_format",
            field=models.CharField(
                choices=[
                    ("native_prompt_v1", "Native prompt template v1"),
                    ("claude_prompt_v1", "Claude prompt-only subset v1"),
                    ("agent_skill_v1", "Installable agent skill package v1"),
                ],
                default="native_prompt_v1",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="aiskillrevision",
            name="skill_format",
            field=models.CharField(
                choices=[
                    ("native_prompt_v1", "Native prompt template v1"),
                    ("claude_prompt_v1", "Claude prompt-only subset v1"),
                    ("agent_skill_v1", "Installable agent skill package v1"),
                ],
                default="native_prompt_v1",
                max_length=30,
            ),
        ),
        migrations.AddField(model_name="aiskillrevision", name="package_manifest", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="aiskillrevision", name="package_files", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="aiskillrevision", name="package_digest", field=models.CharField(blank=True, db_index=True, default="", max_length=64)),
        migrations.AddField(model_name="aiskillrevision", name="validation_report", field=models.JSONField(blank=True, default=dict)),
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
    ]
