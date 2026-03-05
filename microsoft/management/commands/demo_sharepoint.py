"""
Demo command: prove we can read, browse, view, and download files from the
DMS shared folder on SharePoint / OneDrive.

Usage
-----
# 1. List root of the shared folder
python manage.py demo_sharepoint

# 2. Drill into a subfolder (use folder ID from step 1)
python manage.py demo_sharepoint --folder FOLDER_ID

# 3. View metadata of a specific file
python manage.py demo_sharepoint --detail ITEM_ID

# 4. Download a file to disk
python manage.py demo_sharepoint --download ITEM_ID

# 5. Download to a custom path
python manage.py demo_sharepoint --download ITEM_ID --output ./my_file.pdf
"""
import os
import json
import logging
from django.core.management.base import BaseCommand, CommandError
from microsoft.services.graph_service import (
    GraphAPIService,
    DMS_DRIVE_ID,
    DMS_FOLDER_PATH,
    DMS_USER_EMAIL,
)

logger = logging.getLogger(__name__)


def _fmt_size(size, is_folder=False, folder_info=None):
    if is_folder:
        count = (folder_info or {}).get('childCount', '?')
        return f'{count} items'
    if size >= 1_048_576:
        return f'{size / 1_048_576:.1f} MB'
    if size >= 1024:
        return f'{size / 1024:.1f} KB'
    return f'{size} B'


class Command(BaseCommand):
    help = (
        'Demo: browse, view details, and download files from the DMS '
        'SharePoint shared folder via Microsoft Graph API.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--folder', type=str, default=None,
            help='Folder item ID to list (omit for DMS root)',
        )
        parser.add_argument(
            '--detail', type=str, default=None,
            help='Item ID to show full metadata for',
        )
        parser.add_argument(
            '--download', type=str, default=None,
            help='Item ID to download to disk',
        )
        parser.add_argument(
            '--output', '-o', type=str, default=None,
            help='Output path for --download (defaults to ./downloads/<filename>)',
        )
        parser.add_argument(
            '--limit', type=int, default=50,
            help='Max items to list (default 50)',
        )

    # ─── Handlers ────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        self.graph = GraphAPIService()

        if options['detail']:
            return self._show_detail(options['detail'])

        if options['download']:
            return self._download_file(options['download'], options.get('output'))

        # Default: list folder
        return self._list_folder(options.get('folder'), options['limit'])

    # ── LIST ──────────────────────────────────────────────────────────────

    def _list_folder(self, folder_id, limit):
        self.stdout.write(self.style.MIGRATE_HEADING(
            '\n[BROWSE] DMS Shared Folder' + (f'  (subfolder {folder_id})' if folder_id else ' (root)')
        ))
        self.stdout.write(f'   Drive : {DMS_DRIVE_ID[:30]}...')
        self.stdout.write(f'   Path  : {DMS_FOLDER_PATH}')
        self.stdout.write(f'   User  : {DMS_USER_EMAIL}\n')

        try:
            if folder_id:
                data = self.graph.get_drive_item_children(
                    drive_id=DMS_DRIVE_ID, item_id=folder_id,
                    user_email=DMS_USER_EMAIL, top=limit,
                )
            else:
                data = self.graph.list_folder_by_drive_path(
                    drive_id=DMS_DRIVE_ID, folder_path=DMS_FOLDER_PATH,
                    user_email=DMS_USER_EMAIL, top=limit,
                )
        except Exception as e:
            raise CommandError(f'Graph API error: {e}')

        items = data.get('value', [])
        if not items:
            self.stdout.write(self.style.WARNING('  (empty folder)'))
            return

        # Header
        self.stdout.write(
            f'  {"Type":<8} {"Name":<45} {"Size":<12} {"Modified":<20} {"ID"}'
        )
        self.stdout.write('  ' + '-' * 130)

        folders = []
        files = []

        for item in items:
            is_folder = 'folder' in item
            name = item.get('name', '???')
            size = item.get('size', 0)
            modified = item.get('lastModifiedDateTime', '')[:19].replace('T', ' ')
            item_id = item.get('id', '')
            size_str = _fmt_size(size, is_folder, item.get('folder'))

            if is_folder:
                folders.append(item)
                type_tag = self.style.WARNING('FOLDER ')
            else:
                files.append(item)
                type_tag = '  FILE  '

            self.stdout.write(f'  {type_tag} {name:<45} {size_str:<12} {modified:<20} {item_id}')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'  OK: {len(folders)} folder(s), {len(files)} file(s)  --  {len(items)} total'
        ))

        # Hints
        if folders:
            self.stdout.write(self.style.NOTICE(
                f'\n  -> Drill into a folder:\n'
                f'    python manage.py demo_sharepoint --folder {folders[0]["id"]}'
            ))
        if files:
            self.stdout.write(self.style.NOTICE(
                f'\n  -> View file details:\n'
                f'    python manage.py demo_sharepoint --detail {files[0]["id"]}'
            ))
            self.stdout.write(self.style.NOTICE(
                f'\n  -> Download a file:\n'
                f'    python manage.py demo_sharepoint --download {files[0]["id"]}'
            ))
        self.stdout.write('')

    # ── DETAIL ────────────────────────────────────────────────────────────

    def _show_detail(self, item_id):
        self.stdout.write(self.style.MIGRATE_HEADING(f'\n[DETAIL] File/Folder  --  {item_id}\n'))

        try:
            item = self.graph.get_drive_item(DMS_DRIVE_ID, item_id)
        except Exception as e:
            raise CommandError(f'Graph API error: {e}')

        is_folder = 'folder' in item
        name = item.get('name', '???')
        size = item.get('size', 0)

        self.stdout.write(f'  Name          : {name}')
        self.stdout.write(f'  Type          : {"Folder" if is_folder else "File"}')
        self.stdout.write(f'  Size          : {_fmt_size(size, is_folder, item.get("folder"))}')
        self.stdout.write(f'  Created       : {item.get("createdDateTime", "?")}')
        self.stdout.write(f'  Modified      : {item.get("lastModifiedDateTime", "?")}')
        self.stdout.write(f'  Created By    : {item.get("createdBy", {}).get("user", {}).get("displayName", "?")}')
        self.stdout.write(f'  Modified By   : {item.get("lastModifiedBy", {}).get("user", {}).get("displayName", "?")}')
        self.stdout.write(f'  Web URL       : {item.get("webUrl", "?")}')

        dl_url = item.get('@microsoft.graph.downloadUrl', '')
        if dl_url:
            self.stdout.write(f'  Download URL  : {dl_url[:120]}...')

        if not is_folder and item.get('file'):
            mime = item['file'].get('mimeType', '?')
            self.stdout.write(f'  MIME Type     : {mime}')

        # Parent path
        parent = item.get('parentReference', {})
        self.stdout.write(f'  Parent Path   : {parent.get("path", "?")}')
        self.stdout.write(f'  Drive ID      : {parent.get("driveId", DMS_DRIVE_ID)}')

        self.stdout.write(self.style.SUCCESS('\n  OK: Item retrieved successfully\n'))

        if not is_folder:
            self.stdout.write(self.style.NOTICE(
                f'  -> Download this file:\n'
                f'    python manage.py demo_sharepoint --download {item_id}\n'
            ))

    # ── DOWNLOAD ──────────────────────────────────────────────────────────

    def _download_file(self, item_id, output_path):
        self.stdout.write(self.style.MIGRATE_HEADING(f'\n[DOWNLOAD]  --  {item_id}\n'))

        # 1. Get metadata first (for filename)
        try:
            item = self.graph.get_drive_item(DMS_DRIVE_ID, item_id)
        except Exception as e:
            raise CommandError(f'Graph API error getting metadata: {e}')

        filename = item.get('name', f'download_{item_id}')
        size = item.get('size', 0)
        self.stdout.write(f'  File : {filename}')
        self.stdout.write(f'  Size : {_fmt_size(size)}')

        if 'folder' in item:
            raise CommandError(f'{filename} is a folder, not a file. Use --folder to list it.')

        # 2. Download raw bytes
        self.stdout.write('  Downloading ...')
        try:
            content = self.graph.get_drive_item_content(
                user_email=DMS_USER_EMAIL, file_id=item_id, drive_id=DMS_DRIVE_ID,
            )
        except Exception as e:
            raise CommandError(f'Graph API error downloading: {e}')

        if not content:
            raise CommandError('Download returned empty content.')

        # 3. Write to disk
        if not output_path:
            os.makedirs('downloads', exist_ok=True)
            output_path = os.path.join('downloads', filename)

        with open(output_path, 'wb') as f:
            f.write(content)

        self.stdout.write(self.style.SUCCESS(
            f'\n  OK: Saved {len(content):,} bytes -> {os.path.abspath(output_path)}\n'
        ))
