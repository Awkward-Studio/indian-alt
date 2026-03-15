import os
import django
import logging

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from microsoft.services.graph_service import GraphAPIService

user_email = 'dms-demo@india-alt.com'
graph = GraphAPIService()

print(f"Testing OneDrive listing for {user_email}...")

try:
    print("1. Attempting list_shared_with_me...")
    data = graph.list_shared_with_me(user_email=user_email)
    print(f"SUCCESS: Found {len(data.get('value', []))} shared items.")
except Exception as e:
    print(f"sharedWithMe failed as expected: {e}")
    print("2. Falling back to get_drive_root_children...")
    try:
        data = graph.get_drive_root_children(user_email=user_email)
        items = data.get('value', [])
        print(f"FALLBACK SUCCESS: {len(items)} items found in DMS root.")
        for i in items[:5]:
            print(f"- {i.get('name')} ({i.get('id')})")
    except Exception as e2:
        print(f"Fallback also failed: {e2}")
