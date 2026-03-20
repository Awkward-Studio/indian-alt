import logging
import time
from celery import shared_task, chord
from django.db import transaction

from .models import Deal, DealDocument, DocumentType
from microsoft.services.graph_service import GraphAPIService
from ai_orchestrator.services.document_processor import DocumentProcessorService
from ai_orchestrator.services.embedding_processor import EmbeddingService

logger = logging.getLogger(__name__)

@shared_task(bind=True)
def analyze_folder_async(self, drive_id: str, folder_id: str, user_email: str, audit_log_id: str = None):
    """
    Kicks off deep folder traversal and AI extraction in the background.
    """
    logger.info(f"Starting async folder analysis for {folder_id}")
    
    from microsoft.services.graph_service import GraphAPIService
    from ai_orchestrator.services.document_processor import DocumentProcessorService
    from ai_orchestrator.services.ai_processor import AIProcessorService
    from ai_orchestrator.models import AIAuditLog, AIPersonality, AISkill
    import uuid
    
    # 1. Recover or Create Audit Log
    if audit_log_id:
        try:
            audit_log = AIAuditLog.objects.get(id=audit_log_id)
            audit_log.status = 'PROCESSING'
            audit_log.save()
        except AIAuditLog.DoesNotExist:
            audit_log_id = None
            
    if not audit_log_id:
        personality = AIPersonality.objects.filter(is_default=True).first()
        skill = AISkill.objects.filter(name='deal_extraction').first()
        audit_log = AIAuditLog.objects.create(
            source_type='onedrive_folder', source_id=folder_id,
            personality=personality, skill=skill,
            status='PROCESSING', is_success=False,
            model_used='qwen3.5:latest', system_prompt="Forensic traversal...",
            user_prompt=f"Analyzing folder: {folder_id}",
            celery_task_id=self.request.id
        )

    graph = GraphAPIService()
    doc_processor = DocumentProcessorService()
    ai_service = AIProcessorService()
    
    try:
        # 1. Traverse
        file_tree = graph.get_folder_tree(drive_id, folder_id, user_email=user_email)
        if not file_tree:
            audit_log.status = 'FAILED'
            audit_log.error_message = "No files found in folder"
            audit_log.save()
            return {"error": "No files found"}
            
        # Update log with traversal results
        audit_log.system_prompt = f"Traversal complete. Found {len(file_tree)} objects. Starting extraction..."
        audit_log.save()

        # 2. Extract preview text using GLM-OCR
        preview_files = file_tree[:5]
        combined_text = ""
        
        for file_info in preview_files:
            try:
                content = graph.get_drive_item_content(user_email, file_info['id'], drive_id=drive_id)
                # We transcribe all pages for the 5 preview files
                extracted = doc_processor.transcribe_document(content, file_info['name'], page_limit=None)
                combined_text += f"\n--- FILE: {file_info['name']} ---\n{extracted}"
            except Exception as e:
                logger.error(f"Error reading {file_info['name']}: {e}")

        # 3. AI Analysis
        analysis = {}
        raw_thinking = ""
        if combined_text:
            # We pass the existing audit_log to process_content so it UPDATES instead of creating a new one
            meta = {
                '_source_metadata': {
                    "file_tree": file_tree,
                    "drive_id": drive_id,
                    "folder_id": folder_id,
                    "total_files": len(file_tree)
                },
                'audit_log_id': str(audit_log.id),
                'celery_task_id': self.request.id,
                'context_label': f"Folder: {folder_id}" # Will be updated if name is known
            }
            
            result = ai_service.process_content(
                content=combined_text,
                skill_name="deal_extraction",
                source_type="onedrive_folder",
                metadata=meta
            )

            # The service now updates the audit_log internally if we pass the ID (we need to update the service too)
            if isinstance(result, dict) and 'parsed_json' in result:
                analysis = result['parsed_json']
                raw_thinking = result.get('thinking', '')
            else:
                analysis = result
                raw_thinking = analysis.get('thinking', '') if isinstance(analysis, dict) else ""
        else:
            # NO CONTENT EXTRACTED
            audit_log.status = 'FAILED'
            audit_log.error_message = "No readable context found in these folder objects (e.g. legacy binary formats)."
            audit_log.save()
            return {"error": "No content found", "folder_id": folder_id}

        return {
            "status": "success",
            "folder_id": folder_id,
            "total_files": len(file_tree),
            "preview_files_analyzed": len(preview_files),
            "preview_text": combined_text,
            "preliminary_data": analysis,
            "raw_thinking": raw_thinking,
            "file_tree": file_tree,
            "drive_id": drive_id,
            "user_email": user_email
        }
    except Exception as e:
        audit_log.status = 'FAILED'
        audit_log.error_message = str(e)
        audit_log.save()
        raise e

@shared_task(bind=True)
def process_single_document_async(self, file_info, deal_id, user_email, is_preview):
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

        # Download content
        content = graph_service.get_drive_item_content(user_email, file_id, drive_id=drive_id)
        
        # Extract text (Limited to 2 pages for background sync)
        extracted_text = doc_processor.transcribe_document(content, file_name, page_limit=2)
        
        # Create Document Record
        doc = DealDocument.objects.create(
            deal=deal,
            title=file_name,
            document_type=doc_type,
            onedrive_id=file_id,
            extracted_text=extracted_text,
            is_indexed=False,
            is_ai_analyzed=is_preview
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
        if extracted_text and len(extracted_text.strip()) > 50:
            embed_service.vectorize_document(doc)
            
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
    logger.info(f"Starting background processing for Deal {deal_id} with {len(file_tree_map)} files.")
    
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
        system_prompt=f"Starting background vectorization for {len(file_tree_map)} files via chord.",
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

    # Dispatch chord
    tasks = [process_single_document_async.s(f, deal_id, user_email, i < 5) for i, f in enumerate(file_tree_map)]
    chord(tasks)(finalize_folder_background.s(deal_id, str(audit_log.id)))
    
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

        docs = DealDocument.objects.filter(id__in=document_ids)
        doc_processor = DocumentProcessorService()
        graph = GraphAPIService()
        new_text_context = ""
        
        for doc in docs:
            # Check if we need full transcription:
            # 1. Not already analyzed
            # 2. OR extracted_text looks like a 2-page preview (contains PAGE 1 but not higher pages, or is short)
            # Actually, the safest way is to check is_ai_analyzed. 
            # If it's False, it definitely needs a full run.
            
            if not doc.is_ai_analyzed and doc.onedrive_id:
                try:
                    logger.info(f"[TASK] Performing full GLM-OCR transcription for: {doc.title}")
                    content = graph.get_drive_item_content(user_email=DMS_USER_EMAIL, file_id=doc.onedrive_id, drive_id=deal.source_drive_id)
                    full_text = doc_processor.transcribe_document(content, doc.title)
                    doc.extracted_text = full_text
                    doc.save(update_fields=['extracted_text'])
                except Exception as e:
                    logger.error(f"Failed to fully transcribe document {doc.title}: {e}")
            
            if doc.extracted_text:
                new_text_context += f"\n\n--- NEW DOCUMENT: {doc.title} ---\n{doc.extracted_text}"

        if not new_text_context.strip():
            audit_log.status = 'FAILED'
            audit_log.error_message = "No text extracted from selected documents."
            audit_log.save()
            return {"error": "No text extracted"}

        ai_service = AIProcessorService()
        existing_summary = deal.deal_summary or ""
        
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
            # Create a NEW DealAnalysis record for this version
            DealAnalysis.objects.create(
                deal=deal,
                version=current_version,
                thinking=raw_thinking,
                ambiguities=analysis.get('metadata', {}).get('ambiguous_points', []),
                analysis_json=analysis
            )

            # Update Deal meta-fields that we still keep on Deal
            if 'deal_model_data' in analysis:
                deal.themes = analysis['deal_model_data'].get('themes', deal.themes)
                
            deal.save()
            docs.update(is_ai_analyzed=True)
            
            audit_log.status = 'COMPLETED'
            audit_log.is_success = True
            audit_log.save()
            return {"status": "success", "version": current_version}
        else:
            error_msg = str(analysis.get('error', 'AI Output invalid'))
            audit_log.status = 'FAILED'
            audit_log.error_message = error_msg
            audit_log.save()
            return {"error": error_msg}

    except Exception as e:
        logger.error(f"Incremental analysis failed: {str(e)}")
        if 'audit_log' in locals():
            audit_log.status = 'FAILED'
            audit_log.error_message = str(e)
            audit_log.save()
        raise e
