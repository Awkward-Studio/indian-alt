from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0002_alter_profile_email_alter_profile_user'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='can_manage_deal_assignments',
            field=models.BooleanField(
                default=False,
                help_text='Can add or remove IA team members from deals.',
            ),
        ),
    ]
