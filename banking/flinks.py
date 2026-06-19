import requests
from django.conf import settings
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def _api_base():
    return (
        f"https://{settings.FLINKS_INSTANCE}-api.private.fin.ag/v3/"
        f"{settings.FLINKS_CUSTOMER_ID}/BankingServices"
    )


def iframe_base_url():
    configured = getattr(settings, "FLINKS_IFRAME_URL", None)
    if configured:
        return configured
    return f"https://{settings.FLINKS_INSTANCE}-iframe.private.fin.ag/v2/"


def auth_headers(key: str):
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "flinks-auth-key": key,
    }


def data_headers():
    secret_key = settings.FLINKS_SECRET_KEY_CA
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-api-key": secret_key,
    }


def generate_authorize_token():
    secret_key = settings.FLINKS_SECRET_KEY_CA
    if not secret_key:
        raise ValueError("Flinks secret key is not configured")

    response = requests.post(
        f"{_api_base()}/GenerateAuthorizeToken",
        headers=auth_headers(secret_key),
        timeout=30,
    )
    if response.status_code != 200:
        raise ValueError(response.text)

    token = response.json().get("Token")
    if not token:
        raise ValueError("No authorize token returned by Flinks")
    return token


def build_connect_iframe_url(authorize_token: str) -> str:
    parsed = urlparse(iframe_base_url())
    query = dict(parse_qsl(parsed.query))
    query.setdefault("consentEnable", "true")
    query.setdefault("customerName", "LendStack")
    query.setdefault("demo", "true")
    query["authorizeToken"] = authorize_token
    return urlunparse(parsed._replace(query=urlencode(query)))
