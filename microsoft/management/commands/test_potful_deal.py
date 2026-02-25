import os
import json
import time
from django.core.management.base import BaseCommand
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.document_processor import DocumentProcessorService

class Command(BaseCommand):
    help = 'Split Test: Body first, then full context'

    def handle(self, *args, **options):
        file_path = "../Investment Opportunity in Potful _ INR 50 crores.msg"
        if not os.path.exists(file_path):
            file_path = "./Investment Opportunity in Potful _ INR 50 crores.msg"

        # 1. Ingest
        doc_processor = DocumentProcessorService()
        with open(file_path, "rb") as f:
            content = f.read()
        
        # Extract everything
        full_text = doc_processor.extract_text(content, "Potful_Deal.msg")
        
        # Split: The body is usually before "--- INTERNAL ATTACHMENT"
        if "--- INTERNAL ATTACHMENT" in full_text:
            body_only = full_text.split("--- INTERNAL ATTACHMENT")[0]
        else:
            body_only = full_text

        ai_service = AIProcessorService()

        # STEP 1: Test with Body Only
        self.stdout.write(self.style.MIGRATE_HEADING("\n--- STAGE 1: EMAIL BODY ONLY ---"))
        self.stdout.write(f"Context Size: {len(body_only)} chars.")
        
        result_body = ai_service.process_content(
            content=body_only,
            skill_name="deal_extraction",
            metadata={'subject': 'Potful Email Body Test'},
            source_type="msg_body_test"
        )
        self.stdout.write(json.dumps(result_body, indent=2))

        # STEP 2: Test with Full Context (Body + PDF)
        self.stdout.write(self.style.MIGRATE_HEADING("\n--- STAGE 2: FULL CONTEXT (Body + PDF) ---"))
        self.stdout.write(f"Context Size: {len(full_text)} chars.")
        
        result_full = ai_service.process_content(
            content=full_text,
            skill_name="deal_extraction",
            metadata={'subject': 'Potful Full Test'},
            source_type="msg_full_test"
        )
        self.stdout.write(json.dumps(result_full, indent=2))
