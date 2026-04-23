import logging
from typing import List
from .llm_providers import VLLMProviderService
from .runtime import AIRuntimeService

logger = logging.getLogger(__name__)

class OCRService:
    """
    Isolates visual transcription logic for page-level OCR.
    Provides high-fidelity text extraction from document images.
    """

    def __init__(self):
        self.provider = VLLMProviderService()

    def transcribe(self, images: List[str], model: str | None = None) -> str:
        """Specialized OCR using visual model."""
        if not images: return ""
        model = model or AIRuntimeService.get_vision_model()
        
        print(f"[AI-PIPELINE] Phase 1: Transcribing {len(images)} document pages via {model}...")
        transcription = ""
        
        for i, img in enumerate(images):
            print(f"    [AI-PIPELINE] Phase 1: Transcribing page {i+1} of {len(images)} via {model}...")
            payload = {
                "model": model,
                "prompt": "Extract all text and tabular data from this document page exactly. Output Markdown.",
                "images": [img],
                "stream": False,
                "keep_alive": "1m"
            }
            try:
                resp = self.provider.execute_standard(payload, timeout=120)
                page_text = resp.get("response", "")
                transcription += f"\n\n--- PDF PAGE {i+1} TRANSCRIPTION ---\n{page_text}"
            except Exception as e:
                logger.error(f"OCR Phase failed on page {i+1}: {str(e)}")
                
        return transcription
