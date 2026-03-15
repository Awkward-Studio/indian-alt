import os
import requests
import base64
from msal import ConfidentialClientApplication
from decouple import config

# Configuration from .env
CLIENT_ID = config('AZURE_CLIENT_ID')
CLIENT_SECRET = config('AZURE_CLIENT_SECRET')
TENANT_ID = config('AZURE_TENANT_ID')

# The share link you provided
SHARE_URL = "https://indiaalt-my.sharepoint.com/personal/amish_agrawal_india-alt_com/_layouts/15/onedrive.aspx?e=5%3A76786548e2734c2eb002f6d4b7a95266&sharingv2=true&fromShare=true&at=9&CID=76defb11-a0cd-4b57-916f-0f82bd4695e8&id=%2Fpersonal%2Famish_agrawal_india-alt_com%2FDocuments%2FDesktop%2FDMS%20Update%2F4.%20DMS%20Dataroom%20-%20shared%20folder&FolderCTID=0x0120001D7AA1AEC2510B4DBBA1B4A9C3B467C2&view=0"

def encode_sharing_url(url):
    encoded = base64.urlsafe_b64encode(url.encode('utf-8')).decode('utf-8').rstrip('=')
    return f"u!{encoded}"

authority = f"https://login.microsoftonline.com/{TENANT_ID}"
app = ConfidentialClientApplication(CLIENT_ID, client_credential=CLIENT_SECRET, authority=authority)

print(f"Resolving Share URL via Graph API...")
token_response = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" in token_response:
    headers = {'Authorization': f"Bearer {token_response['access_token']}"}
    
    encoded_url = encode_sharing_url(SHARE_URL)
    url = f"https://graph.microsoft.com/v1.0/shares/{encoded_url}/driveItem"
    
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        item = response.json()
        drive_id = item.get('parentReference', {}).get('driveId')
        item_id = item.get('id')
        
        print(f"\n[SUCCESS] Resource Resolved:")
        print(f"Name:      {item.get('name')}")
        print(f"DRIVE_ID:  {drive_id}")
        print(f"ITEM_ID:   {item_id}")
        print(f"Web URL:   {item.get('webUrl')}")
        
        print("\n--- UPDATE YOUR .env ---")
        print(f"DMS_DRIVE_ID={drive_id}")
        print(f"DMS_FOLDER_PATH= (Leave empty if using ITEM_ID directly)")
    else:
        print(f"Error: {response.status_code}")
        print(response.text)
else:
    print("Token Error")
