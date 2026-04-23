import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from ai_orchestrator.models import DocumentChunk

count = DocumentChunk.objects.filter(source_type='extracted_source').count()
print(f"TOTAL_CHUNKS_IN_DB: {count}")
