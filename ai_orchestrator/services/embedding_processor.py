import logging
import requests
import json
from typing import List, Dict, Any, Optional
from django.conf import settings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from ..models import DocumentChunk
from deals.models import Deal
from microsoft.models import Email
from pgvector.django import CosineDistance

logger = logging.getLogger(__name__)

class EmbeddingService:
    """
    Service for chunking text and generating embeddings via Ollama.
    Stores results in pgvector-backed DocumentChunk model.
    """

    def __init__(self):
        self.ollama_url = getattr(settings, 'OLLAMA_URL', 'http://52.172.249.12:11434')
        self.model_name = "nomic-embed-text"
        self.chunk_size = 1000
        self.chunk_overlap = 150
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", " ", ""]
        )

    def _get_embedding(self, text: str) -> List[float]:
        """Call Ollama API for a single embedding."""
        try:
            response = requests.post(
                f"{self.ollama_url}/api/embeddings",
                json={"model": self.model_name, "prompt": text},
                timeout=30
            )
            response.raise_for_status()
            return response.json().get("embedding", [])
        except Exception as e:
            logger.error(f"Error generating embedding: {str(e)}")
            return []

    def chunk_and_embed(
        self, 
        text: str, 
        deal: Deal, 
        source_type: str, 
        source_id: str, 
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Splits text, generates embeddings, and saves to database.
        """
        if not text or len(text.strip()) < 10:
            return False

        # 1. Chunking
        chunks = self.text_splitter.split_text(text)
        
        # 2. Vectorize and Save
        created_chunks = []
        for i, chunk_text in enumerate(chunks):
            embedding = self._get_embedding(chunk_text)
            if not embedding:
                continue
            
            chunk_metadata = metadata.copy() if metadata else {}
            chunk_metadata.update({
                "chunk_index": i,
                "total_chunks": len(chunks)
            })

            doc_chunk = DocumentChunk(
                deal=deal,
                source_type=source_type,
                source_id=source_id,
                content=chunk_text,
                embedding=embedding,
                metadata=chunk_metadata
            )
            created_chunks.append(doc_chunk)

        if created_chunks:
            DocumentChunk.objects.bulk_create(created_chunks)
            return True
        
        return False

    def vectorize_email(self, email: Email) -> bool:
        """Process an email and its extracted text."""
        if not email.extracted_text or not email.deal:
            return False
            
        success = self.chunk_and_embed(
            text=email.extracted_text,
            deal=email.deal,
            source_type='email',
            source_id=str(email.id),
            metadata={
                "subject": email.subject,
                "from": email.from_email,
                "date": email.date_received.isoformat() if email.date_received else None
            }
        )
        
        if success:
            email.is_indexed = True
            email.save(update_fields=['is_indexed'])
            
        return success

    def vectorize_deal(self, deal: Deal) -> bool:
        """Vectorize the deal summary and any core deal data."""
        if not deal.deal_summary:
            return False
            
        success = self.chunk_and_embed(
            text=deal.deal_summary,
            deal=deal,
            source_type='deal_summary',
            source_id=str(deal.id),
            metadata={"title": deal.title}
        )
        
        if success:
            deal.is_indexed = True
            deal.save(update_fields=['is_indexed'])
            
        return success

    def search_similar_chunks(self, query: str, deal: Deal, limit: int = 5) -> List[DocumentChunk]:
        """
        Retrieves the most relevant chunks for a query using cosine similarity.
        """
        query_embedding = self._get_embedding(query)
        if not query_embedding:
            return []

        return DocumentChunk.objects.filter(deal=deal).annotate(
            distance=CosineDistance('embedding', query_embedding)
        ).order_by('distance')[:limit]

    def search_global_chunks(self, query: str, limit: int = 10) -> List[DocumentChunk]:
        """
        Retrieves relevant chunks from ANY deal in the system.
        Useful for the Universal Chat.
        """
        query_embedding = self._get_embedding(query)
        if not query_embedding:
            return []

        return DocumentChunk.objects.all().annotate(
            distance=CosineDistance('embedding', query_embedding)
        ).order_by('distance')[:limit]
