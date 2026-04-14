import uuid
import traceback
import logging
from django.core.cache import cache
from deals.models import Deal
from deals.services.deal_creation import DealCreationService
from deals.tasks import VDR_DOCUMENT_LIMIT

logger = logging.getLogger(__name__)

class FolderAnalysisService:
    """
    Domain Service for handling OneDrive folder analysis orchestration,
    task triggering, and session confirmation state.
    """

    @staticmethod
    def _infer_workflow_stage(meta: dict, has_parsed_json: bool) -> str | None:
        if not meta:
            return None
        if meta.get("workflow_stage"):
            return meta["workflow_stage"]
        if has_parsed_json:
            return "analysis_complete"
        if meta.get("passed_files") or meta.get("failed_files"):
            return "preflight_complete"
        if meta.get("file_tree"):
            return "traversal_complete"
        return None

    @staticmethod
    def get_persisted_file_tree_for_deal(deal: Deal) -> list[dict]:
        """
        Returns the latest persisted OneDrive folder tree for a deal, if available.
        """
        if not deal.source_onedrive_id or not deal.source_drive_id:
            return []

        from ai_orchestrator.models import AIAuditLog

        candidate_logs = AIAuditLog.objects.filter(
            source_type='onedrive_folder',
            source_id=deal.source_onedrive_id,
        ).order_by('-created_at')

        for log in candidate_logs:
            metadata = log.source_metadata or {}
            if metadata.get('drive_id') == deal.source_drive_id and metadata.get('file_tree'):
                return metadata['file_tree']

        return []

    @staticmethod
    def _get_existing_confirmed_deal(session_data: dict) -> Deal | None:
        deal_id = session_data.get("confirmed_deal_id")
        if deal_id:
            return Deal.objects.filter(id=deal_id).first()

        audit_log_id = session_data.get("originating_audit_log_id")
        if not audit_log_id:
            return None

        from ai_orchestrator.models import AIAuditLog

        log = AIAuditLog.objects.filter(id=audit_log_id).first()
        metadata = log.source_metadata if log else {}
        confirmed_deal_id = (metadata or {}).get("deal_id")
        if not confirmed_deal_id:
            return None
        return Deal.objects.filter(id=confirmed_deal_id).first()

    @staticmethod
    def _get_originating_audit_log(session_data: dict):
        audit_log_id = session_data.get("originating_audit_log_id") or session_data.get("preflight_audit_log_id")
        if not audit_log_id:
            return None

        from ai_orchestrator.models import AIAuditLog

        return AIAuditLog.objects.filter(id=audit_log_id).first()

    @staticmethod
    def queue_folder_analysis(folder_id: str, folder_name: str, drive_id: str) -> dict:
        """
        Kicks off an asynchronous folder analysis. Returns tracking info.
        """
        from microsoft.services.graph_service import DMS_USER_EMAIL
        from deals.tasks import analyze_folder_async
        from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
        
        # Create a PENDING audit log immediately for visibility
        personality = AIPersonality.objects.filter(is_default=True).first()
        skill = AISkill.objects.filter(name='deal_extraction').first()
        
        # Use model from personality
        default_model = personality.text_model_name if personality else 'qwen3.5:latest'
        
        audit_log = AIAuditLog.objects.create(
            source_type='onedrive_folder',
            source_id=folder_id,
            context_label=f"Folder: {folder_name}",
            personality=personality,
            skill=skill,
            status='PENDING',
            is_success=False,
            model_used=default_model,
            system_prompt="Queued for forensic traversal...",
            user_prompt=f"Queued analysis for folder: {folder_name}",
            source_metadata={
                "drive_id": drive_id,
                "folder_id": folder_id,
                "workflow_stage": "traversal_pending",
                "interaction_status": "pending",
                "interaction_mode": "editable",
            },
        )

        # Trigger task - Initial analysis is high priority
        task = analyze_folder_async.apply_async(
            kwargs={
                'drive_id': drive_id,
                'folder_id': folder_id,
                'user_email': DMS_USER_EMAIL,
                'audit_log_id': str(audit_log.id) 
            },
            queue='high_priority'
        )
        
        audit_log.celery_task_id = task.id
        audit_log.save(update_fields=['celery_task_id'])
        
        return {
            "task_id": task.id,
            "audit_log_id": str(audit_log.id),
            "status": "queued"
        }

    @staticmethod
    def get_task_status(task_id: str) -> dict:
        """
        Polls the status of an AI analysis task. Caches success data for confirmation.
        """
        from celery.result import AsyncResult
        result = AsyncResult(task_id)
        
        response = {
            "task_id": task_id,
            "status": result.status, 
        }
        
        if result.status == 'SUCCESS':
            data = result.result
            if not data:
                return {"status": "FAILURE", "error": "Task returned no data"}
            if "error" in data:
                return data # Propagate error dict

            if data.get("phase") == "preflight":
                session_id = data.get("session_id")
                if session_id:
                    session_data = cache.get(f"folder_sync_{session_id}") or {}
                    session_data.update({
                        "selected_file_ids": data.get("selected_file_ids", []),
                        "passed_files": data.get("passed_files", []),
                        "failed_files": data.get("failed_files", []),
                        "preflight_audit_log_id": data.get("audit_log_id"),
                    })
                    cache.set(f"folder_sync_{session_id}", session_data, timeout=3600)
                response.update({
                    "phase": "preflight",
                    "session_id": session_id,
                    "selected_files_count": data.get("selected_files_count", 0),
                    "passed_files_count": data.get("passed_files_count", 0),
                    "failed_files_count": data.get("failed_files_count", 0),
                    "passed_files": data.get("passed_files", []),
                    "failed_files": data.get("failed_files", []),
                    "folder_id": data.get("folder_id"),
                    "drive_id": data.get("drive_id"),
                })
                return response
                
            session_id = str(uuid.uuid4())
            cache.set(f"folder_sync_{session_id}", {
                "file_tree": data.get('file_tree', []),
                "drive_id": data.get('drive_id'),
                "folder_id": data.get('folder_id'),
                "user_email": data.get('user_email'),
                "originating_audit_log_id": data.get('audit_log_id'),
                "preliminary_data": data.get('preliminary_data'),
                "preview_text": data.get('preview_text', ''),
                "raw_thinking": data.get('raw_thinking', ''),
                "passed_files": data.get('passed_files', []),
                "failed_files": data.get('failed_files', []),
                "analysis_input_files": data.get('passed_files', []),
            }, timeout=3600)
            
            response.update({
                "phase": data.get("phase", "analysis"),
                "session_id": session_id,
                "folder_id": data.get('folder_id'),
                "total_files": data.get('total_files', 0),
                "file_tree": data.get('file_tree', []),
                "selected_file_ids": data.get('selected_file_ids', []),
                "preview_files_analyzed": data.get('preview_files_analyzed', 0),
                "preliminary_data": data.get('preliminary_data'),
                "raw_thinking": data.get('raw_thinking', ''),
                "passed_files": data.get('passed_files', []),
                "failed_files": data.get('failed_files', []),
            })
            
        elif result.status == 'FAILURE':
            response["error"] = str(result.info)
            
        return response

    @staticmethod
    def create_session_from_audit_log(log_id: str) -> dict:
        """
        Re-caches session data from an existing successful audit log.
        Supports both onedrive_folder and email sources.
        """
        from ai_orchestrator.models import AIAuditLog
        from microsoft.services.graph_service import DMS_USER_EMAIL
        
        try:
            log = AIAuditLog.objects.get(id=log_id)
            if log.source_type not in ['onedrive_folder', 'email']:
                return {"error": f"This audit log source type ({log.source_type}) does not support deal initialization"}
            
            meta = log.source_metadata or {}
            session_id = str(uuid.uuid4())

            # Handle Email Source
            if log.source_type == 'email':
                if not log.parsed_json:
                    return {"error": "This email audit log does not contain extracted deal data. Re-run the analysis before initializing a deal."}
                
                # Fetch the email object to get the full extracted text if available
                from microsoft.models import Email
                email_obj = Email.objects.filter(id=log.source_id).first()
                preview_text = email_obj.extracted_text if email_obj else log.user_prompt

                cache.set(f"folder_sync_{session_id}", {
                    "source_type": "email",
                    "source_id": log.source_id,
                    "user_email": DMS_USER_EMAIL,
                    "preliminary_data": log.parsed_json,
                    "preview_text": preview_text,
                    "raw_thinking": log.raw_thinking,
                    "originating_audit_log_id": str(log.id),
                    "analysis_input_files": meta.get('analysis_input_files', []),
                    "failed_files": meta.get('failed_files', []),
                }, timeout=3600)

                return {
                    "session_id": session_id,
                    "preliminary_data": log.parsed_json,
                    "raw_thinking": log.raw_thinking,
                    "phase": "analysis",
                    "source_type": "email"
                }

            # Handle OneDrive Folder Source
            if not meta:
                return {"error": "This audit log does not contain folder source metadata. It was likely created before metadata capture was added, so re-run the folder analysis and selection flow."}
            if not meta.get('file_tree') or not meta.get('drive_id') or not meta.get('folder_id'):
                return {"error": "This audit log is missing required folder metadata. Re-run the folder analysis and selection flow."}

            workflow_stage = FolderAnalysisService._infer_workflow_stage(meta, bool(log.parsed_json))

            if workflow_stage == "traversal_complete":
                cache_data = {
                    "file_tree": meta['file_tree'],
                    "user_email": DMS_USER_EMAIL,
                    "originating_audit_log_id": str(log.id),
                    "selected_file_ids": meta.get("selected_file_ids", []),
                    "source_type": log.source_type
                }
                if log.source_type == 'onedrive_folder':
                    cache_data.update({"drive_id": meta['drive_id'], "folder_id": meta['folder_id']})
                else:
                    cache_data.update({"email_id": meta.get('email_id', log.source_id)})

                cache.set(f"folder_sync_{session_id}", cache_data, timeout=3600)
                
                return {
                    "phase": "traversal",
                    "session_id": session_id,
                    "file_tree": meta['file_tree'],
                    "total_files": meta.get('total_files', len(meta['file_tree'])),
                    "selected_file_ids": meta.get("selected_file_ids", []),
                    "interaction_status": meta.get("interaction_status", "pending"),
                    "interaction_mode": meta.get("interaction_mode", "editable"),
                    "source_type": log.source_type
                }

            if workflow_stage == "preflight_complete":
                cache_data = {
                    "file_tree": meta['file_tree'],
                    "user_email": DMS_USER_EMAIL,
                    "preflight_audit_log_id": str(log.id),
                    "selected_file_ids": meta.get("selected_file_ids", []),
                    "passed_files": meta.get("passed_files", []),
                    "failed_files": meta.get("failed_files", []),
                    "approved_file_ids": meta.get("approved_file_ids", []),
                    "source_type": log.source_type
                }
                if log.source_type == 'onedrive_folder':
                    cache_data.update({"drive_id": meta['drive_id'], "folder_id": meta['folder_id']})
                else:
                    cache_data.update({"email_id": meta.get('email_id', log.source_id)})

                cache.set(f"folder_sync_{session_id}", cache_data, timeout=3600)

                return {
                    "phase": "preflight",
                    "session_id": session_id,
                    "selected_file_ids": meta.get("selected_file_ids", []),
                    "passed_files": meta.get("passed_files", []),
                    "failed_files": meta.get("failed_files", []),
                    "approved_file_ids": meta.get("approved_file_ids", []),
                    "selected_files_count": meta.get("selected_files_count", len(meta.get("selected_file_ids", []))),
                    "passed_files_count": meta.get("passed_files_count", len(meta.get("passed_files", []))),
                    "failed_files_count": meta.get("failed_files_count", len(meta.get("failed_files", []))),
                    "interaction_status": meta.get("interaction_status", "pending"),
                    "interaction_mode": meta.get("interaction_mode", "editable"),
                    "source_type": log.source_type
                }

            if meta.get("file_tree") and not workflow_stage:
                return {"error": "This folder audit log predates resumable workflow-stage metadata. Re-run the folder traversal or readability check from OneDrive, then reopen the new log from AI History."}

            if not log.parsed_json:
                return {"error": "This audit log does not contain extracted deal data. Re-run the selection analysis before initializing a deal."}
                
            cache.set(f"folder_sync_{session_id}", {
                "file_tree": meta['file_tree'],
                "drive_id": meta['drive_id'],
                "folder_id": meta['folder_id'],
                "user_email": DMS_USER_EMAIL,
                "source_type": log.source_type,
                "originating_audit_log_id": str(log.id),
                "preliminary_data": log.parsed_json,
                "preview_text": meta.get('preview_text', ''),
                "raw_thinking": log.raw_thinking,
                "passed_files": meta.get('analysis_input_files', meta.get('passed_files', [])),
                "analysis_input_files": meta.get('analysis_input_files', meta.get('passed_files', [])),
                "approved_file_ids": meta.get('approved_file_ids', []),
                "failed_files": meta.get('failed_files', []),
            }, timeout=3600)
            
            return {
                "session_id": session_id,
                "preliminary_data": log.parsed_json,
                "total_files": meta.get('total_files', len(meta['file_tree'])),
                "preview_files_analyzed": len(meta.get('analysis_input_files', meta.get('passed_files', []))),
                "raw_thinking": log.raw_thinking,
                "passed_files": meta.get('analysis_input_files', meta.get('passed_files', [])),
                "failed_files": meta.get('failed_files', []),
                "phase": "analysis",
                "source_type": log.source_type,
            }
        except AIAuditLog.DoesNotExist:
            return {"error": "Audit log not found"}

    @staticmethod
    def trigger_selection_analysis(session_id: str, selected_file_ids: list) -> dict:
        """
        Kicks off a preflight extraction pass on user-selected file IDs.
        """
        from deals.tasks import preflight_selection_async
        from microsoft.services.graph_service import DMS_USER_EMAIL
        
        session_data = cache.get(f"folder_sync_{session_id}")
        if not session_data:
            return {"error": "Session expired or invalid. Please re-analyze the folder."}

        source_log_id = session_data.get("originating_audit_log_id")
        if source_log_id:
            from ai_orchestrator.models import AIAuditLog
            source_log = AIAuditLog.objects.filter(id=source_log_id).first()
            if source_log:
                source_meta = source_log.source_metadata or {}
                if source_meta.get("interaction_status") == "completed":
                    return {"error": "This traversal interaction has already been completed and is now read-only."}
                source_log.source_metadata = {
                    **source_meta,
                    "selected_file_ids": selected_file_ids,
                    "interaction_status": "completed",
                    "interaction_mode": "read_only",
                }
                source_log.save(update_fields=["source_metadata"])
            
        # Extract metadata from the previous audit log if available
        # But we actually already have the data in the session cache
        
        # We need the audit log ID to update the same reasoning stream
        # Or should we create a new one? Let's use a new one for clarity in the ledger
        from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
        personality = AIPersonality.objects.filter(is_default=True).first()
        skill = AISkill.objects.filter(name='deal_extraction').first()
        
        # Use model from personality
        default_model = personality.text_model_name if personality else 'qwen3.5:latest'
        
        audit_log = AIAuditLog.objects.create(
            source_type='onedrive_folder',
            source_id=session_data['folder_id'],
            context_label=f"Selection Analysis: {len(selected_file_ids)} files",
            personality=personality,
            skill=skill,
            status='PENDING',
            is_success=False,
            model_used=default_model,
            system_prompt="Queued for extraction from selection...",
            user_prompt=f"Extracting deal data from {len(selected_file_ids)} files",
            source_metadata={
                "file_tree": session_data.get('file_tree', []),
                "drive_id": session_data.get('drive_id'),
                "folder_id": session_data.get('folder_id'),
                "preview_text": session_data.get('preview_text', ''),
                "total_files": len(session_data.get('file_tree', [])),
                "workflow_stage": "preflight_pending",
                "interaction_status": "pending",
                "interaction_mode": "editable",
            },
        )

        task = preflight_selection_async.apply_async(
            kwargs={
                'drive_id': session_data['drive_id'],
                'folder_id': session_data['folder_id'],
                'user_email': DMS_USER_EMAIL,
                'audit_log_id': str(audit_log.id),
                'selected_file_ids': selected_file_ids,
                'session_id': session_id,
            },
            queue='high_priority'
        )
        
        audit_log.celery_task_id = task.id
        audit_log.save(update_fields=['celery_task_id'])
        
        return {
            "task_id": task.id,
            "audit_log_id": str(audit_log.id),
            "status": "queued"
        }

    @staticmethod
    def confirm_selection_analysis(session_id: str, selected_file_ids: list) -> dict:
        """
        Runs the final Qwen-based extraction on the approved subset of selected files.
        """
        from deals.tasks import analyze_selection_async
        from microsoft.services.graph_service import DMS_USER_EMAIL
        from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill

        session_data = cache.get(f"folder_sync_{session_id}")
        if not session_data:
            return {"error": "Session expired or invalid. Please re-analyze the folder."}
        if not selected_file_ids:
            return {"error": "selected_file_ids is required"}

        source_log_id = session_data.get("preflight_audit_log_id")
        if source_log_id:
            source_log = AIAuditLog.objects.filter(id=source_log_id).first()
            if source_log:
                source_meta = source_log.source_metadata or {}
                if source_meta.get("interaction_status") == "completed":
                    return {"error": "This readability review has already been confirmed and is now read-only."}
                source_log.source_metadata = {
                    **source_meta,
                    "approved_file_ids": selected_file_ids,
                    "analysis_input_files": [
                        file for file in session_data.get("passed_files", [])
                        if file.get("file_id") in set(selected_file_ids)
                    ],
                    "interaction_status": "completed",
                    "interaction_mode": "read_only",
                }
                source_log.save(update_fields=["source_metadata"])

        personality = AIPersonality.objects.filter(is_default=True).first()
        skill = AISkill.objects.filter(name='deal_extraction').first()
        default_model = personality.text_model_name if personality else 'qwen3.5:latest'

        audit_log = AIAuditLog.objects.create(
            source_type='onedrive_folder',
            source_id=session_data['folder_id'],
            context_label=f"Selection Analysis: {len(selected_file_ids)} approved files",
            personality=personality,
            skill=skill,
            status='PENDING',
            is_success=False,
            model_used=default_model,
            system_prompt="Queued for extraction from approved selection...",
            user_prompt=f"Extracting deal data from {len(selected_file_ids)} approved files",
            source_metadata={
                "file_tree": session_data.get('file_tree', []),
                "drive_id": session_data.get('drive_id'),
                "folder_id": session_data.get('folder_id'),
                "preview_text": session_data.get('preview_text', ''),
                "total_files": len(session_data.get('file_tree', [])),
                "preflight_passed_files": session_data.get('passed_files', []),
                "preflight_failed_files": session_data.get('failed_files', []),
                "approved_file_ids": selected_file_ids,
                "workflow_stage": "analysis_pending",
            },
        )

        task = analyze_selection_async.apply_async(
            kwargs={
                'session_id': session_id,
                'audit_log_id': str(audit_log.id),
                'selected_file_ids': selected_file_ids
            },
            queue='high_priority'
        )

        audit_log.celery_task_id = task.id
        audit_log.save(update_fields=['celery_task_id'])

        return {
            "task_id": task.id,
            "audit_log_id": str(audit_log.id),
            "status": "queued"
        }

    @staticmethod
    def confirm_deal_from_session(session_id: str, deal: Deal) -> dict:
        """
        Updates the newly created Deal with cached forensic mapping.
        Supports both onedrive_folder and email sources.
        """
        from ai_orchestrator.services.embedding_processor import EmbeddingService
        from deals.models import (
            AnalysisKind,
            ChunkingStatus,
            DealAnalysis,
            DealDocument,
            DocumentType,
            ExtractionMode,
            InitialAnalysisStatus,
            TranscriptionStatus,
        )

        session_data = cache.get(f"folder_sync_{session_id}")
        if not session_data:
            return {"error": "Session expired or invalid. Please re-analyze the source."}

        existing_deal = FolderAnalysisService._get_existing_confirmed_deal(session_data)
        if existing_deal:
            if existing_deal.id != deal.id:
                deal.delete()
            return {
                "status": "success",
                "deal_id": existing_deal.id,
                "message": "Deal already created from this analysis session.",
            }
            
        analysis_json = session_data.get('preliminary_data', {})
        source_type = session_data.get('source_type', 'onedrive_folder')
        origin_log = FolderAnalysisService._get_originating_audit_log(session_data)
        origin_meta = origin_log.source_metadata if origin_log else {}

        deal.processing_status = 'idle'
        deal.processing_error = None
        
        if source_type == 'onedrive_folder':
            deal.source_onedrive_id = session_data.get('folder_id') or origin_meta.get('folder_id') or getattr(origin_log, 'source_id', None)
            deal.source_drive_id = session_data.get('drive_id') or origin_meta.get('drive_id')
        elif source_type == 'email':
            deal.source_email_id = session_data.get('source_id') or origin_meta.get('email_id') or getattr(origin_log, 'source_id', None)

        deal.extracted_text = session_data.get('preview_text', '')
        
        # Determine input files for analysis tracking
        approved_ids = set(session_data.get('approved_file_ids', []))
        input_files = session_data.get('analysis_input_files', session_data.get('passed_files', []))
        
        approved_files = [
            file for file in input_files
            if not approved_ids or file.get('file_id') in approved_ids
        ]

        if analysis_json:
            normalized_analysis = DealCreationService.normalize_analysis_payload(
                analysis_json,
                analysis_kind=AnalysisKind.INITIAL,
                documents_analyzed=[file.get('file_name') for file in approved_files if file.get('file_name')],
                analysis_input_files=approved_files,
                failed_files=session_data.get('failed_files', []),
            )
            # Create DealAnalysis record
            DealAnalysis.objects.create(
                deal=deal,
                version=1,
                analysis_kind=AnalysisKind.INITIAL,
                thinking=session_data.get('raw_thinking', ''),
                ambiguities=normalized_analysis.get('metadata', {}).get('ambiguous_points', []),
                analysis_json=normalized_analysis
            )

            DealCreationService.apply_analysis_to_deal(
                deal,
                normalized_analysis,
                overwrite=False,
                overwrite_themes=True,
            )

        embed_service = EmbeddingService()
        created_docs = []
        for file in approved_files:
            file_name = file.get('file_name') or 'unknown_file'
            extracted_text = (file.get('extracted_text') or '').strip()
            
            doc_kwargs = {
                "deal": deal,
                "title": file_name,
                "document_type": DocumentType.OTHER,
                "extracted_text": extracted_text,
                "is_indexed": False,
                "is_ai_analyzed": True,
                "initial_analysis_status": InitialAnalysisStatus.SELECTED_AND_ANALYZED,
                "extraction_mode": file.get('extraction_mode') or ExtractionMode.FALLBACK_TEXT,
                "transcription_status": file.get('transcription_status') or TranscriptionStatus.COMPLETE,
                "chunking_status": ChunkingStatus.NOT_CHUNKED,
                "last_transcribed_at": deal.created_at,
            }

            if source_type == 'onedrive_folder':
                doc_kwargs["onedrive_id"] = file.get('file_id')
            
            doc = DealDocument.objects.create(**doc_kwargs)
            
            if extracted_text:
                if embed_service.vectorize_document(doc):
                    doc.refresh_from_db(fields=['is_indexed', 'chunking_status', 'last_chunked_at'])
                created_docs.append(doc)

        for file in session_data.get('failed_files', []):
            doc_kwargs = {
                "deal": deal,
                "title": file.get('file_name') or 'unknown_file',
                "document_type": DocumentType.OTHER,
                "extracted_text": '',
                "is_indexed": False,
                "is_ai_analyzed": False,
                "initial_analysis_status": InitialAnalysisStatus.SELECTED_FAILED,
                "initial_analysis_reason": file.get('reason'),
                "extraction_mode": file.get('extraction_mode'),
                "transcription_status": TranscriptionStatus.FAILED,
                "chunking_status": ChunkingStatus.NOT_CHUNKED,
            }
            if source_type == 'onedrive_folder':
                doc_kwargs["onedrive_id"] = file.get('file_id')
            
            DealDocument.objects.create(**doc_kwargs)
            
        deal.save()

        if origin_log:
            origin_log.source_metadata = {
                **(origin_log.source_metadata or {}),
                "deal_id": str(deal.id),
                "interaction_status": "completed",
                "interaction_mode": "read_only",
            }
            origin_log.save(update_fields=["source_metadata"])
        cache.delete(f"folder_sync_{session_id}")
        
        return {
            "status": "success",
            "deal_id": deal.id,
            "message": "Deal created. Start VDR processing from the deal page when ready."
        }

    @staticmethod
    def trigger_vdr_processing(deal: Deal) -> dict:
        """
        Queues the deferred VDR indexing job using persisted OneDrive audit-log metadata.
        """
        if not deal.source_onedrive_id or not deal.source_drive_id:
            return {"error": "This deal is not linked to a OneDrive folder, so deferred VDR processing is unavailable."}

        if deal.processing_status == 'processing':
            return {"error": "VDR processing is already running for this deal."}

        from deals.tasks import process_deal_folder_background
        from microsoft.services.graph_service import DMS_USER_EMAIL

        file_tree = FolderAnalysisService.get_persisted_file_tree_for_deal(deal)

        if not file_tree:
            return {"error": "No persisted folder tree was found for this deal. Re-run the folder analysis to enable VDR processing."}

        task = process_deal_folder_background.apply_async(
            kwargs={
                'deal_id': str(deal.id),
                'file_tree_map': file_tree,
                'user_email': DMS_USER_EMAIL,
            },
            queue='low_priority'
        )

        deal.processing_status = 'processing'
        deal.processing_error = None
        deal.save(update_fields=['processing_status', 'processing_error'])

        return {
            "status": "queued",
            "task_id": task.id,
            "message": f"Queued VDR processing for {min(len(file_tree), VDR_DOCUMENT_LIMIT)} of {len(file_tree)} files."
        }
