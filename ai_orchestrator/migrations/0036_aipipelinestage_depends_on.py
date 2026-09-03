from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai_orchestrator", "0035_documentchunk_search_text_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="aipipelinestage",
            name="depends_on",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Stage keys that must complete before this stage can run.",
            ),
        ),
        migrations.AlterField(
            model_name="aipipelinestage",
            name="kind",
            field=models.CharField(
                choices=[
                    ("prompt", "Prompt"),
                    ("skill", "Skill"),
                    ("operation", "Runtime operation"),
                ],
                max_length=20,
            ),
        ),
    ]
