# Deployment Guide

This project is deployed as:

- Backend: Django + Channels + Celery on Railway
- Database: Railway Postgres
- Broker / cache / websocket layer: Railway Redis
- Models: external Azure VM running Ollama, GLM-OCR, and Qwen
- Frontend: Next.js on Vercel

## Architecture

Use two Railway app services from the same `indian-alt` repo:

1. `backend-web`
   - public service
   - runs migrations, collectstatic, and the ASGI app
   - serves REST API and websocket traffic

2. `backend-worker`
   - private/internal service
   - runs Celery only
   - consumes the same Redis and Postgres as the web service

Required infrastructure:

- Railway Postgres
- Railway Redis
- Azure VM reachable from Railway on the Ollama port

## 1. Push The Repos

Push both repos before creating or redeploying services:

- `indian-alt`
- `india-alternatives-dms`

The backend repo includes migrations and the ASGI startup path required for websocket streaming.

## 2. Railway Backend

### Create services

Create or connect the `indian-alt` repo in Railway, then create:

- `backend-web`
- `backend-worker`
- `postgres`
- `redis`

Both app services should use the same repo and branch.

### Start behavior

`railway.toml` already points to:

```toml
startCommand = "bash start.sh"
```

`start.sh` behaves as follows:

- `RUN_AS_WORKER=false` or unset:
  - ensure pgvector
  - run migrations
  - seed data
  - collectstatic
  - start `daphne config.asgi:application`

- `RUN_AS_WORKER=true`:
  - ensure pgvector
  - run migrations
  - start a small healthcheck server
  - start Celery worker

### Variables for both Railway app services

Set these on both `backend-web` and `backend-worker`:

```env
DJANGO_ENVIRONMENT=production
SECRET_KEY=<strong-random-secret>
DATABASE_URL=<from Railway Postgres>
REDIS_URL=<from Railway Redis>
OLLAMA_URL=http://<azure-vm-ip-or-dns>:11434
OLLAMA_DEFAULT_TEXT_MODEL=qwen3.5:latest
OLLAMA_DEFAULT_VISION_MODEL=glm-ocr:latest

ALLOWED_HOSTS=<your-backend-domain>
CORS_ALLOWED_ORIGINS=<your-vercel-frontend-url>
CSRF_TRUSTED_ORIGINS=<your-vercel-frontend-url>,<your-backend-https-url>
SECURE_SSL_REDIRECT=true

AZURE_CLIENT_ID=<azure-ad-app-client-id>
AZURE_CLIENT_SECRET=<azure-ad-app-client-secret>
AZURE_TENANT_ID=<azure-ad-tenant-id>
GRAPH_API_ENDPOINT=https://graph.microsoft.com/v1.0

DMS_USER_EMAIL=<delegated-dms-user-email>
DMS_SHARED_FOLDER_URL=<sharepoint-or-onedrive-shared-folder-url>
```

Worker-only variable:

```env
RUN_AS_WORKER=true
```

Web-only variable:

```env
RUN_AS_WORKER=false
```

### Important notes

- The backend must run as ASGI, not WSGI, because chat and AI history use websocket streaming.
- Railway Redis is required for:
  - Celery broker
  - Celery result backend
  - Django cache
  - Channels websocket layer
- The Azure VM must allow inbound connections from Railway to the Ollama port.
- If the VM is private, use a VPN or private networking layer before going live.

## 3. Vercel Frontend

Deploy the `india-alternatives-dms` repo to Vercel.

### Required Vercel environment variables

```env
NEXT_PUBLIC_USE_LOCAL_BACKEND=false
NEXT_PUBLIC_PROD_BACKEND=https://<your-railway-backend-domain>
NEXT_PUBLIC_WS_URL=wss://<your-railway-backend-domain>

AUTH_SECRET=<strong-random-secret>
NEXTAUTH_URL=https://<your-vercel-domain>
AUTH_TRUST_HOST=true
```

### Notes

- `NEXT_PUBLIC_PROD_BACKEND` is the base URL for REST requests.
- `NEXT_PUBLIC_WS_URL` is the base websocket host, without the `/ws/...` suffix.
- Do not set local or Tailscale frontend env vars in production.

## 4. Deployment Order

Deploy in this order:

1. Push backend repo
2. Push frontend repo
3. Deploy Railway Postgres and Redis
4. Deploy `backend-web`
5. Deploy `backend-worker`
6. Deploy Vercel frontend
7. Update Vercel envs if Railway assigned a different backend domain after first boot
8. Redeploy frontend once final backend URL is confirmed

## 5. Verification Checklist

### Backend health

- `GET /api/core/health/` returns `200`
- Django migrations complete successfully
- admin static assets load correctly

### Auth

- login works on Vercel
- JWT refresh works
- authenticated API requests succeed from frontend to Railway

### Websockets

- universal chat streams without refresh
- AI history updates in real time
- websocket URL connects as `wss://.../ws/ai-stream/<audit_log_id>/`

### Celery

- folder traversal task runs
- readability preflight task runs
- selection analysis task runs
- VDR background indexing runs
- worker logs show registered tasks instead of `Received unregistered task`

### AI VM

- AI settings page shows the VM reachable
- OCR requests succeed
- text-generation requests succeed

### Deal workflows

- OneDrive traversal opens selection dialog
- AI history can resume traversal and preflight dialogs
- deal creation succeeds from audit logs
- VDR shows:
  - initially analyzed files
  - failed initial files
  - later expanded-analysis files

## 6. First Debugging Targets If Production Fails

If REST works but streaming fails:

- confirm Railway web service is running Daphne / ASGI
- confirm `NEXT_PUBLIC_WS_URL` is `wss://...`
- confirm Redis is attached and reachable

If tasks queue but never execute:

- confirm worker service is deployed
- confirm `RUN_AS_WORKER=true`
- confirm Redis is attached to worker

If AI calls fail:

- confirm `OLLAMA_URL`
- confirm Railway can reach the Azure VM
- confirm required models are loaded on the VM

If frontend login fails:

- confirm `NEXTAUTH_URL`
- confirm `AUTH_SECRET`
- confirm backend CORS and CSRF vars include the Vercel domain
