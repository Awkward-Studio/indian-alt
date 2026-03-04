import os
import requests
from msal import ConfidentialClientApplication
from decouple import config

# Load from .env
CLIENT_ID = config('AZURE_CLIENT_ID')
CLIENT_SECRET = config('AZURE_CLIENT_SECRET')
TENANT_ID = config('AZURE_TENANT_ID')
# Corrected Email
OWNER_EMAIL = "amish.agrawal@india-alt.com" 

authority = f"https://login.microsoftonline.com/{TENANT_ID}"
app = ConfidentialClientApplication(CLIENT_ID, client_credential=CLIENT_SECRET, authority=authority)

print(f"Acquiring token to access shared drive of {OWNER_EMAIL}...")
token_response = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" in token_response:
    headers = {'Authorization': f"Bearer {token_response['access_token']}"}
    
    url = f"https://graph.microsoft.com/v1.0/users/{OWNER_EMAIL}/drives"
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        drives = response.json().get('value', [])
        print("\n--- FOUND DRIVES FOR OWNER ---")
        for drive in drives:
            print(f"Name: {drive.get('name')}")
            print(f"ID:   {drive.get('id')}")
            print("-" * 25)
    else:
        print(f"Error {response.status_code}: {response.text}")
else:
    print("Error acquiring token")
