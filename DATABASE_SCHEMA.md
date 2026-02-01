# Database Schema Summary (`schema_dump.sql`)

This repo contains a PostgreSQL dump (Supabase-style). The database is split across multiple schemas:

- `public`: **your application data model** (deals/contacts/meetings/etc.)
- `auth`: Supabase Auth tables (users, sessions, identities, …)
- `storage`: Supabase Storage (buckets/objects/prefixes/multipart uploads, …)
- `realtime`: Supabase Realtime (messages/subscriptions, …)
- `vault`: Supabase Vault (encrypted secrets view/table/functions, …)
- `graphql`, `graphql_public`, `extensions`, `pgbouncer`, `pgsodium`: Supabase/platform support

Below is a **human-readable map** of all tables and how they connect.

---

## `public` schema (app tables)

### `public.profile`
**Purpose**: app-level profile for a signed-in user (created from `auth.users` via trigger).

- `id uuid PRIMARY KEY` (default `gen_random_uuid()`)
- `name text`
- `email text NOT NULL`
- `image_url text`
- `is_admin boolean NOT NULL DEFAULT false`
- `initials text`
- `is_disabled boolean NOT NULL DEFAULT false`

**RLS**: enabled. Policies allow read for all authenticated; users can insert/update their own; admins can update.

---

### `public.bank`
**Purpose**: investment bank entity.

- `id uuid PRIMARY KEY` (default `gen_random_uuid()`)
- `name text`

**RLS**: enabled. Policies allow read/insert/update for authenticated.

---

### `public.contact`
**Purpose**: banker/contact person, optionally tied to a bank.

- `id uuid PRIMARY KEY` (default `gen_random_uuid()`)
- `name text`
- `email text`
- `designation text`
- `address text`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `bank_id uuid NULL` → FK to `public.bank.id`
- `location text`
- `responsibility uuid[]`
- `phone text`
- `sector_coverage text[] NOT NULL DEFAULT '{}'`
- `rank text`

**Relationships**
- Many contacts belong to one bank:
  - `contact.bank_id → bank.id`

**Triggers**
- `contact_version_trigger` AFTER INSERT/UPDATE → `public.record_version()`

**RLS**: enabled. Policies allow read/insert/update/delete for authenticated.

---

### `public.request`
**Purpose**: inbound requests (stores raw payload and status).

- `id uuid PRIMARY KEY` (default `gen_random_uuid()`)
- `created_at timestamptz NOT NULL DEFAULT now()`
- `metadata jsonb`
- `body jsonb`
- `attachments jsonb`
- `status public.request_status DEFAULT 'Pending'`
- `logs text`

**RLS**: enabled. Policies allow read for authenticated; insert allowed.

---

### `public.deal`
**Purpose**: the core deal record.

- `id uuid PRIMARY KEY` (default `gen_random_uuid()`)
- `title text`
- `bank_id uuid NULL` → FK to `public.bank.id`
- `priority public.deal_priority NULL` (enum)
- `created_at timestamptz NOT NULL DEFAULT now()`
- `deal_summary text`
- `funding_ask text`
- `industry text`
- `sector text`
- `comments text`
- `deal_details text`
- `is_female_led boolean NOT NULL DEFAULT false`
- `management_meeting boolean NOT NULL DEFAULT false`
- `funding_ask_for text`
- `company_details text`
- `business_proposal_stage boolean NOT NULL DEFAULT false`
- `ic_stage boolean NOT NULL DEFAULT false`
- `request_id uuid NULL` → FK to `public.request.id`
- `responsibility uuid[] NOT NULL DEFAULT '{}'`
- `reasons_for_passing text`
- `city text`
- `state text`
- `country text`
- `other_contacts uuid[] NULL` (array of contact IDs, not FK-enforced)
- `primary_contact uuid NULL` → FK to `public.contact.id` (**ON DELETE SET NULL**)
- `fund text NOT NULL DEFAULT 'FUND3'` *(as dumped; see note below)*
- `legacy_investment_bank text`
- `priority_rationale text`
- `themes text[] NOT NULL DEFAULT '{}'`

**Relationships**
- Many deals belong to one bank:
  - `deal.bank_id → bank.id`
- Many deals can optionally point to one “primary contact”:
  - `deal.primary_contact → contact.id` (ON DELETE SET NULL)
- Many deals can optionally point to one request:
  - `deal.request_id → request.id`

**Triggers**
- `deal_version_trigger` AFTER INSERT/UPDATE → `public.record_version()`

**RLS**: enabled. Policies include “read for all users” but with additional checks (admin or assigned responsibility).

**Note on `fund` default**
The dump shows: `fund text DEFAULT '''FUND3''::text'::text NOT NULL` which is effectively a default of `'FUND3'`.

---

### `public.meeting`
**Purpose**: meeting record.

- `id uuid PRIMARY KEY` (default `gen_random_uuid()`)
- `created_at timestamptz NOT NULL DEFAULT now()`
- `notes text NOT NULL`
- `location text`
- `pipeline text`
- `follow_ups text`
- `followup_completed boolean NOT NULL DEFAULT false`

**RLS**: enabled. Policies allow read/insert/update/delete for authenticated.

---

### `public.meeting_contact` (join table)
**Purpose**: many-to-many between meetings and contacts.

- `id bigint PRIMARY KEY` (generated identity)
- `meeting_id uuid NOT NULL` → FK to `public.meeting.id` (**ON DELETE CASCADE**)
- `contact_id uuid NOT NULL` → FK to `public.contact.id` (**ON DELETE CASCADE**)

**RLS**: enabled. Policies allow read/insert/update/delete for authenticated.

---

### `public.meeting_profile` (join table)
**Purpose**: many-to-many between meetings and profiles.

- `id uuid PRIMARY KEY` (default `gen_random_uuid()`)
- `meeting_id uuid NOT NULL` → FK to `public.meeting.id` (**ON DELETE CASCADE**)
- `profile_id uuid NOT NULL` → FK to `public.profile.id` (**ON DELETE CASCADE**)

**RLS**: enabled. Policy allows all for authenticated.

---

### `public.version`
**Purpose**: simple version/audit history (populated by trigger for `deal` and `contact`).

- `id uuid PRIMARY KEY` (default `gen_random_uuid()`)
- `item_id uuid NOT NULL` *(references the object ID logically; not FK-enforced in dump)*
- `type text NOT NULL` *(values written are table names like `'deal'` / `'contact'`)*
- `created_at timestamptz NOT NULL DEFAULT now()`
- `data jsonb NOT NULL DEFAULT '{}'`
- `search text`
- `user_id uuid NULL` *(from `auth.uid()` in trigger)*

**RLS**: enabled. Policies mostly admin-only for read/update/delete.

---

## `public` enums (types)

### `public.deal_priority`
`New`, `To be Passed`, `To Be Pass`, `Passed`, `Portfolio`, `Invested`, `High`, `Medium`, `Low`

### `public.request_status`
`Pending`, `In Progress`, `Completed`, `Conflict`, `High`

---

## Relationships (quick ERD)

- **`bank` 1 → N `contact`**
  - `contact.bank_id → bank.id`
- **`bank` 1 → N `deal`**
  - `deal.bank_id → bank.id`
- **`contact` 1 → N `deal`** *(primary contact, optional)*
  - `deal.primary_contact → contact.id` (ON DELETE SET NULL)
- **`request` 1 → N `deal`** *(optional)*
  - `deal.request_id → request.id`
- **`meeting` N ↔ N `contact`** via `meeting_contact`
  - `meeting_contact.meeting_id → meeting.id` (ON DELETE CASCADE)
  - `meeting_contact.contact_id → contact.id` (ON DELETE CASCADE)
- **`meeting` N ↔ N `profile`** via `meeting_profile`
  - `meeting_profile.meeting_id → meeting.id` (ON DELETE CASCADE)
  - `meeting_profile.profile_id → profile.id` (ON DELETE CASCADE)

---

## `auth` schema (Supabase Auth tables)

These tables are Supabase-managed; you typically **don’t** model them directly in app code unless you’re self-hosting auth.

Key tables present:

- `auth.users` (primary key: `id uuid`)
- `auth.sessions` (primary key: `id uuid`, FK to `auth.users`)
- `auth.identities` (FK to `auth.users`)
- `auth.refresh_tokens` (FK to `auth.sessions`)
- `auth.one_time_tokens` (FK to `auth.users`)
- `auth.*` SSO/oauth tables (`oauth_clients`, `oauth_authorizations`, `oauth_consents`, …)
- plus audit/migrations tables

### Important connection into `public`
- Trigger: `on_auth_user_created` on `auth.users`
  - AFTER INSERT → executes `public.handle_new_user()`
  - This inserts a row into `public.profile` for the new auth user.

---

## `storage` schema (Supabase Storage tables)

Key tables present:

- `storage.buckets` (PK: `id text`)
- `storage.objects` (PK: `id uuid`, FK: `bucket_id → storage.buckets.id`)
- `storage.prefixes` (PK: `(bucket_id, level, name)`, FK: `bucket_id → storage.buckets.id`)
- `storage.s3_multipart_uploads` (FK: `bucket_id → storage.buckets.id`)
- `storage.s3_multipart_uploads_parts` (FK: upload_id → `storage.s3_multipart_uploads.id`)
- `storage.migrations`
- `storage.buckets_analytics`, `storage.buckets_vectors`, `storage.vector_indexes` (vector bucket/indexing)

Triggers/functions exist for prefix maintenance and `updated_at`.

---

## `realtime` schema (Supabase Realtime tables)

Key tables present:

- `realtime.messages` (partitioned table; PK includes `(id, inserted_at)`)
- `realtime.subscription` (stores subscriptions; has trigger `tr_check_filters`)
- `realtime.schema_migrations`

---

## `vault` schema (Supabase Vault)

Includes at least:

- `vault.decrypted_secrets` (VIEW)

Plus trigger/function definitions for encryption/decryption.

---

## Notes / modeling gotchas (useful for future Django conversion)

- **Arrays**: several columns are Postgres arrays (`uuid[]`, `text[]`) such as:
  - `deal.responsibility uuid[]`, `deal.themes text[]`, `contact.sector_coverage text[]`, etc.
- **JSON**: `request.metadata/body/attachments` are `jsonb`.
- **Join tables**: `meeting_contact` and `meeting_profile` are explicit join tables (not implicit many-to-many).
- **`deal.other_contacts uuid[]` is not FK-enforced**: it’s an array of UUIDs; you’d typically remodel this as a proper join table if you want referential integrity.

