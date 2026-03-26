import logging
import requests
from typing import List
from .llm_providers import OllamaProviderService

logger = logging.getLogger(__name__)

class OCRService:
    """
    Isolates the `glm-ocr:latest` visual transcription logic.
    Provides high-fidelity text extraction from document images.
    """

    def __init__(self):
        self.provider = OllamaProviderService()

    def transcribe(self, images: List[str], model: str = 'glm-ocr:latest') -> str:
        """Specialized OCR using visual model."""
        if not images: return ""
        
        print(f"[AI-PIPELINE] Phase 1: Transcribing {len(images)} document pages via {model}...")
        transcription = ""
        
        for i, img in enumerate(images):
            print(f"    [AI-PIPELINE] Phase 1: Transcribing page {i+1} of {len(images)} via {model}...")
            payload = {
                "model": model,
                "prompt": "Extract all text and tabular data from this document page exactly. Output Markdown.",
                "images": [img],
                "stream": False,
                "keep_alive": "30s"
            }
            try:
                resp = self.provider.execute_standard(payload, timeout=120)
                page_text = resp.get("response", "")
                transcription += f"\n\n--- PDF PAGE {i+1} TRANSCRIPTION ---\n{page_text}"
            except Exception as e:
                logger.error(f"OCR Phase failed on page {i+1}: {str(e)}")
                
        return transcription
