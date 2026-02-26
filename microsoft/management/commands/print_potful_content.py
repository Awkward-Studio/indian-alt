import os
from django.core.management.base import BaseCommand
from ai_orchestrator.services.document_processor import DocumentProcessorService

class Command(BaseCommand):
    help = 'Print the entire extracted email thread and attachment content'

    def handle(self, *args, **options):
        file_path = "../Investment Opportunity in Potful _ INR 50 crores.msg"
        if not os.path.exists(file_path):
            file_path = "./Investment Opportunity in Potful _ INR 50 crores.msg"

        if not os.path.exists(file_path):
            self.stdout.write(self.style.ERROR(f"File not found: {file_path}"))
            return

        self.stdout.write(f"Extracting content from: {file_path}...\n")
        
        doc_processor = DocumentProcessorService()
        with open(file_path, "rb") as f:
            content = f.read()
        
        # This will extract the body, sender info, and recursive PDF content
        full_text = doc_processor.extract_text(content, "Potful_Deal.msg")
        
        self.stdout.write(full_text)
        self.stdout.write("\n--- END OF EXTRACTED TEXT ---")
