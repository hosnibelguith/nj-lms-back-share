release: python manage.py migrate
web: gunicorn config.wsgi --log-file -
worker: celery -A config worker --loglevel=info
beat: celery -A config beat --loglevel=info
processor: python manage.py payment_processor_worker
