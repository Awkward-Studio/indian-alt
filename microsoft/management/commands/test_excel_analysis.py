import os
import json
import io
from datetime import datetime
from django.utils import timezone
from django.core.management.base import BaseCommand
from openpyxl import Workbook
from microsoft.models import Email, EmailAccount
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.document_processor import DocumentProcessorService

class Command(BaseCommand):
    help = 'Tests the AI analysis with a generated Excel attachment'

    def handle(self, *args, **options):
        # 1. Setup mock account
        account, _ = EmailAccount.objects.get_or_create(email="test@india-alt.com")

        # 2. Generate a real Excel file in memory
        wb = Workbook()
        ws = wb.active
        ws.title = "Financials"
        ws.append(["Category", "FY23", "FY24 (Proj)"])
        ws.append(["Revenue", 50000000, 85000000])
        ws.append(["EBITDA", 5000000, 12000000])
        ws.append(["PAT", 2000000, 7000000])
        ws.append(["Net Margin", "4%", "8.2%"])
        
        excel_file = io.BytesIO()
        wb.save(excel_file)
        attachment_bytes = excel_file.getvalue()
        attachment_name = "Financial_Summary.xlsx"

        self.stdout.write(self.style.SUCCESS(f"Generated Excel attachment: {attachment_name}"))

        # 3. Process Attachment
        self.stdout.write(f"Extracting text from {attachment_name} using Docling...")
        doc_processor = DocumentProcessorService()
        try:
            extracted_text = doc_processor.extract_text(attachment_bytes, attachment_name)
            self.stdout.write(self.style.SUCCESS(f"Extracted {len(extracted_text)} characters."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Processing error: {str(e)}"))
            extracted_text = "Error extracting text."

        # 4. Create Mock Content
        body = "Attached are the financials for GreenDrive. Revenue growth is looking solid at 70% YoY."
        full_context = f"{body}\n\n--- Attachment: {attachment_name} ---\n{extracted_text}"

        # 5. AI Analysis
        self.stdout.write("Sending to gemma3:4b...")
        ai_service = AIProcessorService()
        result = ai_service.process_content(
            content=full_context,
            skill_name="deal_extraction",
            metadata={'from_email': 'analyst@greendrive.com', 'subject': 'GreenDrive Financials', 'date_received': timezone.now().isoformat()},
            source_type="email_test"
        )

        self.stdout.write(self.style.SUCCESS("\n--- EXCEL-AWARE AI RESULT ---"))
        self.stdout.write(json.dumps(result, indent=2))
