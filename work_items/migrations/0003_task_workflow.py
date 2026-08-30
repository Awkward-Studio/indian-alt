import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('work_items', '0002_taskactivity')]

    operations = [
        migrations.AddField(
            model_name='task', name='position',
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
        migrations.AddField(
            model_name='task', name='review_requested_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='task', name='status',
            field=models.CharField(choices=[('todo', 'To Do'), ('in_progress', 'In Progress'), ('blocked', 'Blocked'), ('in_review', 'In Review'), ('done', 'Done')], default='todo', max_length=20),
        ),
        migrations.AlterModelOptions(
            name='task', options={'ordering': ['position', 'due_date', '-created_at']},
        ),
        migrations.AlterField(
            model_name='taskactivity', name='action',
            field=models.CharField(choices=[('created', 'Created'), ('updated', 'Updated'), ('completed', 'Completed'), ('reopened', 'Reopened'), ('assigned', 'Assigned'), ('prioritized', 'Prioritized'), ('due_date_changed', 'Due date changed'), ('deleted', 'Deleted'), ('suggestion_accepted', 'Suggestion accepted'), ('commented', 'Commented'), ('review_requested', 'Review requested'), ('review_approved', 'Review approved'), ('reordered', 'Reordered')], db_index=True, max_length=30),
        ),
        migrations.CreateModel(
            name='TaskComment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('body', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('author', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='task_comments', to='accounts.profile')),
                ('task', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='comments', to='work_items.task')),
            ],
            options={
                'db_table': 'task_comment',
                'ordering': ['created_at', 'id'],
                'indexes': [models.Index(fields=['task', 'created_at'], name='task_comment_task_time_idx')],
            },
        ),
    ]
