from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("ai_orchestrator", "0028_alter_documentchunk_source_type"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE ai_orchestrator_documentchunk 
                ADD COLUMN IF NOT EXISTS search_text text DEFAULT '';
                
                ALTER TABLE ai_orchestrator_documentchunk 
                ADD COLUMN IF NOT EXISTS search_vector tsvector;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
