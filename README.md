# Lawyer CRM

Small Flask CRM for a Portuguese lawyer. It tracks clients, cases, activities, payments, expenses, simple balances, search, and CSV ZIP backups.

The app is intentionally simple: one Flask application, server-rendered templates, and PostgreSQL through `psycopg2`.

## Project Structure

- `app.py` - Flask app, routes, authentication, health checks, backup export.
- `db_schema.py` - safe PostgreSQL schema setup shared by the app and scripts.
- `init_db.py` - one-time schema initialization command.
- `seed_demo_data.py` - optional fake demo data seed.
- `create_user.py` - one-time helper to create a database-backed login user.
- `.replit` - Replit run command.
- `templates/` - HTML templates.
- `requirements.txt` - Python dependencies.

## Running on Replit with Supabase PostgreSQL

Create a Supabase PostgreSQL database first, then add the connection string to Replit Secrets.

Required Replit Secrets:

- `DATABASE_URL` - Supabase PostgreSQL connection string. Include SSL if Supabase requires it, for example `?sslmode=require`.
- `SESSION_SECRET` - long random string for Flask sessions.

Login options:

- For simple demo login, set `APP_USER` and `APP_PASS` in Replit Secrets.
- For a database-backed login, set optional `CRM_USERNAME`, run `python create_user.py your-password-here`, then log in with that username and password.

Optional Secrets:

- `CRM_USERNAME` - username used by `create_user.py`; defaults to `rui`.
- `SESSION_COOKIE_SECURE` - set to `true` to force secure cookies.
- `AUTO_INIT_DB` - defaults to `true`; set to `false` only if you want to disable startup schema checks.
- `SUPABASE_DATABASE_URL` - older alternate variable still supported, but prefer `DATABASE_URL` on Replit.

Install dependencies:

```bash
pip install -r requirements.txt
```

Initialize the database schema:

```bash
python init_db.py
```

Optional fake demo data:

```bash
python seed_demo_data.py
```

Optional database-backed user:

```bash
python create_user.py your-password-here
```

Start the app:

```bash
python app.py
```

Replit also uses the `.replit` file:

```bash
run = "python app.py"
```

Routes to test after startup:

- `/api/healthz` - app process check.
- `/health` - public app health check.
- `/health/db` - public database connectivity check using `SELECT 1`.
- `/login` - login page.
- `/` - CRM dashboard after login.

## Database Setup

There is no Prisma, Drizzle, SQLAlchemy, SQLite, Docker, or local PostgreSQL container. The app reads PostgreSQL settings from environment variables.

The schema initializer creates tables only if needed and adds missing columns safely. It does not drop tables and does not delete data.

Main tables:

- `clients`
- `cases`
- `case_clients`
- `transactions`
- `activities`
- `users`

The app uses existing field names such as `clients.full_name`, `cases.case_code`, `cases.case_type`, and `activities.activity_date` to preserve the current UI and routes.

## Security Notes

- Do not commit `.env` files or credentials.
- Do not print or expose `DATABASE_URL`.
- Use fake/demo data only for demonstrations.
- Do not store real legal documents or sensitive case files in this app.
- Prefer Replit Secrets for `DATABASE_URL`, `SESSION_SECRET`, and login credentials.

## Local Development

Use Python 3.11 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Set environment variables or create a local `.env` file:

```bash
set DATABASE_URL=postgresql://user:password@host:5432/database?sslmode=require
set SESSION_SECRET=change-this-to-a-long-random-value
set APP_USER=admin
set APP_PASS=change-this-password
```

Then run:

```bash
python init_db.py
python app.py
```

Open `http://localhost:5000`.

## Docker deployment on Render

The native Python Render deployment still works. Docker is optional and uses the same Flask app, same external PostgreSQL database settings, and same environment variables.

Docker image behavior:

- Base image: `python:3.11-slim`
- Work directory: `/app`
- Installs `requirements.txt` first, then copies the project
- Exposes port `8000`
- Starts with `gunicorn app:app`
- Binds to `0.0.0.0:${PORT:-8000}` so Render can provide `PORT`, while local Docker defaults to `8000`

Render Docker Web Service setup:

- Service type: Web Service
- Runtime: Docker
- Dockerfile path: `./Dockerfile`
- Docker context: project root
- Health check path: `/health` or `/api/healthz`

Required Render environment variables:

- `DATABASE_URL` - demo PostgreSQL connection string
- `SESSION_SECRET` - long random string
- `APP_PASS` - temporary password value used by the one-time user creation command
- `CRM_USERNAME` - demo login username, for example `demo`

Optional:

- `APP_USER` and `APP_PASS` - simple fallback login credentials
- `SESSION_COOKIE_SECURE=true`

One-time temporary Start Command to initialize schema and create/update one demo user:

```bash
python init_db.py && python create_user.py "$APP_PASS" && gunicorn app:app --bind 0.0.0.0:${PORT:-8000}
```

One-time temporary Start Command to seed fake demo data:

```bash
python seed_demo_data.py && gunicorn app:app --bind 0.0.0.0:${PORT:-8000}
```

Permanent Docker Start Command:

```bash
gunicorn app:app --bind 0.0.0.0:${PORT:-8000}
```

Local Docker example:

```bash
docker build -t lawyer-crm-demo .
docker run --rm -p 8000:8000 --env-file .env lawyer-crm-demo
```

Then open `http://localhost:8000`.
