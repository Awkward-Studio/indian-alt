import os
import logging
import tempfile
import io
import base64
from io import BytesIO
from docling.datamodel.base_models import InputFormat
from docling.document_converter import DocumentConverter

# Legacy and additional format imports
import fitz  # PyMuPDF
from pptx import Presentation
from openpyxl import load_workbook
from docx import Document
import extract_msg

logger = logging.getLogger(__name__)

class DocumentProcessorService:
    """
    Advanced Service for extracting structured Markdown from various document formats
    using Docling, with fallbacks for specialized types like Outlook .msg.
    
    Includes Vision capabilities to render PDF pages as images for LLM analysis.
    """

    def __init__(self):
        # Initialize Docling Converter
        self.converter = DocumentConverter()

    def extract_text(self, file_content: bytes, filename: str, depth: int = 0) -> str:
        """
        Main entry point for text extraction. Uses Docling for high-fidelity Markdown.
        """
        if depth > 5: # Prevent infinite recursion
            return "[Error: Maximum nesting depth reached]"

        ext = os.path.splitext(filename)[1].lower()
        
        # 1. Specialized Handler: Outlook .msg
        if ext == '.msg':
            try:
                return self._extract_from_msg(file_content, depth=depth)
            except Exception as e:
                logger.error(f"Failed to extract .msg {filename}: {str(e)}")
                return f"[Error parsing Outlook Message: {str(e)}]"

        # 2. Try Docling for supported formats
        docling_formats = ['.pdf', '.docx', '.pptx', '.xlsx', '.html', '.md']
        if ext in docling_formats:
            try:
                return self._extract_with_docling(file_content, filename)
            except Exception as e:
                logger.error(f"Docling failed for {filename}, falling back: {str(e)}")

        # 3. Legacy / Manual Fallbacks
        try:
            if ext == '.pdf':
                return self._extract_from_pdf(file_content)
            elif ext in ['.pptx', '.ppt']:
                return self._extract_from_pptx(file_content)
            elif ext in ['.xlsx', '.xls']:
                return self._extract_from_xlsx(file_content)
            elif ext in ['.docx', '.doc']:
                return self._extract_from_docx(file_content)
            elif ext in ['.txt', '.csv']:
                return file_content.decode('utf-8', errors='ignore')
            else:
                return f"[Unsupported text format: {ext}]"
        except Exception as e:
            logger.error(f"Error extracting text from {filename}: {str(e)}")
            return f"[Error extracting text: {str(e)}]"

    def extract_visuals(self, file_content: bytes, filename: str) -> list:
        """
        Converts document pages or images into a list of Base64 strings for Vision AI.
        Returns first few pages of PDFs as images.
        """
        ext = os.path.splitext(filename)[1].lower()
        images_b64 = []

        try:
            # 1. Standalone Images
            if ext in ['.png', '.jpg', '.jpeg', '.webp']:
                images_b64.append(base64.b64encode(file_content).decode('utf-8'))

            # 2. Render PDF pages as images (Critical for Charts/Tables)
            elif ext == '.pdf':
                with fitz.open(stream=file_content, filetype="pdf") as doc:
                    # Render first 2 pages to keep context window manageable
                    for i in range(min(len(doc), 2)):
                        page = doc.load_page(i)
                        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)) # Good balance of res/size
                        img_bytes = pix.tobytes("png")
                        images_b64.append(base64.b64encode(img_bytes).decode('utf-8'))
        except Exception as e:
            logger.error(f"Visual extraction failed for {filename}: {str(e)}")

        return images_b64

    def _extract_from_msg(self, content: bytes, depth: int = 0) -> str:
        """Parses Outlook .msg files and its internal attachments."""
        try:
            msg = extract_msg.Message(content)
            body = msg.body if msg.body else ""
            metadata = f"From: {msg.sender}\nSubject: {msg.subject}\nDate: {msg.date}\n\n"
            
            attachment_context = ""
            if hasattr(msg, 'attachments') and msg.attachments:
                for a in msg.attachments:
                    name = getattr(a, 'filename', getattr(a, 'longFilename', 'unnamed_attachment'))
                    if not name:
                        continue
                    a_data = getattr(a, 'data', None)
                    if a_data:
                        a_text = self.extract_text(a_data, name, depth=depth+1)
                        attachment_context += f"\n\n--- INTERNAL ATTACHMENT: {name} ---\n{a_text}\n"
            return metadata + body + attachment_context
        except Exception as e:
            logger.error(f"Inner msg parsing error: {str(e)}")
            return f"[Error parsing .msg body: {str(e)}]"

    def _extract_with_docling(self, content: bytes, filename: str) -> str:
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            result = self.converter.convert(tmp_path)
            return result.document.export_to_markdown()
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _extract_from_pdf(self, content: bytes) -> str:
        text = ""
        with fitz.open(stream=content, filetype="pdf") as doc:
            for page in doc:
                text += page.get_text()
        return text

    def _extract_from_pptx(self, content: bytes) -> str:
        text = ""
        prs = Presentation(io.BytesIO(content))
        for slide in prs.slides:
            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    text += shape.text + "\n"
        return text

    def _extract_from_xlsx(self, content: bytes) -> str:
        text = ""
        wb = load_workbook(io.BytesIO(content), data_only=True)
        for sheet in wb.worksheets:
            text += f"--- Sheet: {sheet.title} ---\n"
            for row in sheet.iter_rows(values_only=True):
                row_text = "\t".join([str(cell) if cell is not None else "" for cell in row])
                if row_text.strip():
                    text += row_text + "\n"
        return text

    def _extract_from_docx(self, content: bytes) -> str:
        doc = Document(io.BytesIO(content))
        return "\n".join([para.text for para in doc.paragraphs])
