# Simple Django project for Heroku

This is a stripped-down Django starter based on your uploaded config files.

## Why this exists
Your current project is failing before migrations because Django imports many app admin modules whose admin definitions no longer match the models. This minimal version removes those broken app imports so the project can boot, migrate, and deploy first.

## Local run
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver

## Heroku
1. Create app and Postgres add-on.
2. Set config vars: SECRET_KEY, DEBUG=False, ALLOWED_HOSTS, DATABASE_URL.
3. Deploy.
4. The release phase runs migrations automatically.

## Next step
Reintroduce your apps one by one only after fixing their models/admin/view imports.
