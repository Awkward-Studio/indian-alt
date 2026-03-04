import os
import requests
from msal import ConfidentialClientApplication
from decouple import config

# Load from .env
CLIENT_ID = config('AZURE_CLIENT_ID')
CLIENT_SECRET = config('AZURE_CLIENT_SECRET')
TENANT_ID = config('AZURE_TENANT_ID')
USER_EMAIL = config('DMS_USER_EMAIL')

authority = f"https://login.microsoftonline.com/{TENANT_ID}"
app = ConfidentialClientApplication(CLIENT_ID, client_credential=CLIENT_SECRET, authority=authority)

# Get Token with Application Permissions
print(f"Acquiring token for tenant {TENANT_ID}...")
token_response = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" in token_response:
    headers = {'Authorization': f"Bearer {token_response['access_token']}"}
    
    # 1. First, let's try to find all Sites the user has access to
    print(f"\nSearching for SharePoint sites accessible to {USER_EMAIL}...")
    # This searches across the tenant for sites.
    sites_url = "https://graph.microsoft.com/v1.0/sites?search=*"
    sites_resp = requests.get(sites_url, headers=headers)
    
    if sites_resp.status_code == 200:
        sites = sites_resp.json().get('value', [])
        for site in sites:
            site_id = site.get('id')
            site_name = site.get('displayName')
            print(f"\n[SITE] {site_name}")
            print(f"Site ID: {site_id}")
            
            # 2. For each site, list its Drives (Document Libraries)
            drives_url = f"https://graph.microsoft.com/v1.0/sites/{site_id}/drives"
            drives_resp = requests.get(drives_url, headers=headers)
            
            if drives_resp.status_code == 200:
                drives = drives_resp.json().get('value', [])
                for drive in drives:
                    print(f"  -> DRIVE Name: {drive.get('name')}")
                    print(f"     DRIVE ID:   {drive.get('id')}")
            else:
                print(f"  (Could not list drives for this site: {drives_resp.status_code})")
    
    # 3. Fallback: Check the user's direct drives (OneDrive)
    print(f"\nChecking personal OneDrive for {USER_EMAIL}...")
    user_drives_url = f"https://graph.microsoft.com/v1.0/users/{USER_EMAIL}/drives"
    user_drives_resp = requests.get(user_drives_url, headers=headers)
    if user_drives_resp.status_code == 200:
        for d in user_drives_resp.json().get('value', []):
            print(f"[USER DRIVE] {d.get('name')}")
            print(f"ID: {d.get('id')}")
    
    print("\n---------------------------------------------------------")
    print("If you see the shared SharePoint library above, copy its DRIVE ID")
    print("and paste it into your .env as DMS_DRIVE_ID.")
    print("---------------------------------------------------------")

else:
    print("Error acquiring token:")
    print(token_response.get("error_description"))
