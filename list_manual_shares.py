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

# The two folders you provided
urls = [
    "https://indiaalt-my.sharepoint.com/:f:/g/personal/amish_agrawal_india-alt_com/IgBg1HZEXGaLSqMaaGJCvy7aAYLO83nsqRgRSXAg9erYlrI?e=RZ8uaJ",
    "https://indiaalt-my.sharepoint.com/:f:/g/personal/amish_agrawal_india-alt_com/IgDFUxYCbzQSRYJLue2gIXFUAdHowel_T7J919ZkUijMcfA?e=8ywo4f"
]

user_email = 'dms-demo@india-alt.com'
service = GraphAPIService()
token = service.get_access_token(user_email, require_delegated=True)
headers = {'Authorization': f"Bearer {token}"}

items = []
for url in urls:
    encoded_url = encode_sharing_url(url)
    graph_url = f"https://graph.microsoft.com/v1.0/shares/{encoded_url}/driveItem"
    resp = requests.get(graph_url, headers=headers)
    if resp.status_code == 200:
        item = resp.json()
        # Flatten for consistency
        drive_id = item.get('parentReference', {}).get('driveId')
        item['driveId'] = drive_id
        items.append(item)
    else:
        print(f"Error resolving {url}: {resp.status_code}")

print(f"Successfully resolved {len(items)} folders.")
for item in items:
    print(f"- {item.get('name')} (ID: {item.get('id')}, Drive: {item.get('driveId')})")
