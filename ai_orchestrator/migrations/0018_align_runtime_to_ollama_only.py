from django.db import migrations, models


def force_personality_provider_to_ollama(apps, schema_editor):
    AIPersonality = apps.get_model("ai_orchestrator", "AIPersonality")
    AIPersonality.objects.exclude(model_provider="ollama").update(model_provider="ollama")


class Migration(migrations.Migration):

    dependencies = [
        ("ai_orchestrator", "0017_auto_20260414_0713"),
    ]

    operations = [
        migrations.RunPython(force_personality_provider_to_ollama, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="aipersonality",
            name="model_provider",
            field=models.CharField(
                choices=[("ollama", "Ollama (Local/Azure)")],
                default="ollama",
                max_length=50,
            ),
        ),
        migrations.AlterField(
            model_name="aipersonality",
            name="text_model_name",
            field=models.CharField(
                default="qwen3.5:latest",
                help_text="Model for text-only tasks",
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name="aipersonality",
            name="vision_model_name",
            field=models.CharField(
                default="glm-ocr:latest",
                help_text="Model for tasks with images/charts",
                max_length=100,
            ),
        ),
    ]
