import requests
from msal import ConfidentialClientApplication
from decouple import config

CLIENT_ID = config('AZURE_CLIENT_ID')
CLIENT_SECRET = config('AZURE_CLIENT_SECRET')
TENANT_ID = config('AZURE_TENANT_ID')

authority = f"https://login.microsoftonline.com/{TENANT_ID}"
app = ConfidentialClientApplication(CLIENT_ID, client_credential=CLIENT_SECRET, authority=authority)

token_response = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])

if "access_token" in token_response:
    headers = {'Authorization': f"Bearer {token_response['access_token']}"}
    
    # Search for user by name
    query = "amish"
    url = f"https://graph.microsoft.com/v1.0/users?$search="displayName:{query}"&$select=displayName,userPrincipalName,id"
    # Search requires ConsistencyLevel header
    headers['ConsistencyLevel'] = 'eventual'
    
    print(f"Searching for users containing '{query}'...")
    response = requests.get(url, headers=headers)
    
    if response.status_code == 200:
        users = response.json().get('value', [])
        if not users:
            print("No users found. Trying list all users...")
            url = "https://graph.microsoft.com/v1.0/users?$top=10&$select=displayName,userPrincipalName"
            response = requests.get(url, headers=headers)
            users = response.json().get('value', [])

        for user in users:
            print(f"Name: {user.get('displayName')}")
            print(f"UPN:  {user.get('userPrincipalName')}")
            print(f"ID:   {user.get('id')}")
            print("-" * 25)
    else:
        print(f"Error {response.status_code}: {response.text}")
else:
    print("Token Error")
