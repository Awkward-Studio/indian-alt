# Indian Alt - Django API

A Django REST Framework API for investment management, converted from a PostgreSQL/Supabase schema.

## Features

- **Django 5.1.5** with **Django REST Framework**
- **JWT Authentication** using `djangorestframework-simplejwt`
- **Swagger/OpenAPI Documentation** with `drf-spectacular`
- **PostgreSQL** database support
- **Best Practices**: Split settings, proper app organization, comprehensive serializers/viewsets

## Project Structure

```
indian-alt/
├── accounts/          # User profiles
├── core/              # Main business logic (Banks, Contacts, Deals, Meetings, etc.)
├── config/            # Django project settings
│   └── settings/      # Split settings (base, local, production)
├── venv/              # Virtual environment
├── requirements.txt    # Python dependencies
├── .env.example       # Environment variables template
└── manage.py
```

## Setup Instructions

### 1. Create Virtual Environment

```bash
python -m venv venv
```

### 2. Activate Virtual Environment

**Windows (PowerShell):**
```powershell
.\venv\Scripts\Activate.ps1
```

**Windows (CMD):**
```cmd
venv\Scripts\activate.bat
```

**Linux/Mac:**
```bash
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Copy `.env.example` to `.env` and update with your settings:

```bash
cp .env.example .env
```

Edit `.env` with your database credentials and other settings.

### 5. Database Setup

Make sure PostgreSQL is running and create a database:

```sql
CREATE DATABASE indian_alt;
```

### 6. Run Migrations

```bash
python manage.py makemigrations
python manage.py migrate
```

### 7. Create Superuser (Optional)

```bash
python manage.py createsuperuser
```

### 8. Run Development Server

```bash
python manage.py runserver
```

## API Endpoints

### Authentication

- `POST /api/token/` - Obtain JWT token (username/password)
- `POST /api/token/refresh/` - Refresh JWT token
- `POST /api/token/verify/` - Verify JWT token

### API Documentation

- `GET /api/docs/` - Swagger UI
- `GET /api/redoc/` - ReDoc documentation
- `GET /api/schema/` - OpenAPI schema (JSON/YAML)

### Core Endpoints

- `/api/core/banks/` - Banks
- `/api/core/contacts/` - Contacts
- `/api/core/deals/` - Deals
- `/api/core/requests/` - Requests
- `/api/core/meetings/` - Meetings
- `/api/core/versions/` - Version history (read-only)

### Accounts Endpoints

- `/api/accounts/profiles/` - User profiles

## Models

### Core Models

- **Bank**: Investment banks
- **Contact**: Bankers/contacts (linked to banks)
- **Deal**: Investment deals (linked to banks, contacts, requests)
- **Request**: Inbound requests
- **Meeting**: Meeting records (many-to-many with contacts and profiles)
- **Version**: Audit history for deals and contacts

### Accounts Models

- **Profile**: User profiles (linked to Django User model)

## Authentication

The API uses JWT (JSON Web Tokens) for authentication. To use the API:

1. Obtain a token:
```bash
curl -X POST http://localhost:8000/api/token/ \
  -H "Content-Type: application/json" \
  -d '{"username": "your_username", "password": "your_password"}'
```

2. Use the token in subsequent requests:
```bash
curl -X GET http://localhost:8000/api/core/deals/ \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

## Development

### Running Tests

```bash
python manage.py test
```

### Creating Migrations

```bash
python manage.py makemigrations
```

### Applying Migrations

```bash
python manage.py migrate
```

## Production Deployment

1. Set `DJANGO_ENVIRONMENT=production` in your `.env`
2. Set `DEBUG=False`
3. Configure proper `ALLOWED_HOSTS`
4. Set up SSL/HTTPS
5. Use a production database
6. Set up static file serving (e.g., WhiteNoise, S3, etc.)

## Notes

- The models replicate the `public` schema from the original PostgreSQL dump
- UUIDs are used as primary keys (matching the original schema)
- Array fields (PostgreSQL arrays) are used for `responsibility`, `themes`, `sector_coverage`, etc.
- JSON fields are used for `request.metadata`, `request.body`, `request.attachments`, and `version.data`
- Many-to-many relationships use explicit through models (`MeetingContact`, `MeetingProfile`)

## License

[Your License Here]
