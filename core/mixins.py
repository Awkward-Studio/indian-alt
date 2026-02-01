import logging
from rest_framework import status
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError, NotFound, PermissionDenied
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, DatabaseError

logger = logging.getLogger(__name__)


class ErrorHandlingMixin:
    # Centralized error handling for all ViewSets to ensure consistent API responses
    def handle_exception(self, exc):
        if isinstance(exc, ValidationError):
            logger.warning(f"Validation error: {exc.detail}")
            return Response(
                {
                    'error': 'Validation failed',
                    'details': exc.detail,
                    'status_code': status.HTTP_400_BAD_REQUEST
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        elif isinstance(exc, NotFound):
            logger.warning(f"Not found: {exc.detail}")
            return Response(
                {
                    'error': 'Resource not found',
                    'details': str(exc.detail),
                    'status_code': status.HTTP_404_NOT_FOUND
                },
                status=status.HTTP_404_NOT_FOUND
            )
        
        elif isinstance(exc, PermissionDenied):
            logger.warning(f"Permission denied: {exc.detail}")
            return Response(
                {
                    'error': 'Permission denied',
                    'details': str(exc.detail),
                    'status_code': status.HTTP_403_FORBIDDEN
                },
                status=status.HTTP_403_FORBIDDEN
            )
        
        elif isinstance(exc, IntegrityError):
            logger.error(f"Database integrity error: {str(exc)}")
            return Response(
                {
                    'error': 'Database integrity error',
                    'details': 'The operation violates database constraints',
                    'status_code': status.HTTP_400_BAD_REQUEST
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        elif isinstance(exc, DatabaseError):
            logger.error(f"Database error: {str(exc)}")
            return Response(
                {
                    'error': 'Database error',
                    'details': 'An error occurred while processing your request',
                    'status_code': status.HTTP_500_INTERNAL_SERVER_ERROR
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
        
        # Let DRF handle other exceptions
        return super().handle_exception(exc)
    
    def perform_create(self, serializer):
        # Convert database integrity errors to user-friendly validation errors
        try:
            return super().perform_create(serializer)
        except IntegrityError as e:
            logger.error(f"Integrity error during create: {str(e)}")
            raise ValidationError({
                'error': 'Failed to create resource',
                'details': 'The data violates database constraints'
            })
        except Exception as e:
            logger.error(f"Unexpected error during create: {str(e)}")
            raise
    
    def perform_update(self, serializer):
        try:
            return super().perform_update(serializer)
        except IntegrityError as e:
            logger.error(f"Integrity error during update: {str(e)}")
            raise ValidationError({
                'error': 'Failed to update resource',
                'details': 'The data violates database constraints'
            })
        except Exception as e:
            logger.error(f"Unexpected error during update: {str(e)}")
            raise
    
    def perform_destroy(self, instance):
        try:
            return super().perform_destroy(instance)
        except IntegrityError as e:
            logger.error(f"Integrity error during delete: {str(e)}")
            raise ValidationError({
                'error': 'Cannot delete resource',
                'details': 'This resource is referenced by other objects'
            })
        except Exception as e:
            logger.error(f"Unexpected error during delete: {str(e)}")
            raise
