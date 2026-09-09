from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('microsoft', '0014_email_deal_initialization_prompt')]

    operations = [
        migrations.AddField(
            model_name='emailprivateblob',
            name='payload',
            field=models.BinaryField(editable=False, null=True),
        ),
    ]
