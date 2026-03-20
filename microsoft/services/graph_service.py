"""
Microsoft Graph API service for authentication, email, and OneDrive operations.
"""
import base64
import logging
import os
from typing import Optional, Dict, Any, List, Tuple
from django.utils import timezone
from datetime import timedelta
from decouple import config
import requests
from requests import HTTPError
import msal

from ..models import MicrosoftToken

logger = logging.getLogger(__name__)

# ─── Known DMS Shared Folder constants ────────────────────────────────
DMS_DRIVE_ID = os.environ.get('DMS_DRIVE_ID') or config(
    'DMS_DRIVE_ID',
    default='b!3S_Fhil_uEKQVZnv_LhVSs0jBzTo-59CpDghDEe3hAZpHh-zpeg8QbO1VWqjQeKg',
)
DMS_FOLDER_PATH = os.environ.get('DMS_FOLDER_PATH') or config(
    'DMS_FOLDER_PATH',
    default='Documents/1. Advanced Stage Deals - DMS',
)
DMS_USER_EMAIL = os.environ.get('DMS_USER_EMAIL') or config(
    'DMS_USER_EMAIL',
    default='dms-demo@india-alt.com',
)
DMS_SHARED_FOLDER_URL = config(
    'DMS_SHARED_FOLDER_URL',
    default=os.environ.get('DMS_SHARED_FOLDER_URL', ''),
)


class GraphAPIService:
    """
    Wrapper around the Microsoft Graph API.

    Handles:
    - Token management (delegated ROPC + refresh, application fallback)
    - Email retrieval (application permissions)
    - OneDrive / SharePoint file browsing and download (delegated permissions)
    """

    def __init__(self):
        self.client_id = os.environ.get('AZURE_CLIENT_ID') or config('AZURE_CLIENT_ID', default='')
        self.client_secret = os.environ.get('AZURE_CLIENT_SECRET') or config('AZURE_CLIENT_SECRET', default='')
        self.tenant_id = os.environ.get('AZURE_TENANT_ID') or config('AZURE_TENANT_ID', default='')
        
        if not self.tenant_id:
            logger.error("AZURE_TENANT_ID is missing from environment!")
            
        self.authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self.graph_endpoint = os.environ.get('GRAPH_API_ENDPOINT') or config('GRAPH_API_ENDPOINT', default='https://graph.microsoft.com/v1.0')
        
        self.msal_app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=self.authority,
            client_credential=self.client_secret,
        )
        # Device-code flow uses a public client (interactive in browser, no redirect URI required)
        self.msal_public_app = msal.PublicClientApplication(
            self.client_id,
            authority=self.authority,
        )

    # ─── Auth ────────────────────────────────────────────────────────────

    def get_access_token(self, user_email: str = DMS_USER_EMAIL, 
                         require_delegated: bool = False,
                         prefer_application: bool = False) -> Optional[str]:
        """
        Get a valid access token for the given user.

        Resolution order:
        1. If prefer_application=True, try application token first.
        2. Cached delegated token (if still valid for > 5 min).
        3. Refresh the delegated token via MSAL.
        4. Fallback to application-level client-credentials token (unless require_delegated=True).
        """
        if prefer_application:
             app_token = self._get_application_token()
             if app_token:
                 return app_token

        token_obj = MicrosoftToken.objects.filter(account_email=user_email, token_type='delegated').first()
        
        if token_obj and token_obj.expires_at > timezone.now() + timedelta(minutes=5):
            return token_obj.access_token
        
        if token_obj and token_obj.refresh_token:
            # Only request file and user scopes for delegated tokens.
            # Mail.Read is handled via application permissions.
            result = self.msal_app.acquire_token_by_refresh_token(
                token_obj.refresh_token,
                scopes=["https://graph.microsoft.com/Files.Read.All", "https://graph.microsoft.com/User.Read"]
            )
            if "access_token" in result:
                token_obj.access_token = result["access_token"]
                token_obj.refresh_token = result.get("refresh_token", token_obj.refresh_token)
                token_obj.expires_at = timezone.now() + timedelta(seconds=result.get("expires_in", 3600))
                token_obj.save()
                return token_obj.access_token
            else:
                error_desc = result.get('error_description') or result.get('error', 'Unknown error')
                if require_delegated:
                    logger.warning(f"Token refresh failed for {user_email}: {error_desc}")
                    raise ValueError(
                        f"Failed to refresh delegated token for {user_email}. "
                        f"Error: {error_desc}. "
                        f"Please re-authenticate using: python manage.py authenticate_ms_graph"
                    )

        # Check if we need delegated permissions
        if require_delegated:
            if not token_obj:
                raise ValueError(
                    f"No delegated token found for {user_email}. "
                    f"OneDrive operations require delegated permissions. "
                    f"Please authenticate using: python manage.py authenticate_ms_graph"
                )
            else:
                raise ValueError(
                    f"Delegated token for {user_email} is expired and refresh failed. "
                    f"Please re-authenticate using: python manage.py authenticate_ms_graph"
                )

        # Fallback to Application permissions
        return self._get_application_token()

    def _get_application_token(self) -> Optional[str]:
        """Acquire an application-level token."""
        result = self.msal_app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            logger.error(f"Failed to acquire application token: {result.get('error_description')}")
            return None
        return result.get("access_token")

    def authenticate_with_password(self, user_email: str, password: str) -> Tuple[bool, str]:
        """
        Authenticate using Resource Owner Password Credentials (ROPC).

        No browser redirect or admin-configured URI required.
        Stores the resulting delegated token in the database.

        Returns:
            (True, 'Success') on success, (False, error_description) on failure.
        """
        result = self.msal_app.acquire_token_by_username_password(
            username=user_email,
            password=password,
            scopes=[
                "https://graph.microsoft.com/Files.Read.All",
                "https://graph.microsoft.com/Files.ReadWrite.All",
                "https://graph.microsoft.com/User.Read",
            ]
        )
        
        if "access_token" in result:
            MicrosoftToken.objects.update_or_create(
                account_email=user_email,
                defaults={
                    'token_type': 'delegated',
                    'access_token': result['access_token'],
                    'refresh_token': result.get('refresh_token'),
                    'expires_at': timezone.now() + timedelta(seconds=result.get('expires_in', 3600))
                }
            )
            return True, "Success"
        
        return False, result.get('error_description') or result.get('error') or "Unknown error"

    def authenticate_with_device_code(self, user_email: str) -> Tuple[bool, str]:
        """
        Authenticate using the Device Code flow (interactive).

        This is the most reliable way to get a delegated token when:
        - MFA is enabled, or
        - Admin/user consent is required (AADSTS65001), or
        - ROPC is blocked by tenant policy.

        Returns:
            (True, message) on success, (False, error_description) on failure.
        """
        # Use v2-style scopes (no resource prefix) – required for device-code flow.
        # Graph permissions:
        # - User.Read            -> basic profile
        # - Files.Read.All       -> read files
        # - Files.ReadWrite.All  -> read/write files (for future use)
        # MSAL forbids passing reserved scopes (openid/profile/offline_access) into device-flow initiation.
        # This still yields an access token suitable for OneDrive browsing.
        scopes = ["User.Read", "Files.Read.All", "Files.ReadWrite.All"]

        flow = self.msal_public_app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            return False, flow.get("error_description") or flow.get("error") or "Failed to start device flow"

        # MSAL returns a human-friendly instruction string in `message` (includes URL + code).
        message = f"{flow.get('message')}"

        result = self.msal_public_app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            return False, result.get("error_description") or result.get("error") or "Device flow failed"

        MicrosoftToken.objects.update_or_create(
            account_email=user_email,
            defaults={
                "token_type": "delegated",
                "access_token": result["access_token"],
                "refresh_token": result.get("refresh_token"),
                "expires_at": timezone.now() + timedelta(seconds=result.get("expires_in", 3600)),
            },
        )
        return True, message

    def start_device_code_flow(self) -> Tuple[bool, dict, str]:
        """
        Start a device-code flow and return the flow dict + human-friendly message.

        Returns:
            (True, flow, message) on success, (False, {}, error) on failure.
        """
        scopes = ["User.Read", "Files.Read.All", "Files.ReadWrite.All"]
        flow = self.msal_public_app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            return False, {}, flow.get("error_description") or flow.get("error") or "Failed to start device flow"
        return True, flow, f"{flow.get('message')}"

    def finish_device_code_flow(self, user_email: str, flow: dict) -> Tuple[bool, str]:
        """
        Poll the device-code flow until completion and persist the delegated token.
        """
        result = self.msal_public_app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            return False, result.get("error_description") or result.get("error") or "Device flow failed"

        MicrosoftToken.objects.update_or_create(
            account_email=user_email,
            defaults={
                "token_type": "delegated",
                "access_token": result["access_token"],
                "refresh_token": result.get("refresh_token"),
                "expires_at": timezone.now() + timedelta(seconds=result.get("expires_in", 3600)),
            },
        )
        return True, "Success"

    # ─── HTTP helpers ────────────────────────────────────────────────────

    def _make_request(self, method: str, endpoint: str, token: Optional[str] = None,
                      params: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Make a JSON request to the Graph API.

        Raises ``requests.HTTPError`` on non-2xx responses.
        If *token* is ``None``, fetches one for the default DMS user.
        """
        if token is None:
            token = self.get_access_token()
        res = requests.request(
            method, f"{self.graph_endpoint}{endpoint}",
            headers={'Authorization': f'Bearer {token}'}, params=params, timeout=30
        )
        if not res.ok:
            logger.error(f"Graph API {res.status_code}: {res.text[:500]}")
        res.raise_for_status()
        return res.json()

    def _make_raw_request(self, method: str, endpoint: str, token: Optional[str] = None,
                          params: Optional[Dict] = None) -> bytes:
        """
        Make a raw-bytes request to the Graph API (for file downloads).

        Raises ``requests.HTTPError`` on non-2xx responses.
        """
        if token is None:
            token = self.get_access_token()
        res = requests.request(
            method, f"{self.graph_endpoint}{endpoint}",
            headers={'Authorization': f'Bearer {token}'}, params=params, timeout=60
        )
        if not res.ok:
            logger.error(f"Graph API {res.status_code}: {res.text[:500]}")
        res.raise_for_status()
        return res.content

    # ─── Email (Application permissions) ─────────────────────────────────

    def get_messages(self, user_email: str, top: int = 50, skip: int = 0,
                     since: Optional[str] = None, search: Optional[str] = None) -> Dict:
        """Fetch emails for a user. Prefers application permissions for reliability."""
        token = self.get_access_token(user_email, prefer_application=True)
        params = {'$top': top, '$skip': skip, '$orderby': 'receivedDateTime desc'}
        if since:
            params['$filter'] = f"receivedDateTime ge {since}"
        if search:
            params['$search'] = f'"{search}"'
        return self._make_request('GET', f"/users/{user_email}/messages", token, params)

    def get_message_attachments(self, user_email: str, message_id: str) -> List[Dict]:
        """Get attachments metadata for a specific email. Prefers application permissions."""
        token = self.get_access_token(user_email, prefer_application=True)
        data = self._make_request('GET', f"/users/{user_email}/messages/{message_id}/attachments", token)
        return data.get('value', [])

    def get_attachment_content(self, user_email: str, message_id: str, attachment_id: str) -> Dict:
        """Get full attachment content (including contentBytes). Prefers application permissions."""
        token = self.get_access_token(user_email, prefer_application=True)
        return self._make_request('GET', f"/users/{user_email}/messages/{message_id}/attachments/{attachment_id}", token)

    # ─── OneDrive / SharePoint (Drive-based) ─────────────────────────────

    @staticmethod
    def _encode_sharing_url(url: str) -> str:
        """Encode a sharing URL for the /shares/ endpoint (Microsoft spec)."""
        encoded = base64.urlsafe_b64encode(url.encode('utf-8')).decode('utf-8').rstrip('=')
        return f"u!{encoded}"

    def get_site_drive_id(self, site_host: str, site_path: str,
                          user_email: str = DMS_USER_EMAIL) -> str:
        """
        Get the drive ID for a SharePoint personal site.

        Raises ``ValueError`` if no drives are found on the site.
        """
        token = self.get_access_token(user_email)
        site = self._make_request('GET', f"/sites/{site_host}:/{site_path}", token)
        site_id = site['id']
        drives = self._make_request('GET', f"/sites/{site_id}/drives", token)
        if drives.get('value'):
            return drives['value'][0]['id']
        raise ValueError(f"No drives found on site {site_host}/{site_path}")

    # ── Folder listing ──

    def list_folder_by_drive_path(self, drive_id: str = DMS_DRIVE_ID,
                                  folder_path: str = DMS_FOLDER_PATH,
                                  user_email: str = DMS_USER_EMAIL,
                                  top: int = 200) -> Dict[str, Any]:
        """List children of a folder by drive ID and path."""
        token = self.get_access_token(user_email, require_delegated=True)
        params = {'$top': top}
        return self._make_request('GET', f"/drives/{drive_id}/root:/{folder_path}:/children", token, params)

    def list_drive_root_children(self, drive_id: str = DMS_DRIVE_ID,
                                 user_email: str = DMS_USER_EMAIL,
                                 top: int = 200) -> Dict[str, Any]:
        """List children directly from the drive root."""
        token = self.get_access_token(user_email, require_delegated=True)
        params = {'$top': top}
        return self._make_request('GET', f"/drives/{drive_id}/root/children", token, params)

    def get_drive_item_children(self, drive_id: str, item_id: str,
                                user_email: str = DMS_USER_EMAIL,
                                top: int = 200) -> Dict[str, Any]:
        """List children of a specific item by drive ID and item ID."""
        token = self.get_access_token(user_email, require_delegated=True)
        params = {'$top': top}
        return self._make_request('GET', f"/drives/{drive_id}/items/{item_id}/children", token, params)

    def get_folder_tree(self, drive_id: str, item_id: str, user_email: str = DMS_USER_EMAIL) -> List[Dict[str, Any]]:
        """Recursively fetch all files inside a given folder ID."""
        all_files = []
        
        def traverse(current_item_id):
            children_data = self.get_drive_item_children(drive_id, current_item_id, user_email=user_email, top=999)
            items = children_data.get('value', [])
            
            for item in items:
                # Explicitly attach the current drive_id to the item so background tasks know where it lives
                item['driveId'] = drive_id
                
                if 'folder' in item:
                    # Recursive call for subfolders
                    traverse(item['id'])
                elif 'file' in item:
                    # Collect file metadata
                    all_files.append(item)
                    
        traverse(item_id)
        return all_files

    def get_drive_root_children(self, user_email: str = DMS_USER_EMAIL,
                                top: int = 200, **kwargs) -> Dict[str, Any]:
        """
        List the DMS shared folder(s).

        Resolution order:
        1. Shared-folder URL(s) (supports comma-separated list)
        2. Legacy drive path(s) (supports comma-separated list)
        3. Drive root
        """
        if DMS_SHARED_FOLDER_URL:
            urls = [u.strip() for u in DMS_SHARED_FOLDER_URL.split(',') if u.strip()]
            
            if len(urls) > 1:
                # Multiple URLs: return the folders themselves as the root view
                items = []
                for url in urls:
                    try:
                        info = self.get_shared_folder_info(url, user_email)
                        # Flatten driveId for frontend consistency
                        if 'parentReference' in info:
                            info['driveId'] = info['parentReference'].get('driveId')
                        items.append(info)
                    except Exception as e:
                        logger.error(f"Failed to fetch shared folder info for {url}: {e}")
                
                if items:
                    return {'value': items}
                # If ALL shared URLs failed, fall through to legacy drive path
            else:
                # Single URL: maintain legacy behavior of listing children immediately
                try:
                    return self.list_shared_folder(urls[0], user_email)
                except HTTPError as exc:
                    response = getattr(exc, "response", None)
                    status_code = response.status_code if response is not None else "unknown"
                    logger.warning(
                        "Shared DMS folder URL failed with status %s. Falling back to legacy drive path.",
                        status_code,
                    )
                # Fall through to legacy drive path on error

        if DMS_DRIVE_ID and DMS_FOLDER_PATH:
            paths = [p.strip() for p in DMS_FOLDER_PATH.split(',') if p.strip()]
            
            if len(paths) > 1:
                # Multiple paths: return the folders themselves as the root view
                items = []
                for path in paths:
                    try:
                        # Use root:/path: to get the folder item itself
                        info = self.get_drive_item(DMS_DRIVE_ID, f"root:/{path}:", user_email)
                        info['driveId'] = DMS_DRIVE_ID
                        items.append(info)
                    except Exception as e:
                        logger.error(f"Failed to fetch folder info for path '{path}': {e}")
                
                if items:
                    return {'value': items}
                # Fall through to drive root on error
            else:
                # Single path: list children immediately
                try:
                    return self.list_folder_by_drive_path(DMS_DRIVE_ID, paths[0], user_email, top)
                except HTTPError as exc:
                    response = getattr(exc, "response", None)
                    if response is not None and response.status_code == 404:
                        logger.warning(
                            "Configured DMS folder path '%s' was not found in drive %s. Falling back to drive root.",
                            paths[0],
                            DMS_DRIVE_ID,
                        )
                        return self.list_drive_root_children(DMS_DRIVE_ID, user_email, top)
                    raise

        if DMS_DRIVE_ID:
            logger.warning("DMS_FOLDER_PATH is empty or failed. Falling back to drive root for drive %s.", DMS_DRIVE_ID)
            return self.list_drive_root_children(DMS_DRIVE_ID, user_email, top)

        raise ValueError(
            "OneDrive is not configured. Set DMS_SHARED_FOLDER_URL or DMS_DRIVE_ID in the environment."
        )

    def get_drive_folder_children(self, user_email: str = DMS_USER_EMAIL,
                                  folder_id: str = '', drive_id: Optional[str] = None,
                                  top: int = 200, **kwargs) -> Dict[str, Any]:
        """List children of a folder by its item ID. Uses provided drive_id or defaults to DMS_DRIVE_ID."""
        target_drive = drive_id or DMS_DRIVE_ID
        return self.get_drive_item_children(target_drive, folder_id, user_email, top)

    def list_shared_with_me(self, user_email: str = DMS_USER_EMAIL, top: int = 200) -> Dict[str, Any]:
        """List items shared with the authenticated user."""
        token = self.get_access_token(user_email, require_delegated=True)
        params = {'$top': top}
        # /me/drive/sharedWithMe returns DriveItems shared with the user
        return self._make_request('GET', "/me/drive/sharedWithMe", token, params)

    # ── Item metadata ──

    def get_drive_item(self, drive_id: str, item_id: str,
                       user_email: str = DMS_USER_EMAIL) -> Dict[str, Any]:
        """Get metadata for a specific drive item."""
        token = self.get_access_token(user_email, require_delegated=True)
        return self._make_request('GET', f"/drives/{drive_id}/items/{item_id}", token)

    # ── File download ──

    def get_drive_item_content(self, user_email: str, file_id: str,
                               drive_id: str = DMS_DRIVE_ID) -> bytes:
        """Download the raw content of a file from a drive."""
        token = self.get_access_token(user_email, require_delegated=True)
        return self._make_raw_request('GET', f"/drives/{drive_id}/items/{file_id}/content", token)

    def get_drive_item_download_url(self, drive_id: str, item_id: str,
                                    user_email: str = DMS_USER_EMAIL) -> str:
        """Get a short-lived download URL for a file."""
        token = self.get_access_token(user_email, require_delegated=True)
        item = self._make_request('GET', f"/drives/{drive_id}/items/{item_id}", token,
                                  params={'select': '@microsoft.graph.downloadUrl,name,size'})
        return item.get('@microsoft.graph.downloadUrl', '')

    # ── Sharing link based access ──

    def list_shared_folder(self, sharing_url: str, user_email: str = DMS_USER_EMAIL) -> Dict[str, Any]:
        """Access a shared folder directly via its sharing URL."""
        token = self.get_access_token(user_email, require_delegated=True)
        encoded = self._encode_sharing_url(sharing_url)
        return self._make_request('GET', f"/shares/{encoded}/driveItem/children", token)

    def get_shared_folder_info(self, sharing_url: str, user_email: str = DMS_USER_EMAIL) -> Dict[str, Any]:
        """Get metadata about a shared folder via its sharing URL."""
        token = self.get_access_token(user_email, require_delegated=True)
        encoded = self._encode_sharing_url(sharing_url)
        return self._make_request('GET', f"/shares/{encoded}/driveItem", token)

