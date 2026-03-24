import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from ai_orchestrator.models import AIAuditLog

logs = AIAuditLog.objects.all().order_by('-created_at')[:5]
for log in logs:
    print(f"Log ID: {log.id}")
    print(f"Source Type: {log.source_type}")
    print(f"Skill: {log.skill.name if log.skill else 'None'}")
    print(f"Status: {log.status}")
    print(f"User Prompt Start: {log.user_prompt[:200]}...")
    print(f"Raw Response Start: {log.raw_response[:200]}...")
    print("-" * 40)
