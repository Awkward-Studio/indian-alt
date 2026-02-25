import os
import json
from datetime import datetime
from django.utils import timezone
from django.core.management.base import BaseCommand
from microsoft.models import Email, EmailAccount
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.document_processor import DocumentProcessorService

class Command(BaseCommand):
    help = 'Tests the AI analysis with an email and a real attachment'

    def handle(self, *args, **options):
        # 1. Setup mock account
        account, _ = EmailAccount.objects.get_or_create(email="test@india-alt.com")

        # 2. Path to the sample pptx
        sample_pptx_path = "/home/omi_2650/Omi_Home_NAS/Code/Work/India_Alternatives/indian-alt/venv/lib/python3.12/site-packages/pptx/templates/default.pptx"

        attachment_name = "CompanyProfile.pptx"
        attachment_bytes = b""
        
        if os.path.exists(sample_pptx_path):
            with open(sample_pptx_path, "rb") as f:
                attachment_bytes = f.read()
            self.stdout.write(self.style.SUCCESS(f"Loaded real attachment: {sample_pptx_path}"))
        else:
            self.stdout.write(self.style.WARNING("Sample PPTX not found, using mock string."))
            attachment_bytes = b"Mock content: GreenDrive Motors is an EV startup."

        # 3. Process Attachment
        self.stdout.write(f"Extracting text from {attachment_name} using Docling...")
        doc_processor = DocumentProcessorService()
        try:
            extracted_text = doc_processor.extract_text(attachment_bytes, attachment_name)
            self.stdout.write(self.style.SUCCESS(f"Extracted {len(extracted_text)} characters."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Docling error: {str(e)}"))
            extracted_text = "Error extracting text."

        # 4. Create Mock Content
        body = "Hi Team, PFA the profile for GreenDrive Motors. They are doing great work in EV components."
        # Truncate attachment text to keep within gemma context window
        context_window_limit = 5000
        truncated_text = extracted_text[:context_window_limit]
        
        full_context = f"{body}\n\n--- Attachment: {attachment_name} ---\n{truncated_text}"

        # 5. AI Analysis
        self.stdout.write("Sending to gemma3:4b...")
        ai_service = AIProcessorService()
        result = ai_service.process_content(
            content=full_context,
            skill_name="deal_extraction",
            metadata={'from_email': 'broker@investment.com', 'subject': 'GreenDrive Proposal', 'date_received': timezone.now().isoformat()},
            source_type="email_test"
        )

        self.stdout.write(self.style.SUCCESS("\n--- ATTACHMENT-AWARE AI RESULT ---"))
        self.stdout.write(json.dumps(result, indent=2))
