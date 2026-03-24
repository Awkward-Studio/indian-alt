import uuid
import traceback
import logging
from django.core.cache import cache
from deals.models import Deal

logger = logging.getLogger(__name__)

class FolderAnalysisService:
    """
    Domain Service for handling OneDrive folder analysis orchestration,
    task triggering, and session confirmation state.
    """

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
        
        audit_log = AIAuditLog.objects.create(
            source_type='onedrive_folder',
            source_id=folder_id,
            context_label=f"Folder: {folder_name}",
            personality=personality,
            skill=skill,
            status='PENDING',
            is_success=False,
            model_used='qwen3.5:latest',
            system_prompt="Queued for forensic traversal...",
            user_prompt=f"Queued analysis for folder: {folder_name}"
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
                
            session_id = str(uuid.uuid4())
            cache.set(f"folder_sync_{session_id}", {
                "file_tree": data['file_tree'],
                "drive_id": data['drive_id'],
                "folder_id": data['folder_id'],
                "user_email": data['user_email'],
                "preliminary_data": data['preliminary_data'],
                "preview_text": data.get('preview_text', ''),
                "raw_thinking": data.get('raw_thinking', '')
            }, timeout=3600)
            
            response.update({
                "session_id": session_id,
                "folder_id": data['folder_id'],
                "total_files": data['total_files'],
                "preview_files_analyzed": data['preview_files_analyzed'],
                "preliminary_data": data['preliminary_data'],
                "raw_thinking": data.get('raw_thinking', '')
            })
            
        elif result.status == 'FAILURE':
            response["error"] = str(result.info)
            
        return response

    @staticmethod
    def create_session_from_audit_log(log_id: str) -> dict:
        """
        Re-caches session data from an existing successful audit log.
        """
        from ai_orchestrator.models import AIAuditLog
        from microsoft.services.graph_service import DMS_USER_EMAIL
        
        try:
            log = AIAuditLog.objects.get(id=log_id)
            if log.source_type != 'onedrive_folder':
                return {"error": "This audit log is not a folder analysis"}
            
            meta = log.source_metadata
            if not meta:
                return {"error": "This log does not contain source metadata"}
                
            session_id = str(uuid.uuid4())
            cache.set(f"folder_sync_{session_id}", {
                "file_tree": meta['file_tree'],
                "drive_id": meta['drive_id'],
                "folder_id": meta['folder_id'],
                "user_email": DMS_USER_EMAIL,
                "preliminary_data": log.parsed_json,
                "preview_text": meta.get('preview_text', '')
            }, timeout=3600)
            
            return {
                "session_id": session_id,
                "preliminary_data": log.parsed_json,
                "total_files": meta.get('total_files', len(meta['file_tree'])),
                "preview_files_analyzed": 5,
                "raw_thinking": log.raw_response
            }
        except AIAuditLog.DoesNotExist:
            return {"error": "Audit log not found"}

    @staticmethod
    def confirm_deal_from_session(session_id: str, deal: Deal) -> dict:
        """
        Updates the newly created Deal with cached forensic mapping and triggers background indexing.
        """
        session_data = cache.get(f"folder_sync_{session_id}")
        if not session_data:
            return {"error": "Session expired or invalid. Please re-analyze the folder."}
            
        analysis_json = session_data.get('preliminary_data', {})
        
        deal.processing_status = 'processing'
        deal.source_onedrive_id = session_data.get('folder_id')
        deal.source_drive_id = session_data.get('drive_id')
        deal.extracted_text = session_data.get('preview_text', '')
        
        if analysis_json:
            from deals.models import DealAnalysis
            # Create DealAnalysis record
            DealAnalysis.objects.create(
                deal=deal,
                version=1,
                thinking=session_data.get('raw_thinking', ''),
                ambiguities=analysis_json.get('metadata', {}).get('ambiguous_points', []),
                analysis_json=analysis_json
            )
            
            if 'deal_model_data' in analysis_json:
                deal.themes = analysis_json['deal_model_data'].get('themes', [])
            
        deal.save()
        
        # Trigger Background Task - Indexing is low priority
        from deals.tasks import process_deal_folder_background
        process_deal_folder_background.apply_async(
            kwargs={
                'deal_id': str(deal.id),
                'file_tree_map': session_data['file_tree'],
                'user_email': session_data['user_email']
            },
            queue='low_priority'
        )
        
        cache.delete(f"folder_sync_{session_id}")
        
        return {
            "status": "success",
            "deal_id": deal.id,
            "message": f"Deal created. Processing {len(session_data['file_tree'])} files in background."
        }
