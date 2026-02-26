import os
import logging
import tempfile
import io
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
    """

    def __init__(self):
        # Initialize Docling Converter
        self.converter = DocumentConverter()

    def extract_text(self, file_content: bytes, filename: str, depth: int = 0) -> str:
        """
        Main entry point. Uses Docling for high-fidelity Markdown,
        specialized parsers for types like .msg, and basic extraction as fallback.
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
                # Fall through to legacy methods

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
                logger.warning(f"Unsupported file extension: {ext}")
                return f"[Unsupported format: {ext}]"
        except Exception as e:
            logger.error(f"Error extracting text from {filename}: {str(e)}")
            return f"[Error extracting text: {str(e)}]"

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
                    
                    try:
                        # Extract-msg specific: get bytes or nested msg
                        a_data = getattr(a, 'data', None)
                        if a_data:
                            # Recurse with incremented depth
                            a_text = self.extract_text(a_data, name, depth=depth+1)
                            attachment_context += f"\n\n--- INTERNAL ATTACHMENT: {name} ---\n{a_text}\n"
                    except Exception as ae:
                        logger.error(f"Failed internal attachment {name}: {str(ae)}")
                
            return metadata + body + attachment_context
        except Exception as e:
            logger.error(f"Inner msg parsing error: {str(e)}")
            return f"[Error parsing .msg body: {str(e)}]"

    def _extract_with_docling(self, content: bytes, filename: str) -> str:
        """Converts document to Markdown using Docling."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            result = self.converter.convert(tmp_path)
            markdown_content = result.document.export_to_markdown()
            return markdown_content
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
