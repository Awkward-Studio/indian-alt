import os
import django
import requests
import base64

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from microsoft.services.graph_service import GraphAPIService

def encode_sharing_url(url):
    encoded = base64.urlsafe_b64encode(url.encode('utf-8')).decode('utf-8').rstrip('=')
    return f"u!{encoded}"

from decouple import config
env_url = config('DMS_SHARED_FOLDER_URL', default='')
urls = [u.strip() for u in env_url.split(',') if u.strip()]
print(f"DEBUG: Found {len(urls)} URLs in .env")

user_email = 'dms-demo@india-alt.com'
service = GraphAPIService()
token = service.get_access_token(user_email, require_delegated=True)
headers = {'Authorization': f"Bearer {token}"}

for url in urls:
    print(f"\nTesting URL: {url}")
    encoded = encode_sharing_url(url)
    
    # Test /driveItem
    info_url = f"https://graph.microsoft.com/v1.0/shares/{encoded}/driveItem"
    res_info = requests.get(info_url, headers=headers)
    print(f"  /driveItem status: {res_info.status_code}")
    if res_info.status_code != 200:
        print(f"  Error: {res_info.text}")
        
    # Test /driveItem/children
    children_url = f"https://graph.microsoft.com/v1.0/shares/{encoded}/driveItem/children"
    res_children = requests.get(children_url, headers=headers)
    print(f"  /driveItem/children status: {res_children.status_code}")
