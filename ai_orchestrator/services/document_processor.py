import os
import logging
import io
import base64
import requests
from django.conf import settings
from decouple import config

import fitz  # PyMuPDF
from pptx import Presentation
from openpyxl import load_workbook
from docx import Document
import extract_msg

logger = logging.getLogger(__name__)

class DocumentProcessorService:
    """
    Forensic Document Processor optimized for remote GPU analysis (GLM-OCR -> Qwen).
    Offloads all heavy text extraction to the AI VM to prevent local worker OOM.
    """
    def __init__(self):
        self.ollama_url = os.environ.get('OLLAMA_URL') or config('OLLAMA_URL', default='http://localhost:11434')

    def get_extraction_result(self, file_content: bytes, filename: str, page_limit: int = None) -> dict:
        """
        Returns structured extraction details so callers can distinguish OCR success,
        text fallback, and outright failures for auditability.
        """
        ext = os.path.splitext(filename)[1].lower()
        images_b64 = self._convert_to_images(file_content, filename, page_limit)

        if not images_b64:
            logger.info("[DOC-PROC] No renderable images for %s. Using fallback extraction.", filename)
            if ext in ['.txt', '.csv']:
                text = file_content.decode('utf-8', errors='ignore').strip()
                if text:
                    return {"text": text, "mode": "fallback_text"}
                return {"text": "", "mode": "fallback_text", "error": "Plain-text file produced no readable content"}

            fallback_text = self.extract_text_fallback(file_content, filename, page_limit=page_limit).strip()
            if fallback_text:
                return {"text": fallback_text, "mode": "fallback_text"}

            return {"text": "", "mode": "fallback_text", "error": f"No readable content extracted for {filename}"}

        from ..models import AIPersonality

        personality = AIPersonality.objects.filter(is_default=True).first()
        vision_model = (
            personality.vision_model_name
            if personality and personality.vision_model_name
            else getattr(settings, "OLLAMA_DEFAULT_VISION_MODEL", "llava:latest")
        )

        transcription = ""
        total_pages = len(images_b64)
        print(f"[DOC-PROC] Sending {total_pages} pages of {filename} to {vision_model} VM...")

        for i, img in enumerate(images_b64):
            try:
                print(f"    [DOC-PROC] Transcribing page {i+1} of {total_pages}...")
                payload = {
                    "model": vision_model,
                    "prompt": "Extract all text and tabular data from this document exactly. Output Markdown.",
                    "images": [img],
                    "stream": False,
                    "keep_alive": "1m"
                }
                resp = requests.post(f"{self.ollama_url}/api/generate", json=payload, timeout=120)
                if resp.status_code == 200:
                    page_text = resp.json().get("response", "")
                    transcription += f"\n\n--- {filename} (PAGE {i+1}) ---\n{page_text}"
                else:
                    logger.error(f"GLM-OCR returned {resp.status_code}: {resp.text}")
                    return {
                        "text": transcription.strip(),
                        "mode": "glm_ocr",
                        "error": f"OCR returned {resp.status_code} for page {i+1}"
                    }
            except Exception as e:
                logger.error(f"GLM-OCR failed on page {i+1} of {filename}: {str(e)}")
                return {
                    "text": transcription.strip(),
                    "mode": "glm_ocr",
                    "error": f"OCR failed on page {i+1}: {str(e)}"
                }

        transcription = transcription.strip()
        if transcription:
            return {"text": transcription, "mode": "glm_ocr"}
        return {"text": "", "mode": "glm_ocr", "error": f"OCR produced no readable content for {filename}"}

    def transcribe_document(self, file_content: bytes, filename: str, page_limit: int = None) -> str:
        """
        Master method: Converts document to images (up to page_limit) and sends to GLM-OCR on the VM.
        Returns the markdown transcription.
        """
        result = self.get_extraction_result(file_content, filename, page_limit=page_limit)
        if result.get("text"):
            return result["text"]
        return f"[No readable content extracted for: {filename}]"

    def _convert_to_images(self, file_content: bytes, filename: str, page_limit: int = None) -> list:
        """
        Converts supported files to a list of base64 PNGs.
        Memory optimization: We only keep the base64 strings, freeing the raw render objects immediately.
        """
        ext = os.path.splitext(filename)[1].lower()
        images_b64 = []

        try:
            if ext in ['.png', '.jpg', '.jpeg']:
                images_b64.append(base64.b64encode(file_content).decode('utf-8'))
                
            elif ext == '.pdf':
                with fitz.open(stream=file_content, filetype="pdf") as doc:
                    total = len(doc)
                    limit = min(page_limit, total) if page_limit else total
                    
                    for i in range(limit):
                        page = doc.load_page(i)
                        # Matrix 1.0 keeps memory footprint low while GLM-OCR still performs excellently
                        pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0)) 
                        images_b64.append(base64.b64encode(pix.tobytes("png")).decode('utf-8'))
                        # Explicit cleanup
                        del pix
                        del page
                        
            elif ext in ['.pptx', '.ppt']:
                prs = Presentation(io.BytesIO(file_content))
                limit = min(page_limit, len(prs.slides)) if page_limit else len(prs.slides)
                # PPTX doesn't render slides to images easily in pure python without Windows/LibreOffice.
                # As a fallback, we extract text locally just for PPTX/DOCX if we can't image them.
                # However, for pure image extraction, we try to grab embedded visuals.
                for i in range(limit):
                    slide = prs.slides[i]
                    for shape in slide.shapes:
                        if hasattr(shape, "image"):
                            images_b64.append(base64.b64encode(shape.image.blob).decode('utf-8'))
                            
            # Note: For strict text extraction of Word/Excel on Linux without heavy renderers, 
            # we might still need lightweight local text extraction if GLM-OCR is strictly image-in.
            # But the user specifically wants everything sent to GLM-OCR. 
            # If we MUST send to GLM-OCR, we need images. If we can't make images (like DOCX on Linux),
            # we must fallback to text.
            elif ext in ['.docx', '.doc']:
                 doc = Document(io.BytesIO(file_content))
                 text = "\n".join([p.text for p in doc.paragraphs])
                 # Return empty image list, letting the caller fallback to text
                 
        except Exception as e:
            logger.error(f"Image Conversion Fail for {filename}: {str(e)}")
            
        return images_b64

    # Keep lightweight local extractors ONLY for formats we can't reliably render to images on a headless Linux worker
    def extract_text_fallback(self, file_content: bytes, filename: str, page_limit: int = None) -> str:
        ext = os.path.splitext(filename)[1].lower()
        try:
            if ext in ['.docx', '.doc']:
                 doc = Document(io.BytesIO(file_content))
                 paragraphs = doc.paragraphs
                 if page_limit:
                     # Heuristic: 10 paragraphs ~ 1 page
                     paragraphs = paragraphs[:page_limit * 20]
                 return "\n".join([p.text for p in paragraphs])
                 
            if ext in ['.xlsx', '.xls']:
                 # MEMORY OPTIMIZATION: Use read_only=True
                 wb = load_workbook(io.BytesIO(file_content), data_only=True, read_only=True)
                 text = ""
                 sheets = wb.sheetnames
                 if page_limit:
                     sheets = sheets[:page_limit]
                     
                 for name in sheets:
                     sheet = wb[name]
                     text += f"--- Sheet: {name} ---\n"
                     row_count = 0
                     for row in sheet.iter_rows(values_only=True):
                         text += "\t".join([str(c) if c else "" for c in row]) + "\n"
                         row_count += 1
                         # Limit rows for VDR preview/metadata to prevent OOM
                         if page_limit and row_count > 100:
                             text += "... [Truncated for preview] ...\n"
                             break
                 return text
                 
            if ext in ['.pptx', '.ppt']:
                 text = ""
                 prs = Presentation(io.BytesIO(file_content))
                 slides = prs.slides
                 if page_limit:
                     limit = min(page_limit, len(slides))
                     slides = [slides[i] for i in range(limit)]
                 for slide in slides:
                     for shape in slide.shapes:
                         if hasattr(shape, "text"): text += shape.text + "\n"
                 return text
        except Exception as e:
            logger.error(f"Fallback extraction failed for {filename}: {e}")
            
        return ""
