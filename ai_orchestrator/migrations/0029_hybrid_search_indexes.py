from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently
from django.contrib.postgres.search import SearchVectorField
from django.db import migrations, models
import pgvector.django.indexes


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
        migrations.RunSQL(
            sql="""
                UPDATE ai_orchestrator_documentchunk
                SET search_text = trim(concat_ws(E'\n',
                    NULLIF(content, ''),
                    NULLIF(source_type, ''),
                    NULLIF(source_id, ''),
                    COALESCE(metadata::text, '')
                ));

                UPDATE ai_orchestrator_documentchunk
                SET search_vector = to_tsvector('english', COALESCE(search_text, ''));
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
        AddIndexConcurrently(
            model_name="documentchunk",
            index=GinIndex(fields=["search_vector"], name="docchunk_search_vector_gin"),
        ),
        AddIndexConcurrently(
            model_name="documentchunk",
            index=pgvector.django.indexes.HnswIndex(
                fields=["embedding"],
                m=16,
                ef_construction=64,
                name="docchunk_embedding_hnsw",
                opclasses=["vector_cosine_ops"],
            ),
        ),
        AddIndexConcurrently(
            model_name="dealretrievalprofile",
            index=pgvector.django.indexes.HnswIndex(
                fields=["embedding"],
                m=16,
                ef_construction=64,
                name="dealprofile_embedding_hnsw",
                opclasses=["vector_cosine_ops"],
            ),
        ),
    ]
