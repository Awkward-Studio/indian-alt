from django.db import migrations, models


def backfill_analysis_kind(apps, schema_editor):
    DealAnalysis = apps.get_model("deals", "DealAnalysis")
    DealAnalysis.objects.filter(version=1).update(analysis_kind="initial")
    DealAnalysis.objects.exclude(version=1).update(analysis_kind="supplemental")


class Migration(migrations.Migration):

    dependencies = [
        ("deals", "0016_dealdocument_ingestion_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="dealanalysis",
            name="analysis_kind",
            field=models.CharField(
                choices=[("initial", "Initial"), ("supplemental", "Supplemental")],
                default="initial",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_analysis_kind, migrations.RunPython.noop),
    ]
