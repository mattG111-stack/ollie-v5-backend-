# Ollie Backend (FastAPI)

## Local setup

```bash
cd ollie/backend
python -m venv .venv
.venv\Scripts\activate          # Windows PowerShell / Bash
pip install -r requirements.txt
cp .env.example .env             # then edit .env with your Supabase URL
uvicorn app.main:app --reload
```

API docs at http://localhost:8000/docs.

## Environment

See `.env.example` for the full list. Required: `DATABASE_URL`, `JWT_SECRET`.
Generate a JWT secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```
