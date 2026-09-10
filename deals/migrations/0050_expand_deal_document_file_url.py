from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("deals", "0049_sectorresearchsourcerule"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dealdocument",
            name="file_url",
            field=models.URLField(blank=True, max_length=2000, null=True),
        ),
    ]
