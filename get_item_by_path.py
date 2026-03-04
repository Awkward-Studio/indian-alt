import requests
from msal import ConfidentialClientApplication
from decouple import config
import urllib.parse

CLIENT_ID = config('AZURE_CLIENT_ID')
CLIENT_SECRET = config('AZURE_CLIENT_SECRET')
TENANT_ID = config('AZURE_TENANT_ID')
OWNER_EMAIL = "amish.agrawal@india-alt.com"

# Path from your URL (decoded)
target_path = "Desktop/DMS Update/3. DMS Dataroom - shared folder"

authority = f"https://login.microsoftonline.com/{TENANT_ID}"
app = ConfidentialClientApplication(CLIENT_ID, client_credential=CLIENT_SECRET, authority=authority)
token_response = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" in token_response:
    headers = {'Authorization': f"Bearer {token_response['access_token']}"}
    
    print(f"Attempting to find the 'Documents' drive for {OWNER_EMAIL}...")
    url = f"https://graph.microsoft.com/v1.0/users/{OWNER_EMAIL}/drive"
    resp = requests.get(url, headers=headers)
    
    if resp.status_code == 200:
        drive_id = resp.json().get('id')
        print(f"SUCCESS! Found Drive ID: {drive_id}")
        
        encoded_path = urllib.parse.quote(target_path)
        item_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:/{encoded_path}"
        item_resp = requests.get(item_url, headers=headers)
        
        if item_resp.status_code == 200:
            print(f"SUCCESS! Found Folder ID: {item_resp.json().get('id')}")
            print(f"\nSET THIS AS YOUR DMS_DRIVE_ID in .env:\n{drive_id}")
        else:
            print(f"Path not found (Error {item_resp.status_code}). But you can use the Drive ID above.")
    else:
        print(f"Failed to find drive: {resp.status_code}")
        print(resp.text)
else:
    print("Token error")
