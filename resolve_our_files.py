import os
import django
import requests
from msal import ConfidentialClientApplication
from decouple import config

# Initialize Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

# Configuration from .env
CLIENT_ID = config('AZURE_CLIENT_ID')
CLIENT_SECRET = config('AZURE_CLIENT_SECRET')
TENANT_ID = config('AZURE_TENANT_ID')
USER_EMAIL = config('DMS_USER_EMAIL')

authority = f"https://login.microsoftonline.com/{TENANT_ID}"
app = ConfidentialClientApplication(CLIENT_ID, client_credential=CLIENT_SECRET, authority=authority)

print(f"Resolving 'Our Files' for account: {USER_EMAIL}")

token_response = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" in token_response:
    headers = {'Authorization': f"Bearer {token_response['access_token']}"}
    
    # 1. Check Shared With Me
    print("\n[1] Checking 'Shared With Me'...")
    url = f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}/drive/sharedWithMe"
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        items = resp.json().get('value', [])
        for item in items:
            name = item.get('name')
            remote = item.get('remoteItem', {})
            drive_id = remote.get('parentReference', {}).get('driveId')
            print(f" -> Found: {name} (Drive: {drive_id})")
    
    # 2. Check Sites
    print("\n[2] Searching all SharePoint Sites...")
    sites_url = "https://graph.microsoft.com/v1.0/sites?search=*"
    resp = requests.get(sites_url, headers=headers)
    if resp.status_code == 200:
        sites = resp.json().get('value', [])
        for site in sites:
            site_name = site.get('displayName')
            site_id = site.get('id')
            print(f" Site: {site_name}")
            
            # List drives for this site
            drives_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
            d_resp = requests.get(drives_url, headers=headers)
            if d_resp.status_code == 200:
                drives = d_resp.json().get('value', [])
                for drive in drives:
                    print(f"   -> Drive: {drive.get('name')} (ID: {drive.get('id')})")

    # 3. Check User's own drives
    print("\n[3] Checking User's own Drives...")
    drives_url = f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}/drives"
    resp = requests.get(drives_url, headers=headers)
    if resp.status_code == 200:
        drives = resp.json().get('value', [])
        for drive in drives:
            print(f" -> Found: {drive.get('name')} (ID: {drive.get('id')})")

    print("\n---------------------------------------------------------")
    print("Look for 'Our Files' in the lists above.")
    print("If found, copy the ID and update DMS_DRIVE_ID in your .env")
    print("---------------------------------------------------------")
else:
    print("Error acquiring token")
