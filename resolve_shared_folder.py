import os
import requests
from msal import ConfidentialClientApplication
from decouple import config

# Configuration from .env
CLIENT_ID = config('AZURE_CLIENT_ID')
CLIENT_SECRET = config('AZURE_CLIENT_SECRET')
TENANT_ID = config('AZURE_TENANT_ID')

# The account that the folder is shared WITH
USER_EMAIL = "dms-demo@india-alt.com" 

# Part of the name to look for in 'Shared With Me'
SEARCH_NAME = "4. DMS Dataroom - shared folder"

authority = f"https://login.microsoftonline.com/{TENANT_ID}"
app = ConfidentialClientApplication(CLIENT_ID, client_credential=CLIENT_SECRET, authority=authority)

print(f"Searching 'Shared With Me' for account: {USER_EMAIL}")
print(f"Looking for item: {SEARCH_NAME}")

token_response = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" in token_response:
    headers = {'Authorization': f"Bearer {token_response['access_token']}"}
    
    # Use the sharedWithMe endpoint for the demo account
    url = f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}/drive/sharedWithMe"
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        items = response.json().get('value', [])
        found = False
        print(f"\nScanning {len(items)} shared items...")
        
        for item in items:
            name = item.get('name')
            remote = item.get('remoteItem', {})
            
            if SEARCH_NAME.lower() in name.lower():
                found = True
                drive_id = remote.get('parentReference', {}).get('driveId')
                item_id = remote.get('id')
                
                print(f"\n[MATCH FOUND]")
                print(f"Name:     {name}")
                print(f"DRIVE_ID: {drive_id}")
                print(f"ITEM_ID:  {item_id}")
                print(f"Web URL:  {item.get('webUrl')}")
                
                print("\n--- UPDATE YOUR .env ---")
                print(f"DMS_DRIVE_ID={drive_id}")
                print(f"DMS_FOLDER_PATH= (Leave empty if using ITEM_ID directly)")
        
        if not found:
            print("\n[NOT FOUND] No items matching that name were found in 'Shared With Me'.")
            print("Items currently visible:")
            for item in items[:10]:
                print(f" - {item.get('name')}")
    else:
        print(f"Error: {response.status_code}")
        print(response.text)
else:
    print("Token Error")
