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

        # 2. Extract preview text and visuals
        preview_files = file_tree[:5]
        combined_text = ""
        all_images = []
        
        for file_info in preview_files:
            try:
                content = graph.get_drive_item_content(user_email, file_info['id'], drive_id=drive_id)
                extracted = doc_processor.extract_text(content, file_info['name'])
                combined_text += f"\n--- FILE: {file_info['name']} (TEXT) ---\n{extracted[:5000]}"
                
                visuals = doc_processor.extract_visuals(content, file_info['name'])
                if visuals:
                    all_images.extend(visuals)
            except Exception as e:
                logger.error(f"Error reading {file_info['name']}: {e}")

        # 3. AI Analysis
        analysis = {}
        raw_thinking = ""
        if combined_text or all_images:
            # We pass the existing audit_log to process_content so it UPDATES instead of creating a new one
            meta = {
                '_source_metadata': {
                    "file_tree": file_tree,
                    "drive_id": drive_id,
                    "folder_id": folder_id,
                    "preview_text": combined_text,
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
                images=all_images,
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
            
            # Extract text
            extracted_text = doc_processor.extract_text(content, file_name)
            
            # Create Document Record
            doc = DealDocument.objects.create(
                deal=deal,
                title=file_name,
                document_type=doc_type,
                onedrive_id=file_id,
                extracted_text=extracted_text,
                is_indexed=False # Will be set to True by vectorizer
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
            
            # Give the system a tiny breather between heavy files
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
    if errors: audit_log.error_message = f"Errors encountered: {'; '.join(errors)}"
    audit_log.save()
    
    logger.info(f"Finished background processing for Deal {deal_id}. Processed {processed_count} files.")
    return {"processed": processed_count, "errors": len(errors)}
