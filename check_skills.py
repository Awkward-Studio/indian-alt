import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from ai_orchestrator.models import AISkill

skills = AISkill.objects.all()
for s in skills:
    print(f"Skill Name: {s.name}")
    print(f"Description: {s.description}")
    print(f"Prompt Template Start: {s.prompt_template[:200]}...")
    print("-" * 40)
