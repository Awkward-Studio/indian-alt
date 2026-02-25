"""
Management command to list files and folders from OneDrive via Microsoft Graph API.

Usage:
    python manage.py list_onedrive --email dms-demo@india-alt.com --shared-url "https://..."
    python manage.py list_onedrive --email dms-demo@india-alt.com --drive-id DRIVE_ID --path "Desktop/DMS Update/..."
    python manage.py list_onedrive --email dms-demo@india-alt.com --drive-id DRIVE_ID --item-id ITEM_ID
"""
import json
import logging
from django.core.management.base import BaseCommand, CommandError
from microsoft.services.graph_service import GraphAPIService

logger = logging.getLogger(__name__)

# Hardcoded known values for the DMS shared folder
DMS_DRIVE_ID = 'b!3S_Fhil_uEKQVZnv_LhVSs0jBzTo-59CpDghDEe3hAZpHh-zpeg8QbO1VWqjQeKg'
DMS_FOLDER_PATH = 'Desktop/DMS Update/3. DMS Dataroom - shared folder'


class Command(BaseCommand):
    """Management command to list OneDrive files and folders."""

    help = 'List files and folders from OneDrive/SharePoint via Microsoft Graph API'

    def add_arguments(self, parser):
        parser.add_argument(
            '--email',
            type=str,
            required=True,
            help='Email address used for authentication (e.g. dms-demo@india-alt.com)',
        )
        parser.add_argument(
            '--drive-id',
            type=str,
            default=None,
            help='Drive ID (defaults to the known DMS drive)',
        )
        parser.add_argument(
            '--path',
            type=str,
            default=None,
            help='Folder path within the drive (defaults to the DMS shared folder)',
        )
        parser.add_argument(
            '--item-id',
            type=str,
            default=None,
            help='Item ID to list children of (overrides --path)',
        )
        parser.add_argument(
            '--shared-url',
            type=str,
            default=None,
            help='SharePoint/OneDrive sharing URL to access directly',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            dest='output_json',
            help='Output raw JSON instead of the formatted table',
        )
        parser.add_argument(
            '--mock',
            action='store_true',
            help='Use mock data instead of calling Azure (for testing)',
        )

    def handle(self, *args, **options):
        email = options['email']
        drive_id = options.get('drive_id') or DMS_DRIVE_ID
        folder_path = options.get('path') or DMS_FOLDER_PATH
        item_id = options.get('item_id')
        shared_url = options.get('shared_url')
        output_json = options.get('output_json', False)

        self.stdout.write(f'Fetching OneDrive items for {email} ...')

        try:
            if options.get('mock'):
                from microsoft.views import ONEDRIVE_MOCK_DATA
                items = ONEDRIVE_MOCK_DATA[:50]
                data = {'value': items}
            else:
                graph = GraphAPIService()

                if shared_url:
                    data = graph.list_shared_folder(user_email=email, sharing_url=shared_url)
                elif item_id:
                    data = graph.get_drive_item_children(user_email=email, drive_id=drive_id, item_id=item_id)
                else:
                    data = graph.list_folder_by_drive_path(user_email=email, drive_id=drive_id, folder_path=folder_path)

                items = data.get('value', [])

            if not items:
                self.stdout.write(self.style.WARNING('No items found.'))
                return

            # ---- raw JSON output ----
            if output_json:
                self.stdout.write(json.dumps(items, indent=2, default=str))
                return

            # ---- formatted table output ----
            self.stdout.write(
                self.style.SUCCESS(f'\nFound {len(items)} item(s):\n')
            )

            # Header
            self.stdout.write(
                f'{"Type":<8} {"Name":<40} {"Size":<12} {"Last Modified":<22} {"ID"}'
            )
            self.stdout.write('-' * 120)

            for item in items:
                is_folder = item.get('folder') is not None
                item_type = 'FOLDER' if is_folder else 'FILE'
                name = item.get('name', '???')
                size = item.get('size', 0)
                modified = item.get('lastModifiedDateTime', '')[:19].replace('T', ' ')
                item_id = item.get('id', '')

                # Format size
                if is_folder:
                    child_count = item.get('folder', {}).get('childCount', '?')
                    size_str = f'{child_count} items'
                elif size >= 1_048_576:
                    size_str = f'{size / 1_048_576:.1f} MB'
                elif size >= 1024:
                    size_str = f'{size / 1024:.1f} KB'
                else:
                    size_str = f'{size} B'

                # Color based on type
                type_display = self.style.WARNING(f'{item_type:<8}') if is_folder else f'{item_type:<8}'

                self.stdout.write(
                    f'{type_display} {name:<40} {size_str:<12} {modified:<22} {item_id}'
                )

            # Footer
            next_link = data.get('@odata.nextLink')
            if next_link:
                self.stdout.write(
                    self.style.NOTICE(
                        f'\nMore items available. Use --limit {limit + 50} to see more.'
                    )
                )

        except ValueError as e:
            raise CommandError(f'Configuration error: {e}')
        except Exception as e:
            logger.error(f'Error in list_onedrive: {e}', exc_info=True)
            raise CommandError(f'Failed to list OneDrive items: {e}')
