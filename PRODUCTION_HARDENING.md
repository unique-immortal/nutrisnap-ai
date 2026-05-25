# Production hardening checklist

## Required Cloud Run environment variables

- `JWT_SECRET_KEY`: long random secret, for example `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `DATABASE_URL`: Supabase pooler URL on port `6543`, not direct port `5432`.
- `RATELIMIT_STORAGE_URI`: shared limiter backend such as Redis. Production refuses `memory://`.
- `CORS_ORIGINS`: comma-separated production origins.
- `RUN_DB_MIGRATIONS=false`: keep normal app instances from running schema changes at import time.
- `UPDATE_DOWNLOAD_USERS`: optional comma-separated allowlist for APK download.

## One-shot migration

Run schema initialization or migration separately from regular serving instances:

```bash
RUN_DB_MIGRATIONS=true python app.py
```

For Cloud Run, prefer a Cloud Run Job using the same image and environment with `RUN_DB_MIGRATIONS=true`.

## Remove sensitive files from Git tracking

Run this in an environment where `git` is available:

```bash
git rm --cached debug.keystore debug.keystore.base64 local_server.err.log local_server.out.log model_list.txt models.txt
```

After removing tracked keystores, rotate any Android debug/release signing material that may have been exposed.
