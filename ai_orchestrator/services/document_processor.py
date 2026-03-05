import os
import logging
import tempfile
import io
import base64
from io import BytesIO

# Lightweight libraries for core processing
import fitz  # PyMuPDF
from pptx import Presentation
from openpyxl import load_workbook
from docx import Document
import extract_msg

logger = logging.getLogger(__name__)

class DocumentProcessorService:
    """
    Forensic Document Processor optimized for Qwen 3.5 (64k context).
    - Small PDFs (<10 pages): 100% Vision-based (No local OCR/Docling).
    - Large PDFs: Hybrid (Text Extraction + 5-page Vision sample).
    - Office Docs: Fast structural extraction.
    """

    def __init__(self):
        # We lazy-load Docling only for LARGE files to save RAM on the local server
        self._converter = None

    def _get_docling(self):
        if self._converter is None:
            print("[DOC-PROC] Lazy-loading Docling for large-scale structure...")
            from docling.document_converter import DocumentConverter
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            
            opts = PdfPipelineOptions()
            opts.do_ocr = False
            opts.do_table_structure = True
            self._converter = DocumentConverter(pipeline_options=opts)
        return self._converter

    def extract_text(self, file_content: bytes, filename: str, depth: int = 0) -> str:
        if depth > 5: return "[Error: Max Depth]"
        ext = os.path.splitext(filename)[1].lower()

        # Handle Outlook .msg first
        if ext == '.msg':
            return self._extract_from_msg(file_content, depth)

        # Always extract the text layer for context
        if ext == '.pdf':
            print(f"[DOC-PROC] Extracting text layer from {filename}...")
            return self._extract_from_pdf(file_content)

        # Office formats
        if ext == '.docx': return self._extract_from_docx(file_content)
        if ext == '.pptx': return self._extract_from_pptx(file_content)
        if ext == '.xlsx': return self._extract_from_xlsx(file_content)
        
        return file_content.decode('utf-8', errors='ignore') if ext in ['.txt', '.csv'] else f"[Format: {ext}]"

    def extract_visuals(self, file_content: bytes, filename: str) -> list:
        """
        Extracts optimized forensic images for the remote Vision model.
        Balanced for T4 GPU VRAM and request stability.
        """
        ext = os.path.splitext(filename)[1].lower()
        images_b64 = []
        if ext not in ['.pdf', '.png', '.jpg', '.jpeg']: return []

        try:
            if ext in ['.png', '.jpg', '.jpeg']:
                images_b64.append(base64.b64encode(file_content).decode('utf-8'))
            elif ext == '.pdf':
                with fitz.open(stream=file_content, filetype="pdf") as doc:
                    total_pages = len(doc)
                    pages_to_scan = min(total_pages, 15)
                    
                    if total_pages > 15:
                        print(f"[DOC-PROC] WARNING: PDF contains {total_pages} pages. Truncating to top 15 pages for forensic audit.")
                    else:
                        print(f"[DOC-PROC] Processing all {total_pages} pages of PDF.")
                    
                    for i in range(pages_to_scan):
                        print(f"    [DOC-PROC] Rendering PDF Page {i+1} of {total_pages}...")
                        page = doc.load_page(i)
                        # Matrix 1.0 is fast and perfect for GLM-OCR
                        pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0)) 
                        images_b64.append(base64.b64encode(pix.tobytes("png")).decode('utf-8'))
        except Exception as e:
            logger.error(f"Visual Extraction Fail: {str(e)}")
        
        return images_b64

    def _extract_from_pdf(self, content: bytes) -> str:
        text = ""
        with fitz.open(stream=content, filetype="pdf") as doc:
            for page in doc:
                text += f"\n--- Page {page.number + 1} ---\n{page.get_text()}"
        return text

    def _extract_from_msg(self, content: bytes, depth: int) -> str:
        msg = extract_msg.Message(content)
        body = msg.body if msg.body else ""
        metadata = f"From: {msg.sender}\nSubject: {msg.subject}\n\n"
        context = ""
        if hasattr(msg, 'attachments'):
            for a in msg.attachments:
                name = getattr(a, 'filename', 'unnamed')
                if a.data:
                    context += f"\n--- ATT: {name} ---\n{self.extract_text(a.data, name, depth+1)}\n"
        return metadata + body + context

    def _extract_from_pptx(self, content: bytes) -> str:
        text = ""
        prs = Presentation(io.BytesIO(content))
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text"): text += shape.text + "\n"
        return text

    def _extract_from_xlsx(self, content: bytes) -> str:
        text = ""
        wb = load_workbook(io.BytesIO(content), data_only=True)
        for sheet in wb.worksheets:
            text += f"--- Sheet: {sheet.title} ---\n"
            for row in sheet.iter_rows(values_only=True):
                text += "\t".join([str(c) if c else "" for c in row]) + "\n"
        return text

    def _extract_from_docx(self, content: bytes) -> str:
        doc = Document(io.BytesIO(content))
        return "\n".join([p.text for p in doc.paragraphs])
