from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ai_orchestrator", "0032_aipipelinedefinition_aipipelinestage_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="aipersonality",
            name="vision_model_name",
        ),
    ]
