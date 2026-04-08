import logging
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
    InitialAnalysisStatus,
    TranscriptionStatus,
)
from .services.deal_creation import DealCreationService
from .services.phase_readiness import (
    DealPhaseReadinessService,
    PHASE_READINESS_SKILL_NAME,
    PHASE_READINESS_SOURCE_TYPE,
)
from microsoft.services.graph_service import GraphAPIService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.realtime import broadcast_audit_log_update, log_worker_event

logger = logging.getLogger(__name__)

VDR_DOCUMENT_LIMIT = 50


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


@shared_task(bind=True)
def run_phase_readiness_analysis_async(self, deal_id: str, audit_log_id: str):
    """
    Background task for a quick deal phase-readiness recommendation.
    """
    from ai_orchestrator.models import AIAuditLog
    from ai_orchestrator.services.ai_processor import AIProcessorService

    logger.info("Starting phase-readiness analysis for Deal %s", deal_id)

    try:
        deal = Deal.objects.get(id=deal_id)
        audit_log = AIAuditLog.objects.get(id=audit_log_id)
        audit_log.celery_task_id = self.request.id
        audit_log.status = "PROCESSING"
        audit_log.save(update_fields=["celery_task_id", "status"])

        DealPhaseReadinessService.ensure_skill()
        ai_service = AIProcessorService()
        result = ai_service.process_content(
            content=DealPhaseReadinessService.build_context(deal),
            skill_name=PHASE_READINESS_SKILL_NAME,
            source_type=PHASE_READINESS_SOURCE_TYPE,
            source_id=str(deal.id),
            metadata={
                "audit_log_id": str(audit_log.id),
                "_source_metadata": {
                    "deal_id": str(deal.id),
                    "current_phase_at_run": deal.current_phase,
                    "trigger": "manual_status_check",
                },
            },
        )

        audit_log.refresh_from_db()
        parsed_json = result.get("parsed_json") if isinstance(result, dict) and isinstance(result.get("parsed_json"), dict) else result
        if isinstance(parsed_json, dict) and "error" not in parsed_json:
            audit_log.parsed_json = DealPhaseReadinessService.normalize_result(parsed_json, deal)
            audit_log.status = "COMPLETED"
            audit_log.is_success = True
            audit_log.error_message = None
            audit_log.save(update_fields=["parsed_json", "status", "is_success", "error_message"])
            broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
            return {"status": "success", "audit_log_id": str(audit_log.id)}

        error_message = (
            parsed_json.get("error")
            if isinstance(parsed_json, dict)
            else "AI returned an invalid readiness payload."
        )
        audit_log.status = "FAILED"
        audit_log.is_success = False
        audit_log.error_message = str(error_message)
        audit_log.save(update_fields=["status", "is_success", "error_message"])
        broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
        return {"error": str(error_message)}
    except Exception as e:
        logger.error("Phase-readiness analysis failed: %s", str(e))
        try:
            audit_log = AIAuditLog.objects.get(id=audit_log_id)
            audit_log.status = "FAILED"
            audit_log.is_success = False
            audit_log.error_message = str(e)
            audit_log.save(update_fields=["status", "is_success", "error_message"])
            broadcast_audit_log_update(audit_log, event_type="terminal", done=True)
        except Exception:
            pass
        raise


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
            extracted_text = (extraction.get("text") or "").strip()
            extraction_mode = extraction.get("mode")

            if not extracted_text:
                file_record["reason"] = extraction.get("error") or f"No readable content extracted for {name}"
                file_record["extraction_mode"] = extraction_mode
                failed_files.append(file_record)
                continue

            file_record["status"] = "passed"
            file_record["extraction_mode"] = extraction_mode
            file_record["transcription_status"] = TranscriptionStatus.COMPLETE
            file_record["text_length"] = len(extracted_text)
            file_record["extracted_text"] = extracted_text
            passed_files.append(file_record)
            combined_text_parts.append(f"\n--- FILE: {name} ---\n{extracted_text}")
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
        extracted_text = (file.get("extracted_text") or "").strip()
        if not extracted_text:
            continue
        parts.append(f"\n--- FILE: {file.get('file_name', 'unknown_file')} ---\n{extracted_text}")
    return "".join(parts).strip()


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
    if mode not in {ExtractionMode.GLM_OCR, ExtractionMode.FALLBACK_TEXT}:
        mode = None
    extracted_text = (extraction.get("text") or "").strip()
    return {
        "extracted_text": extracted_text,
        "extraction_mode": mode,
        "transcription_status": (
            TranscriptionStatus.COMPLETE if full and extracted_text else TranscriptionStatus.PARTIAL if extracted_text else TranscriptionStatus.FAILED
        ),
        "last_transcribed_at": timezone.now() if extracted_text else None,
    }


def _vectorize_document_and_capture(doc: DealDocument, embed_service: EmbeddingService) -> int:
    chunk_count = embed_service.chunk_and_embed(
        text=doc.extracted_text or "",
        deal=doc.deal,
        source_type='document',
        source_id=str(doc.id),
        metadata={"title": doc.title, "type": doc.document_type},
        replace_existing=True,
    )
    doc.is_indexed = bool(chunk_count)
    doc.chunking_status = ChunkingStatus.CHUNKED if chunk_count else ChunkingStatus.FAILED
    doc.last_chunked_at = timezone.now() if chunk_count else None
    doc.save(update_fields=['is_indexed', 'chunking_status', 'last_chunked_at'])
    return chunk_count


def _sync_deal_extracted_text_for_documents(deal: Deal, docs: list[DealDocument]) -> None:
    existing = deal.extracted_text or ""
    additions = []
    for doc in docs:
        text = (doc.extracted_text or "").strip()
        if not text:
            continue
        marker = f"--- DOCUMENT: {doc.title} ---"
        if marker in existing:
            continue
        additions.append(f"\n\n{marker}\n{text}")
    if additions:
        deal.extracted_text = existing + "".join(additions)
        deal.save(update_fields=['extracted_text'])

@shared_task(bind=True)
def analyze_folder_async(self, drive_id: str, folder_id: str, user_email: str, audit_log_id: str = None):
    """
    Kicks off deep folder traversal. Returns the full tree for selection.
    """
    logger.info(f"Starting async folder traversal for {folder_id}")
    
    from microsoft.services.graph_service import GraphAPIService
    from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
    
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
        
        # Use model from personality
        default_model = personality.text_model_name if personality else 'qwen3.5:latest'
        
        audit_log = AIAuditLog.objects.create(
            source_type='onedrive_folder', source_id=folder_id,
            personality=personality, skill=skill,
            status='PROCESSING', is_success=False,
            model_used=default_model, system_prompt="Forensic traversal...",
            user_prompt=f"Traversing folder: {folder_id}",
            celery_task_id=self.request.id
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
            
        # Update log with traversal results - Transition to WAITING_FOR_SELECTION
        log_worker_event(audit_log, f"Traversal complete. Discovered {len(file_tree)} objects.", status='COMPLETED', done=True)
        audit_log.system_prompt = f"Traversal complete. Found {len(file_tree)} objects. Awaiting selection..."
        audit_log.is_success = True
        # Store the tree in source_metadata for later recovery
        audit_log.source_metadata = {
            "file_tree": file_tree,
            "drive_id": drive_id,
            "folder_id": folder_id,
            "total_files": len(file_tree),
            "workflow_stage": "traversal_complete",
            "interaction_status": "pending",
            "interaction_mode": "editable",
        }
        audit_log.save()

        return {
            "status": "success",
            "phase": "traversal",
            "audit_log_id": str(audit_log.id),
            "folder_id": folder_id,
            "total_files": len(file_tree),
            "file_tree": file_tree,
            "drive_id": drive_id,
            "user_email": user_email
        }
    except Exception as e:
        log_worker_event(audit_log, f"Traversal error: {str(e)}", status='FAILED', done=True)
        raise e

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
        combined_text = _build_combined_text(passed_files, selected_file_ids)

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
        if combined_text:
            ai_service = AIProcessorService()
            meta = {
                '_source_metadata': audit_log.source_metadata,
                'audit_log_id': str(audit_log.id),
                'celery_task_id': self.request.id,
                'context_label': audit_log.context_label
            }
            
            result = ai_service.process_content(
                content=combined_text,
                skill_name="deal_extraction",
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

        if isinstance(analysis, dict):
            metadata = analysis.setdefault("metadata", {})
            metadata["selected_files_count"] = len(selected_file_ids)
            metadata["passed_files_count"] = len(approved_files)
            metadata["failed_files_count"] = len(failed_files)
            metadata["passed_files"] = passed_files
            metadata["failed_files"] = failed_files
            metadata["analysis_input_files"] = approved_files

        return {
            "phase": "analysis",
            "status": "success",
            "folder_id": session_data.get("folder_id"),
            "total_files": len(selected_file_ids),
            "preview_files_analyzed": len(approved_files),
            "preview_text": combined_text,
            "preliminary_data": analysis,
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
    
    graph_service = GraphAPIService()
    doc_processor = DocumentProcessorService()
    embed_service = EmbeddingService()
    
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
        extracted_text = (extraction.get("text") or "").strip()

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
            is_indexed=False,
            is_ai_analyzed=False,
            initial_analysis_status=initial_analysis_status,
            initial_analysis_reason=initial_analysis_reason,
            extraction_mode=extraction.get("mode"),
            transcription_status=TranscriptionStatus.PARTIAL if extracted_text else TranscriptionStatus.FAILED,
            chunking_status=ChunkingStatus.NOT_CHUNKED,
            last_transcribed_at=timezone.now() if extracted_text else None,
        )
        
        # Update combined deal text (Race condition warning: multiple tasks appending to the same field)
        if extracted_text:
            with transaction.atomic():
                # Re-fetch deal within transaction to minimize race condition window
                deal_locked = Deal.objects.select_for_update().get(id=deal_id)
                new_context = f"\n\n--- DOCUMENT: {file_name} ---\n{extracted_text}"
                if not deal_locked.extracted_text:
                    deal_locked.extracted_text = new_context
                else:
                    deal_locked.extracted_text += new_context
                deal_locked.save(update_fields=['extracted_text'])
        
        # Vectorize for RAG
        if extracted_text and len(extracted_text.strip()) > 50 and not _is_cancel_requested(audit_log_id):
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
    audit_log = AIAuditLog.objects.create(
        source_type='vdr_indexing',
        source_id=deal_id,
        personality=personality,
        status='PROCESSING',
        is_success=False,
        model_used='nomic-embed-text:latest',
        system_prompt=f"Starting background vectorization for {len(limited_file_tree)} files via chord.",
        user_prompt=f"Indexing dataroom for deal ID: {deal_id}",
        celery_task_id=self.request.id
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
        new_text_context = ""
        diagnostics = []
        prepared_docs = []

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
                    logger.info(f"[TASK] Performing full GLM-OCR transcription for: {doc.title}")
                    content = graph.get_drive_item_content(
                        user_email=DMS_USER_EMAIL,
                        file_id=doc.onedrive_id,
                        drive_id=deal.source_drive_id,
                    )
                    extraction = doc_processor.get_extraction_result(content, doc.title, page_limit=None)
                    update = _prepare_document_update_from_extraction(extraction, full=True)
                    doc.extracted_text = update["extracted_text"]
                    doc.extraction_mode = update["extraction_mode"]
                    doc.transcription_status = update["transcription_status"]
                    doc.last_transcribed_at = update["last_transcribed_at"]
                    doc.save(update_fields=['extracted_text', 'extraction_mode', 'transcription_status', 'last_transcribed_at'])
                else:
                    file_diag["used_cached_text"] = True

                file_diag["transcription_mode"] = doc.extraction_mode
                file_diag["transcription_status"] = doc.transcription_status

                if (doc.extracted_text or "").strip():
                    chunk_count = _vectorize_document_and_capture(doc, embed_service)
                    file_diag["chunk_count"] = chunk_count
                    file_diag["chunking_status"] = doc.chunking_status
                    new_text_context += f"\n\n--- NEW DOCUMENT: {doc.title} ---\n{doc.extracted_text}"
                    file_diag["analysis_included"] = True
                    prepared_docs.append(doc)
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

        if not new_text_context.strip():
            audit_log.status = 'FAILED'
            audit_log.error_message = "No text extracted from selected documents."
            audit_log.source_metadata = {
                **(audit_log.source_metadata or {}),
                "file_diagnostics": diagnostics,
            }
            audit_log.save(update_fields=['status', 'error_message', 'source_metadata'])
            return {"error": "No text extracted"}

        ai_service = AIProcessorService()
        existing_canonical_snapshot = ((deal.current_analysis or {}).get("canonical_snapshot") or {}) if hasattr(deal, "current_analysis") else {}
        existing_summary = (existing_canonical_snapshot.get("analyst_report") or deal.deal_summary or "")
        
        # Calculate next version from existing analyses
        from .models import DealAnalysis
        latest_analysis = deal.analyses.order_by('-version').first()
        current_version = (latest_analysis.version + 1) if latest_analysis else 2

        result = ai_service.process_content(
            content=new_text_context,
            skill_name="vdr_incremental_analysis",
            source_type="vdr_incremental_analysis",
            source_id=str(deal.id),
            metadata={
                'audit_log_id': str(audit_log.id),
                'existing_summary': existing_summary,
                'existing_canonical_snapshot': json.dumps(existing_canonical_snapshot, default=str),
                'version_num': current_version
            }
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
            normalized_analysis = DealCreationService.normalize_analysis_payload(
                analysis,
                previous_snapshot=existing_canonical_snapshot,
                analysis_kind=AnalysisKind.SUPPLEMENTAL,
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
