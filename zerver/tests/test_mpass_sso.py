import base64
import json
from unittest import mock

from django.test import override_settings

from zerver.lib.mpass_sso import (
    access_token_claims,
    is_placeholder_address,
    mpass_identity_to_email,
)
from zerver.lib.test_classes import ZulipTestCase

EDGE_SECRET = "s" * 32
SSO_ONLY_BACKENDS = (
    "zproject.backends.ZulipRemoteUserBackend",
    "zproject.backends.ZulipDummyBackend",
)


def access_token(claims: dict[str, object]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


class MpassSSOTestCase(ZulipTestCase):
    def mpass_settings(self, **overrides: object) -> override_settings:
        return self.settings(
            AUTHENTICATION_BACKENDS=SSO_ONLY_BACKENDS,
            MPASS_SSO=True,
            MPASS_DEFAULT_EMAIL_DOMAIN="zulip.com",
            MPASS_ALLOW_ANY_TENANT=True,
            **overrides,
        )


class MpassIdentityToEmailTest(ZulipTestCase):
    def test_bare_mpass_id_gets_default_domain(self) -> None:
        with self.settings(MPASS_DEFAULT_EMAIL_DOMAIN="Example.COM"):
            self.assertEqual(
                mpass_identity_to_email(" 1020010000019120 "), "1020010000019120@example.com"
            )

    def test_email_is_kept_and_lowercased(self) -> None:
        with self.settings(MPASS_DEFAULT_EMAIL_DOMAIN="example.com"):
            self.assertEqual(mpass_identity_to_email("Hamlet@Zulip.com"), "hamlet@zulip.com")

    def test_unresolvable_identities(self) -> None:
        with self.settings(MPASS_DEFAULT_EMAIL_DOMAIN=""):
            self.assertIsNone(mpass_identity_to_email("1020010000019120"))
        with self.settings(MPASS_DEFAULT_EMAIL_DOMAIN="example.com"):
            self.assertIsNone(mpass_identity_to_email(""))
            self.assertIsNone(mpass_identity_to_email("@zulip.com"))
            self.assertIsNone(mpass_identity_to_email("hamlet@zulip"))
            self.assertIsNone(mpass_identity_to_email("hamlet@.com"))

    def test_access_token_claims(self) -> None:
        self.assertEqual(access_token_claims(access_token({"a": "b"})), {"a": "b"})
        self.assertIsNone(access_token_claims(""))
        self.assertIsNone(access_token_claims("one.two"))
        self.assertIsNone(access_token_claims("a.!!!.c"))
        self.assertIsNone(access_token_claims("a..c"))
        self.assertIsNone(access_token_claims(access_token({"a": "b"}).replace("ey", "x", 1)))
        not_an_object = base64.urlsafe_b64encode(b"[1]").decode()
        self.assertIsNone(access_token_claims(f"a.{not_an_object}.c"))


class MpassSSOLoginTest(MpassSSOTestCase):
    def test_bare_mpass_id_logs_in(self) -> None:
        hamlet = self.example_user("hamlet")
        with self.mpass_settings():
            result = self.client_get(
                "/accounts/login/sso/", REMOTE_USER="hamlet", HTTP_X_AUTH_REQUEST_EMAIL="hamlet"
            )
        self.assertEqual(result.status_code, 302)
        self.assert_logged_in_user_id(hamlet.id)

    def test_unresolvable_identity_is_refused(self) -> None:
        with self.mpass_settings(MPASS_DEFAULT_EMAIL_DOMAIN=""):
            result = self.client_get(
                "/accounts/login/sso/", REMOTE_USER="hamlet", HTTP_X_AUTH_REQUEST_EMAIL="hamlet"
            )
        self.assertEqual(result.status_code, 403)
        self.assert_logged_in_user_id(None)

    def test_empty_corporate_id_refuses_without_opt_in(self) -> None:
        hamlet = self.example_user("hamlet")
        with self.mpass_settings(MPASS_ALLOW_ANY_TENANT=False):
            result = self.client_get(
                "/accounts/login/sso/",
                REMOTE_USER=hamlet.delivery_email,
                HTTP_X_AUTH_REQUEST_EMAIL=hamlet.delivery_email,
            )
        self.assertEqual(result.status_code, 403)
        self.assert_logged_in_user_id(None)

    def test_corporate_gate(self) -> None:
        hamlet = self.example_user("hamlet")
        email = hamlet.delivery_email
        cases: list[tuple[dict[str, object], int]] = [
            ({"custom:is_corporate": "true", "custom:corporate_id": "acme"}, 302),
            ({"custom:is_corporate": True, "custom:corporate_id": "acme"}, 302),
            ({"custom:is_corporate": "false", "custom:corporate_id": "acme"}, 403),
            ({"custom:is_corporate": "true", "custom:corporate_id": "other"}, 403),
            ({"custom:corporate_id": "acme"}, 403),
        ]
        for claims, status in cases:
            with self.subTest(claims=claims), self.mpass_settings(MPASS_SMB_CORPORATE_ID="acme"):
                self.client_post("/accounts/logout/")
                result = self.client_get(
                    "/accounts/login/sso/",
                    REMOTE_USER=email,
                    HTTP_X_AUTH_REQUEST_EMAIL=email,
                    HTTP_X_AUTH_REQUEST_ACCESS_TOKEN=access_token(claims),
                )
                self.assertEqual(result.status_code, status)

        with self.mpass_settings(MPASS_SMB_CORPORATE_ID="acme"):
            result = self.client_get(
                "/accounts/login/sso/", REMOTE_USER=email, HTTP_X_AUTH_REQUEST_EMAIL=email
            )
        self.assertEqual(result.status_code, 403)

    def test_edge_secret(self) -> None:
        hamlet = self.example_user("hamlet")
        email = hamlet.delivery_email
        with (
            self.mpass_settings(),
            mock.patch("zerver.lib.mpass_sso.configured_edge_secret", return_value=EDGE_SECRET),
        ):
            result = self.client_get(
                "/accounts/login/sso/", REMOTE_USER=email, HTTP_X_AUTH_REQUEST_EMAIL=email
            )
            self.assertEqual(result.status_code, 403)
            result = self.client_get(
                "/accounts/login/sso/",
                REMOTE_USER=email,
                HTTP_X_AUTH_REQUEST_EMAIL=email,
                HTTP_X_MPASS_EDGE_SECRET="wrong",
            )
            self.assertEqual(result.status_code, 403)
            result = self.client_get(
                "/accounts/login/sso/",
                REMOTE_USER=email,
                HTTP_X_AUTH_REQUEST_EMAIL=email,
                HTTP_X_MPASS_EDGE_SECRET=EDGE_SECRET,
            )
            self.assertEqual(result.status_code, 302)
        self.assert_logged_in_user_id(hamlet.id)

    def test_short_edge_secret_trusts_nothing(self) -> None:
        email = self.example_user("hamlet").delivery_email
        with (
            self.mpass_settings(),
            mock.patch("zerver.lib.mpass_sso.configured_edge_secret", return_value="short"),
        ):
            result = self.client_get(
                "/accounts/login/sso/",
                REMOTE_USER=email,
                HTTP_X_AUTH_REQUEST_EMAIL=email,
                HTTP_X_MPASS_EDGE_SECRET="short",
            )
        self.assertEqual(result.status_code, 403)


class MpassIdentitySwitchTest(MpassSSOTestCase):
    def test_different_edge_identity_ends_the_session(self) -> None:
        self.login("hamlet")
        with self.mpass_settings():
            self.client_get("/json/users/me", HTTP_X_AUTH_REQUEST_EMAIL="cordelia@zulip.com")
        self.assert_logged_in_user_id(None)

    def test_same_identity_keeps_the_session(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        with self.mpass_settings():
            result = self.client_get("/json/users/me", HTTP_X_AUTH_REQUEST_EMAIL="HAMLET")
        self.assert_json_success(result)
        self.assert_logged_in_user_id(hamlet.id)

    def test_absent_header_keeps_the_session(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        with self.mpass_settings():
            result = self.client_get("/json/users/me")
        self.assert_json_success(result)
        self.assert_logged_in_user_id(hamlet.id)

    def test_untrusted_header_is_ignored(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        with (
            self.mpass_settings(),
            mock.patch("zerver.lib.mpass_sso.configured_edge_secret", return_value=EDGE_SECRET),
        ):
            self.client_get("/json/users/me", HTTP_X_AUTH_REQUEST_EMAIL="cordelia@zulip.com")
        self.assert_logged_in_user_id(hamlet.id)

    def test_inactive_without_mpass_sso(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        self.client_get("/json/users/me", HTTP_X_AUTH_REQUEST_EMAIL="cordelia@zulip.com")
        self.assert_logged_in_user_id(hamlet.id)


class MpassLogoutTest(MpassSSOTestCase):
    def test_logout_navigates_to_the_portal(self) -> None:
        self.login("hamlet")
        with self.mpass_settings(MPASS_LOGOUT_REDIRECT_URL="https://portal.example.com"):
            result = self.client_post("/accounts/logout/")
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result["Location"], "https://portal.example.com")
        self.assert_logged_in_user_id(None)

    def test_logout_without_portal_url_is_upstream(self) -> None:
        self.login("hamlet")
        with self.mpass_settings():
            result = self.client_post("/accounts/logout/")
        self.assertEqual(result.status_code, 302)
        self.assertNotEqual(result["Location"], "https://portal.example.com")


class MpassPlaceholderAddressTest(MpassSSOTestCase):
    def test_placeholder_addresses(self) -> None:
        with self.mpass_settings(MPASS_DEFAULT_EMAIL_DOMAIN="askii.ai"):
            self.assertTrue(is_placeholder_address("1020010000019120@askii.ai"))
            self.assertTrue(is_placeholder_address("Some One <1020010000019120@ASKII.ai>"))
            self.assertFalse(is_placeholder_address("hamlet@zulip.com"))
            self.assertFalse(is_placeholder_address("someone@notaskii.ai"))
        with self.settings(MPASS_SSO=False, MPASS_DEFAULT_EMAIL_DOMAIN="askii.ai"):
            self.assertFalse(is_placeholder_address("1020010000019120@askii.ai"))
