from rest_framework import exceptions
from rest_framework.authentication import CSRFCheck
from rest_framework.permissions import SAFE_METHODS
from rest_framework_simplejwt.authentication import JWTAuthentication


def enforce_csrf(request):
    """
    Enforce CSRF validation for cookie-authenticated unsafe requests.
    """
    check = CSRFCheck(lambda req: None)
    check.process_request(request)
    reason = check.process_view(request, None, (), {})
    if reason:
        raise exceptions.PermissionDenied(f"CSRF Failed: {reason}")


class CookieJWTAuthentication(JWTAuthentication):
    """
    Read JWT access token from HttpOnly cookie instead of Authorization header.
    """

    def authenticate(self, request):
        header = self.get_header(request)
        used_cookie_token = header is None

        if used_cookie_token:
            raw_token = request.COOKIES.get('access_token')
        else:
            raw_token = self.get_raw_token(header)

        if raw_token is None:
            return None

        validated_token = self.get_validated_token(raw_token)
        if used_cookie_token and request.method not in SAFE_METHODS:
            enforce_csrf(request)

        return self.get_user(validated_token), validated_token
