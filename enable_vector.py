import os
import django
from django.db import connection

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

def enable_vector():
    print("Checking for pgvector extension...")
    try:
        with connection.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
        print("Success: pgvector extension enabled.")
    except Exception as e:
        print(f"Error enabling pgvector: {e}")
        print("\nNote: You may need superuser privileges on the database to create extensions.")
        print(f"Current DB Connection: {connection.settings_dict.get('HOST')}:{connection.settings_dict.get('PORT')}/{connection.settings_dict.get('NAME')}")

if __name__ == "__main__":
    enable_vector()
