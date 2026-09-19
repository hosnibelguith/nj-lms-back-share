"""Isolated tests for iCollector and the current backend; no live integrations."""
import os

os.environ['DJANGO_DEBUG'] = 'True'
os.environ['B2_STORAGE_ENABLED'] = 'False'
os.environ.pop('DATABASE_URL', None)
os.environ.pop('TENANT_DATABASES', None)
os.environ.pop('TENANT_DATABASE_DOMAINS', None)
os.environ['TENANT_DEFAULT_DATABASE_ALIAS'] = 'default'
from config.settings import *  # noqa: F403

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
TENANT_DATABASE_ALIASES = {'default'}
TENANT_DEFAULT_DATABASE_ALIAS = 'default'
TENANT_DATABASE_DOMAIN_MAP = {}
TENANT_DEBUG_HEADER_ENABLED = False
ALLOWED_HOSTS = ['testserver', 'localhost', '127.0.0.1']
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
CELERY_BROKER_URL = 'memory://'
CELERY_RESULT_BACKEND = 'cache+memory://'
ZUMRAILS_TRANSACTIONS_ENABLED = False
ZUMRAILS_DRY_RUN = True
STORAGES = {'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
            'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
SECURE_SSL_REDIRECT = False
ICOLLECTOR_BASE_URL = 'https://oceon.example.test'
ICOLLECTOR_API_KEY = 'local-test-key'
ICOLLECTOR_HMAC_SECRET = 'local-test-secret'
ICOLLECTOR_PROXY_URL = ''
