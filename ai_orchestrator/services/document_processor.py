import base64
import io
import logging
import os

import fitz  # PyMuPDF
import requests
from django.conf import settings
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation

from .llm_providers import VLLMProviderService
from .runtime import AIRuntimeService

logger = logging.getLogger(__name__)


class DocumentProcessorService:
    """
    Backend-side client and fallback processor for document extraction.

    Primary path:
    - send the file to the remote VM docproc service when configured

    Fallback path:
    - keep the existing local rendering/text extraction behavior so the
      backend can still function if docproc is unavailable.
    """

    def __init__(self):
        self.provider = VLLMProviderService()
        self.docproc_url = (getattr(settings, "DOC_PROCESSOR_URL", "") or "").rstrip("/")
        self.docproc_api_key = getattr(settings, "DOC_PROCESSOR_API_KEY", "") or ""
        self.docproc_timeout = getattr(settings, "DOC_PROCESSOR_TIMEOUT", 300)

    def get_extraction_result(
        self,
        file_content: bytes,
        filename: str,
        page_limit: int = None,
        allow_local_fallback: bool = True,
    ) -> dict:
        if self.docproc_url:
            remote_result = self._remote_extract(file_content, filename, page_limit=page_limit)
            if remote_result:
                return remote_result
            if not allow_local_fallback:
                return {
                    "text": "",
                    "raw_extracted_text": "",
                    "normalized_text": "",
                    "mode": "docproc_remote",
                    "transcription_status": "failed",
                    "quality_flags": ["remote_docproc_failed"],
                    "error": f"Remote docproc unavailable for {filename}",
                }
            logger.warning("[DOC-PROC] Remote docproc unavailable for %s. Falling back locally.", filename)
        return self._local_extract(file_content, filename, page_limit=page_limit)

    def transcribe_document(self, file_content: bytes, filename: str, page_limit: int = None) -> str:
        result = self.get_extraction_result(file_content, filename, page_limit=page_limit)
        text = result.get("normalized_text") or result.get("text")
        if text:
            return text
        return f"[No readable content extracted for: {filename}]"

    def _remote_extract(self, file_content: bytes, filename: str, page_limit: int = None) -> dict | None:
        try:
            payload = {
                "filename": filename,
                "page_limit": page_limit,
                "content_base64": base64.b64encode(file_content).decode("utf-8"),
            }
            headers = {"Content-Type": "application/json"}
            if self.docproc_api_key:
                headers["Authorization"] = f"Bearer {self.docproc_api_key}"
            response = requests.post(
                f"{self.docproc_url}/extract/document",
                headers=headers,
                json=payload,
                timeout=self.docproc_timeout,
            )
            response.raise_for_status()
            data = response.json()
            return self._normalize_remote_result(data, filename)
        except Exception as e:
            logger.warning("[DOC-PROC] Remote extraction failed for %s: %s", filename, e)
            return None

    @staticmethod
    def _normalize_remote_result(data: dict, filename: str) -> dict:
        raw_text = (data.get("raw_extracted_text") or "").strip()
        normalized_text = (data.get("normalized_text") or raw_text).strip()
        extraction_mode = data.get("extraction_mode") or "docproc_remote"
        status = data.get("transcription_status") or ("complete" if normalized_text else "failed")
        result = {
            "text": normalized_text,
            "raw_extracted_text": raw_text or normalized_text,
            "normalized_text": normalized_text,
            "mode": extraction_mode,
            "transcription_status": status,
            "quality_flags": data.get("quality_flags") if isinstance(data.get("quality_flags"), list) else [],
            "render_metadata": data.get("render_metadata") if isinstance(data.get("render_metadata"), dict) else {},
        }
        if data.get("error"):
            result["error"] = data["error"]
        return result

    def _local_extract(self, file_content: bytes, filename: str, page_limit: int = None) -> dict:
        ext = os.path.splitext(filename)[1].lower()
        images_b64 = self._convert_to_images(file_content, filename, page_limit)

        if not images_b64:
            logger.info("[DOC-PROC] No renderable images for %s. Using fallback extraction.", filename)
            if ext in [".txt", ".csv"]:
                text = file_content.decode("utf-8", errors="ignore").strip()
                if text:
                    return self._build_local_result(
                        raw_text=text,
                        normalized_text=text,
                        mode="fallback_text",
                        quality_flags=["local_backend_fallback"],
                    )
                return {
                    "text": "",
                    "raw_extracted_text": "",
                    "normalized_text": "",
                    "mode": "fallback_text",
                    "transcription_status": "failed",
                    "quality_flags": ["local_backend_fallback"],
                    "error": "Plain-text file produced no readable content",
                }

            fallback_text = self.extract_text_fallback(file_content, filename, page_limit=page_limit).strip()
            if fallback_text:
                return self._build_local_result(
                    raw_text=fallback_text,
                    normalized_text=fallback_text,
                    mode="fallback_text",
                    quality_flags=["local_backend_fallback"],
                )

            return {
                "text": "",
                "raw_extracted_text": "",
                "normalized_text": "",
                "mode": "fallback_text",
                "transcription_status": "failed",
                "quality_flags": ["local_backend_fallback"],
                "error": f"No readable content extracted for {filename}",
            }

        personality = AIRuntimeService.get_default_personality()
        vision_model = AIRuntimeService.get_vision_model(personality)

        transcription = ""
        total_pages = len(images_b64)
        logger.info("[DOC-PROC] Sending %s pages of %s to %s.", total_pages, filename, vision_model)

        for i, img in enumerate(images_b64):
            try:
                payload = {
                    "model": vision_model,
                    "prompt": "Extract all text and tabular data from this document exactly. Output Markdown.",
                    "images": [img],
                    "stream": False,
                }
                resp = self.provider.execute_standard(payload, timeout=120)
                page_text = resp.get("response", "")
                if page_text is not None:
                    transcription += f"\n\n--- {filename} (PAGE {i+1}) ---\n{page_text}"
            except Exception as e:
                logger.error("Vision extraction failed on page %s of %s: %s", i + 1, filename, e)
                return {
                    "text": transcription.strip(),
                    "raw_extracted_text": transcription.strip(),
                    "normalized_text": transcription.strip(),
                    "mode": "vllm_vision",
                    "transcription_status": "partial" if transcription.strip() else "failed",
                    "quality_flags": ["local_backend_fallback", "vision_partial_failure"],
                    "error": f"Vision extraction failed on page {i+1}: {str(e)}",
                }

        transcription = transcription.strip()
        if transcription:
            return self._build_local_result(
                raw_text=transcription,
                normalized_text=transcription,
                mode="vllm_vision",
                quality_flags=["local_backend_fallback"],
            )
        return {
            "text": "",
            "raw_extracted_text": "",
            "normalized_text": "",
            "mode": "vllm_vision",
            "transcription_status": "failed",
            "quality_flags": ["local_backend_fallback"],
            "error": f"Vision extraction produced no readable content for {filename}",
        }

    @staticmethod
    def _build_local_result(
        *,
        raw_text: str,
        normalized_text: str,
        mode: str,
        quality_flags: list[str] | None = None,
    ) -> dict:
        text = (normalized_text or raw_text or "").strip()
        raw_text = (raw_text or text).strip()
        return {
            "text": text,
            "raw_extracted_text": raw_text,
            "normalized_text": text,
            "mode": mode,
            "transcription_status": "complete" if text else "failed",
            "quality_flags": quality_flags or [],
            "render_metadata": {},
        }

    def _convert_to_images(self, file_content: bytes, filename: str, page_limit: int = None) -> list[str]:
        ext = os.path.splitext(filename)[1].lower()
        images_b64: list[str] = []

        try:
            if ext in [".png", ".jpg", ".jpeg"]:
                images_b64.append(base64.b64encode(file_content).decode("utf-8"))
            elif ext == ".pdf":
                with fitz.open(stream=file_content, filetype="pdf") as doc:
                    total = len(doc)
                    limit = min(page_limit, total) if page_limit else total
                    for i in range(limit):
                        page = doc.load_page(i)
                        pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
                        images_b64.append(base64.b64encode(pix.tobytes("png")).decode("utf-8"))
                        del pix
                        del page
            elif ext in [".pptx", ".ppt"]:
                prs = Presentation(io.BytesIO(file_content))
                limit = min(page_limit, len(prs.slides)) if page_limit else len(prs.slides)
                for i in range(limit):
                    slide = prs.slides[i]
                    for shape in slide.shapes:
                        if hasattr(shape, "image"):
                            images_b64.append(base64.b64encode(shape.image.blob).decode("utf-8"))
        except Exception as e:
            logger.error("Image conversion failed for %s: %s", filename, e)

        return images_b64

    def extract_text_fallback(self, file_content: bytes, filename: str, page_limit: int = None) -> str:
        ext = os.path.splitext(filename)[1].lower()
        try:
            if ext in [".docx", ".doc"]:
                doc = Document(io.BytesIO(file_content))
                paragraphs = doc.paragraphs
                if page_limit:
                    paragraphs = paragraphs[: page_limit * 20]
                return "\n".join([p.text for p in paragraphs])

            if ext in [".xlsx", ".xls"]:
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
                        if page_limit and row_count > 100:
                            text += "... [Truncated for preview] ...\n"
                            break
                return text

            if ext in [".pptx", ".ppt"]:
                text = ""
                prs = Presentation(io.BytesIO(file_content))
                slides = prs.slides
                if page_limit:
                    limit = min(page_limit, len(slides))
                    slides = [slides[i] for i in range(limit)]
                for slide in slides:
                    for shape in slide.shapes:
                        if hasattr(shape, "text"):
                            text += shape.text + "\n"
                return text
        except Exception as e:
            logger.error("Fallback extraction failed for %s: %s", filename, e)
        return ""
