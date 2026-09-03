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

PostgreSQL with pgvector is required locally and in production. SQLite is not
supported because retrieval depends on pgvector HNSW indexes and PostgreSQL
full-text search.

**Recommended local database:**

```bash
docker compose up -d db redis
```

Set in `.env`:

```bash
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/indian_alt
DB_SSL_REQUIRE=false
```

The `db` service uses the `pgvector/pgvector:pg16` image.

**Railway Production:**
- See [RAILWAY_DEPLOY.md](./RAILWAY_DEPLOY.md) for detailed setup
- Add PostgreSQL service in Railway (automatic `DATABASE_URL`)
- See [RAILWAY_POSTGRES_SETUP.md](./RAILWAY_POSTGRES_SETUP.md) for PostgreSQL-specific instructions

### 6. Run Migrations

```bash
python manage.py makemigrations
python manage.py migrate
python manage.py check_retrieval_stack
```

After deploying the hybrid retrieval migration, rebuild existing chunk embeddings
with contextual headers:

```bash
python manage.py rebuild_contextual_chunk_embeddings --batch-size 50
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

### Interactive bulk document pipeline

Run the complete OneDrive extraction, artifact, and analysis workflow from one
terminal interface:

```bash
venv/bin/python bulk_pipeline_cli.py
```

The interface reads `deal_discovery.json` and opens a keyboard-controlled folder
picker. Use Up and Down to move, Space to select up to five deal folders, `B` or
Right to inspect a selected folder's live OneDrive file tree, and Enter to move
to phase choices. The tree view uses Enter or Right to expand a folder, Left to
collapse it, and Backspace to return. Processing remains deal-folder scoped so
Phase 3 never presents a partial file selection as a complete deal analysis.

The runner calls the existing `bulk_1_extract.py`, `bulk_2_normalize.py`, and
`bulk_3_synthesize.py` entry points. It does not alter their artifact formats.
Child-process output is also copied to
`data/extractions/audit/pipeline_cli/phaseN_latest.log`.

The runner checkpoints every phase transition in
`data/extractions/audit/pipeline_cli/run_state.json`. After interruption, launch
the interactive CLI again and accept the resume prompt, or run:

```bash
venv/bin/python bulk_pipeline_cli.py --resume-run --yes
```

Completed phases are skipped. The interrupted phase restarts in resume mode so
its existing outputs and caches are reused even when the original run requested
a full redo.

Phases run as complete batches rather than a per-deal conveyor. Phase 1 finishes
for every selected deal before Phase 2 starts, and Phase 2 finishes for every
selected deal before Phase 3 starts. The worker pools inside each phase remain
concurrent.

You can preview a run without calling OneDrive or the model services:

```bash
venv/bin/python bulk_pipeline_cli.py --select 1-10 --phases 1,2,3 --dry-run
```

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

### Railway (Recommended)

See **[RAILWAY_DEPLOY.md](./RAILWAY_DEPLOY.md)** for complete Railway deployment guide.

Quick steps:
1. Create Railway project and connect GitHub repo
2. Add PostgreSQL service (automatic `DATABASE_URL`)
3. Set environment variables (see `env.example`)
4. Deploy - migrations run automatically via `start.sh`

### Manual Deployment

1. Set `DJANGO_ENVIRONMENT=production` in your `.env`
2. Set `DEBUG=False`
3. Configure proper `ALLOWED_HOSTS`
4. Set up SSL/HTTPS
5. Use a production database (PostgreSQL recommended)
6. Set up static file serving (WhiteNoise is already configured)

## Notes

- The models replicate the `public` schema from the original PostgreSQL dump
- UUIDs are used as primary keys (matching the original schema)
- Array fields (PostgreSQL arrays) are used for `responsibility`, `themes`, `sector_coverage`, etc.
- JSON fields are used for `request.metadata`, `request.body`, `request.attachments`, and `version.data`
- Many-to-many relationships use explicit through models (`MeetingContact`, `MeetingProfile`)

## License

[Your License Here]
