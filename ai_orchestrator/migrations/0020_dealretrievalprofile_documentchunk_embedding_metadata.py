from django.db import migrations, models
import django.db.models.deletion
import pgvector.django.vector
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("ai_orchestrator", "0019_documentchunk_analysis_document_support"),
        ("deals", "0024_folderanalysisdocument"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentchunk",
            name="embedding_dimensions",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="documentchunk",
            name="embedding_model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="documentchunk",
            name="indexed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="DealRetrievalProfile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("profile_text", models.TextField()),
                ("embedding", pgvector.django.vector.VectorField(blank=True, dimensions=768, null=True)),
                ("embedding_model", models.CharField(blank=True, default="", max_length=200)),
                ("embedding_dimensions", models.IntegerField(blank=True, null=True)),
                ("source_version", models.CharField(blank=True, default="v1", max_length=100)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("indexed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("deal", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="retrieval_profile", to="deals.deal")),
            ],
            options={
                "ordering": ["-updated_at"],
            },
        ),
        migrations.AddIndex(
            model_name="dealretrievalprofile",
            index=models.Index(fields=["deal"], name="ai_orchestr_deal_id_77216e_idx"),
        ),
        migrations.AddIndex(
            model_name="dealretrievalprofile",
            index=models.Index(fields=["embedding_model"], name="ai_orchestr_embeddi_c5f7cc_idx"),
        ),
    ]
