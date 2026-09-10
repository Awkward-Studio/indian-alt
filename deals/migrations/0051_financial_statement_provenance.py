from django.db import migrations, models


def backfill_statement_sources(apps, schema_editor):
    Statement = apps.get_model("deals", "VentureIntelligenceFinancialStatement")
    Statement.objects.filter(
        company_profile__data_source__in=["screener", "public_screener"],
    ).update(data_source="screener")


class Migration(migrations.Migration):
    dependencies = [("deals", "0050_expand_deal_document_file_url")]

    operations = [
        migrations.AddField(
            model_name="ventureintelligencefinancialstatement",
            name="data_source",
            field=models.CharField(db_index=True, default="venture_intelligence", max_length=40),
        ),
        migrations.AddField(
            model_name="ventureintelligencefinancialstatement",
            name="provenance",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(backfill_statement_sources, migrations.RunPython.noop),
    ]
