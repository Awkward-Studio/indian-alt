import os
import io
import logging
import fitz  # PyMuPDF
from pptx import Presentation
from openpyxl import load_workbook
from docx import Document

logger = logging.getLogger(__name__)

class DocumentProcessorService:
    """
    Service for extracting text content from various document formats
    to be processed by an LLM.
    """

    def extract_text(self, file_content: bytes, filename: str) -> str:
        """
        Main entry point to extract text based on file extension.
        """
        ext = os.path.splitext(filename)[1].lower()
        
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
                return ""
        except Exception as e:
            logger.error(f"Error extracting text from {filename}: {str(e)}")
            return f"[Error extracting text: {str(e)}]"

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
                # Filter out None values and join with tabs
                row_text = "\t".join([str(cell) if cell is not None else "" for cell in row])
                if row_text.strip():
                    text += row_text + "\n"
        return text

    def _extract_from_docx(self, content: bytes) -> str:
        doc = Document(io.BytesIO(content))
        return "\n".join([para.text for para in doc.paragraphs])
