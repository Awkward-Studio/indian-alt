from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('microsoft', '0008_alter_email_conversation_id_alter_email_graph_id_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='email',
            name='body_html_sanitized_at',
            field=models.DateTimeField(
                blank=True,
                help_text='When body_html was last processed by the email sanitizer',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='email',
            name='body_html_sanitizer_version',
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text='Version of the allowlist policy applied to body_html',
                null=True,
            ),
        ),
    ]
