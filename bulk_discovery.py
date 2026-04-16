import os
import django
import json
import base64

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from microsoft.services.graph_service import GraphAPIService

def encode_sharing_url(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode('utf-8')).decode('utf-8').rstrip('=')
    return f"u!{encoded}"

def get_deal_stats(graph, drive_id, root_id, user_email):
    """Recursively counts files and folders in a deal tree."""
    file_count = 0
    folder_count = 0
    queue = [root_id]
    token = graph.get_access_token(user_email, require_delegated=True)
    
    while queue:
        current_id = queue.pop(0)
        try:
            endpoint = f"/drives/{drive_id}/items/{current_id}/children"
            params = {'$top': 999, '$select': 'id,folder,file'}
            data = graph._make_request('GET', endpoint, token, params)
            items = data.get('value', [])
            
            for item in items:
                if 'folder' in item:
                    folder_count += 1
                    queue.append(item['id'])
                elif 'file' in item:
                    file_count += 1
        except Exception as e:
            print(f"      Error counting children for {current_id}: {e}")
            
    return file_count, folder_count

def discover():
    graph = GraphAPIService()
    sharing_url = "https://indiaalt-my.sharepoint.com/personal/amish_agrawal_india-alt_com/_layouts/15/onedrive.aspx?id=%2Fpersonal%2Famish_agrawal_india-alt_com%2FDocuments%2FDocuments%2F2.%20DMS%20Update%2F4.%20DMS%20Dataroom%2F4.%20Deal%20Folder%2F3.%20New%20Set%20of%20Deals"
    user_email = "dms-demo@india-alt.com"
    
    print(f"Discovering deals in: {sharing_url}")
    
    try:
        encoded = encode_sharing_url(sharing_url)
        root_info = graph._make_request('GET', f"/shares/{encoded}/driveItem", graph.get_access_token(user_email, require_delegated=True))
        
        drive_id = root_info['parentReference']['driveId']
        root_id = root_info['id']
        
        print(f"Found Root: {root_info.get('name')}")
        
        # List top-level deal folders
        children = graph.get_drive_item_children(drive_id, root_id, user_email)
        deal_items = [item for item in children.get('value', []) if 'folder' in item]
        
        print(f"Found {len(deal_items)} potential deal folders. Gathering stats (this may take a minute)...")
        
        deals = []
        for i, item in enumerate(deal_items, 1):
            name = item['name']
            print(f"  [{i}/{len(deal_items)}] Analyzing: {name}...")
            
            file_count, folder_count = get_deal_stats(graph, drive_id, item['id'], user_email)
            
            deals.append({
                'name': name,
                'id': item['id'],
                'drive_id': drive_id,
                'file_count': file_count,
                'subfolder_count': folder_count
            })
            print(f"      -> {file_count} files, {folder_count} subfolders")
        
        # Save to file
        output = {
            'deals': deals, 
            'drive_id': drive_id, 
            'root_id': root_id, 
            'user_email': user_email,
            'total_files_discovered': sum(d['file_count'] for d in deals),
            'total_folders_discovered': sum(d['subfolder_count'] for d in deals)
        }
        
        with open('deal_discovery.json', 'w') as f:
            json.dump(output, f, indent=2)
            
        print("\n" + "="*30)
        print(f"DISCOVERY COMPLETE")
        print(f"Total Deals:   {len(deals)}")
        print(f"Total Files:   {output['total_files_discovered']}")
        print(f"Total Folders: {output['total_folders_discovered']}")
        print(f"Saved to deal_discovery.json")
        
    except Exception as e:
        print(f"Error during discovery: {e}")

if __name__ == "__main__":
    discover()
