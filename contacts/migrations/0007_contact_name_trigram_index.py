from django.contrib.postgres.indexes import GinIndex
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('banks', '0003_bank_name_trigram_index'),
        ('contacts', '0006_contact_interactions_and_cards'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='contact',
            index=GinIndex(fields=['name'], name='contact_name_trgm', opclasses=['gin_trgm_ops']),
        ),
    ]
