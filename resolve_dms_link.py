import os
import requests
from msal import ConfidentialClientApplication
from decouple import config

# Configuration from .env
CLIENT_ID = config('AZURE_CLIENT_ID')
CLIENT_SECRET = config('AZURE_CLIENT_SECRET')
TENANT_ID = config('AZURE_TENANT_ID')
USER_EMAIL = "amish.agrawal@india-alt.com" 

# The path from your URL (relative to the drive root)
# URL: ...id=%2Fpersonal%2Famish_agrawal_india-alt_com%2FDocuments%2FDesktop%2FDMS%20Update%2F4.%20DMS%20Dataroom%20-%20shared%20folder
# SharePoint paths usually strip the leading "/personal/user/Documents/"
TARGET_PATH = "Desktop/DMS Update/4. DMS Dataroom - shared folder"

authority = f"https://login.microsoftonline.com/{TENANT_ID}"
app = ConfidentialClientApplication(CLIENT_ID, client_credential=CLIENT_SECRET, authority=authority)

print(f"Resolving folder for: {USER_EMAIL}")
print(f"Target Path: {TARGET_PATH}")

token_response = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" in token_response:
    headers = {'Authorization': f"Bearer {token_response['access_token']}"}
    
    # 1. Get the drive ID
    drives_url = f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}/drives"
    drives_resp = requests.get(drives_url, headers=headers)
    
    if drives_resp.status_code == 200:
        drives = drives_resp.json().get('value', [])
        if not drives:
            print("No drives found for this user.")
            exit()
            
        drive = drives[0] # Usually the 'OneDrive' library
        drive_id = drive['id']
        print(f"\n[SUCCESS] Found Drive: {drive['name']}")
        print(f"DRIVE_ID: {drive_id}")
        
        # 2. Resolve the specific folder ID by path
        # Note: We use the /root:/path:/ notation
        item_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{TARGET_PATH}"
        item_resp = requests.get(item_url, headers=headers)
        
        if item_resp.status_code == 200:
            item = item_resp.json()
            print(f"\n[SUCCESS] Found Folder: {item['name']}")
            print(f"FOLDER_ID (ITEM_ID): {item['id']}")
            print(f"WEB_URL: {item['webUrl']}")
            
            print("\n--- UPDATE YOUR .env ---")
            print(f"DMS_DRIVE_ID={drive_id}")
            print(f"DMS_FOLDER_PATH={TARGET_PATH}")
        else:
            print(f"\n[ERROR] Could not resolve folder path. Status: {item_resp.status_code}")
            print(item_resp.text)
    else:
        print(f"Error fetching drives: {drives_resp.status_code}")
        print(drives_resp.text)
else:
    print("Error acquiring token")
