import logging
import requests
import json
from typing import List, Dict, Any, Optional
from django.conf import settings
from django.db import connection
from langchain_text_splitters import RecursiveCharacterTextSplitter
from ..models import DocumentChunk
from deals.models import Deal
from microsoft.models import Email

logger = logging.getLogger(__name__)

class EmbeddingService:
    """
    Service for chunking text and generating embeddings via Ollama.
    Supports pgvector for Postgres and keyword-fallback for SQLite.
    """

    def __init__(self):
        self.ollama_url = getattr(settings, 'OLLAMA_URL', 'http://52.172.249.12:11434')
        self.model_name = "nomic-embed-text"
        self.chunk_size = 1000
        self.chunk_overlap = 150
        self.is_sqlite = connection.vendor == 'sqlite'
        
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", " ", ""]
        )

    def _get_embedding(self, text: str) -> List[float]:
        if self.is_sqlite: return [] # Skip embedding calls if on SQLite to save latency
        try:
            response = requests.post(f"{self.ollama_url}/api/embeddings", json={"model": self.model_name, "prompt": text}, timeout=30)
            response.raise_for_status()
            return response.json().get("embedding", [])
        except Exception as e:
            logger.error(f"Error generating embedding: {str(e)}")
            return []

    def chunk_and_embed(self, text: str, deal: Deal, source_type: str, source_id: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not text or len(text.strip()) < 10: return False
        chunks = self.text_splitter.split_text(text)
        created_chunks = []
        for i, chunk_text in enumerate(chunks):
            embedding = self._get_embedding(chunk_text) if not self.is_sqlite else None
            chunk_metadata = metadata.copy() if metadata else {}
            chunk_metadata.update({"chunk_index": i, "total_chunks": len(chunks)})
            doc_chunk = DocumentChunk(deal=deal, source_type=source_type, source_id=source_id, content=chunk_text, embedding=embedding, metadata=chunk_metadata)
            created_chunks.append(doc_chunk)
        if created_chunks:
            DocumentChunk.objects.bulk_create(created_chunks)
            return True
        return False

    def vectorize_email(self, email: Email) -> bool:
        if not email.extracted_text or not email.deal: return False
        success = self.chunk_and_embed(text=email.extracted_text, deal=email.deal, source_type='email', source_id=str(email.id), metadata={"subject": email.subject, "from": email.from_email})
        if success:
            email.is_indexed = True
            email.save(update_fields=['is_indexed'])
        return success

    def vectorize_deal(self, deal: Deal) -> bool:
        if not deal.deal_summary: return False
        success = self.chunk_and_embed(text=deal.deal_summary, deal=deal, source_type='deal_summary', source_id=str(deal.id), metadata={"title": deal.title})
        if success:
            deal.is_indexed = True
            deal.save(update_fields=['is_indexed'])
        return success

    def vectorize_document(self, doc: DocumentChunk) -> bool:
        """Vectorizes a specific deal document artifact."""
        from deals.models import DealDocument
        if not isinstance(doc, DealDocument) or not doc.extracted_text:
            return False
            
        success = self.chunk_and_embed(
            text=doc.extracted_text, 
            deal=doc.deal, 
            source_type='document', 
            source_id=str(doc.id), 
            metadata={"title": doc.title, "type": doc.document_type}
        )
        if success:
            doc.is_indexed = True
            doc.save(update_fields=['is_indexed'])
        return success

    def search_similar_chunks(self, query: str, deal: Deal, limit: int = 5) -> List[DocumentChunk]:
        """Hybrid Search: Vector for Postgres, Ranked Keyword for SQLite."""
        if self.is_sqlite:
            from django.db.models import Q, Count, When, Case, IntegerField, Value
            words = [w.lower() for w in query.split() if len(w) >= 3]
            if not words: return []

            # Filter chunks for this deal
            queryset = DocumentChunk.objects.filter(deal=deal)
            
            # Build a ranking system based on how many keywords match the content
            cases = []
            for word in words[:10]:
                cases.append(When(content__icontains=word, then=Value(1)))
            
            # Note: SQLite doesn't support easy column addition in this way, 
            # so we use a simple OR filter and rely on the AI's intelligence
            # BUT we filter for the most specific terms first (like CM1, CM2)
            important_terms = [w for w in words if any(x in w.upper() for x in ['CM', 'ARR', 'INR', 'CR'])]
            
            q_obj = Q()
            if important_terms:
                for term in important_terms:
                    q_obj |= Q(content__icontains=term)
            else:
                for word in words[:5]:
                    q_obj |= Q(content__icontains=word)
            
            return queryset.filter(q_obj)[:limit]
        
        # Postgres Logic
        from pgvector.django import CosineDistance
        query_embedding = self._get_embedding(query)
        if not query_embedding: return []
        return DocumentChunk.objects.filter(deal=deal).annotate(distance=CosineDistance('embedding', query_embedding)).order_by('distance')[:limit]

    def search_global_chunks(self, query: str, limit: int = 10) -> List[DocumentChunk]:
        """Global search across all deals with term priority for SQLite."""
        if self.is_sqlite:
            from django.db.models import Q
            words = [w.lower() for w in query.split() if len(w) >= 3]
            if not words: return []
            
            important_terms = [w for w in words if any(x in w.upper() for x in ['CM', 'ARR', 'INR', 'CR'])]
            q_obj = Q()
            if important_terms:
                for term in important_terms: q_obj |= Q(content__icontains=term)
            else:
                for word in words[:5]: q_obj |= Q(content__icontains=word)
            
            return DocumentChunk.objects.filter(q_obj)[:limit]

        from pgvector.django import CosineDistance
        query_embedding = self._get_embedding(query)
        if not query_embedding: return []
        return DocumentChunk.objects.all().annotate(distance=CosineDistance('embedding', query_embedding)).order_by('distance')[:limit]
