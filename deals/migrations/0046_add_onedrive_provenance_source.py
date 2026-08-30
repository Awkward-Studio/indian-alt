from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("deals", "0045_remove_legacy_deal_phase_choices")]

    operations = [
        migrations.AlterField(
            model_name="dealfieldprovenance",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("AI", "AI"),
                    ("SHEET", "Spreadsheet"),
                    ("HUMAN", "Human"),
                    ("ONEDRIVE", "OneDrive"),
                ],
                max_length=10,
            ),
        ),
    ]
