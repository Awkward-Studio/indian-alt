import requests
import json
from msal import PublicClientApplication

# Note: This uses PublicClientApplication for Device Code Flow
CLIENT_ID = "b810b124-b097-46dd-882b-15ef80db58f5"
TENANT_ID = "77498d9f-ab76-4aec-a9d6-3e9178c663db"

authority = f"https://login.microsoftonline.com/{TENANT_ID}"
app = PublicClientApplication(CLIENT_ID, authority=authority)

# 1. Start Device Code Flow
flow = app.initiate_device_flow(scopes=["https://graph.microsoft.com/Files.Read.All", "https://graph.microsoft.com/Sites.Read.All"])
if "user_code" not in flow:
    print("Failed to initiate flow")
    exit()

print(f"\n{flow['message']}")

# 2. Poll for token
result = app.acquire_token_by_device_flow(flow)

if "access_token" in result:
    headers = {'Authorization': f"Bearer {result['access_token']}"}
    
    # Check 'Shared with me' items
    print("\nSearching items shared with you...")
    url = "https://graph.microsoft.com/v1.0/me/drive/sharedWithMe"
    resp = requests.get(url, headers=headers)
    
    if resp.status_code == 200:
        items = resp.json().get('value', [])
        if not items:
            print("No shared items found.")
        for item in items:
            name = item.get('name')
            remote = item.get('remoteItem', {})
            drive_id = remote.get('parentReference', {}).get('driveId')
            print(f"\nFound Shared Item: {name}")
            print(f"DRIVE ID: {drive_id}")
            print(f"ITEM ID:  {remote.get('id')}")
    else:
        print(f"Error {resp.status_code}: {resp.text}")
else:
    print("Failed to get token")
