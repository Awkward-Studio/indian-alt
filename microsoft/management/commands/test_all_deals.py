import os
import json
import time
from django.core.management.base import BaseCommand
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.document_processor import DocumentProcessorService

class Command(BaseCommand):
    help = 'Batch test AI analysis on all available .msg deal files'

    def handle(self, *args, **options):
        parent_dir = ".."
        msg_files = [f for f in os.listdir(parent_dir) if f.endswith('.msg')]
        
        if not msg_files:
            self.stdout.write(self.style.ERROR("No .msg files found in parent directory."))
            return

        self.stdout.write(f"FOUND {len(msg_files)} DEALS. STARTING BATCH ANALYSIS...\n")

        doc_processor = DocumentProcessorService()
        ai_service = AIProcessorService()

        for filename in msg_files:
            file_path = os.path.join(parent_dir, filename)
            self.stdout.write(f"\nPROCESSING: {filename}")
            
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                
                self.stdout.write("  - Ingesting & Extracting (Recursive)...")
                extracted_text = doc_processor.extract_text(content, filename)
                self.stdout.write(f"  - Extracted {len(extracted_text)} chars.")

                self.stdout.write("  - Analyzing with Mistral Nemo (T4 GPU)...")
                start_time = time.time()
                result = ai_service.process_content(
                    content=extracted_text,
                    skill_name="deal_extraction",
                    metadata={'subject': filename},
                    source_type="batch_test"
                )
                duration = time.time() - start_time

                if "error" in result:
                    self.stdout.write(self.style.ERROR(f"  FAILED in {duration:.2f}s: {result.get('error')}"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"  SUCCESS in {duration:.2f}s!"))
                    self.stdout.write("\n  --- CHAIRMAN BRIEFING ---")
                    self.stdout.write(f"  {result.get('chairman_briefing', 'N/A')}")
                    
                    data = result.get('deal_model_data', {})
                    self.stdout.write("\n  --- KEY DATA ---")
                    self.stdout.write(f"  Entity:   {data.get('title', 'N/A')}")
                    self.stdout.write(f"  Priority: {data.get('priority', 'N/A')}")
                    self.stdout.write(f"  Ask:      {data.get('funding_ask', 'N/A')}")
                    
                    red_flags = result.get('metadata', {}).get('red_flags', [])
                    if red_flags:
                        self.stdout.write("\n  --- RED FLAGS ---")
                        for flag in red_flags:
                            self.stdout.write(f"  [!] {flag}")
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Critical error processing {filename}: {str(e)}"))
            
            self.stdout.write("\n" + "="*60 + "\n")

        self.stdout.write("\nBATCH ANALYSIS COMPLETE.")
