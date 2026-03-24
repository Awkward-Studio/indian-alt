import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from ai_orchestrator.models import AIAuditLog

log = AIAuditLog.objects.filter(source_type='universal_chat').order_by('-created_at').first()
if log:
    print(f"Log ID: {log.id}")
    print(f"Skill: {log.skill.name if log.skill else 'None'}")
    print(f"System Prompt Start: {log.system_prompt[:500]}...")
    print("-" * 20)
    print(f"User Prompt Start: {log.user_prompt[:500]}...")
else:
    print("No universal_chat logs found.")
