import logging
import json
import time
from celery import shared_task, chord
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .models import (
    AnalysisKind,
    ChunkingStatus,
    Deal,
    DealDocument,
    DocumentType,
    ExtractionMode,
    FolderAnalysisDocument,
    InitialAnalysisStatus,
    TranscriptionStatus,
)
from .services.deal_creation import DealCreationService
from .services.document_artifacts import DocumentArtifactService
from microsoft.services.graph_service import GraphAPIService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.realtime import broadcast_audit_log_update, log_worker_event
from ai_orchestrator.services.runtime import AIRuntimeService

logger = logging.getLogger(__name__)

VDR_DOCUMENT_LIMIT = 50
SUPPORTED_ANALYSIS_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".docx", ".doc",
    ".pptx", ".ppt", ".xlsx", ".xls", ".txt", ".csv",
}


def _is_supported_analysis_file(file_info: dict) -> bool:
    name = str(file_info.get("name") or "").lower()
    return any(name.endswith(ext) for ext in SUPPORTED_ANALYSIS_EXTENSIONS)


def _select_folder_analysis_files(file_tree: list[dict], limit: int = VDR_DOCUMENT_LIMIT) -> list[dict]:
    supported_files = [file for file in file_tree if _is_supported_analysis_file(file)]
    candidate_files = supported_files or list(file_tree)
    return candidate_files[:limit]


def _is_cancel_requested(audit_log_id: str | None) -> bool:
    if not audit_log_id:
        return False
    from ai_orchestrator.models import AIAuditLog

    meta = AIAuditLog.objects.filter(id=audit_log_id).values_list("source_metadata", flat=True).first() or {}
    return bool(meta.get("cancel_requested"))


def _mark_vdr_cancelled(audit_log_id: str, deal_id: str, message: str) -> None:
    from ai_orchestrator.models import AIAuditLog

    audit_log = AIAuditLog.objects.filter(id=audit_log_id).first()
    if audit_log:
        audit_log.status = "FAILED"
        audit_log.is_success = False
        audit_log.error_message = message
        source_metadata = audit_log.source_metadata or {}
        audit_log.source_metadata = {
            **source_metadata,
            "cancel_requested": True,
            "cancel_reason": source_metadata.get("cancel_reason", "manual"),
        }
        audit_log.save(update_fields=["status", "is_success", "error_message", "source_metadata"])
        broadcast_audit_log_update(audit_log, event_type="terminal", done=True)

    deal = Deal.objects.filter(id=deal_id).first()
    if deal:
        deal.processing_status = "failed"
        deal.processing_error = message
        deal.save(update_fields=["processing_status", "processing_error"])


def _prepare_vdr_task_ids(signatures, callback_signature) -> tuple[list, object, list[str], str | None]:
    frozen_signatures = []
    child_task_ids: list[str] = []

    for signature in signatures:
        frozen = signature.freeze()
        child_task_ids.append(str(frozen.id))
        frozen_signatures.append(signature)

    callback_task_id = None
    if callback_signature is not None:
        frozen_callback = callback_signature.freeze()
        callback_task_id = str(frozen_callback.id)

    return frozen_signatures, callback_signature, child_task_ids, callback_task_id


def _extract_selected_files(drive_id: str | None, user_email: str, selected_file_ids: list, email_id: str | None = None) -> dict:
    """
    Downloads and extracts text for the selected files, supporting both OneDrive and Email sources.
    """
    graph = GraphAPIService()
    doc_processor = DocumentProcessorService()

    passed_files = []
    failed_files = []
    combined_text_parts = []
    
    email_obj = None
    if email_id:
        from microsoft.models import Email
        email_obj = Email.objects.filter(id=email_id).first()

    for file_id in selected_file_ids:
        file_record = {
            "file_id": file_id,
            "file_name": "unknown_file",
            "status": "failed",
            "extraction_mode": None,
            "transcription_status": TranscriptionStatus.FAILED,
            "chunking_status": ChunkingStatus.NOT_CHUNKED,
            "reason": None,
        }
        try:
            content = None
            name = "unknown_file"
            
            if file_id == "body" and email_obj:
                name = "Email Body"
                content = (email_obj.body_html or email_obj.body_text or email_obj.body_preview or "").encode('utf-8')
            elif drive_id:
                item_info = graph.get_drive_item(drive_id, file_id, user_email=user_email)
                name = item_info.get('name', 'unknown_file')
                logger.info("Selection preflight: downloading OneDrive file %s (%s)", name, file_id)
                content = graph.get_drive_item_content(user_email, file_id, drive_id=drive_id)
            elif email_obj:
                # Find attachment in email
                attachments = email_obj.attachments if isinstance(email_obj.attachments, list) else []
                att = next((a for a in attachments if a.get('id') == file_id), None)
                if att:
                    name = att.get('name', 'unknown_attachment')
                    logger.info("Selection preflight: downloading Email attachment %s (%s)", name, file_id)
                    att_content = graph.get_attachment_content(user_email, email_obj.graph_id, file_id)
                    if 'contentBytes' in att_content:
                        import base64
                        content = base64.b64decode(att_content['contentBytes'])

            file_record["file_name"] = name
            if content is None:
                file_record["reason"] = f"Could not retrieve content for {name}"
                failed_files.append(file_record)
                continue

            logger.info("Selection preflight: extracting %s (%s)", name, file_id)
            extraction = doc_processor.get_extraction_result(content, name, page_limit=None)
            extracted_text = (extraction.get("raw_extracted_text") or extraction.get("text") or "").strip()
            normalized_text = (extraction.get("normalized_text") or extraction.get("text") or extracted_text).strip()
            extraction_mode = extraction.get("mode")

            if not normalized_text:
                file_record["reason"] = extraction.get("error") or f"No readable content extracted for {name}"
                file_record["extraction_mode"] = extraction_mode
                failed_files.append(file_record)
                continue

            file_record["status"] = "passed"
            file_record["extraction_mode"] = extraction_mode
            file_record["transcription_status"] = TranscriptionStatus.COMPLETE
            file_record["text_length"] = len(normalized_text)
            file_record["extracted_text"] = extracted_text
            file_record["normalized_text"] = normalized_text
            file_record["quality_flags"] = extraction.get("quality_flags") or []
            file_record["render_metadata"] = extraction.get("render_metadata") or {}
            passed_files.append(file_record)
            combined_text_parts.append(f"\n--- FILE: {name} ---\n{normalized_text}")
        except Exception as e:
            file_record["reason"] = str(e)
            logger.error("Error reading selected file %s: %s", file_id, e)
            failed_files.append(file_record)

    return {
        "passed_files": passed_files,
        "failed_files": failed_files,
        "combined_text": "".join(combined_text_parts).strip(),
    }


def _build_combined_text(passed_files: list, selected_file_ids: list | None = None) -> str:
    """
    Reconstructs the final model context from preflight-cached extraction output.
    """
    selected_ids = set(selected_file_ids or [])
    parts = []
    for file in passed_files:
        if selected_ids and file.get("file_id") not in selected_ids:
            continue
        combined_text = (file.get("normalized_text") or file.get("extracted_text") or "").strip()
        if not combined_text:
            continue
        parts.append(f"\n--- FILE: {file.get('file_name', 'unknown_file')} ---\n{combined_text}")
    return "".join(parts).strip()


def _build_deal_context_from_files(files: list[dict]) -> str:
    parts = []
    for file in files:
        normalized_text = (file.get("normalized_text") or file.get("extracted_text") or "").strip()
        if not normalized_text:
            continue
        file_name = file.get("file_name") or "unknown_file"
        parts.append(f"\n\n--- DOCUMENT: {file_name} ---\n{normalized_text}")
    return "".join(parts).strip()


def _build_document_evidence_for_files(
    files: list[dict],
    *,
    ai_service,
    audit_log_id: str | None = None,
) -> tuple[list[dict], list[dict]]:
    artifacts: list[dict] = []
    enriched_files: list[dict] = []

    for file in files:
        artifact = DocumentArtifactService.build_document_artifact(
            file_name=file.get("file_name") or "unknown_file",
            extracted_text=file.get("extracted_text") or "",
            document_type=file.get("document_type") or DocumentType.OTHER,
            extraction_mode=file.get("extraction_mode"),
            ai_service=ai_service,
            source_metadata={
                "audit_log_id": audit_log_id,
                "source_id": file.get("file_id"),
                "file_name": file.get("file_name"),
            },
        )
        artifacts.append(artifact)
        enriched_file = dict(file)
        enriched_file["document_artifact"] = artifact
        enriched_file["normalized_text"] = artifact.get("normalized_text") or file.get("extracted_text") or ""
        enriched_file["document_reasoning"] = artifact.get("reasoning") or ""
        enriched_files.append(enriched_file)

    return artifacts, enriched_files


def _build_synthesis_metadata(
    *,
    document_evidence: list[dict],
    supporting_raw_chunks: list[dict],
    extra: dict | None = None,
) -> dict:
    payload = dict(extra or {})
    payload["document_evidence_json"] = json.dumps(document_evidence, default=str)
    payload["supporting_raw_chunks_json"] = json.dumps(supporting_raw_chunks, default=str)
    return payload


def _normalize_synthesis_result(
    analysis: dict | None,
    *,
    analysis_kind: str,
    document_evidence: list[dict],
    analysis_input_files: list[dict],
    failed_files: list[dict],
    previous_snapshot: dict | None = None,
    documents_analyzed: list[str] | None = None,
) -> dict:
    base_analysis = analysis if isinstance(analysis, dict) else {}
    normalized = DealCreationService.normalize_analysis_payload(
        base_analysis,
        previous_snapshot=previous_snapshot,
        analysis_kind=analysis_kind,
        documents_analyzed=documents_analyzed or [
            file.get("file_name") for file in analysis_input_files if file.get("file_name")
        ],
        analysis_input_files=analysis_input_files,
        failed_files=failed_files,
    )
    if not isinstance(normalized.get("document_evidence"), list) or not normalized.get("document_evidence"):
        normalized["document_evidence"] = document_evidence
    metadata = normalized.setdefault("metadata", {})
    metadata["analysis_input_files"] = analysis_input_files
    metadata["failed_files"] = failed_files
    metadata["documents_analyzed"] = documents_analyzed or metadata.get("documents_analyzed") or [
        file.get("file_name") for file in analysis_input_files if file.get("file_name")
    ]
    return normalized


def _resolve_initial_analysis_status(deal: Deal, file_id: str | None, file_name: str | None) -> tuple[str, str | None]:
    analysis = deal.analyses.filter(version=1).first()
    metadata = (analysis.analysis_json or {}).get("metadata", {}) if analysis else {}
    normalized_name = (file_name or "").strip().lower()

    for file in metadata.get("analysis_input_files", []) or metadata.get("passed_files", []):
        if file_id and str(file.get("file_id") or "") == str(file_id):
            return InitialAnalysisStatus.SELECTED_AND_ANALYZED, None
        if normalized_name and str(file.get("file_name") or "").strip().lower() == normalized_name:
            return InitialAnalysisStatus.SELECTED_AND_ANALYZED, None

    for file in metadata.get("failed_files", []):
        if file_id and str(file.get("file_id") or "") == str(file_id):
            return InitialAnalysisStatus.SELECTED_FAILED, file.get("reason")
        if normalized_name and str(file.get("file_name") or "").strip().lower() == normalized_name:
            return InitialAnalysisStatus.SELECTED_FAILED, file.get("reason")

    return InitialAnalysisStatus.NOT_SELECTED, None


def _is_full_transcription(doc: DealDocument) -> bool:
    return (
        doc.transcription_status == TranscriptionStatus.COMPLETE
        and bool((doc.extracted_text or "").strip())
    )


def _prepare_document_update_from_extraction(extraction: dict, *, full: bool) -> dict:
    mode = extraction.get("mode")
    if mode not in {ExtractionMode.DOCPROC_REMOTE, ExtractionMode.VLLM_VISION, ExtractionMode.FALLBACK_TEXT}:
        mode = None
    raw_extracted_text = (extraction.get("raw_extracted_text") or extraction.get("text") or "").strip()
    normalized_text = (extraction.get("normalized_text") or extraction.get("text") or raw_extracted_text).strip()
    return {
        "extracted_text": raw_extracted_text,
        "normalized_text": normalized_text,
        "extraction_mode": mode,
        "transcription_status": (
            TranscriptionStatus.COMPLETE if full and normalized_text else TranscriptionStatus.PARTIAL if normalized_text else TranscriptionStatus.FAILED
        ),
        "last_transcribed_at": timezone.now() if normalized_text else None,
    }


def _vectorize_document_and_capture(doc: DealDocument, embed_service: EmbeddingService) -> int:
    success = embed_service.vectorize_document(doc)
    if not success:
        return 0
    return DocumentChunk.objects.filter(
        deal=doc.deal,
        source_type='document',
        source_id=str(doc.id),
    ).count()


def _sync_deal_extracted_text_for_documents(deal: Deal, docs: list[DealDocument]) -> None:
    existing = deal.extracted_text or ""
    additions = []
    for doc in docs:
        text = (doc.normalized_text or doc.extracted_text or "").strip()
        if not text:
            continue
        marker = f"--- DOCUMENT: {doc.title} ---"
        if marker in existing:
            continue
        additions.append(f"\n\n{marker}\n{text}")
    if additions:
        deal.extracted_text = existing + "".join(additions)
        deal.save(update_fields=['extracted_text'])


def _build_folder_doc_result(file_info: dict, extraction: dict) -> dict:
    extracted_text = (extraction.get("raw_extracted_text") or extraction.get("text") or "").strip()
    normalized_text = (extraction.get("normalized_text") or extraction.get("text") or extracted_text).strip()
    return {
        "file_id": file_info.get("id"),
        "file_name": file_info.get("name") or "unknown_file",
        "path": file_info.get("path"),
        "drive_id": file_info.get("driveId"),
        "status": "passed" if normalized_text else "failed",
        "reason": None if normalized_text else (extraction.get("error") or "No readable content extracted"),
        "extraction_mode": extraction.get("mode"),
        "transcription_status": extraction.get("transcription_status"),
        "chunking_status": ChunkingStatus.NOT_CHUNKED,
        "text_length": len(normalized_text),
        "extracted_text": extracted_text,
        "normalized_text": normalized_text,
        "quality_flags": extraction.get("quality_flags") or [],
        "render_metadata": extraction.get("render_metadata") or {},
    }


def _analysis_document_to_result(doc: FolderAnalysisDocument) -> dict:
    artifact = DocumentArtifactService.artifact_from_analysis_document(doc)
    return {
        "analysis_document_id": str(doc.id),
        "file_id": doc.source_file_id,
        "file_name": doc.file_name,
        "path": doc.file_path,
        "drive_id": doc.source_drive_id,
        "status": "passed" if (doc.normalized_text or "").strip() else "failed",
        "reason": doc.error_message,
        "document_type": doc.document_type,
        "extraction_mode": doc.extraction_mode,
        "transcription_status": doc.transcription_status,
        "chunking_status": doc.chunking_status,
        "text_length": len((doc.normalized_text or "").strip()),
        "extracted_text": (doc.raw_extracted_text or "").strip(),
        "normalized_text": (doc.normalized_text or "").strip(),
        "quality_flags": doc.quality_flags or [],
        "render_metadata": doc.render_metadata or {},
        "document_artifact": artifact,
        "document_reasoning": doc.reasoning or artifact.get("reasoning") or "",
        "chunk_count": doc.chunk_count or 0,
    }


def _persist_folder_analysis_document(
    *,
    audit_log_id: str,
    file_info: dict,
    extraction: dict,
    ai_service,
    embed_service: EmbeddingService,
) -> FolderAnalysisDocument:
    raw_extracted_text = (extraction.get("raw_extracted_text") or extraction.get("text") or "").strip()
    normalized_text = (extraction.get("normalized_text") or extraction.get("text") or raw_extracted_text).strip()
    extraction_mode = extraction.get("mode")
    if extraction_mode not in {ExtractionMode.DOCPROC_REMOTE, ExtractionMode.VLLM_VISION, ExtractionMode.FALLBACK_TEXT}:
        extraction_mode = extraction_mode or None

    defaults = {
        "source_drive_id": file_info.get("driveId") or "",
        "file_name": file_info.get("name") or "unknown_file",
        "file_path": file_info.get("path") or "",
        "document_type": file_info.get("document_type") or DocumentType.OTHER,
        "raw_extracted_text": raw_extracted_text,
        "normalized_text": normalized_text,
        "extraction_mode": extraction_mode,
        "transcription_status": extraction.get("transcription_status") or (
            TranscriptionStatus.COMPLETE if normalized_text else TranscriptionStatus.FAILED
        ),
        "chunking_status": ChunkingStatus.NOT_CHUNKED,
        "quality_flags": extraction.get("quality_flags") or [],
        "render_metadata": extraction.get("render_metadata") or {},
        "error_message": extraction.get("error") if not normalized_text else None,
        "last_transcribed_at": timezone.now() if normalized_text else None,
    }

    analysis_doc, _ = FolderAnalysisDocument.objects.update_or_create(
        audit_log_id=audit_log_id,
        source_file_id=file_info.get("id"),
        defaults=defaults,
    )

    if normalized_text:
        # Check if we already have normalized JSON (from the email pipeline)
        pre_normalized = extraction.get("normalized_json")
        
        if pre_normalized and isinstance(pre_normalized, dict) and "metrics" in pre_normalized:
            # OPTIMIZED: Use the high-fidelity data we already have
            artifact = pre_normalized
        else:
            # LEGACY: Trigger a new analysis pass (standard folder behavior)
            artifact = DocumentArtifactService.build_document_artifact(
                file_name=analysis_doc.file_name,
                extracted_text=raw_extracted_text or normalized_text,
                document_type=analysis_doc.document_type,
                extraction_mode=analysis_doc.extraction_mode,
                ai_service=ai_service,
                source_metadata={
                    "audit_log_id": audit_log_id,
                    "source_id": analysis_doc.source_file_id,
                    "file_name": analysis_doc.file_name,
                },
            )
        
        DocumentArtifactService.persist_analysis_artifact(analysis_doc, artifact)
        embed_service.vectorize_analysis_document(analysis_doc)
        analysis_doc.refresh_from_db()
    else:
        analysis_doc.is_indexed = False
        analysis_doc.chunk_count = 0
        analysis_doc.chunking_status = ChunkingStatus.NOT_CHUNKED
        analysis_doc.save(update_fields=["is_indexed", "chunk_count", "chunking_status"])

    return analysis_doc

@shared_task(bind=True)
def analyze_folder_async(self, drive_id: str, folder_id: str, user_email: str, audit_log_id: str = None):
    """
    Performs direct folder analysis: traversal, extraction of up to the
    configured document limit, and final synthesis.
    """
    logger.info(f"Starting async folder traversal for {folder_id}")
    
    from microsoft.services.graph_service import GraphAPIService
    from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
    from ai_orchestrator.services.ai_processor import AIProcessorService
    
    # 1. Recover or Create Audit Log
    if audit_log_id:
        try:
            audit_log = AIAuditLog.objects.get(id=audit_log_id)
            log_worker_event(audit_log, f"Worker picking up traversal for folder {folder_id}", status='PROCESSING')
        except AIAuditLog.DoesNotExist:
            audit_log_id = None
            
    if not audit_log_id:
        personality = AIPersonality.objects.filter(is_default=True).first()
        skill = AISkill.objects.filter(name='deal_extraction').first()
        
        default_model = AIRuntimeService.get_text_model(personality)
        
        audit_log = AIRuntimeService.create_audit_log(
            source_type='onedrive_folder',
            source_id=folder_id,
            personality=personality,
            skill=skill,
            status='PROCESSING',
            is_success=False,
            model_used=default_model,
            system_prompt="Forensic traversal...",
            user_prompt=f"Traversing folder: {folder_id}",
            celery_task_id=self.request.id,
        )
        log_worker_event(audit_log, f"Initialized traversal log for folder {folder_id}")

    graph = GraphAPIService()
    
    try:
        # 1. Traverse recursively
        log_worker_event(audit_log, "Querying Microsoft Graph for folder tree...")
        file_tree = graph.get_folder_tree(drive_id, folder_id, user_email=user_email)
        if not file_tree:
            log_worker_event(audit_log, "No files found in specified folder.", status='FAILED', done=True)
            return {"error": "No files found"}

        selected_files = _select_folder_analysis_files(file_tree)
        selected_file_ids = [file.get("id") for file in selected_files if file.get("id")]
        log_worker_event(
            audit_log,
            f"Traversal complete. Discovered {len(file_tree)} files. Auto-selecting {len(selected_files)} files for direct analysis.",
            status='PROCESSING',
        )
        audit_log.source_metadata = {
            "file_tree": file_tree,
            "drive_id": drive_id,
            "folder_id": folder_id,
            "total_files": len(file_tree),
            "selected_files_count": len(selected_files),
            "selected_file_ids": selected_file_ids,
            "analysis_input_files": [
                {
                    "file_id": file.get("id"),
                    "file_name": file.get("name"),
                    "path": file.get("path"),
                    "drive_id": file.get("driveId"),
                }
                for file in selected_files
            ],
            "workflow_stage": "analysis_pending",
            "interaction_status": "completed",
            "interaction_mode": "read_only",
        }
        audit_log.save(update_fields=["source_metadata"])

        if not selected_file_ids:
            log_worker_event(audit_log, "Traversal found no supported files for analysis.", status='FAILED', done=True)
            return {"error": "No supported files found for analysis"}

        log_worker_event(audit_log, f"Queueing {len(selected_files)} documents for parallel extraction...")
        tasks = [
            process_single_folder_analysis_document_async.s(file, drive_id, user_email, str(audit_log.id))
            for file in selected_files
        ]
        callback = finalize_folder_analysis_async.s(drive_id, folder_id, user_email, str(audit_log.id))
        _, _, child_task_ids, callback_task_id = _prepare_vdr_task_ids(tasks, callback)
        audit_log.celery_task_id = self.request.id
        audit_log.source_metadata = {
            **(audit_log.source_metadata or {}),
            "child_task_ids": child_task_ids,
            "callback_task_id": callback_task_id,
            "workflow_stage": "analysis_pending",
        }
        audit_log.save(update_fields=["celery_task_id", "source_metadata"])
        chord(tasks)(callback)

        return {
            "status": "queued",
            "phase": "analysis",
            "audit_log_id": str(audit_log.id),
            "folder_id": folder_id,
            "total_files": len(selected_files),
            "file_tree": file_tree,
            "selected_file_ids": selected_file_ids,
            "drive_id": drive_id,
            "user_email": user_email,
        }
    except Exception as e:
        log_worker_event(audit_log, f"Traversal error: {str(e)}", status='FAILED', done=True)
        raise e


@shared_task(bind=True)
def process_single_folder_analysis_document_async(self, file_info: dict, drive_id: str, user_email: str, audit_log_id: str):
    """
    Atomized task for initial folder analysis.
    Downloads, transcribes, and returns a normalized file record.
    """
    from ai_orchestrator.models import AIAuditLog

    file_name = file_info.get("name") or "unknown_file"
    graph = GraphAPIService()
    doc_processor = DocumentProcessorService()
    embed_service = EmbeddingService()
    from ai_orchestrator.services.ai_processor import AIProcessorService

    audit_log = AIAuditLog.objects.filter(id=audit_log_id).first()
    if _is_cancel_requested(audit_log_id):
        return {"status": "cancelled", "file_name": file_name, "file_id": file_info.get("id"), "reason": "manual termination requested"}

    try:
        if audit_log:
            log_worker_event(audit_log, f"Processing document {file_name}", status='PROCESSING')

        content = graph.get_drive_item_content(user_email, file_info.get("id"), drive_id=drive_id)
        extraction = doc_processor.get_extraction_result(content, file_name, page_limit=None)
        ai_service = AIProcessorService()
        analysis_doc = _persist_folder_analysis_document(
            audit_log_id=audit_log_id,
            file_info=file_info,
            extraction=extraction,
            ai_service=ai_service,
            embed_service=embed_service,
        )
        return _analysis_document_to_result(analysis_doc)
    except Exception as e:
        logger.error("Direct folder document processing failed for %s: %s", file_name, e)
        analysis_doc, _ = FolderAnalysisDocument.objects.update_or_create(
            audit_log_id=audit_log_id,
            source_file_id=file_info.get("id"),
            defaults={
                "source_drive_id": file_info.get("driveId") or "",
                "file_name": file_name,
                "file_path": file_info.get("path") or "",
                "document_type": file_info.get("document_type") or DocumentType.OTHER,
                "transcription_status": TranscriptionStatus.FAILED,
                "chunking_status": ChunkingStatus.NOT_CHUNKED,
                "error_message": str(e),
                "quality_flags": ["document_processing_failed"],
            },
        )
        return _analysis_document_to_result(analysis_doc)


@shared_task(bind=True)
def finalize_folder_analysis_async(self, results, drive_id: str, folder_id: str, user_email: str, audit_log_id: str):
    """
    Chord callback for initial direct folder analysis.
    Builds document evidence and final synthesis from atomized document results.
    """
    from ai_orchestrator.models import AIAuditLog
    from ai_orchestrator.services.ai_processor import AIProcessorService

    audit_log = AIAuditLog.objects.get(id=audit_log_id)
    analysis_docs = list(FolderAnalysisDocument.objects.filter(audit_log_id=audit_log_id).order_by("created_at"))
    persisted_results = [_analysis_document_to_result(doc) for doc in analysis_docs]
    passed_files = [result for result in persisted_results if result.get("status") == "passed"]
    failed_files = [result for result in persisted_results if result.get("status") != "passed"]

    if _is_cancel_requested(audit_log_id):
        audit_log.status = 'FAILED'
        audit_log.is_success = False
        audit_log.error_message = "Task manually terminated by forensic user."
        audit_log.source_metadata = {
            **(audit_log.source_metadata or {}),
            "passed_files": passed_files,
            "failed_files": failed_files,
            "workflow_stage": "analysis_complete",
        }
        audit_log.save(update_fields=["status", "is_success", "error_message", "source_metadata"])
        broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
        return {"error": "cancelled"}

    if not passed_files:
        audit_log.status = 'FAILED'
        audit_log.is_success = False
        audit_log.error_message = "No readable content extracted from selected files."
        audit_log.source_metadata = {
            **(audit_log.source_metadata or {}),
            "passed_files": [],
            "failed_files": failed_files,
            "workflow_stage": "analysis_complete",
        }
        audit_log.save(update_fields=["status", "is_success", "error_message", "source_metadata"])
        broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
        return {"error": "No readable content extracted from selected files"}

    log_worker_event(audit_log, f"Building document evidence for {len(passed_files)} readable files...", status='PROCESSING')
    ai_service = AIProcessorService()
    document_evidence, enriched_files = _build_document_evidence_for_files(
        passed_files,
        ai_service=ai_service,
        audit_log_id=str(audit_log.id),
    )
    supporting_raw_chunks = DocumentArtifactService.build_supporting_raw_chunks(document_evidence)
    meta = _build_synthesis_metadata(
        document_evidence=document_evidence,
        supporting_raw_chunks=supporting_raw_chunks,
        extra={
            '_source_metadata': audit_log.source_metadata,
            'audit_log_id': str(audit_log.id),
            'celery_task_id': self.request.id,
            'context_label': audit_log.context_label,
        },
    )

    result = ai_service.process_content(
        content="Synthesize the final deal analysis from the supplied document evidence and supporting raw chunks.",
        skill_name="deal_synthesis",
        source_type='onedrive_folder',
        metadata=meta,
    )
    if isinstance(result, dict) and 'parsed_json' in result:
        analysis = result['parsed_json']
        raw_thinking = result.get('thinking', '')
    else:
        analysis = result if isinstance(result, dict) else {}
        raw_thinking = analysis.get('thinking', '') if isinstance(analysis, dict) else ""

    normalized_analysis = _normalize_synthesis_result(
        analysis,
        analysis_kind=AnalysisKind.INITIAL,
        document_evidence=document_evidence,
        analysis_input_files=enriched_files,
        failed_files=failed_files,
    )
    metadata = normalized_analysis.setdefault("metadata", {})
    metadata["selected_files_count"] = len(persisted_results) or len(results)
    metadata["passed_files_count"] = len(enriched_files)
    metadata["failed_files_count"] = len(failed_files)

    audit_log.source_metadata = {
        **(audit_log.source_metadata or {}),
        "passed_files": enriched_files,
        "failed_files": failed_files,
        "analysis_input_files": enriched_files,
        "workflow_stage": "analysis_complete",
    }
    audit_log.save(update_fields=["source_metadata"])
    log_worker_event(audit_log, f"Direct folder analysis complete for {len(enriched_files)} files.", status='COMPLETED', done=True)

    return {
        "status": "success",
        "phase": "analysis",
        "audit_log_id": str(audit_log.id),
        "folder_id": folder_id,
        "total_files": len(persisted_results) or len(results),
        "preview_files_analyzed": len(enriched_files),
        "preview_text": _build_deal_context_from_files(enriched_files),
        "preliminary_data": normalized_analysis,
        "raw_thinking": raw_thinking,
        "analyzed_files": [file["file_name"] for file in enriched_files],
        "passed_files": enriched_files,
        "failed_files": failed_files,
        "file_tree": (audit_log.source_metadata or {}).get("file_tree", []),
        "selected_file_ids": (audit_log.source_metadata or {}).get("selected_file_ids", []),
        "drive_id": drive_id,
        "user_email": user_email,
    }

@shared_task(bind=True)
def analyze_selection_async(self, session_id: str, audit_log_id: str, selected_file_ids: list):
    """
    Performs AI extraction and deal modeling based on a manual selection of files.
    """
    logger.info("Starting selection analysis for session %s with %s files", session_id, len(selected_file_ids))
    
    from ai_orchestrator.services.ai_processor import AIProcessorService
    from ai_orchestrator.models import AIAuditLog
    
    try:
        audit_log = AIAuditLog.objects.get(id=audit_log_id)
        log_worker_event(audit_log, f"Worker starting AI analysis for {len(selected_file_ids)} documents", status='PROCESSING')
    except AIAuditLog.DoesNotExist:
        logger.error("Audit log %s not found for selection analysis", audit_log_id)
        return {"error": "Audit log not found"}

    try:
        cache_key = f"folder_sync_{session_id}"
        session_data = cache.get(cache_key)
        
        if not session_data:
            log_worker_event(audit_log, f"Session {session_id} expired. Re-open log to retry.", status='FAILED', done=True)
            return {"error": "Session expired"}

        passed_files = session_data.get("passed_files", [])
        failed_files = session_data.get("failed_files", [])
        selected_set = set(selected_file_ids)
        approved_files = [file for file in passed_files if file.get("file_id") in selected_set]

        log_worker_event(audit_log, f"Compiled context for {len(approved_files)} documents. Sending to AI VM...")
        source_type = audit_log.source_type
        audit_log.source_metadata = {
            **(audit_log.source_metadata or {}),
            "selected_files_count": len(selected_file_ids),
            "passed_files_count": len(approved_files),
            "failed_files_count": len(failed_files),
            "selected_file_ids": selected_file_ids,
            "passed_files": passed_files,
            "failed_files": failed_files,
            "analysis_input_files": approved_files,
            "workflow_stage": "analysis_complete",
            "source_type": source_type,
        }
        audit_log.save(update_fields=['source_metadata'])

        analysis = {}
        raw_thinking = ""
        normalized_analysis = {}
        if approved_files:
            ai_service = AIProcessorService()
            document_evidence, enriched_files = _build_document_evidence_for_files(
                approved_files,
                ai_service=ai_service,
                audit_log_id=str(audit_log.id),
            )
            supporting_raw_chunks = DocumentArtifactService.build_supporting_raw_chunks(document_evidence)
            meta = {
                '_source_metadata': audit_log.source_metadata,
                'audit_log_id': str(audit_log.id),
                'celery_task_id': self.request.id,
                'context_label': audit_log.context_label,
            }
            meta = _build_synthesis_metadata(
                document_evidence=document_evidence,
                supporting_raw_chunks=supporting_raw_chunks,
                extra=meta,
            )
            
            result = ai_service.process_content(
                content="Synthesize the final deal analysis from the supplied document evidence and supporting raw chunks.",
                skill_name="deal_synthesis",
                source_type=source_type,
                metadata=meta
            )
            log_worker_event(audit_log, "AI Analysis complete.")
            if isinstance(result, dict) and 'parsed_json' in result:
                analysis = result['parsed_json']
                raw_thinking = result.get('thinking', '')
            else:
                analysis = result if isinstance(result, dict) else {}
                raw_thinking = analysis.get('thinking', '') if isinstance(analysis, dict) else ""
            approved_files = enriched_files
            normalized_analysis = _normalize_synthesis_result(
                analysis,
                analysis_kind=AnalysisKind.INITIAL,
                document_evidence=document_evidence,
                analysis_input_files=approved_files,
                failed_files=failed_files,
            )

        if isinstance(normalized_analysis, dict) and normalized_analysis:
            metadata = normalized_analysis.setdefault("metadata", {})
            metadata["selected_files_count"] = len(selected_file_ids)
            metadata["passed_files_count"] = len(approved_files)
            metadata["failed_files_count"] = len(failed_files)
            metadata["passed_files"] = passed_files
            metadata["failed_files"] = failed_files
            metadata["analysis_input_files"] = approved_files
            normalized_analysis["document_evidence"] = normalized_analysis.get("document_evidence") if isinstance(normalized_analysis.get("document_evidence"), list) else document_evidence if 'document_evidence' in locals() else []
            normalized_analysis["missing_information_requests"] = normalized_analysis.get("missing_information_requests") if isinstance(normalized_analysis.get("missing_information_requests"), list) else []
            normalized_analysis["cross_document_conflicts"] = normalized_analysis.get("cross_document_conflicts") if isinstance(normalized_analysis.get("cross_document_conflicts"), list) else []

        return {
            "phase": "analysis",
            "status": "success",
            "folder_id": session_data.get("folder_id"),
            "total_files": len(selected_file_ids),
            "preview_files_analyzed": len(approved_files),
            "preview_text": _build_deal_context_from_files(approved_files),
            "preliminary_data": normalized_analysis if normalized_analysis else analysis,
            "raw_thinking": raw_thinking,
            "analyzed_files": [file["file_name"] for file in approved_files],
            "passed_files": approved_files,
            "failed_files": failed_files,
            "drive_id": session_data.get("drive_id"),
            "user_email": session_data.get("user_email")
        }
    except Exception as e:
        audit_log.status = 'FAILED'
        audit_log.error_message = str(e)
        audit_log.save()
        broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
        raise e


@shared_task(bind=True)
def preflight_selection_async(self, drive_id: str | None, folder_id: str | None, user_email: str, audit_log_id: str, selected_file_ids: list, session_id: str):
    """
    Performs a read/extraction preflight on selected files before the user confirms the Qwen analysis.
    Supports both OneDrive and Email sources.
    """
    from ai_orchestrator.models import AIAuditLog

    try:
        audit_log = AIAuditLog.objects.get(id=audit_log_id)
        log_worker_event(audit_log, f"Worker starting preflight for {len(selected_file_ids)} files", status='PROCESSING')
    except AIAuditLog.DoesNotExist:
        return {"error": "Audit log not found"}

    try:
        cache_key = f"folder_sync_{session_id}"
        session_data = cache.get(cache_key) or {}
        
        email_id = session_data.get("email_id")
        source_type = audit_log.source_type

        log_worker_event(audit_log, f"Downloading and extracting selected {source_type} content...")
        extraction_result = _extract_selected_files(drive_id, user_email, selected_file_ids, email_id=email_id)
        passed_files = extraction_result["passed_files"]
        failed_files = extraction_result["failed_files"]

        log_worker_event(audit_log, f"Preflight results: {len(passed_files)} PASSED, {len(failed_files)} FAILED", status='COMPLETED', done=True)
        audit_log.is_success = True
        audit_log.source_metadata = {
            **(audit_log.source_metadata or {}),
            "selected_files_count": len(selected_file_ids),
            "passed_files_count": len(passed_files),
            "failed_files_count": len(failed_files),
            "selected_file_ids": selected_file_ids,
            "passed_files": passed_files,
            "failed_files": failed_files,
            "workflow_stage": "preflight_complete",
            "interaction_status": "pending",
            "interaction_mode": "editable",
            "source_type": source_type
        }
        audit_log.raw_response = (
            f"Preflight completed for {len(selected_file_ids)} selected files.\n"
            f"Passed: {len(passed_files)}\nFailed: {len(failed_files)}"
        )
        audit_log.save()
        broadcast_audit_log_update(audit_log, event_type="terminal", done=True)

        session_data.update({
            "folder_id": folder_id,
            "drive_id": drive_id,
            "email_id": email_id,
            "source_type": source_type,
            "user_email": user_email,
            "selected_file_ids": selected_file_ids,
            "passed_files": passed_files,
            "failed_files": failed_files,
            "preflight_audit_log_id": str(audit_log.id),
        })
        cache.set(cache_key, session_data, timeout=3600)
        logger.info("Session %s updated in cache with %s passed files. Source: %s", session_id, len(passed_files), source_type)

        return {
            "phase": "preflight",
            "status": "success",
            "audit_log_id": str(audit_log.id),
            "session_id": session_id,
            "source_type": source_type,
            "selected_files_count": len(selected_file_ids),
            "passed_files_count": len(passed_files),
            "failed_files_count": len(failed_files),
            "passed_files": passed_files,
            "failed_files": failed_files,
            "drive_id": drive_id,
            "folder_id": folder_id,
            "email_id": email_id,
            "user_email": user_email,
        }
    except Exception as e:
        audit_log.status = 'FAILED'
        audit_log.error_message = str(e)
        audit_log.save()
        broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
        raise e

@shared_task(bind=True)
def process_single_document_async(self, file_info, deal_id, user_email, is_preview, audit_log_id=None):
    """
    Atomized task to process a single document from OneDrive.
    """
    logger.info(f"Atomized Task: Processing file {file_info.get('name')} for Deal {deal_id}")
    
    from ai_orchestrator.services.ai_processor import AIProcessorService

    graph_service = GraphAPIService()
    doc_processor = DocumentProcessorService()
    embed_service = EmbeddingService()
    ai_service = AIProcessorService()
    
    file_id = file_info.get('id')
    file_name = file_info.get('name')
    drive_id = file_info.get('driveId')
    
    if not file_id or not file_name:
        return {"status": "skipped", "reason": "missing file info"}

    try:
        if _is_cancel_requested(audit_log_id):
            return {"status": "cancelled", "file": file_name, "reason": "manual termination requested"}

        deal = Deal.objects.get(id=deal_id)
        
        # Avoid duplicates
        if DealDocument.objects.filter(deal=deal, onedrive_id=file_id).exists():
            return {"status": "skipped", "reason": "already processed", "file": file_name}
            
        # Determine document type
        doc_type = DocumentType.OTHER
        name_lower = file_name.lower()
        if any(k in name_lower for k in ['financial', 'mis', 'model', 'projection']): 
            doc_type = DocumentType.FINANCIALS
        elif any(k in name_lower for k in ['legal', 'sha', 'ssa', 'term sheet']): 
            doc_type = DocumentType.LEGAL
        elif any(k in name_lower for k in ['teaser', 'deck', 'pitch', 'im']): 
            doc_type = DocumentType.PITCH_DECK

        if _is_cancel_requested(audit_log_id):
            return {"status": "cancelled", "file": file_name, "reason": "manual termination requested"}

        # Download content
        content = graph_service.get_drive_item_content(user_email, file_id, drive_id=drive_id)

        if _is_cancel_requested(audit_log_id):
            return {"status": "cancelled", "file": file_name, "reason": "manual termination requested"}

        # Background VDR sync stores a preview transcription only.
        extraction = doc_processor.get_extraction_result(content, file_name, page_limit=2)
        extracted_text = (extraction.get("raw_extracted_text") or extraction.get("text") or "").strip()
        normalized_text = (extraction.get("normalized_text") or extraction.get("text") or extracted_text).strip()

        if _is_cancel_requested(audit_log_id):
            return {"status": "cancelled", "file": file_name, "reason": "manual termination requested"}
        
        # Create Document Record
        initial_analysis_status, initial_analysis_reason = _resolve_initial_analysis_status(deal, file_id, file_name)
        doc = DealDocument.objects.create(
            deal=deal,
            title=file_name,
            document_type=doc_type,
            onedrive_id=file_id,
            extracted_text=extracted_text,
            normalized_text=normalized_text,
            is_indexed=False,
            is_ai_analyzed=False,
            initial_analysis_status=initial_analysis_status,
            initial_analysis_reason=initial_analysis_reason,
            extraction_mode=extraction.get("mode"),
            transcription_status=TranscriptionStatus.PARTIAL if normalized_text else TranscriptionStatus.FAILED,
            chunking_status=ChunkingStatus.NOT_CHUNKED,
            last_transcribed_at=timezone.now() if normalized_text else None,
        )

        if normalized_text:
            doc.save(update_fields=["normalized_text"])
            DocumentArtifactService.ensure_document_artifact(doc, ai_service=ai_service, force=True)
        
        # Update combined deal text (Race condition warning: multiple tasks appending to the same field)
        if normalized_text:
            with transaction.atomic():
                # Re-fetch deal within transaction to minimize race condition window
                deal_locked = Deal.objects.select_for_update().get(id=deal_id)
                aggregate_text = doc.normalized_text or normalized_text
                new_context = f"\n\n--- DOCUMENT: {file_name} ---\n{aggregate_text}"
                if not deal_locked.extracted_text:
                    deal_locked.extracted_text = new_context
                else:
                    deal_locked.extracted_text += new_context
                deal_locked.save(update_fields=['extracted_text'])
        
        # Vectorize for RAG
        if normalized_text and len(normalized_text.strip()) > 50 and not _is_cancel_requested(audit_log_id):
            _vectorize_document_and_capture(doc, embed_service)
            
        return {"status": "success", "file": file_name}
        
    except Exception as e:
        logger.error(f"Error processing {file_name}: {str(e)}")
        return {"status": "failed", "file": file_name, "error": str(e)}

@shared_task(bind=True)
def finalize_folder_background(self, results, deal_id, audit_log_id):
    """
    Callback task to finalize the deal and audit log once all documents are processed.
    """
    logger.info(f"Finalizing VDR Indexing for Deal {deal_id}")
    from ai_orchestrator.models import AIAuditLog
    
    try:
        deal = Deal.objects.get(id=deal_id)
        audit_log = AIAuditLog.objects.get(id=audit_log_id)
        cancellation_message = "Task manually terminated by forensic user."

        if _is_cancel_requested(audit_log_id) or any(r.get('status') == 'cancelled' for r in results):
            _mark_vdr_cancelled(audit_log_id, deal_id, cancellation_message)
            logger.info("VDR indexing cancelled for Deal %s", deal_id)
            return {"processed": 0, "errors": 0, "cancelled": True}
        
        errors = [r for r in results if r.get('status') == 'failed']
        processed_count = len([r for r in results if r.get('status') == 'success'])
        
        deal.processing_status = 'completed' if not errors else 'failed'
        if errors:
            error_msgs = [f"{e['file']}: {e['error']}" for e in errors]
            deal.processing_error = "; ".join(error_msgs)
        deal.save(update_fields=['processing_status', 'processing_error'])
        
        audit_log.status = 'COMPLETED' if not errors else 'FAILED'
        audit_log.is_success = True if not errors else False
        audit_log.system_prompt = f"Successfully indexed {processed_count} documents into the VDR."
        if errors:
            audit_log.error_message = f"Errors encountered in {len(errors)} files."
            
        audit_log.save()
        logger.info(f"Finished VDR Indexing for Deal {deal_id}. Processed {processed_count} files.")
        return {"processed": processed_count, "errors": len(errors)}
        
    except Exception as e:
        logger.error(f"Failed to finalize deal {deal_id}: {str(e)}")
        raise e

@shared_task(bind=True, max_retries=3)
def process_deal_folder_background(self, deal_id: str, file_tree_map: list, user_email: str):
    """
    Background task to download and vectorize all remaining files in a folder tree using a chord.
    """
    limited_file_tree = list(file_tree_map[:VDR_DOCUMENT_LIMIT])
    logger.info(f"Starting background processing for Deal {deal_id} with {len(limited_file_tree)} of {len(file_tree_map)} files.")
    
    from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
    personality = AIPersonality.objects.filter(is_default=True).first()
    
    # 1. Create PENDING audit log for indexing
    audit_log = AIRuntimeService.create_audit_log(
        source_type='vdr_indexing',
        source_id=deal_id,
        personality=personality,
        status='PROCESSING',
        is_success=False,
        model_used=AIRuntimeService.get_embedding_model(),
        system_prompt=f"Starting background vectorization for {len(limited_file_tree)} files via chord.",
        user_prompt=f"Indexing dataroom for deal ID: {deal_id}",
        celery_task_id=self.request.id,
    )

    try:
        deal = Deal.objects.get(id=deal_id)
    except Deal.DoesNotExist:
        logger.error(f"Deal {deal_id} not found. Aborting task.")
        audit_log.status = 'FAILED'
        audit_log.error_message = "Deal not found"
        audit_log.save()
        return

    # Update status to processing
    deal.processing_status = 'processing'
    deal.save(update_fields=['processing_status'])

    graph_service = GraphAPIService()
    
    try:
        graph_service.get_access_token(user_email, require_delegated=True)
    except Exception as e:
        logger.error(f"Failed to acquire delegated token for {user_email}: {e}")
        deal.processing_status = 'failed'
        deal.processing_error = f"Authentication Error: {str(e)}"
        deal.save()
        
        audit_log.status = 'FAILED'
        audit_log.error_message = f"Auth Error: {str(e)}"
        audit_log.save()
        return

    if _is_cancel_requested(str(audit_log.id)):
        _mark_vdr_cancelled(str(audit_log.id), deal_id, "Task manually terminated by forensic user.")
        return {"status": "cancelled", "task_count": 0}

    # Dispatch chord
    tasks = [process_single_document_async.s(f, deal_id, user_email, i < 5, str(audit_log.id)) for i, f in enumerate(limited_file_tree)]
    callback = finalize_folder_background.s(deal_id, str(audit_log.id))
    _, _, child_task_ids, callback_task_id = _prepare_vdr_task_ids(tasks, callback)
    audit_log.source_metadata = {
        **(audit_log.source_metadata or {}),
        "child_task_ids": child_task_ids,
        "callback_task_id": callback_task_id,
        "document_limit": VDR_DOCUMENT_LIMIT,
        "requested_task_count": len(file_tree_map),
        "task_count": len(tasks),
    }
    audit_log.save(update_fields=["source_metadata"])
    chord(tasks)(callback)
    
    logger.info(f"Dispatched chord with {len(tasks)} tasks for Deal {deal_id}")
    return {"status": "dispatched", "task_count": len(tasks)}

@shared_task(bind=True)
def analyze_additional_documents_async(self, deal_id: str, document_ids: list, audit_log_id: str):
    """
    Background task for incremental (V2+) deal analysis.
    """
    from .models import Deal, DealDocument
    from ai_orchestrator.models import AIAuditLog
    from ai_orchestrator.services.ai_processor import AIProcessorService
    from ai_orchestrator.services.document_processor import DocumentProcessorService
    from microsoft.services.graph_service import GraphAPIService, DMS_USER_EMAIL
    from django.utils import timezone
    import json

    logger.info(f"Starting incremental analysis for Deal {deal_id}")
    
    try:
        deal = Deal.objects.get(id=deal_id)
        audit_log = AIAuditLog.objects.get(id=audit_log_id)
        audit_log.celery_task_id = self.request.id
        audit_log.status = 'PROCESSING'
        audit_log.save()

        docs = list(DealDocument.objects.filter(id__in=document_ids, deal=deal))
        doc_processor = DocumentProcessorService()
        graph = GraphAPIService()
        embed_service = EmbeddingService()
        diagnostics = []
        prepared_docs = []
        document_evidence = []
        ai_service = AIProcessorService()

        for doc in docs:
            file_diag = {
                "document_id": str(doc.id),
                "file_name": doc.title,
                "selected_for_run": True,
                "used_cached_text": False,
                "transcription_mode": doc.extraction_mode,
                "transcription_status": doc.transcription_status,
                "chunking_status": doc.chunking_status,
                "chunk_count": 0,
                "analysis_included": False,
                "error": None,
            }
            try:
                requires_full_transcription = (
                    not _is_full_transcription(doc)
                    or len((doc.extracted_text or "").strip()) < 50
                )
                if requires_full_transcription and doc.onedrive_id:
                    logger.info(f"[TASK] Performing full document transcription for: {doc.title}")
                    content = graph.get_drive_item_content(
                        user_email=DMS_USER_EMAIL,
                        file_id=doc.onedrive_id,
                        drive_id=deal.source_drive_id,
                    )
                    extraction = doc_processor.get_extraction_result(content, doc.title, page_limit=None)
                    update = _prepare_document_update_from_extraction(extraction, full=True)
                    doc.extracted_text = update["extracted_text"]
                    doc.normalized_text = update["normalized_text"]
                    doc.extraction_mode = update["extraction_mode"]
                    doc.transcription_status = update["transcription_status"]
                    doc.last_transcribed_at = update["last_transcribed_at"]
                    doc.save(update_fields=['extracted_text', 'normalized_text', 'extraction_mode', 'transcription_status', 'last_transcribed_at'])
                else:
                    file_diag["used_cached_text"] = True

                file_diag["transcription_mode"] = doc.extraction_mode
                file_diag["transcription_status"] = doc.transcription_status

                document_text = (doc.normalized_text or doc.extracted_text or "").strip()
                if document_text:
                    DocumentArtifactService.ensure_document_artifact(doc, ai_service=ai_service, force=requires_full_transcription)
                    chunk_count = _vectorize_document_and_capture(doc, embed_service)
                    file_diag["chunk_count"] = chunk_count
                    file_diag["chunking_status"] = doc.chunking_status
                    file_diag["analysis_included"] = True
                    prepared_docs.append(doc)
                    document_evidence.append(DocumentArtifactService.artifact_from_document(doc))
                else:
                    doc.transcription_status = TranscriptionStatus.FAILED
                    doc.save(update_fields=['transcription_status'])
                    file_diag["transcription_status"] = doc.transcription_status
                    file_diag["error"] = "No readable text extracted"
            except Exception as e:
                logger.error(f"Failed to fully transcribe document {doc.title}: {e}")
                doc.transcription_status = TranscriptionStatus.FAILED
                doc.chunking_status = ChunkingStatus.FAILED
                doc.save(update_fields=['transcription_status', 'chunking_status'])
                file_diag["transcription_status"] = doc.transcription_status
                file_diag["chunking_status"] = doc.chunking_status
                file_diag["error"] = str(e)
            diagnostics.append(file_diag)

        if not document_evidence:
            audit_log.status = 'FAILED'
            audit_log.error_message = "No text extracted from selected documents."
            audit_log.source_metadata = {
                **(audit_log.source_metadata or {}),
                "file_diagnostics": diagnostics,
            }
            audit_log.save(update_fields=['status', 'error_message', 'source_metadata'])
            return {"error": "No text extracted"}

        existing_canonical_snapshot = ((deal.current_analysis or {}).get("canonical_snapshot") or {}) if hasattr(deal, "current_analysis") else {}
        existing_summary = (existing_canonical_snapshot.get("analyst_report") or deal.deal_summary or "")
        
        # Calculate next version from existing analyses
        from .models import DealAnalysis
        latest_analysis = deal.analyses.order_by('-version').first()
        current_version = (latest_analysis.version + 1) if latest_analysis else 2

        supporting_raw_chunks = DocumentArtifactService.build_supporting_raw_chunks(document_evidence)
        result = ai_service.process_content(
            content="Generate a supplemental analysis focused only on the supplied new document evidence and raw chunks.",
            skill_name="vdr_incremental_analysis",
            source_type="vdr_incremental_analysis",
            source_id=str(deal.id),
            metadata=_build_synthesis_metadata(
                document_evidence=document_evidence,
                supporting_raw_chunks=supporting_raw_chunks,
                extra={
                'audit_log_id': str(audit_log.id),
                'existing_summary': existing_summary,
                'existing_canonical_snapshot': json.dumps(existing_canonical_snapshot, default=str),
                'version_num': current_version
                },
            )
        )

        analysis = {}
        raw_thinking = ""
        if isinstance(result, dict) and 'parsed_json' in result:
            analysis = result['parsed_json']
            raw_thinking = result.get('thinking', '')
        else:
            analysis = result
            raw_thinking = analysis.get('thinking', '') if isinstance(analysis, dict) else ""

        if analysis and "error" not in analysis:
            normalized_analysis = _normalize_synthesis_result(
                analysis,
                previous_snapshot=existing_canonical_snapshot,
                analysis_kind=AnalysisKind.SUPPLEMENTAL,
                document_evidence=document_evidence,
                documents_analyzed=[doc.title for doc in prepared_docs if doc.title],
                analysis_input_files=[
                    {
                        "file_id": str(doc.onedrive_id or doc.id),
                        "file_name": doc.title,
                    }
                    for doc in prepared_docs
                ],
                failed_files=[],
            )
            # Create a NEW DealAnalysis record for this version
            DealAnalysis.objects.create(
                deal=deal,
                version=current_version,
                analysis_kind=AnalysisKind.SUPPLEMENTAL,
                thinking=raw_thinking,
                ambiguities=normalized_analysis.get('metadata', {}).get('ambiguous_points', []),
                analysis_json=normalized_analysis
            )

            DealCreationService.apply_analysis_to_deal(
                deal,
                normalized_analysis,
                overwrite=False,
                overwrite_themes=True,
            )
            DealDocument.objects.filter(id__in=[doc.id for doc in prepared_docs]).update(is_ai_analyzed=True)
            _sync_deal_extracted_text_for_documents(deal, prepared_docs)
            
            audit_log.status = 'COMPLETED'
            audit_log.is_success = True
            audit_log.source_metadata = {
                **(audit_log.source_metadata or {}),
                "file_diagnostics": diagnostics,
            }
            audit_log.save(update_fields=['status', 'is_success', 'source_metadata'])
            return {"status": "success", "version": current_version}
        else:
            error_msg = str(analysis.get('error', 'AI Output invalid'))
            audit_log.status = 'FAILED'
            audit_log.error_message = error_msg
            audit_log.source_metadata = {
                **(audit_log.source_metadata or {}),
                "file_diagnostics": diagnostics,
            }
            audit_log.save(update_fields=['status', 'error_message', 'source_metadata'])
            return {"error": error_msg}

    except Exception as e:
        logger.error(f"Incremental analysis failed: {str(e)}")
        if 'audit_log' in locals():
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.save()
        raise e


@shared_task(bind=True)
def process_single_thread_document_async(self, file_info: dict, deal_id: str, user_email: str, audit_log_id: str):
    """
    Atomized task for email thread analysis.
    Downloads, transcribes, normalizes into strict JSON, and returns a file record.
    """
    from ai_orchestrator.models import AIAuditLog
    from microsoft.models import Email
    from ai_orchestrator.services.ai_processor import AIProcessorService
    from ai_orchestrator.services.document_processor import DocumentProcessorService
    from ai_orchestrator.services.embedding_processor import EmbeddingService

    file_name = file_info.get("name") or "unknown_file"
    file_id = file_info.get("id")
    email_id = file_info.get("email_id")
    
    graph = GraphAPIService()
    doc_processor = DocumentProcessorService()
    ai_service = AIProcessorService()
    embed_service = EmbeddingService()
    
    audit_log = AIAuditLog.objects.filter(id=audit_log_id).first()

    try:
        if audit_log:
            log_worker_event(audit_log, f"Processing document: {file_name}", status='PROCESSING')

        extraction = {}
        raw_markdown = ""

        is_body = file_info.get("is_body", False)

        if is_body:
            # OPTIMIZED PATH: Extract text directly from DB/HTML (Zero Loss)
            email = Email.objects.get(id=email_id)
            raw_body = email.body_html or email.body_text or email.body_preview or ""
            
            # Pre-clean HTML to save tokens and prevent model confusion
            import re
            def fast_strip_html(text):
                if not text: return ""
                t = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
                t = re.sub(r'<[^>]+>', ' ', t)
                return re.sub(r'\s+', ' ', t).strip()
            
            clean_body = fast_strip_html(raw_body)
            
            # CHUNKING LOGIC: If body is massive (> 40k chars), chunk it
            MAX_BODY_CHARS = 40000
            if len(clean_body) > MAX_BODY_CHARS:
                log_worker_event(audit_log, f"Email body is massive ({len(clean_body)} chars). Chunking...", status='PROCESSING')
                chunks = [clean_body[i:i + 35000] for i in range(0, len(clean_body), 30000)]
                unrolled_parts = []
                for idx, chunk in enumerate(chunks):
                    log_worker_event(audit_log, f"Unrolling chunk {idx+1}/{len(chunks)}...", status='PROCESSING')
                    chunk_res = ai_service.process_content(
                        content=chunk, 
                        skill_name="email_unroll", 
                        source_type="email",
                        metadata={"chat_template_kwargs": {"enable_thinking": False}}
                    )
                    unrolled_parts.append(chunk_res.get('response') or chunk_res.get('text') or "")
                raw_markdown = "\n\n--- THREAD CONTINUATION ---\n\n".join(unrolled_parts)
            else:
                log_worker_event(audit_log, f"Unrolling email thread history: {file_name}", status='PROCESSING')
                unroll_result = ai_service.process_content(
                    content=clean_body, 
                    skill_name="email_unroll", 
                    source_type="email",
                    metadata={"chat_template_kwargs": {"enable_thinking": False}}
                )
                raw_markdown = unroll_result.get('response') or unroll_result.get('text') or ""
            
            # DEBUG LOG
            if raw_markdown:
                log_worker_event(audit_log, f"Successfully unrolled thread history.", status='PROCESSING')
            
            extraction = {
                "normalized_text": raw_markdown,
                "text": raw_markdown,
                "mode": "text_direct",
                "transcription_status": "complete" if raw_markdown else "failed"
            }
        else:
            # LEGACY PATH: Binary files (PDF, Excel, etc.) go to docproc
            email = Email.objects.get(id=email_id)
            real_file_id = file_id # For attachments, the id is already correct
            att_content = graph.get_attachment_content(user_email, email.graph_id, real_file_id)
            content = None
            if 'contentBytes' in att_content:
                import base64
                content = base64.b64decode(att_content['contentBytes'])

            if not content:
                raise ValueError(f"Could not retrieve content for {file_name}")

            extraction = doc_processor.get_extraction_result(content, file_name, page_limit=None)
            raw_markdown = extraction.get("normalized_text") or extraction.get("text") or ""
        
        # 2. Normalization (vLLM Qwen)
        log_worker_event(audit_log, f"Normalizing extracted evidence: {file_name}", status='PROCESSING')
        norm_result = ai_service.process_content(
            content=raw_markdown,
            skill_name="document_normalization",
            source_type="normalization",
            metadata={"chat_template_kwargs": {"enable_thinking": False}}
        )
        
        # FIX: result is already the parsed JSON dict
        normalized_json = norm_result if isinstance(norm_result, dict) else {}
        
        # 3. Persist as DealDocument & DocumentChunk
        # We repurpose _persist_folder_analysis_document but pass normalized data
        analysis_doc = _persist_folder_analysis_document(
            audit_log_id=audit_log_id,
            file_info={**file_info, "deal_id": deal_id},
            extraction={**extraction, "normalized_json": normalized_json},
            ai_service=ai_service,
            embed_service=embed_service,
        )
        
        return {
            **_analysis_document_to_result(analysis_doc),
            "normalized_json": normalized_json
        }
    except Exception as e:
        logger.error(f"Thread document processing failed for {file_name}: {e}")
        return {"status": "failed", "file_name": file_name, "error": str(e)}


@shared_task(bind=True)
def finalize_thread_analysis_async(self, results, deal_id: str | None, audit_log_id: str):
    """
    Chord callback for autonomous email thread analysis.
    Synthesizes the finalized deal state from all parallel document extractions.
    """
    from ai_orchestrator.models import AIAuditLog
    from ai_orchestrator.services.ai_processor import AIProcessorService
    from deals.models import Deal
    from deals.services.deal_creation import DealCreationService

    audit_log = AIAuditLog.objects.get(id=audit_log_id)
    deal = Deal.objects.filter(id=deal_id).first() if deal_id else None
    ai_service = AIProcessorService()
    
    passed_results = [r for r in results if r.get("status") == "passed"]
    
    if not passed_results:
        log_worker_event(audit_log, "No documents successfully processed in thread.", status='FAILED', done=True)
        return {"error": "No documents processed"}

    log_worker_event(audit_log, f"Synthesizing thread intelligence from {len(passed_results)} documents...", status='PROCESSING')
    
    # Gather all normalized data for the final prompt
    intelligence_context = []
    for r in passed_results:
        intelligence_context.append({
            "name": r.get("file_name"),
            "full_text": r.get("normalized_text"), # The unrolled chronological history
            "intel": r.get("normalized_json")      # The structured metrics/facts
        })

    # Add proposed intelligence if available
    proposed_intel = (audit_log.source_metadata or {}).get("proposed_intel", {})
    if proposed_intel:
        intelligence_context.insert(0, {
            "name": "ROUTING_PROPOSAL",
            "intel": proposed_intel
        })

    # HIERARCHICAL BUCKETING (Institutional Fusion v46 logic)
    # Chars limit approx 60k for safe 32k token window
    CONTEXT_SAFE_CHARS = 60000 
    full_context_json = json.dumps(intelligence_context, default=str)
    
    if len(full_context_json) > CONTEXT_SAFE_CHARS:
        log_worker_event(audit_log, f"Context is too large ({len(full_context_json)} chars). Performing hierarchical synthesis...", status='PROCESSING')
        
        # Split into buckets (approx 45k chars each)
        buckets = []
        current_bucket = []
        current_len = 0
        for item in intelligence_context:
            item_str = json.dumps(item, default=str)
            if current_len + len(item_str) > 45000 and current_bucket:
                buckets.append(current_bucket)
                current_bucket = []
                current_len = 0
            current_bucket.append(item)
            current_len += len(item_str)
        if current_bucket:
            buckets.append(current_bucket)
            
        bucket_summaries = []
        for idx, bucket in enumerate(buckets):
            log_worker_event(audit_log, f"Fusing intermediate bucket {idx+1}/{len(buckets)}...", status='PROCESSING')
            bucket_res = ai_service.process_content(
                content=json.dumps(bucket, default=str),
                skill_name="email_intermediate_fusion", # Use dedicated fusion skill
                source_type="email",
                metadata={
                    "audit_log_id": audit_log_id, 
                    "temperature": 0.0,
                    "chat_template_kwargs": {"enable_thinking": False}
                }
            )
            # We want the text response of the summary
            summary = bucket_res.get('response') or bucket_res.get('text') or ""
            bucket_summaries.append(f"[BUCKET {idx+1} SUMMARY]\n{summary}")
            
        final_content = "\n\n".join(bucket_summaries)
    else:
        final_content = full_context_json

    # DEBUG: Log the exact JSON payload being sent for final synthesis
    log_worker_event(audit_log, f"Starting final synthesis pass. Context Payload (First 3000 chars):\n{final_content[:3000]}", status='PROCESSING')

    # 3. FINAL FUSION
    # We now fetch the dynamic schema from the skill if possible, or use a default
    DEAL_SCHEMA = {
        "type": "object",
        "properties": {
            "deal_model_data": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "industry": {"type": "string"},
                    "sector": {"type": "string"},
                    "funding_ask": {"type": "string"},
                    "funding_ask_for": {"type": "string"},
                    "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "city": {"type": "string"},
                    "state": {"type": "string"},
                    "country": {"type": "string"},
                    "themes": {"type": "array", "items": {"type": "string"}},
                    "is_female_led": {"type": "boolean"},
                    "deal_summary": {"type": "string"},
                    "deal_details": {"type": "string"},
                    "company_details": {"type": "string"},
                    "priority_rationale": {"type": "string"}
                },
                "required": [
                    "title", "industry", "sector", "funding_ask", "funding_ask_for", 
                    "priority", "city", "state", "country", "themes", 
                    "is_female_led", "deal_summary", "deal_details", "company_details", "priority_rationale"
                ],
                "additionalProperties": False
            },
            "source_relationships": {
                "type": "object",
                "properties": {
                    "bank": {
                        "type": "object",
                        "properties": {
                            "name": {"type": ["string", "null"]},
                            "website_domain": {"type": ["string", "null"]},
                            "description": {"type": ["string", "null"]}
                        },
                        "required": ["name", "website_domain", "description"],
                        "additionalProperties": False
                    },
                    "primary_contact": {
                        "type": ["object", "null"],
                        "properties": {
                            "name": {"type": ["string", "null"]},
                            "email": {"type": ["string", "null"]},
                            "designation": {"type": ["string", "null"]},
                            "linkedin_url": {"type": ["string", "null"]}
                        },
                        "required": ["name", "email", "designation", "linkedin_url"],
                        "additionalProperties": False
                    },
                    "additional_contacts": {"type": "array", "items": {"type": "object"}},
                    "relationship_metadata": {
                        "type": "object",
                        "properties": {
                            "source_type": {"type": ["string", "null"]},
                            "source_documents": {"type": "array", "items": {"type": "string"}},
                            "confidence": {"type": ["string", "null"], "enum": ["High", "Medium", "Low", None]},
                            "ambiguities": {"type": "array", "items": {"type": "string"}}
                        },
                        "required": ["source_type", "source_documents", "confidence", "ambiguities"],
                        "additionalProperties": False
                    }
                },
                "required": ["bank", "primary_contact", "additional_contacts", "relationship_metadata"],
                "additionalProperties": False
            },
            "analyst_report": {"type": "string"},
            "metadata": {
                "type": "object",
                "properties": {
                    "ambiguous_points": {"type": "array", "items": {"type": "string"}},
                    "documents_analyzed": {"type": "array", "items": {"type": "string"}},
                    "cross_document_conflicts": {"type": "array", "items": {"type": "object"}},
                    "missing_information_requests": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["ambiguous_points", "documents_analyzed", "cross_document_conflicts", "missing_information_requests"],
                "additionalProperties": False
            }
        },
        "required": ["deal_model_data", "source_relationships", "analyst_report", "metadata"],
        "additionalProperties": False
    }

    result = ai_service.process_content(
        content=final_content,
        skill_name="email_thread_synthesis",
        source_type="email",
        metadata={
            "deal_title": deal.title if deal else proposed_intel.get("company_name"),
            "deal_summary": deal.deal_summary if deal else "",
            "audit_log_id": audit_log_id,
            "temperature": 0.0,
            "max_tokens": 8192,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema", 
                "json_schema": {
                    "name": "email_deal_synth", 
                    "schema": DEAL_SCHEMA, 
                    "strict": True
                }
            }
        }
    )

    analysis = result
    
    if analysis and "error" not in analysis:
        # Ensure deal_model_data has title if deal is missing
        if not analysis.get("deal_model_data", {}).get("title"):
            analysis.setdefault("deal_model_data", {})["title"] = proposed_intel.get("company_name")

        # Apply the synthesis to the actual Deal object if it exists
        normalized_analysis = _normalize_synthesis_result(
            analysis,
            analysis_kind=AnalysisKind.INITIAL,
            document_evidence=[], 
            analysis_input_files=[{"file_name": r["file_name"]} for r in passed_results],
            failed_files=[r for r in results if r.get("status") != "passed"],
        )
        
        if deal:
            DealCreationService.apply_analysis_to_deal(deal, normalized_analysis)
        
        audit_log.parsed_json = normalized_analysis
        audit_log.status = 'COMPLETED'
        audit_log.is_success = True
        
        # Prepare file tree for later VDR confirm
        source_meta = audit_log.source_metadata or {}
        source_meta["passed_files"] = passed_results
        source_meta["failed_files"] = [r for r in results if r.get("status") != "passed"]
        source_meta["interaction_status"] = "pending"
        source_meta["interaction_mode"] = "editable"
        audit_log.source_metadata = source_meta
        
        audit_log.save(update_fields=['parsed_json', 'status', 'is_success', 'source_metadata'])
        log_worker_event(audit_log, "Deal intelligence synthesized successfully.", status='COMPLETED', done=True)
        
        return {"status": "success", "deal_id": str(deal.id) if deal else None}
    else:
        error_msg = analysis.get("error", "AI model returned an empty or unparseable response.")
        log_worker_event(audit_log, f"Synthesis failed: {error_msg}", status='FAILED', done=True)
        return {"error": f"Synthesis failed: {error_msg}"}
