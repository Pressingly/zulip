# mPass SSO for the Moneta FOSS bundle: Traefik's mpass-auth ForwardAuth
# (oauth2-proxy) asserts the user in X-Auth-Request-* headers, and nginx
# hands X-Auth-Request-Email to Django as REMOTE_USER on
# /accounts/login/sso/ only.  See doc/mpass_sso.md for the contract.
import base64
import binascii
import hmac
import json
from collections.abc import Callable

from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from zproject.config import get_secret

EMAIL_HEADER = "HTTP_X_AUTH_REQUEST_EMAIL"
ACCESS_TOKEN_HEADER = "HTTP_X_AUTH_REQUEST_ACCESS_TOKEN"
EDGE_SECRET_HEADER = "HTTP_X_MPASS_EDGE_SECRET"
MIN_EDGE_SECRET_LENGTH = 32


def configured_edge_secret() -> str:
    return (get_secret("mpass_edge_secret") or "").strip()


def request_is_from_edge(request: HttpRequest) -> bool:
    secret = configured_edge_secret()
    if secret == "":
        return True
    if len(secret) < MIN_EDGE_SECRET_LENGTH:
        return False
    presented = request.META.get(EDGE_SECRET_HEADER, "")
    return hmac.compare_digest(secret.encode(), presented.encode())


def is_email_shaped(value: str) -> bool:
    at = value.find("@")
    if at <= 0:
        return False
    dot = value.find(".", at + 1)
    return dot > at + 1


def mpass_identity_to_email(identity: str) -> str | None:
    value = identity.strip().lower()
    if value == "":
        return None
    if "@" in value:
        return value if is_email_shaped(value) else None
    domain = settings.MPASS_DEFAULT_EMAIL_DOMAIN.strip().lower()
    if domain == "":
        return None
    return f"{value}@{domain}"


def asserted_email(request: HttpRequest) -> str | None:
    if not request_is_from_edge(request):
        return None
    return mpass_identity_to_email(request.META.get(EMAIL_HEADER, ""))


def access_token_claims(token: str) -> dict[str, object] | None:
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[1] == "":
        return None
    padded_payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded_payload))
    except (binascii.Error, ValueError):
        return None
    return claims if isinstance(claims, dict) else None


def corporate_tenant_allowed(request: HttpRequest) -> bool:
    required_corporate_id = settings.MPASS_SMB_CORPORATE_ID.strip()
    if required_corporate_id == "":
        return settings.MPASS_ALLOW_ANY_TENANT
    claims = access_token_claims(request.META.get(ACCESS_TOKEN_HEADER, ""))
    return (
        claims is not None
        and claims.get("custom:is_corporate") in ("true", True)
        and claims.get("custom:corporate_id") == required_corporate_id
    )


def sso_login_refusal(request: HttpRequest) -> HttpResponse | None:
    # The access token is decoded without verification: oauth2-proxy
    # validated it before the request reached us, and the edge secret
    # (when set) proves it came through that ForwardAuth hop.
    if (
        request_is_from_edge(request)
        and asserted_email(request) is not None
        and corporate_tenant_allowed(request)
    ):
        return None
    return render(request, "zerver/mpass_access_denied.html", status=403)


def session_identity_differs_from_edge(request: HttpRequest) -> bool:
    if not request.user.is_authenticated:
        return False
    asserted = asserted_email(request)
    return asserted is not None and asserted != request.user.delivery_email.lower()


class MpassIdentitySwitchMiddleware:
    # A different identity at the edge means another person now holds
    # the browser's mPass session, so the old Zulip session must not
    # survive.  A missing header carries no signal: internal callers
    # never send one.
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if settings.MPASS_SSO and session_identity_differs_from_edge(request):
            logout(request)
        return self.get_response(request)
