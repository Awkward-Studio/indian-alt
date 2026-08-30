import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('contacts', '0005_contact_contact_type'), ('deals', '0044_dealfieldprovenance'), ('meetings', '0003_meetingsignalflag'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='ContactCardExtraction', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)), ('file_name', models.TextField()), ('file_size', models.PositiveIntegerField(default=0)), ('raw_text', models.TextField(blank=True, default='')), ('extracted_data', models.JSONField(blank=True, default=dict)), ('status', models.CharField(choices=[('PENDING', 'Pending review'), ('COMPLETED', 'Completed')], default='PENDING', max_length=20)), ('created_at', models.DateTimeField(auto_now_add=True)), ('updated_at', models.DateTimeField(auto_now=True)), ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='contact_card_extractions', to=settings.AUTH_USER_MODEL)), ('matched_contact', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='card_extractions', to='contacts.contact')),
        ], options={'ordering': ['-created_at']}),
        migrations.CreateModel(name='ContactInteraction', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)), ('kind', models.CharField(choices=[('MEETING', 'Meeting'), ('CALL', 'Call'), ('EMAIL', 'Email'), ('NOTE', 'Note')], default='NOTE', max_length=20)), ('occurred_at', models.DateTimeField()), ('notes', models.TextField(blank=True, default='')), ('created_at', models.DateTimeField(auto_now_add=True)), ('contact', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='interactions', to='contacts.contact')), ('deal', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='contact_interactions', to='deals.deal')), ('meeting_note', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='contact_interactions', to='meetings.meetingnote')),
        ], options={'ordering': ['-occurred_at', '-created_at']}),
        migrations.AddConstraint(model_name='contactinteraction', constraint=models.UniqueConstraint(fields=('contact', 'meeting_note'), name='unique_contact_meeting_interaction')),
    ]
