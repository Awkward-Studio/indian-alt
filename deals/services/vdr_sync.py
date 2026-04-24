import logging
from django.db import transaction
from microsoft.services.graph_service import GraphAPIService
from deals.models import Deal, DealDocument

logger = logging.getLogger(__name__)

class VDRSyncService:
    """
    Service to synchronize Virtual Data Room (VDR) state with OneDrive.
    Handles 'picking up' existing analyzed files when a folder is connected.
    """

    @staticmethod
    def sync_existing_analyses_to_folder(deal: Deal, user_email: str = None):
        """
        Scans the connected OneDrive folder and links existing DealDocument records
        that match filenames.
        """
        if not deal.source_onedrive_id or not deal.source_drive_id:
            logger.warning(f"Deal {deal.id} has no connected OneDrive folder.")
            return

        graph = GraphAPIService()
        # Use a system email if none provided, or fall back to a known one
        email = user_email or "dms-demo@india-alt.com" 

        try:
            # 1. Get all files in the folder
            items = graph.list_drive_items(email, deal.source_onedrive_id, deal.source_drive_id)
            if not items:
                logger.info(f"No items found in folder {deal.source_onedrive_id}")
                return

            # 2. Get existing documents for this deal
            existing_docs = DealDocument.objects.filter(deal=deal)
            doc_map = {doc.title.lower(): doc for doc in existing_docs}

            updated_count = 0
            
            with transaction.atomic():
                for item in items:
                    if 'folder' in item:
                        continue # Skip subfolders for now, or handle recursively if needed

                    name = item.get('name')
                    if not name:
                        continue

                    normalized_name = name.lower()
                    
                    # Try to find a match in existing documents
                    doc = doc_map.get(normalized_name)
                    
                    # Also try matching without .json suffix if the script added it
                    if not doc and normalized_name.endswith('.json'):
                        doc = doc_map.get(normalized_name[:-5])

                    if doc:
                        # Link this document to the OneDrive item
                        doc.onedrive_id = item.get('id')
                        # If the script stored a file URL, we might want to update it to the OneDrive webUrl
                        doc.file_url = item.get('webUrl')
                        doc.save(update_fields=['onedrive_id', 'file_url'])
                        updated_count += 1
                        logger.info(f"Linked existing DealDocument '{doc.title}' to OneDrive ID {doc.onedrive_id}")

            return updated_count
        except Exception as e:
            logger.error(f"VDR Pickup failed for deal {deal.id}: {str(e)}")
            return 0
