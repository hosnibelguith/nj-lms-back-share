from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.arrive_integration import (
    ArriveIdentityConflict,
    arrive_api_key_valid,
    consume_handoff_token,
    create_or_resume_lead,
    extract_bearer_token,
    lead_response_payload,
    resume_portal_session,
)
from accounts.arrive_serializers import (
    ArriveCreateLeadSerializer,
    ArriveHandoffSerializer,
    ArrivePortalSessionSerializer,
)
from accounts.serializers import CustomerPortalMeSerializer
from accounts.views import set_auth_cookies


class ArriveApiKeyPermission(permissions.BasePermission):
    message = "Invalid API key."

    def has_permission(self, request, view):
        token = extract_bearer_token(request.headers.get("Authorization"))
        return arrive_api_key_valid(token)


class ArriveCreateLeadView(APIView):
    authentication_classes = []
    permission_classes = [ArriveApiKeyPermission]

    def post(self, request):
        serializer = ArriveCreateLeadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            customer, loan, token, created = create_or_resume_lead(serializer.validated_data)
        except ArriveIdentityConflict as exc:
            return Response(
                {"detail": exc.message, "code": exc.code},
                status=status.HTTP_409_CONFLICT,
            )

        payload = lead_response_payload(customer, loan, token)
        return Response(
            payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ArrivePortalSessionView(APIView):
    authentication_classes = []
    permission_classes = [ArriveApiKeyPermission]

    def post(self, request):
        serializer = ArrivePortalSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            payload = resume_portal_session(
                arrive_application_id=serializer.validated_data["arrive_application_id"],
                zum_user_id=serializer.validated_data["zum_user_id"],
                loan_id=str(serializer.validated_data["loan_id"]),
            )
        except LookupError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(payload, status=status.HTTP_200_OK)


class ArriveHandoffExchangeView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = ArriveHandoffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            customer, tokens = consume_handoff_token(str(serializer.validated_data["token"]))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        response = Response(
            {
                "customer": CustomerPortalMeSerializer(customer).data,
                "access": tokens["access"],
                "refresh": tokens["refresh"],
                "embed_mode": True,
            }
        )
        return set_auth_cookies(request, response, tokens["access"], tokens["refresh"])
