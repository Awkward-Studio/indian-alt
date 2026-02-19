"""
Email reading service that orchestrates fetching emails from Microsoft Graph API
and storing them in the database.

Handles:
- Multi-account email fetching
- Deduplication
- Error handling and retry logic
- Status tracking
"""
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from django.utils import timezone
from django.db import transaction
from .graph_service import GraphAPIService
from ..models import EmailAccount, Email
from contacts.models import Contact

logger = logging.getLogger(__name__)


class EmailReaderService:
    """
    Service for reading emails from Microsoft Graph API and storing in database.
    """
    
    def __init__(self):
        """Initialize email reader service."""
        self.graph_service = GraphAPIService()
    
    def _parse_email_addresses(self, recipients: List[Dict]) -> List[str]:
        """
        Parse email addresses from Graph API recipient format.
        """
        if not recipients:
            return []
        
        emails = []
        for recipient in recipients:
            if isinstance(recipient, dict):
                # Graph API usually returns {'emailAddress': {'name': '...', 'address': '...'}}
                email_addr_obj = recipient.get('emailAddress', {})
                email = email_addr_obj.get('address')
                if email:
                    emails.append(email)
                elif 'address' in recipient: # Direct address in dict
                    emails.append(recipient['address'])
            elif isinstance(recipient, str):
                emails.append(recipient)
        
        return emails

    def _parse_email_body(self, body: Dict) -> tuple:
        """
        Parse email body from Graph API format.
        
        Args:
            body: Body object from Graph API
            
        Returns:
            Tuple of (body_text, body_html)
        """
        if not body:
            return '', ''
        
        content = body.get('content', '')
        content_type = body.get('contentType', 'text')
        
        if content_type == 'html':
            # If we only have HTML, we return it as HTML
            # We also return a plain version if possible, but for now we just 
            # ensure both aren't empty if content exists
            return content, content
        else:
            return content, ''

    def _convert_graph_email_to_model(
        self,
        graph_email: Dict[str, Any],
        email_account: EmailAccount
    ) -> Dict[str, Any]:
        """
        Convert Graph API email response to Email model fields.
        """
        # Parse recipients
        to_emails = self._parse_email_addresses(graph_email.get('toRecipients', []))
        cc_emails = self._parse_email_addresses(graph_email.get('ccRecipients', []))
        bcc_emails = self._parse_email_addresses(graph_email.get('bccRecipients', []))
        
        # Parse from email
        from_data = graph_email.get('from', {})
        from_email = None
        if isinstance(from_data, dict):
            from_email = from_data.get('emailAddress', {}).get('address')
        elif isinstance(from_data, str):
            from_email = from_data
        
        # Parse body
        body_text, body_html = self._parse_email_body(graph_email.get('body', {}))
        
        # Parse dates
        date_received = None
        date_sent = None
        created_date_time = None
        last_modified_date_time = None
        
        if graph_email.get('receivedDateTime'):
            try:
                date_received = datetime.fromisoformat(
                    graph_email['receivedDateTime'].replace('Z', '+00:00')
                )
            except (ValueError, AttributeError):
                pass
        
        if graph_email.get('sentDateTime'):
            try:
                date_sent = datetime.fromisoformat(
                    graph_email['sentDateTime'].replace('Z', '+00:00')
                )
            except (ValueError, AttributeError):
                pass
        
        if graph_email.get('createdDateTime'):
            try:
                created_date_time = datetime.fromisoformat(
                    graph_email['createdDateTime'].replace('Z', '+00:00')
                )
            except (ValueError, AttributeError):
                pass
        
        if graph_email.get('lastModifiedDateTime'):
            try:
                last_modified_date_time = datetime.fromisoformat(
                    graph_email['lastModifiedDateTime'].replace('Z', '+00:00')
                )
            except (ValueError, AttributeError):
                pass
        
        # Build model data
        model_data = {
            'email_account': email_account,
            'graph_id': graph_email.get('id', ''),
            'internet_message_id': graph_email.get('internetMessageId', ''),
            'subject': graph_email.get('subject', ''),
            'from_email': from_email,
            'to_emails': to_emails,
            'cc_emails': cc_emails,
            'bcc_emails': bcc_emails,
            'body_text': body_text,
            'body_html': body_html,
            'body_preview': graph_email.get('bodyPreview', ''),
            'date_received': date_received,
            'date_sent': date_sent,
            'created_date_time': created_date_time,
            'last_modified_date_time': last_modified_date_time,
            'importance': graph_email.get('importance', 'normal'),
            'is_read': graph_email.get('isRead', False),
            'is_read_receipt_requested': graph_email.get('isReadReceiptRequested', False),
            'conversation_id': graph_email.get('conversationId', ''),
            'conversation_index': graph_email.get('conversationIndex', ''),
            'categories': graph_email.get('categories', []),
            'flag': graph_email.get('flag', {}),
            'has_attachments': graph_email.get('hasAttachments', False),
            'web_link': graph_email.get('webLink', ''),
            'graph_metadata': {
                k: v for k, v in graph_email.items()
                if k not in [
                    'id', 'internetMessageId', 'subject', 'from', 'toRecipients',
                    'ccRecipients', 'bccRecipients', 'body', 'bodyPreview',
                    'receivedDateTime', 'sentDateTime', 'createdDateTime',
                    'lastModifiedDateTime', 'importance', 'isRead',
                    'isReadReceiptRequested', 'conversationId', 'conversationIndex',
                    'categories', 'flag', 'hasAttachments', 'webLink'
                ]
            }
        }
        
        return model_data
    

    def _process_contacts_from_email(self, graph_email: Dict[str, Any]):
        """
        Extract and create contacts from From and CC fields if they don't exist.
        """
        recipients = []
        
        # 1. Add From
        from_data = graph_email.get('from', {})
        if isinstance(from_data, dict):
            recipients.append(from_data.get('emailAddress', {}))
            
        # 2. Add CCs
        cc_data = graph_email.get('ccRecipients', [])
        for cc in cc_data:
            recipients.append(cc.get('emailAddress', {}))
            
        for r in recipients:
            email = r.get('address')
            name = r.get('name')
            
            if email and '@' in email:
                # Check if contact already exists
                if not Contact.objects.filter(email__iexact=email).exists():
                    try:
                        Contact.objects.create(
                            name=name or email.split('@')[0],
                            email=email.lower(),
                            designation='Auto-created from Email'
                        )
                        logger.info(f'Auto-created contact: {email}')
                    except Exception as e:
                        logger.error(f'Failed to auto-create contact {email}: {str(e)}')

    def fetch_emails_for_account(
        self,
        email_account: EmailAccount,
        limit: Optional[int] = None,
        since: Optional[datetime] = None,
        search_query: Optional[str] = None,
        return_emails: bool = False
    ) -> Dict[str, Any]:
        """
        Fetch emails for a specific email account.
        
        Args:
            email_account: EmailAccount instance to fetch emails for
            limit: Maximum number of emails to fetch (None = no limit)
            since: Only fetch emails received after this datetime
            search_query: Optional search query for Graph API
            return_emails: Whether to return the list of email objects
            
        Returns:
            Dictionary with fetch results (count, errors, etc.)
        """
        if not email_account.is_active:
            logger.info(f"Skipping inactive email account: {email_account.email}")
            return {
                'success': False,
                'error': 'Email account is not active',
                'count': 0,
                'new_count': 0,
                'updated_count': 0,
                'emails': [],
                'errors': ['Email account is not active']
            }
        
        logger.info(f"Fetching emails for account: {email_account.email}")
        
        result = {
            'success': True,
            'count': 0,
            'new_count': 0,
            'updated_count': 0,
            'emails': [],
            'errors': []
        }
        
        try:
            # Build filter query if since date provided
            filter_query = None
            if since:
                # Format for OData: receivedDateTime gt 2024-01-01T00:00:00Z
                since_str = since.strftime('%Y-%m-%dT%H:%M:%SZ')
                filter_query = f"receivedDateTime gt {since_str}"
            
            # Fetch emails from Graph API
            top = limit if limit else 100
            skip = 0
            total_fetched = 0
            
            while True:
                try:
                    response = self.graph_service.get_user_messages(
                        user_email=email_account.email,
                        top=min(top, 100),  # Graph API max is 100
                        skip=skip,
                        filter_query=filter_query,
                        search_query=search_query
                    )
                    
                    messages = response.get('value', [])
                    if not messages:
                        break
                    
                    # Process each message
                    for message_data in messages:
                        try:
                            with transaction.atomic():
                                # Check if email already exists
                                graph_id = message_data.get('id')
                                if not graph_id:
                                    logger.warning("Message missing ID, skipping")
                                    continue
                                
                                email, created = Email.objects.update_or_create(
                                    graph_id=graph_id,
                                    email_account=email_account,
                                    defaults=self._convert_graph_email_to_model(
                                        message_data,
                                        email_account
                                    )
                                )
                                
                                if created:
                                    result['new_count'] += 1
                                else:
                                    result['updated_count'] += 1
                                
                                if return_emails:
                                    result['emails'].append(email)
                                
                                result['count'] += 1
                                total_fetched += 1
                                
                        except Exception as e:
                            error_msg = f"Error processing message {graph_id}: {str(e)}"
                            logger.error(error_msg)
                            result['errors'].append(error_msg)
                            continue
                    
                    # Check if there are more messages
                    if len(messages) < top or (limit and total_fetched >= limit):
                        break
                    
                    skip += len(messages)
                    
                except Exception as e:
                    error_msg = f"Error fetching messages: {str(e)}"
                    logger.error(error_msg)
                    result['errors'].append(error_msg)
                    result['success'] = False
                    break
            
            # Update account sync status
            if result['success']:
                email_account.last_synced = timezone.now()
                email_account.sync_error = None
            else:
                email_account.sync_error = '; '.join(result['errors'][:3])  # Store first 3 errors
            
            email_account.save(update_fields=['last_synced', 'sync_error'])
            
            logger.info(
                f"Completed fetching for {email_account.email}: "
                f"{result['count']} total, {result['new_count']} new, "
                f"{result['updated_count']} updated"
            )
            
        except Exception as e:
            error_msg = f"Failed to fetch emails for {email_account.email}: {str(e)}"
            logger.error(error_msg, exc_info=True)
            result['success'] = False
            result['errors'].append(error_msg)
            
            # Update account with error
            email_account.sync_error = error_msg
            email_account.save(update_fields=['sync_error'])
        
        return result
    
    def fetch_all_active_accounts(
        self,
        limit_per_account: Optional[int] = None,
        since: Optional[datetime] = None,
        search_query: Optional[str] = None,
        return_emails: bool = False
    ) -> Dict[str, Any]:
        """
        Fetch emails for all active email accounts.
        
        Args:
            limit_per_account: Maximum emails per account (None = no limit)
            since: Only fetch emails received after this datetime
            search_query: Optional search query for Graph API
            return_emails: Whether to return the list of email objects
            
        Returns:
            Dictionary with results for all accounts
        """
        active_accounts = EmailAccount.objects.filter(is_active=True)
        
        logger.info(f"Fetching emails for {active_accounts.count()} active accounts")
        
        results = {
            'success': True,
            'total_accounts': active_accounts.count(),
            'successful_accounts': 0,
            'failed_accounts': 0,
            'total_emails': 0,
            'emails': [],
            'account_results': {}
        }
        
        for account in active_accounts:
            account_result = self.fetch_emails_for_account(
                email_account=account,
                limit=limit_per_account,
                since=since,
                search_query=search_query,
                return_emails=return_emails
            )
            
            # Copy result and remove emails list for the per-account summary
            summary_result = account_result.copy()
            account_emails = summary_result.pop('emails', [])
            
            results['account_results'][account.email] = summary_result
            
            if account_result['success']:
                results['successful_accounts'] += 1
            else:
                results['failed_accounts'] += 1
            
            results['total_emails'] += account_result['count']
            if return_emails:
                results['emails'].extend(account_emails)
        
        logger.info(
            f"Completed fetching for all accounts: "
            f"{results['successful_accounts']} successful, "
            f"{results['failed_accounts']} failed, "
            f"{results['total_emails']} total emails"
        )
        
        return results

