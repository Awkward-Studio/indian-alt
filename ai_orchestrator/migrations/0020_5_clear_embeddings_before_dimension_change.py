from django.db import migrations


class Migration(migrations.Migration):
    """
    Clear all embedding vectors before expanding dimensions from 768 to 1024.

    PostgreSQL cannot cast vector(768) to vector(1024) directly, so we null out
    all existing embeddings here. They will be regenerated after the dimension
    expansion in migration 0021.
    """

    dependencies = [
        ("ai_orchestrator", "0020_dealretrievalprofile_documentchunk_embedding_metadata"),
    ]

    operations = [
        migrations.RunSQL(
            sql="UPDATE ai_orchestrator_documentchunk SET embedding = NULL;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="UPDATE ai_orchestrator_dealretrievalprofile SET embedding = NULL;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
