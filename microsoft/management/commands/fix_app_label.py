"""
One-time management command to rename the 'emails' app label to 'microsoft'
in the django_migrations table.

Run this ONCE on Railway (or any existing DB) before deploying the renamed app:
    python manage.py fix_app_label

Safe to run multiple times — it's a no-op if already fixed.
Delete this file after the migration is done on all environments.
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = (
        "Rename app label 'emails' -> 'microsoft' in django_migrations table. "
        "Run once on existing databases after the emails->microsoft app rename."
    )

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            # Check if there are any old 'emails' rows
            cursor.execute(
                "SELECT COUNT(*) FROM django_migrations WHERE app = 'emails'"
            )
            count = cursor.fetchone()[0]

            if count == 0:
                self.stdout.write(
                    self.style.SUCCESS("Nothing to fix — no 'emails' entries found.")
                )
                return

            # Rename them
            cursor.execute(
                "UPDATE django_migrations SET app = 'microsoft' WHERE app = 'emails'"
            )

            # Also update content types if the table exists
            try:
                cursor.execute(
                    "UPDATE django_content_type SET app_label = 'microsoft' "
                    "WHERE app_label = 'emails'"
                )
                ct_msg = " + content types updated"
            except Exception:
                ct_msg = ""

            # Update auth permissions that reference the old app label
            try:
                cursor.execute(
                    "UPDATE auth_permission SET codename = REPLACE(codename, 'emails_', 'microsoft_') "
                    "WHERE codename LIKE 'emails_%'"
                )
            except Exception:
                pass

            self.stdout.write(
                self.style.SUCCESS(
                    f"Done — renamed {count} migration row(s) from 'emails' to 'microsoft'{ct_msg}."
                )
            )
            self.stdout.write(
                self.style.WARNING(
                    "You can delete microsoft/management/commands/fix_app_label.py now."
                )
            )
