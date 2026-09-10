from django.contrib.postgres.operations import TrigramExtension
from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('banks', '0002_bank_description_bank_website_domain'),
    ]

    operations = [
        TrigramExtension(),
        migrations.AddIndex(
            model_name='bank',
            index=GinIndex(fields=['name'], name='bank_name_trgm', opclasses=['gin_trgm_ops']),
        ),
    ]
