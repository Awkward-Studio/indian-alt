import os
import requests
from django.core.management.base import BaseCommand
from microsoft.services.graph_service import GraphAPIService, DMS_SHARED_FOLDER_URL

class Command(BaseCommand):
    help = 'Download all files from a specific folder inside the DMS shared space'

    def add_arguments(self, parser):
        parser.add_argument('--target', type=str, default='6.  Old DMS Data', help='Name of the folder inside DMS Dataroom to download')
        parser.add_argument('--dest', type=str, default='data/legacy_dms_files', help='Local destination directory')

    def handle(self, *args, **options):
        target_name = options['target']
        dest_dir = options['dest']
        user_email = 'dms-demo@india-alt.com'

        if not DMS_SHARED_FOLDER_URL:
            self.stdout.write(self.style.ERROR("DMS_SHARED_FOLDER_URL is not set in .env"))
            return

        if not os.path.exists(dest_dir):
            os.makedirs(dest_dir)

        graph = GraphAPIService()
        self.stdout.write(f"Authenticating as {user_email}...")
        self.stdout.write(f"Starting from configured base: {DMS_SHARED_FOLDER_URL}")
        
        try:
            # 1. Get info about the base shared folder
            # This uses the /shares/{encoded_url}/driveItem endpoint
            base_info = graph.get_shared_folder_info(DMS_SHARED_FOLDER_URL, user_email=user_email)
            
            # The base_info for a share has the remoteItem which contains the driveId and its own id
            remote = base_info.get('remoteItem', {})
            drive_id = remote.get('parentReference', {}).get('driveId')
            base_folder_id = remote.get('id')

            if not drive_id or not base_folder_id:
                # Fallback check if it's not a remoteItem but the item itself
                drive_id = base_info.get('parentReference', {}).get('driveId')
                base_folder_id = base_info.get('id')

            self.stdout.write(self.style.SUCCESS(f"Connected to Base: {base_info.get('name')}"))

            # 2. List children of the base folder to find the target
            children_data = graph.get_drive_item_children(drive_id, base_folder_id, user_email=user_email)
            children = children_data.get('value', [])
            
            target_folder = None
            for child in children:
                if target_name.lower() in child.get('name', '').lower():
                    target_folder = child
                    break
            
            if not target_folder:
                self.stdout.write(self.style.ERROR(f"Could not find folder '{target_name}' inside '{base_info.get('name')}'"))
                self.stdout.write("Available folders:")
                for child in children:
                    if 'folder' in child:
                        self.stdout.write(f" - {child.get('name')}")
                return

            self.stdout.write(self.style.SUCCESS(f"Found Target: {target_folder['name']} (ID: {target_folder['id']})"))

            # 3. List files in the target folder
            files_data = graph.get_drive_item_children(drive_id, target_folder['id'], user_email=user_email)
            all_items = files_data.get('value', [])
            files = [f for f in all_items if 'file' in f]
            
            self.stdout.write(f"Found {len(files)} files to download (ignoring {len(all_items) - len(files)} subfolders).")

            token = graph.get_access_token(user_email=user_email, require_delegated=True)
            headers = {'Authorization': f'Bearer {token}'}

            # 4. Download
            for f in files:
                file_name = f['name']
                # Use the pre-signed downloadUrl for efficiency
                download_url = f.get('@microsoft.graph.downloadUrl')
                if not download_url:
                    download_url = f"{graph.graph_endpoint}/drives/{drive_id}/items/{f['id']}/content"
                
                self.stdout.write(f"Downloading {file_name}...")
                resp = requests.get(download_url, headers=headers, stream=True, timeout=300)
                resp.raise_for_status()
                
                local_path = os.path.join(dest_dir, file_name)
                with open(local_path, 'wb') as out_f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        out_f.write(chunk)
                self.stdout.write(self.style.SUCCESS(f"  Saved to {local_path}"))

            self.stdout.write(self.style.SUCCESS(f"\nSuccessfully downloaded {len(files)} files to {dest_dir}"))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error: {str(e)}"))
            if hasattr(e, 'response') and e.response is not None:
                self.stdout.write(self.style.ERROR(f"Response: {e.response.text}"))
