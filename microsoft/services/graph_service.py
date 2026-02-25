"""
Microsoft Graph API service for authentication, email, and OneDrive operations.
"""
import base64
import logging
from typing import Optional, Dict, Any, List, Tuple
from django.utils import timezone
from datetime import timedelta
from decouple import config
import requests
import msal

from ..models import MicrosoftToken

logger = logging.getLogger(__name__)

# ─── Known DMS Shared Folder constants ────────────────────────────────
DMS_DRIVE_ID = 'b!3S_Fhil_uEKQVZnv_LhVSs0jBzTo-59CpDghDEe3hAZpHh-zpeg8QbO1VWqjQeKg'
DMS_FOLDER_PATH = 'Desktop/DMS Update/3. DMS Dataroom - shared folder'
DMS_USER_EMAIL = 'dms-demo@india-alt.com'


class GraphAPIService:
    """
    Wrapper around the Microsoft Graph API.

    Handles:
    - Token management (delegated ROPC + refresh, application fallback)
    - Email retrieval (application permissions)
    - OneDrive / SharePoint file browsing and download (delegated permissions)
    """

    def __init__(self):
        self.client_id = config('AZURE_CLIENT_ID', default='')
        self.client_secret = config('AZURE_CLIENT_SECRET', default='')
        self.tenant_id = config('AZURE_TENANT_ID', default='')
        self.authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self.graph_endpoint = config('GRAPH_API_ENDPOINT', default='https://graph.microsoft.com/v1.0')
        
        self.msal_app = msal.ConfidentialClientApplication(
            self.client_id,
            authority=self.authority,
            client_credential=self.client_secret,
        )

    # ─── Auth ────────────────────────────────────────────────────────────

    def get_access_token(self, user_email: str = DMS_USER_EMAIL) -> Optional[str]:
        """
        Get a valid access token for the given user.

        Resolution order:
        1. Cached delegated token (if still valid for > 5 min).
        2. Refresh the delegated token via MSAL.
        3. Fallback to application-level client-credentials token.
        """
        token_obj = MicrosoftToken.objects.filter(account_email=user_email, token_type='delegated').first()
        
        if token_obj and token_obj.expires_at > timezone.now() + timedelta(minutes=5):
            return token_obj.access_token
        
        if token_obj and token_obj.refresh_token:
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
                logger.warning(f"Token refresh failed for {user_email}: {result.get('error_description')}")

        # Fallback to Application permissions
        result = self.msal_app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
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
        """Fetch emails for a user via application permissions."""
        token = self.get_access_token(user_email)
        params = {'$top': top, '$skip': skip, '$orderby': 'receivedDateTime desc'}
        if since:
            params['$filter'] = f"receivedDateTime ge {since}"
        if search:
            params['$search'] = f'"{search}"'
        return self._make_request('GET', f"/users/{user_email}/messages", token, params)

    def get_message_attachments(self, user_email: str, message_id: str) -> List[Dict]:
        """Get attachments metadata for a specific email."""
        token = self.get_access_token(user_email)
        data = self._make_request('GET', f"/users/{user_email}/messages/{message_id}/attachments", token)
        return data.get('value', [])

    def get_attachment_content(self, user_email: str, message_id: str, attachment_id: str) -> Dict:
        """Get full attachment content (including contentBytes)."""
        token = self.get_access_token(user_email)
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
        token = self.get_access_token(user_email)
        params = {'$top': top}
        return self._make_request('GET', f"/drives/{drive_id}/root:/{folder_path}:/children", token, params)

    def get_drive_item_children(self, drive_id: str, item_id: str,
                                user_email: str = DMS_USER_EMAIL,
                                top: int = 200) -> Dict[str, Any]:
        """List children of a specific item by drive ID and item ID."""
        token = self.get_access_token(user_email)
        params = {'$top': top}
        return self._make_request('GET', f"/drives/{drive_id}/items/{item_id}/children", token, params)

    def get_drive_root_children(self, user_email: str = DMS_USER_EMAIL,
                                top: int = 200, **kwargs) -> Dict[str, Any]:
        """List root of the DMS shared folder (default entry point)."""
        return self.list_folder_by_drive_path(DMS_DRIVE_ID, DMS_FOLDER_PATH, user_email, top)

    def get_drive_folder_children(self, user_email: str = DMS_USER_EMAIL,
                                  folder_id: str = '', top: int = 200, **kwargs) -> Dict[str, Any]:
        """List children of a folder by its item ID within the DMS drive."""
        return self.get_drive_item_children(DMS_DRIVE_ID, folder_id, user_email, top)

    # ── Item metadata ──

    def get_drive_item(self, drive_id: str, item_id: str,
                       user_email: str = DMS_USER_EMAIL) -> Dict[str, Any]:
        """Get metadata for a specific drive item."""
        token = self.get_access_token(user_email)
        return self._make_request('GET', f"/drives/{drive_id}/items/{item_id}", token)

    # ── File download ──

    def get_drive_item_content(self, user_email: str, file_id: str,
                               drive_id: str = DMS_DRIVE_ID) -> bytes:
        """Download the raw content of a file from a drive."""
        token = self.get_access_token(user_email)
        return self._make_raw_request('GET', f"/drives/{drive_id}/items/{file_id}/content", token)

    def get_drive_item_download_url(self, drive_id: str, item_id: str,
                                    user_email: str = DMS_USER_EMAIL) -> str:
        """Get a short-lived download URL for a file."""
        token = self.get_access_token(user_email)
        item = self._make_request('GET', f"/drives/{drive_id}/items/{item_id}", token,
                                  params={'select': '@microsoft.graph.downloadUrl,name,size'})
        return item.get('@microsoft.graph.downloadUrl', '')

    # ── Sharing link based access ──

    def list_shared_folder(self, sharing_url: str, user_email: str = DMS_USER_EMAIL) -> Dict[str, Any]:
        """Access a shared folder directly via its sharing URL."""
        token = self.get_access_token(user_email)
        encoded = self._encode_sharing_url(sharing_url)
        return self._make_request('GET', f"/shares/{encoded}/driveItem/children", token)

    def get_shared_folder_info(self, sharing_url: str, user_email: str = DMS_USER_EMAIL) -> Dict[str, Any]:
        """Get metadata about a shared folder via its sharing URL."""
        token = self.get_access_token(user_email)
        encoded = self._encode_sharing_url(sharing_url)
        return self._make_request('GET', f"/shares/{encoded}/driveItem", token)

