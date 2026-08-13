from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService
from deals.models import Deal, DealDocument, DocumentType, SectorResearchAcquisition
from deals.services.document_artifacts import DocumentArtifactService
from deals.services.research_discovery import ResearchDiscoveryService


class ResearchAcquisitionError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


class ResearchAcquisitionService:
    ALLOWED_MIME_TYPES = {"application/pdf", "text/plain", "text/html"}
    REDIRECT_CODES = {301, 302, 303, 307, 308}

    def __init__(self, *, http_session=None):
        self.http = http_session or requests.Session()
        self.max_bytes = int(getattr(settings, "RESEARCH_ACQUISITION_MAX_BYTES", 25 * 1024 * 1024))
        self.timeout = float(getattr(settings, "RESEARCH_ACQUISITION_TIMEOUT", 30))

    def download(self, url: str) -> tuple[bytes, dict]:
        current = url
        redirects = []
        for _ in range(5):
            if not ResearchDiscoveryService.is_safe_public_url(current):
                raise ResearchAcquisitionError("UNSAFE_URL", "The source resolved to a non-public or blocked address.")
            response = self.http.get(
                current,
                allow_redirects=False,
                stream=True,
                timeout=self.timeout,
                headers={"User-Agent": "IndiaAlternativesResearchAcquisition/1.0"},
            )
            if response.status_code in self.REDIRECT_CODES:
                location = response.headers.get("Location")
                if not location:
                    raise ResearchAcquisitionError("INVALID_REDIRECT", "The publisher returned a redirect without a destination.")
                next_url = urljoin(current, location)
                redirects.append(next_url)
                current = next_url
                continue
            if response.status_code != 200:
                raise ResearchAcquisitionError("HTTP_STATUS", f"The publisher returned HTTP {response.status_code}.")
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in self.ALLOWED_MIME_TYPES:
                raise ResearchAcquisitionError("MIME_NOT_ALLOWED", f"Content type {content_type or 'unknown'} is not permitted.")
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > self.max_bytes:
                raise ResearchAcquisitionError("FILE_TOO_LARGE", "The source exceeds the configured acquisition size limit.")
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.max_bytes:
                    raise ResearchAcquisitionError("FILE_TOO_LARGE", "The streamed source exceeded the configured acquisition size limit.")
                chunks.append(chunk)
            return b"".join(chunks), {
                "final_url": current,
                "redirects": redirects,
                "content_type": content_type,
                "content_length": total,
                "verified_at": timezone.now().isoformat(),
            }
        raise ResearchAcquisitionError("TOO_MANY_REDIRECTS", "The source exceeded the redirect limit.")

    @transaction.atomic
    def attach(self, acquisition: SectorResearchAcquisition, content: bytes, access: dict) -> DealDocument:
        Deal.objects.select_for_update().get(pk=acquisition.deal_id)
        checksum = hashlib.sha256(content).hexdigest()
        duplicate = SectorResearchAcquisition.objects.filter(
            deal=acquisition.deal,
            checksum_sha256=checksum,
            document__isnull=False,
        ).exclude(pk=acquisition.pk).select_related("document").first()
        if duplicate:
            acquisition.document = duplicate.document
            acquisition.checksum_sha256 = checksum
            acquisition.final_url = access["final_url"]
            acquisition.content_type = access["content_type"]
            acquisition.content_length = access["content_length"]
            acquisition.access_evidence = {**access, "deduplicated_from": str(duplicate.id)}
            acquisition.citations = duplicate.citations
            acquisition.status = SectorResearchAcquisition.Status.COMPLETED
            acquisition.completed_at = timezone.now()
            acquisition.save()
            return duplicate.document

        parsed = urlparse(access["final_url"])
        suffix = PurePosixPath(parsed.path).suffix or ({"application/pdf": ".pdf", "text/html": ".html"}.get(access["content_type"], ".txt"))
        filename = f"{acquisition.recommendation.title[:180]}{suffix}"
        extraction = DocumentProcessorService().get_extraction_result(content, filename)
        extracted_text = str(extraction.get("normalized_text") or extraction.get("extracted_text") or "").strip()
        if not extracted_text:
            raise ResearchAcquisitionError("EXTRACTION_EMPTY", "The acquired document contained no extractable text.")
        artifact = DocumentArtifactService.build_document_artifact(
            file_name=filename,
            extracted_text=extracted_text,
            document_type=DocumentType.OTHER,
            extraction_mode=extraction.get("mode"),
            source_metadata={
                "research_recommendation_id": str(acquisition.recommendation_id),
                "research_acquisition_id": str(acquisition.id),
                "source_url": acquisition.source_url,
                "final_url": access["final_url"],
                "checksum_sha256": checksum,
            },
        )
        document = DealDocument.objects.create(
            deal=acquisition.deal,
            title=acquisition.recommendation.title,
            document_type=DocumentType.OTHER,
            file_url=access["final_url"],
            extracted_text=extracted_text,
            normalized_text=artifact.get("normalized_text") or extracted_text,
            evidence_json=artifact,
            source_map_json=artifact.get("source_map") or {},
            reasoning=artifact.get("reasoning") or "",
            extraction_mode=extraction.get("mode"),
            transcription_status="complete",
        )
        acquisition.document = document
        acquisition.status = SectorResearchAcquisition.Status.ATTACHED
        acquisition.save(update_fields=["document", "status", "updated_at"])
        acquisition.checksum_sha256 = checksum
        acquisition.final_url = access["final_url"]
        acquisition.content_type = access["content_type"]
        acquisition.content_length = access["content_length"]
        acquisition.access_evidence = access
        acquisition.citations = artifact.get("citations") or artifact.get("industry_overview", {}).get("citations") or []
        acquisition.status = SectorResearchAcquisition.Status.EXTRACTING
        acquisition.save()
        indexed = EmbeddingService().vectorize_document(document)
        document.refresh_from_db()
        acquisition.status = SectorResearchAcquisition.Status.COMPLETED if indexed else SectorResearchAcquisition.Status.PARTIAL
        acquisition.error_code = "" if indexed else "INDEXING_FAILED"
        acquisition.error_detail = "" if indexed else (document.reasoning or "Document attached but indexing did not complete.")
        acquisition.completed_at = timezone.now()
        acquisition.save()
        return document

    def execute(self, acquisition: SectorResearchAcquisition):
        acquisition.status = SectorResearchAcquisition.Status.DOWNLOADING
        acquisition.started_at = acquisition.started_at or timezone.now()
        acquisition.error_code = ""
        acquisition.error_detail = ""
        acquisition.save()
        try:
            content, access = self.download(acquisition.source_url)
            return self.attach(acquisition, content, access)
        except Exception as error:
            acquisition.status = SectorResearchAcquisition.Status.FAILED
            acquisition.error_code = getattr(error, "code", "ACQUISITION_FAILED")
            acquisition.error_detail = str(error)
            acquisition.completed_at = timezone.now()
            acquisition.save()
            raise
