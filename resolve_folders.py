import os
import django
import requests
import base64

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from microsoft.models import MicrosoftToken
from microsoft.services.graph_service import GraphAPIService

def encode_sharing_url(url):
    encoded = base64.urlsafe_b64encode(url.encode('utf-8')).decode('utf-8').rstrip('=')
    return f"u!{encoded}"

urls = [
    "https://indiaalt-my.sharepoint.com/personal/amish_agrawal_india-alt_com/_layouts/15/onedrive.aspx?e=5%3A76786548e2734c2eb002f6d4b7a95266&sharingv2=true&fromShare=true&at=9&CID=703ee582-c146-4468-870a-bb9c41baab80&id=%2Fpersonal%2Famish_agrawal_india-alt_com%2FDocuments%2FDesktop%2FDMS%20Update%2F4.%20DMS%20Dataroom%20-%20shared%20folder&FolderCTID=0x0120001D7AA1AEC2510B4DBBA1B4A9C3B467C2&view=0",
    "https://indiaalt-my.sharepoint.com/personal/amish_agrawal_india-alt_com/_layouts/15/onedrive.aspx?e=5%3A3c52d4ac45c845b7a9179d52e1f5afd2&sharingv2=true&fromShare=true&at=9&CID=740c89a2-4f7d-4ab5-9b03-a7a4d6a92a50&id=%2Fpersonal%2Famish_agrawal_india-alt_com%2FDocuments%2FDocuments%2F1.%20Advanced%20Stage%20Deals%20-%20DMS&FolderCTID=0x0120001D7AA1AEC2510B4DBBA1B4A9C3B467C2&view=0"
]

user_email = 'dms-demo@india-alt.com'
service = GraphAPIService()
token = service.get_access_token(user_email, require_delegated=True)

headers = {'Authorization': f"Bearer {token}"}

for i, url in enumerate(urls, 1):
    print(f"\n--- Resolving Folder {i} ---")
    encoded_url = encode_sharing_url(url)
    graph_url = f"https://graph.microsoft.com/v1.0/shares/{encoded_url}/driveItem"
    
    resp = requests.get(graph_url, headers=headers)
    if resp.status_code == 200:
        item = resp.json()
        drive_id = item.get('parentReference', {}).get('driveId')
        item_id = item.get('id')
        name = item.get('name')
        print(f"Name:     {name}")
        print(f"DRIVE_ID: {drive_id}")
        print(f"ITEM_ID:  {item_id}")
    else:
        print(f"Error {resp.status_code}: {resp.text}")
