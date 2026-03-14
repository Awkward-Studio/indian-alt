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
        if ext == '.docx' or ext == '.doc': return self._extract_from_docx(file_content)
        if ext == '.pptx' or ext == '.ppt': return self._extract_from_pptx(file_content)
        if ext == '.xlsx' or ext == '.xls': return self._extract_from_xlsx(file_content)
        
        return file_content.decode('utf-8', errors='ignore') if ext in ['.txt', '.csv'] else f"[Format: {ext}]"

    def extract_visuals(self, file_content: bytes, filename: str) -> list:
        """
        Extracts optimized forensic images for the remote Vision model.
        Balanced for T4 GPU VRAM and request stability.
        """
        ext = os.path.splitext(filename)[1].lower()
        images_b64 = []
        if ext not in ['.pdf', '.png', '.jpg', '.jpeg', '.pptx', '.ppt', '.docx', '.doc', '.xlsx', '.xls']: return []

        try:
            if ext in ['.png', '.jpg', '.jpeg']:
                images_b64.append(base64.b64encode(file_content).decode('utf-8'))
            elif ext == '.pdf':
                with fitz.open(stream=file_content, filetype="pdf") as doc:
                    total_pages = len(doc)
                    print(f"[DOC-PROC] Processing all {total_pages} pages of PDF.")
                    
                    for i in range(total_pages):
                        print(f"    [DOC-PROC] Rendering PDF Page {i+1} of {total_pages}...")
                        page = doc.load_page(i)
                        # Matrix 1.0 is fast and perfect for GLM-OCR
                        pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0)) 
                        images_b64.append(base64.b64encode(pix.tobytes("png")).decode('utf-8'))
            elif ext in ['.pptx', '.ppt']:
                try:
                    prs = Presentation(io.BytesIO(file_content))
                    print(f"[DOC-PROC] Extracting embedded visuals from all {len(prs.slides)} slides of {ext}.")
                    for slide in prs.slides:
                        for shape in slide.shapes:
                            if hasattr(shape, "image"):
                                image_bytes = shape.image.blob
                                images_b64.append(base64.b64encode(image_bytes).decode('utf-8'))
                except Exception as ppt_err:
                    print(f"[DOC-PROC] Failed visuals for {ext}: {ppt_err}")
            elif ext in ['.docx', '.doc']:
                doc = Document(io.BytesIO(file_content))
                print(f"[DOC-PROC] Extracting inline images from {ext} document.")
                for rel in doc.part.rels.values():
                    if "image" in rel.target_ref:
                        images_b64.append(base64.b64encode(rel.target_part.blob).decode('utf-8'))
            elif ext in ['.xlsx', '.xls']:
                # For Excel, we extract any embedded drawings/images
                wb = load_workbook(io.BytesIO(file_content))
                print(f"[DOC-PROC] Extracting drawings from {ext} spreadsheet.")
                for sheet in wb.worksheets:
                    if hasattr(sheet, '_images'):
                        for img in sheet._images:
                            img_data = img.ref.read() if hasattr(img.ref, 'read') else None
                            if img_data:
                                images_b64.append(base64.b64encode(img_data).decode('utf-8'))
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
        try:
            # Try as modern PPTX first (Zip-based)
            prs = Presentation(io.BytesIO(content))
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text"): text += shape.text + "\n"
        except Exception as e:
            # Fallback for legacy .PPT (OLE binary format)
            if "not a zip file" in str(e):
                logger.info("Detected legacy .PPT binary format. Attempting stream extraction.")
                # We can use BeautifulSoup or simple regex to find text in the binary
                # For a robust solution, we'd use a tool like 'antiword' or 'catdoc' but 
                # here we'll do a safe fallback
                return f"[Legacy .PPT Binary Data - High-Fidelity extraction not supported without conversion]"
            logger.error(f"PPTX Extraction Fail: {e}")
            text = f"[Error extracting PPTX: {str(e)}]"
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
