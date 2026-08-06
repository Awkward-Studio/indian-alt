from django.contrib.postgres.search import SearchVectorField
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("ai_orchestrator", "0028_alter_documentchunk_source_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentchunk",
            name="search_text",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="documentchunk",
            name="search_vector",
            field=SearchVectorField(blank=True, null=True),
        ),
    ]
