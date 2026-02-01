# Indian Alt - Application Architecture

## Overview

Indian Alt is a Django REST Framework API for investment management, converted from a PostgreSQL/Supabase schema. The application manages investment deals, contacts, banks, meetings, and user profiles with comprehensive audit history.

## What This Application Does

Indian Alt is an **investment deal pipeline management system** that helps investment firms track and manage potential investment opportunities. The system enables:

- **Deal Management**: Track investment deals through various stages (New, High, Medium, Low, Passed, Portfolio, Invested) with detailed information about funding requirements, industry sectors, and deal characteristics
- **Contact Management**: Maintain relationships with bankers and contacts at investment banks, tracking their sector coverage and responsibilities
- **Bank Relationships**: Organize contacts and deals by their associated investment banks
- **Meeting Tracking**: Record meetings with contacts and team members, including notes, follow-ups, and pipeline status
- **Request Processing**: Handle inbound investment requests with status tracking and metadata storage
- **Audit Trail**: Automatic version history for deals and contacts, capturing full snapshots of changes made by users

### How Things Connect

The application follows a hierarchical relationship structure:

1. **Banks** are the top-level entities representing investment banks
2. **Contacts** (bankers) belong to banks and can be associated with multiple deals
3. **Deals** are the core entities that:
   - Belong to a bank
   - Have a primary contact (banker)
   - Can reference multiple other contacts via arrays
   - May originate from an inbound request
   - Have assigned team members (profiles) via responsibility arrays
4. **Meetings** connect contacts and team members (profiles) for collaboration tracking
5. **Requests** are inbound opportunities that can generate deals
6. **Profiles** represent team members who can be assigned to deals/contacts and participate in meetings
7. **Version** table automatically tracks all changes to deals and contacts via database triggers

The system uses PostgreSQL arrays for flexible many-to-many relationships (responsibility, other_contacts, themes) where explicit foreign keys aren't required, and proper M2M through tables for relationships that need additional metadata (meetings with contacts/profiles).

## Technology Stack

- **Framework**: Django 5.1.5
- **API**: Django REST Framework 3.15.2
- **Authentication**: JWT (djangorestframework-simplejwt)
- **Documentation**: OpenAPI/Swagger (drf-spectacular)
- **Database**: PostgreSQL (with ArrayField and JSONField support)
- **Filtering**: django-filter
- **CORS**: django-cors-headers

## Project Structure

```
indian-alt/
├── accounts/          # User profile management
│   ├── models.py      # Profile model
│   ├── serializers.py # Profile serializers
│   ├── views.py      # Profile ViewSet
│   └── urls.py        # Profile routes
│
├── banks/             # Investment bank management
│   ├── models.py      # Bank model
│   ├── serializers.py # Bank serializers
│   ├── views.py       # Bank ViewSet
│   └── urls.py        # Bank routes
│
├── contacts/          # Contact/banker management
│   ├── models.py      # Contact model
│   ├── serializers.py # Contact serializers (with list/detail variants)
│   ├── views.py       # Contact ViewSet
│   └── urls.py        # Contact routes
│
├── deals/             # Investment deal management
│   ├── models.py      # Deal model with priority enum
│   ├── serializers.py # Deal serializers (with list/detail variants)
│   ├── views.py       # Deal ViewSet with custom actions
│   └── urls.py        # Deal routes
│
├── meetings/          # Meeting management
│   ├── models.py      # Meeting, MeetingContact, MeetingProfile models
│   ├── serializers.py # Meeting serializers with M2M handling
│   ├── views.py       # Meeting ViewSets
│   └── urls.py        # Meeting routes
│
├── requests/          # Inbound request management
│   ├── models.py      # Request model with status enum
│   ├── serializers.py # Request serializers
│   ├── views.py       # Request ViewSet
│   └── urls.py        # Request routes
│
├── core/              # Core functionality
│   ├── models.py      # Version (audit history) model
│   ├── serializers.py # Version serializers
│   ├── views.py       # Version ViewSet (read-only)
│   ├── mixins.py      # ErrorHandlingMixin
│   └── urls.py        # Core routes
│
└── config/            # Django project configuration
    ├── settings/
    │   ├── base.py    # Shared settings
    │   ├── local.py   # Development settings
    │   └── production.py # Production settings
    └── urls.py        # Root URL configuration
```

## Application Architecture

### Domain-Driven Design

The application is organized into domain-specific apps, each responsible for a specific business domain:

- **accounts**: User profile management
- **banks**: Investment bank entities
- **contacts**: Banker/contact person management
- **deals**: Investment deal pipeline
- **meetings**: Meeting scheduling and tracking
- **requests**: Inbound request processing
- **core**: Shared functionality (audit history, error handling)

### Data Models

#### Primary Models

1. **Profile** (`accounts`): User profiles linked to Django's User model
2. **Bank** (`banks`): Investment bank entities
3. **Contact** (`contacts`): Bankers/contacts, optionally linked to banks
4. **Deal** (`deals`): Investment deals with priority levels, linked to banks, contacts, and requests
5. **Request** (`requests`): Inbound requests with status tracking
6. **Meeting** (`meetings`): Meeting records with many-to-many relationships
7. **Version** (`core`): Audit history for deals and contacts (read-only, populated by triggers)

#### Relationships

- **Bank** → **Contact** (One-to-Many): A bank can have multiple contacts
- **Bank** → **Deal** (One-to-Many): A bank can have multiple deals
- **Contact** → **Deal** (One-to-Many via `primary_contact`): A contact can be primary contact for multiple deals
- **Request** → **Deal** (One-to-Many): A request can generate multiple deals
- **Meeting** ↔ **Contact** (Many-to-Many via `MeetingContact`): Meetings can have multiple contacts
- **Meeting** ↔ **Profile** (Many-to-Many via `MeetingProfile`): Meetings can have multiple profiles

#### Special Field Types

- **UUID Primary Keys**: All models use UUID primary keys matching the original schema
- **ArrayField**: PostgreSQL arrays for `responsibility`, `themes`, `sector_coverage`, `other_contacts`
- **JSONField**: JSONB fields for `request.metadata`, `request.body`, `request.attachments`, `version.data`
- **Through Models**: Explicit through models (`MeetingContact`, `MeetingProfile`) for M2M relationships

## API Architecture

### RESTful Design

All endpoints follow RESTful conventions:
- `GET /api/{resource}/` - List resources
- `POST /api/{resource}/` - Create resource
- `GET /api/{resource}/{id}/` - Retrieve resource
- `PUT /api/{resource}/{id}/` - Update resource (full)
- `PATCH /api/{resource}/{id}/` - Update resource (partial)
- `DELETE /api/{resource}/{id}/` - Delete resource

### ViewSets and Routers

The application uses DRF ViewSets with DefaultRouter for automatic URL generation:

**Why ViewSets over APIView?**
- **Less boilerplate**: Automatic CRUD operations
- **Consistent patterns**: Standardized endpoint structure
- **Router integration**: Automatic URL routing
- **Action decorators**: Easy custom endpoints with `@action`
- **Built-in filtering**: Integrated with django-filter

**Example:**
```python
class DealViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    queryset = Deal.objects.select_related('bank', 'primary_contact', 'request').all()
    serializer_class = DealSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    
    @action(detail=False, methods=['get'])
    def by_priority(self, request):
        # Custom endpoint: GET /api/deals/by_priority/
```

### Serializers

**Dual Serializer Pattern**: List and detail serializers for performance optimization:

- **List Serializers**: Lightweight, minimal fields for list views
- **Detail Serializers**: Full fields including nested relationships

**Example:**
```python
class ContactListSerializer(serializers.ModelSerializer):
    # Minimal fields for list view
    bank_name = serializers.CharField(source='bank.name', read_only=True)
    
class ContactSerializer(serializers.ModelSerializer):
    # Full fields for detail view
    bank_name = serializers.CharField(source='bank.name', read_only=True)
```

**M2M Handling**: Custom create/update methods for many-to-many relationships:

```python
def create(self, validated_data):
    contacts = validated_data.pop('contacts', [])
    profiles = validated_data.pop('profiles', [])
    meeting = Meeting.objects.create(**validated_data)
    meeting.contacts.set(contacts)
    meeting.profiles.set(profiles)
    return meeting
```

### Error Handling

**ErrorHandlingMixin**: Centralized error handling for all ViewSets:

```python
class ErrorHandlingMixin:
    def handle_exception(self, exc):
        # Converts exceptions to user-friendly API responses
        if isinstance(exc, ValidationError):
            return Response({'error': 'Validation failed', ...}, status=400)
        # ... handles other exception types
```

**Error Handling Strategy**:
- **Validation Errors**: 400 Bad Request with detailed error messages
- **Not Found**: 404 Not Found
- **Permission Denied**: 403 Forbidden
- **Integrity Errors**: 400 Bad Request (converted from 500)
- **Database Errors**: 500 Internal Server Error
- **Custom Actions**: Try-except blocks with logging

### Authentication & Authorization

**JWT Authentication**: All endpoints require authentication via JWT tokens:

```python
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
}
```

**Token Endpoints**:
- `POST /api/token/` - Obtain access/refresh tokens
- `POST /api/token/refresh/` - Refresh access token
- `POST /api/token/verify/` - Verify token validity

### API Documentation

**Swagger/OpenAPI Integration**: Automatic API documentation via `drf-spectacular`:

- **Decorators**: `@extend_schema_view` and `@extend_schema` for endpoint documentation
- **Automatic Schema Generation**: OpenAPI 3.0 schema generation
- **Interactive UI**: Swagger UI at `/api/docs/`
- **Alternative UI**: ReDoc at `/api/redoc/`

**Example:**
```python
@extend_schema_view(
    list=extend_schema(
        summary="List all deals",
        description="Retrieve a list of all deals with optional filtering.",
        tags=["Deals"],
    ),
)
class DealViewSet(ErrorHandlingMixin, viewsets.ModelViewSet):
    ...
```

## Database Architecture

### Query Optimization

**select_related**: Used for ForeignKey relationships to prevent N+1 queries:
```python
queryset = Deal.objects.select_related('bank', 'primary_contact', 'request').all()
```

**prefetch_related**: Used for ManyToMany and reverse ForeignKey relationships:
```python
queryset = Meeting.objects.prefetch_related(
    'contacts', 'profiles',
    'meeting_contacts__contact',
    'meeting_profiles__profile'
).all()
```

### Indexes

Strategic database indexes for performance:
- `Version`: Indexed on `(item_id, type)` and `created_at` for audit queries
- `Deal`: Indexed on `created_at`, `priority`, and `bank` for filtering
- `Profile`: Indexed on `email` and `is_admin` for lookups

### Audit History

**Version Model**: Read-only audit history populated by database triggers:
- Records full JSON snapshot of deals and contacts on changes
- Tracks user who made changes (via `user_id`)
- Searchable text field for quick lookups
- Read-only via API (created by triggers, not Django)

## Settings Architecture

### Split Settings Pattern

Settings are split into three files for environment-specific configuration:

1. **base.py**: Shared settings (installed apps, middleware, DRF config)
2. **local.py**: Development settings (DEBUG=True, development database)
3. **production.py**: Production settings (DEBUG=False, production database, security)

**Settings Loading**:
```python
# config/settings/__init__.py
if os.environ.get('DJANGO_SETTINGS_MODULE') == 'config.settings.production':
    from .production import *
else:
    from .local import *
```

### Environment Variables

Configuration via `python-decouple`:
- Database credentials
- Secret keys
- CORS allowed origins
- Debug mode

## URL Routing

### Router-Based Routing

Each app uses DRF's DefaultRouter for automatic URL generation:

```python
# banks/urls.py
router = DefaultRouter()
router.register(r'banks', BankViewSet, basename='bank')
urlpatterns = router.urls
```

**Root URL Configuration**:
```python
# config/urls.py
path('api/banks/', include('banks.urls')),
path('api/contacts/', include('contacts.urls')),
path('api/deals/', include('deals.urls')),
# ... etc
```

### Custom Actions

Custom endpoints via `@action` decorator:
- `GET /api/deals/by_priority/` - Group deals by priority
- `GET /api/core/versions/by_item/?item_id={uuid}&type={deal|contact}` - Get version history

## Security

### Authentication
- JWT tokens with configurable expiration
- Token refresh mechanism
- Token blacklisting on refresh rotation

### CORS
- Configurable allowed origins
- Credentials support for authenticated requests

### Data Validation
- Serializer validation for all inputs
- Database constraint enforcement
- Error handling for integrity violations

## Performance Considerations

### Serializer Optimization
- List serializers reduce payload size
- Nested serializers only in detail views
- Read-only fields for computed values

### Query Optimization
- select_related for ForeignKey relationships
- prefetch_related for ManyToMany relationships
- Strategic database indexes

### Pagination
- Default pagination: 20 items per page
- Configurable via `PAGE_SIZE` setting

## Error Handling Strategy

### Layered Error Handling

1. **Serializer Level**: Field validation errors
2. **ViewSet Level**: ErrorHandlingMixin catches exceptions
3. **Custom Actions**: Try-except blocks with logging
4. **Model Level**: Database constraint errors caught and converted

### Error Response Format

Consistent error response structure:
```json
{
    "error": "Error type",
    "details": "Detailed error message or object",
    "status_code": 400
}
```

## Testing Strategy

### Test Structure
- Unit tests per app in `tests.py`
- Integration tests for API endpoints
- Model validation tests

### Test Coverage Areas
- Model creation and validation
- Serializer serialization/deserialization
- ViewSet CRUD operations
- Custom action endpoints
- Error handling scenarios

## Deployment Considerations

### Environment Configuration
- Separate settings for local/production
- Environment variable management
- Database connection pooling
- Static file serving (WhiteNoise, S3, etc.)

### Production Checklist
- `DEBUG=False`
- Secure `SECRET_KEY`
- Proper `ALLOWED_HOSTS`
- SSL/HTTPS configuration
- Database backups
- Logging configuration
- Monitoring and alerting

## Future Enhancements

### Potential Improvements
- Custom permission classes for role-based access
- Caching layer (Redis) for frequently accessed data
- Background task processing (Celery) for heavy operations
- WebSocket support for real-time updates
- GraphQL API option alongside REST
- Advanced search (Elasticsearch) for full-text search
- File upload handling for attachments
- Email notifications for deal updates

## Key Design Decisions

1. **ViewSets over APIView**: Less boilerplate, consistent patterns, router integration
2. **Split Settings**: Environment-specific configuration, easier deployment
3. **Domain Apps**: Clear separation of concerns, easier maintenance
4. **Dual Serializers**: Performance optimization for list vs detail views
5. **ErrorHandlingMixin**: Centralized error handling, consistent API responses
6. **Through Models**: Explicit M2M relationships for future extensibility
7. **UUID Primary Keys**: Matching original schema, better for distributed systems
8. **Read-only Version Model**: Audit history managed by database triggers, not Django

## API Endpoints Summary

### Authentication
- `POST /api/token/` - Obtain JWT tokens
- `POST /api/token/refresh/` - Refresh access token
- `POST /api/token/verify/` - Verify token

### Documentation
- `GET /api/docs/` - Swagger UI
- `GET /api/redoc/` - ReDoc documentation
- `GET /api/schema/` - OpenAPI schema

### Resources
- `GET/POST /api/banks/` - Banks
- `GET/POST /api/contacts/` - Contacts
- `GET/POST /api/deals/` - Deals
- `GET/POST /api/requests/` - Requests
- `GET/POST /api/meetings/` - Meetings
- `GET /api/core/versions/` - Version history (read-only)
- `GET/POST /api/accounts/profiles/` - User profiles

### Custom Actions
- `GET /api/deals/by_priority/` - Deals grouped by priority
- `GET /api/core/versions/by_item/?item_id={uuid}&type={deal|contact}` - Version history for item
