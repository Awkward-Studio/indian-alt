import os
import django
import json
from pathlib import Path
import sys

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from deals.models import Deal
from ai_orchestrator.models import DocumentChunk, AIAuditLog, AIPersonality, AISkill
from ai_orchestrator.services.ai_processor import AIProcessorService
from ai_orchestrator.services.runtime import AIRuntimeService

def synthesize_batch(limit=5):
    # Get deals that have chunks but no analysis yet
    deals = Deal.objects.filter(current_analysis={}).order_by('created_at')[:limit]
    
    ai_service = AIProcessorService()
    # FETCH YOUR ACTUAL PERSONALITY AND SKILL
    personality = AIRuntimeService.get_default_personality()
    skill = AISkill.objects.filter(name='deal_synthesis').first()

    print(f"\n>>> PHASE B: SYNTHESIS (Synthesis Model)")
    print(f">>> Using Personality: {personality.name if personality else 'Default'}")
    print(f">>> Using Skill: {skill.name if skill else 'Default'}")
    
    for deal in deals:
        print(f"\nSynthesizing Deal: {deal.title}")
        
        chunks = DocumentChunk.objects.filter(deal=deal, source_type='extracted_source')
        if not chunks.exists():
            print(f"  No chunks found for {deal.title}. Skipping.")
            continue
            
        combined_text = ""
        for chunk in chunks:
            combined_text += f"\n\n--- DOCUMENT: {chunk.metadata.get('filename')} ---\n{chunk.content}"

        # 2. Run AI Analysis using institutional skill/personality
        try:
            # We pass personality and skill to process_content
            # The service will automatically wrap the content in the skill's template
            result = ai_service.process_content(
                content=combined_text[:120000], # Qwen3-VL-8B handles large context
                skill_name=skill.name if skill else "deal_synthesis",
                personality_name=personality.name if personality else "default",
                source_type="batch_synthesis",
                source_id=str(deal.id)
            )
            
            # 3. Handle Thinking and JSON Result
            if isinstance(result, dict):
                deal.current_analysis = result.get('parsed_json') or {}
                # Store thinking in metadata for the UI
                deal.deal_summary = result.get('response', '')[:5000] # Excerpt
                deal.processing_status = 'completed'
                deal.save()
                print(f"  Completed Synthesis for {deal.title}")
            else:
                print(f"  Warning: Unexpected result format for {deal.title}")

        except Exception as e:
            print(f"  Error synthesizing {deal.title}: {e}")

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    synthesize_batch(limit)
