from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('deals', '0035_public_company_competitor_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='deal',
            name='received_at',
            field=models.DateField(
                blank=True,
                db_index=True,
                help_text='Business date on which the fund received the deal.',
                null=True,
            ),
        ),
        migrations.AlterModelOptions(
            name='deal',
            options={
                'ordering': ['-received_at', '-created_at', 'title'],
                'verbose_name': 'Deal',
                'verbose_name_plural': 'Deals',
            },
        ),
    ]
