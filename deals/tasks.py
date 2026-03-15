import logging
import time
from celery import shared_task
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

@shared_task(bind=True, max_retries=3)
def process_deal_folder_background(self, deal_id: str, file_tree_map: list, user_email: str):
    """
    Background task to download and vectorize all remaining files in a folder tree.
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
        system_prompt=f"Starting background vectorization for {len(file_tree_map)} files.",
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
    doc_processor = DocumentProcessorService()
    embed_service = EmbeddingService()
    
    # Crucial: Background processing MUST use delegated permissions for OneDrive
    # We ensure we have a valid delegated token for the DMS user
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

    processed_count = 0
    errors = []

    for i, file_info in enumerate(file_tree_map):
        try:
            # Update log periodically
            if i % 2 == 0:
                audit_log.system_prompt = f"Indexing progress: {i}/{len(file_tree_map)} files completed."
                audit_log.save()

            file_id = file_info.get('id')
            file_name = file_info.get('name')
            drive_id = file_info.get('driveId')
            
            if not file_id or not file_name:
                continue
                
            # Avoid duplicates if the file was already processed during the preview phase
            if DealDocument.objects.filter(deal=deal, onedrive_id=file_id).exists():
                continue
                
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
            
            # Extract text (Limited to 2 pages for fast metadata/preview processing during background sync)
            extracted_text = doc_processor.transcribe_document(content, file_name, page_limit=2)
            
            # Create Document Record
            doc = DealDocument.objects.create(
                deal=deal,
                title=file_name,
                document_type=doc_type,
                onedrive_id=file_id,
                extracted_text=extracted_text,
                is_indexed=False, # Will be set to True by vectorizer
                is_ai_analyzed=(i < 5) # The first 5 files were used in the initial analyze_folder_async preview
            )
            
            # Update combined deal text for RAG and Source Data Hub
            if extracted_text:
                new_context = f"\n\n--- DOCUMENT: {file_name} ---\n{extracted_text}"
                if not deal.extracted_text:
                    deal.extracted_text = new_context
                else:
                    deal.extracted_text += new_context
                deal.save(update_fields=['extracted_text'])
            
            # Vectorize for RAG
            if extracted_text and len(extracted_text.strip()) > 50:
                embed_service.vectorize_document(doc)
            
            processed_count += 1
            
            # PROGRESS HEARTBEAT: Update audit log every 5 files
            if processed_count % 5 == 0:
                audit_log.system_prompt = f"Indexing progress: {processed_count}/{total_files} files completed."
                audit_log.save(update_fields=['system_prompt'])
            
            # MEMORY MANAGEMENT: Clear large strings and force collection
            del extracted_text
            del content
            import gc
            gc.collect()
            
            # Give the system a breather
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"Failed to process file {file_info.get('name')} for Deal {deal_id}: {str(e)}")
            errors.append(f"{file_info.get('name')}: {str(e)}")

    # Update Deal status when finished
    deal.processing_status = 'completed' if not errors else 'failed'
    deal.processing_error = "; ".join(errors) if errors else ""
    deal.save(update_fields=['processing_status', 'processing_error'])
    
    # Finalize Audit Log
    audit_log.status = 'COMPLETED' if not errors else 'FAILED'
    audit_log.is_success = True if not errors else False
    audit_log.system_prompt = f"Successfully indexed {processed_count} documents into the VDR."
    if errors: 
        error_detail = "; ".join(errors)
        audit_log.error_message = f"Errors encountered: {error_detail}"
        audit_log.save()
        logger.error(f"Background processing for Deal {deal_id} finished with errors.")
        raise Exception(f"VDR Indexing failed for some files: {error_detail}")
    
    audit_log.save()
    
    logger.info(f"Finished background processing for Deal {deal_id}. Processed {processed_count} files.")
    return {"processed": processed_count, "errors": 0}

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
        current_version = len(deal.analysis_history or []) + 2

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
            # Update Deal JSON Fields
            deal.analysis_json = analysis
            if 'deal_model_data' in analysis:
                deal.themes = analysis['deal_model_data'].get('themes', deal.themes)
            if 'metadata' in analysis:
                deal.ambiguities = analysis['metadata'].get('ambiguous_points', deal.ambiguities)
                
            if 'analyst_report' in analysis:
                new_history = list(deal.analysis_history or [])
                new_history.append({
                    "version": current_version,
                    "report": analysis['analyst_report'],
                    "timestamp": timezone.now().isoformat(),
                    "documents_analyzed": [d.title for d in docs]
                })
                deal.analysis_history = new_history
            
            if raw_thinking:
                deal.thinking = (deal.thinking or "") + f"\n\n--- V{current_version} INCREMENTAL ANALYSIS ---\n{raw_thinking}"
                
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
