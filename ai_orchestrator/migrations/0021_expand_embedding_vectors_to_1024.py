from django.db import migrations
import pgvector.django.vector


class Migration(migrations.Migration):

    dependencies = [
        ("ai_orchestrator", "0020_5_clear_embeddings_before_dimension_change"),
    ]

    operations = [
        migrations.AlterField(
            model_name="documentchunk",
            name="embedding",
            field=pgvector.django.vector.VectorField(blank=True, dimensions=1024, null=True),
        ),
        migrations.AlterField(
            model_name="dealretrievalprofile",
            name="embedding",
            field=pgvector.django.vector.VectorField(blank=True, dimensions=1024, null=True),
        ),
    ]
