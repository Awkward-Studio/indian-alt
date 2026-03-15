import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from ai_orchestrator.models import AISkill

prompt_template = """[EXISTING ANALYSIS]
Summary: {{ existing_summary }}

[NEW DOCUMENTS TO ANALYZE]
{{ content }}

[TASK]
You are a senior Private Equity Analyst. Generate a "Version {{ version_num }}" supplementary analysis report in Markdown format.
Do NOT rewrite the entire V1 summary. Focus strictly on:
1. New insights surfaced exclusively from the new documents.
2. Resolving any ambiguities identified in the previous reports.
3. Extracting new specific financial or operational metrics.

Format your output inside a JSON block with the key 'analyst_report'. Example:
```json
{
  "analyst_report": "### V{{ version_num }} Supplementary Analysis\\n\\nBased on the newly provided term sheet, we can confirm..."
}
```
"""

skill, created = AISkill.objects.get_or_create(
    name='vdr_incremental_analysis',
    defaults={
        'description': 'Analyzes newly added VDR documents to generate an incremental supplementary report, rather than overwriting the base analysis.',
        'prompt_template': prompt_template
    }
)

if not created:
    skill.prompt_template = prompt_template
    skill.save()
    print("Updated existing skill.")
else:
    print("Created new skill.")
