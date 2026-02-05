"""
Microsoft Graph API service for authentication and email operations.

This service handles:
- OAuth 2.0 client credentials flow (Application permissions)
- Token management and caching
- Email fetching from Microsoft Graph API
- Error handling and retry logic
"""
import logging
import time
from typing import Optional, Dict, List, Any
from django.conf import settings
from django.core.cache import cache
from decouple import config
import requests  # HTTP requests library (fixed in emails/__init__.py)

logger = logging.getLogger(__name__)


class GraphAPIService:
    """
    Service for interacting with Microsoft Graph API.
    
    Uses Application permissions (client credentials flow) to access
    any mailbox in the Azure AD tenant.
    """
    
    # Cache key for access token
    TOKEN_CACHE_KEY = 'graph_api_access_token'
    TOKEN_CACHE_TIMEOUT = 3300  # 55 minutes (tokens expire in 1 hour)
    
    def __init__(self):
        """Initialize Graph API service with configuration from settings."""
        self.client_id = config('AZURE_CLIENT_ID', default='')
        self.client_secret = config('AZURE_CLIENT_SECRET', default='')
        self.tenant_id = config('AZURE_TENANT_ID', default='')
        self.graph_endpoint = config(
            'GRAPH_API_ENDPOINT',
            default='https://graph.microsoft.com/v1.0'
        )
        
        if not all([self.client_id, self.client_secret, self.tenant_id]):
            logger.warning(
                "Azure AD credentials not fully configured. "
                "Set AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, and AZURE_TENANT_ID in environment."
            )
    
    def get_access_token(self) -> Optional[str]:
        """
        Get access token for Microsoft Graph API.
        
        Uses client credentials flow (Application permissions).
        Tokens are cached to avoid unnecessary requests.
        
        Returns:
            Access token string, or None if authentication fails
            
        Raises:
            Exception: If authentication fails after retries
        """
        # Check cache first
        cached_token = cache.get(self.TOKEN_CACHE_KEY)
        if cached_token:
            logger.debug("Using cached access token")
            return cached_token
        
        if not all([self.client_id, self.client_secret, self.tenant_id]):
            logger.error("Azure AD credentials not configured")
            raise ValueError(
                "Azure AD credentials not configured. "
                "Please set AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, and AZURE_TENANT_ID."
            )
        
        # OAuth 2.0 token endpoint
        token_url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        
        token_data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'scope': 'https://graph.microsoft.com/.default',
            'grant_type': 'client_credentials'
        }
        
        try:
            logger.info("Requesting new access token from Azure AD")
            response = requests.post(
                token_url,
                data=token_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=30
            )
            response.raise_for_status()
            
            token_response = response.json()
            access_token = token_response.get('access_token')
            
            if not access_token:
                logger.error("No access token in response")
                raise ValueError("Failed to obtain access token from Azure AD")
            
            # Cache the token
            expires_in = token_response.get('expires_in', 3600)
            cache_timeout = min(expires_in - 300, self.TOKEN_CACHE_TIMEOUT)  # Cache for slightly less than expiry
            cache.set(self.TOKEN_CACHE_KEY, access_token, cache_timeout)
            
            logger.info("Successfully obtained and cached access token")
            return access_token
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error requesting access token: {str(e)}")
            raise Exception(f"Failed to authenticate with Azure AD: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error during token acquisition: {str(e)}")
            raise
    
    def _make_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        retry_count: int = 3
    ) -> Dict[str, Any]:
        """
        Make authenticated request to Microsoft Graph API.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: Graph API endpoint (e.g., '/users/{email}/messages')
            params: Query parameters
            retry_count: Number of retry attempts for rate limiting
            
        Returns:
            JSON response from Graph API
            
        Raises:
            Exception: If request fails after retries
        """
        token = self.get_access_token()
        if not token:
            raise Exception("Failed to obtain access token")
        
        url = f"{self.graph_endpoint}{endpoint}"
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        
        for attempt in range(retry_count):
            try:
                logger.debug(f"Making {method} request to {url} (attempt {attempt + 1})")
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    timeout=60
                )
                
                # Handle rate limiting (429)
                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 60))
                    logger.warning(f"Rate limited. Retrying after {retry_after} seconds")
                    time.sleep(retry_after)
                    continue
                
                # Handle token expiration (401)
                if response.status_code == 401:
                    logger.warning("Token expired, clearing cache and retrying")
                    cache.delete(self.TOKEN_CACHE_KEY)
                    token = self.get_access_token()
                    headers['Authorization'] = f'Bearer {token}'
                    continue
                
                response.raise_for_status()
                return response.json()
                
            except requests.exceptions.Timeout:
                logger.error(f"Request timeout for {url}")
                if attempt < retry_count - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                    continue
                raise Exception(f"Request timeout after {retry_count} attempts")
                
            except requests.exceptions.RequestException as e:
                logger.error(f"Request error for {url}: {str(e)}")
                if attempt < retry_count - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise Exception(f"Graph API request failed: {str(e)}")
        
        raise Exception(f"Failed to complete request after {retry_count} attempts")
    
    def get_user_messages(
        self,
        user_email: str,
        top: int = 100,
        skip: int = 0,
        filter_query: Optional[str] = None,
        order_by: str = 'receivedDateTime desc'
    ) -> Dict[str, Any]:
        """
        Get messages for a specific user.
        
        Args:
            user_email: Email address of the user
            top: Maximum number of messages to return
            skip: Number of messages to skip
            filter_query: OData filter query (e.g., "receivedDateTime gt 2024-01-01T00:00:00Z")
            order_by: Order by clause (default: receivedDateTime desc)
            
        Returns:
            Graph API response with messages
        """
        endpoint = f"/users/{user_email}/messages"
        params = {
            '$top': top,
            '$skip': skip,
            '$orderby': order_by,
            '$select': (
                'id,internetMessageId,subject,from,toRecipients,ccRecipients,'
                'bccRecipients,body,bodyPreview,receivedDateTime,sentDateTime,'
                'createdDateTime,lastModifiedDateTime,importance,isRead,'
                'isReadReceiptRequested,conversationId,conversationIndex,'
                'categories,flag,hasAttachments,webLink'
            )
        }
        
        if filter_query:
            params['$filter'] = filter_query
        
        return self._make_request('GET', endpoint, params=params)
    
    def get_message_details(self, user_email: str, message_id: str) -> Dict[str, Any]:
        """
        Get detailed information about a specific message.
        
        Args:
            user_email: Email address of the user
            message_id: Graph API message ID
            
        Returns:
            Message details from Graph API
        """
        endpoint = f"/users/{user_email}/messages/{message_id}"
        params = {
            '$select': (
                'id,internetMessageId,subject,from,toRecipients,ccRecipients,'
                'bccRecipients,body,bodyPreview,receivedDateTime,sentDateTime,'
                'createdDateTime,lastModifiedDateTime,importance,isRead,'
                'isReadReceiptRequested,conversationId,conversationIndex,'
                'categories,flag,hasAttachments,webLink'
            )
        }
        
        return self._make_request('GET', endpoint, params=params)
    
    def get_message_attachments(self, user_email: str, message_id: str) -> List[Dict[str, Any]]:
        """
        Get attachments for a specific message.
        
        Args:
            user_email: Email address of the user
            message_id: Graph API message ID
            
        Returns:
            List of attachment metadata
        """
        endpoint = f"/users/{user_email}/messages/{message_id}/attachments"
        response = self._make_request('GET', endpoint)
        return response.get('value', [])
