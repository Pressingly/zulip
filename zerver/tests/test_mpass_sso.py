import base64
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING
from unittest import mock

from django.conf import settings
from django.test import override_settings
from typing_extensions import override

from zerver.actions.user_settings import do_change_full_name
from zerver.actions.users import do_change_can_change_user_emails
from zerver.lib.events import fetch_initial_state_data
from zerver.lib.mpass_names import FALLBACK_NAME, display_name_for_email
from zerver.lib.mpass_sso import (
    access_token_claims,
    is_placeholder_address,
    mpass_identity_to_email,
)
from zerver.lib.test_classes import ZulipTestCase
from zerver.models import PreregistrationUser, UserProfile
from zerver.models.realms import get_realm
from zerver.models.users import get_user_by_delivery_email

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestHttpResponse

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
        defaults: dict[str, object] = {
            "AUTHENTICATION_BACKENDS": SSO_ONLY_BACKENDS,
            "MPASS_SSO": True,
            "MPASS_DEFAULT_EMAIL_DOMAIN": "zulip.com",
            "MPASS_ALLOW_ANY_TENANT": True,
        }
        return self.settings(**{**defaults, **overrides})

    def sso_login(self, user: UserProfile, headers: Mapping[str, str] = {}) -> None:
        # Must run inside mpass_settings(): the session then records
        # ZulipRemoteUserBackend, the only backend the views accept.
        email = user.delivery_email
        result = self.client_get(
            "/accounts/login/sso/",
            REMOTE_USER=email,
            headers={"X-Auth-Request-Email": email, **headers},
        )
        self.assertEqual(result.status_code, 302)
        self.assert_logged_in_user_id(user.id)


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
    def test_returning_user_with_a_changed_name_signs_in(self) -> None:
        hamlet = self.example_user("hamlet")
        do_change_full_name(hamlet, "Prince of Denmark", acting_user=None, notify=False)
        self.assertNotEqual(hamlet.full_name, display_name_for_email(hamlet.delivery_email))
        with self.mpass_settings():
            result = self.client_get(
                "/accounts/login/sso/",
                REMOTE_USER=hamlet.delivery_email,
                headers={"X-Auth-Request-Email": hamlet.delivery_email},
            )
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result["Location"], "http://zulip.testserver/")
        self.assert_logged_in_user_id(hamlet.id)
        hamlet.refresh_from_db()
        self.assertEqual(hamlet.full_name, "Prince of Denmark")

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
        hamlet = self.example_user("hamlet")
        with self.mpass_settings():
            self.sso_login(hamlet)
            result = self.client_get(
                "/json/users/me", HTTP_X_AUTH_REQUEST_EMAIL="cordelia@zulip.com"
            )
            self.assertEqual(result.status_code, 401)
            self.assert_logged_in_user_id(None)
            result = self.client_get(
                "/json/users/me", HTTP_X_AUTH_REQUEST_EMAIL=hamlet.delivery_email
            )
        self.assertEqual(result.status_code, 401)

    def test_same_identity_keeps_the_session(self) -> None:
        hamlet = self.example_user("hamlet")
        with self.mpass_settings():
            self.sso_login(hamlet)
            result = self.client_get("/json/users/me", HTTP_X_AUTH_REQUEST_EMAIL="HAMLET")
        self.assert_json_success(result)
        self.assert_logged_in_user_id(hamlet.id)

    def test_absent_header_keeps_the_session(self) -> None:
        hamlet = self.example_user("hamlet")
        with self.mpass_settings():
            self.sso_login(hamlet)
            result = self.client_get("/json/users/me")
        self.assert_json_success(result)
        self.assert_logged_in_user_id(hamlet.id)

    def test_untrusted_header_is_ignored(self) -> None:
        hamlet = self.example_user("hamlet")
        with (
            self.mpass_settings(),
            mock.patch("zerver.lib.mpass_sso.configured_edge_secret", return_value=EDGE_SECRET),
        ):
            self.sso_login(hamlet, {"X-Mpass-Edge-Secret": EDGE_SECRET})
            result = self.client_get(
                "/json/users/me", HTTP_X_AUTH_REQUEST_EMAIL="cordelia@zulip.com"
            )
            self.assert_json_success(result)
            self.assert_logged_in_user_id(hamlet.id)
            result = self.client_get(
                "/json/users/me",
                HTTP_X_AUTH_REQUEST_EMAIL="cordelia@zulip.com",
                HTTP_X_MPASS_EDGE_SECRET=EDGE_SECRET,
            )
        self.assertEqual(result.status_code, 401)
        self.assert_logged_in_user_id(None)

    def test_inactive_without_mpass_sso(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        result = self.client_get("/json/users/me", HTTP_X_AUTH_REQUEST_EMAIL="cordelia@zulip.com")
        self.assert_json_success(result)
        self.assert_logged_in_user_id(hamlet.id)


class MpassLogoutTest(MpassSSOTestCase):
    def test_logout_navigates_to_the_portal(self) -> None:
        with self.mpass_settings(MPASS_LOGOUT_REDIRECT_URL="https://portal.example.com"):
            self.sso_login(self.example_user("hamlet"))
            result = self.client_post("/accounts/logout/")
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result["Location"], "https://portal.example.com")
        self.assert_logged_in_user_id(None)

    def test_logout_without_portal_url_is_upstream(self) -> None:
        with self.mpass_settings():
            self.sso_login(self.example_user("hamlet"))
            result = self.client_post("/accounts/logout/")
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result["Location"], settings.LOGIN_URL)
        self.assert_logged_in_user_id(None)


class MpassEmailChangeTest(MpassSSOTestCase):
    def test_own_email_change_is_refused(self) -> None:
        iago = self.example_user("iago")
        with self.mpass_settings():
            self.sso_login(iago)
            result = self.client_patch("/json/settings", {"email": "iago-new@zulip.com"})
        self.assert_json_error(result, "Email addresses are managed by your sign-in provider.")
        iago.refresh_from_db()
        self.assertEqual(iago.delivery_email, "iago@zulip.com")

    def test_admin_change_of_another_users_email_is_refused(self) -> None:
        iago = self.example_user("iago")
        hamlet = self.example_user("hamlet")
        do_change_can_change_user_emails(iago, True)
        with self.mpass_settings():
            self.sso_login(iago)
            result = self.client_patch(
                f"/json/users/{hamlet.id}",
                {
                    "new_email": "hamlet-new@zulip.com",
                    "role": str(UserProfile.ROLE_MODERATOR),
                    "full_name": "Renamed Hamlet",
                },
            )
        self.assert_json_error(result, "Email addresses are managed by your sign-in provider.")
        hamlet.refresh_from_db()
        self.assertEqual(hamlet.delivery_email, "hamlet@zulip.com")
        self.assertEqual(hamlet.role, UserProfile.ROLE_MEMBER)
        self.assertEqual(hamlet.full_name, "King Hamlet")

    def test_initial_state_reports_email_changes_disabled(self) -> None:
        hamlet = self.example_user("hamlet")
        with self.mpass_settings():
            state = fetch_initial_state_data(hamlet, realm=hamlet.realm, event_types=["realm"])
        self.assertTrue(state["server_email_changes_disabled"])
        state = fetch_initial_state_data(hamlet, realm=hamlet.realm, event_types=["realm"])
        self.assertFalse(state["server_email_changes_disabled"])
        with self.settings(MPASS_SSO=1):
            state = fetch_initial_state_data(hamlet, realm=hamlet.realm, event_types=["realm"])
        self.assertIs(state["server_email_changes_disabled"], False)


class MpassGateHardeningTest(MpassSSOTestCase):
    def test_non_bool_mpass_sso_leaves_the_integration_off(self) -> None:
        email = self.example_user("hamlet").delivery_email
        with self.mpass_settings(MPASS_SSO=1):
            result = self.client_get(
                "/accounts/login/sso/",
                REMOTE_USER=email,
                HTTP_X_AUTH_REQUEST_EMAIL=email,
                MPASS_REMOTE_USER_FROM_EDGE="1",
            )
        self.assertEqual(result.status_code, 403)
        self.assert_logged_in_user_id(None)

    def test_allow_any_tenant_requires_a_real_true(self) -> None:
        email = self.example_user("hamlet").delivery_email
        with self.mpass_settings(MPASS_ALLOW_ANY_TENANT="no"):
            result = self.client_get(
                "/accounts/login/sso/", REMOTE_USER=email, HTTP_X_AUTH_REQUEST_EMAIL=email
            )
        self.assertEqual(result.status_code, 403)
        self.assert_logged_in_user_id(None)

    def test_edge_header_sign_in_is_refused_without_mpass_sso(self) -> None:
        email = self.example_user("hamlet").delivery_email
        with self.settings(AUTHENTICATION_BACKENDS=SSO_ONLY_BACKENDS):
            result = self.client_get(
                "/accounts/login/sso/",
                REMOTE_USER=email,
                HTTP_X_AUTH_REQUEST_EMAIL=email,
                MPASS_REMOTE_USER_FROM_EDGE="1",
            )
        self.assertEqual(result.status_code, 403)
        self.assert_logged_in_user_id(None)

    def test_other_remote_user_sources_keep_upstream_behaviour(self) -> None:
        hamlet = self.example_user("hamlet")
        with self.settings(AUTHENTICATION_BACKENDS=SSO_ONLY_BACKENDS):
            result = self.client_get("/accounts/login/sso/", REMOTE_USER=hamlet.delivery_email)
        self.assertEqual(result.status_code, 302)
        self.assert_logged_in_user_id(hamlet.id)


class MpassPlaceholderAddressTest(MpassSSOTestCase):
    def test_placeholder_addresses(self) -> None:
        with self.mpass_settings(MPASS_DEFAULT_EMAIL_DOMAIN="askii.ai"):
            self.assertTrue(is_placeholder_address("1020010000019120@askii.ai"))
            self.assertTrue(is_placeholder_address("Some One <1020010000019120@ASKII.ai>"))
            self.assertFalse(is_placeholder_address("hamlet@zulip.com"))
            self.assertFalse(is_placeholder_address("someone@notaskii.ai"))
        with self.settings(MPASS_SSO=False, MPASS_DEFAULT_EMAIL_DOMAIN="askii.ai"):
            self.assertFalse(is_placeholder_address("1020010000019120@askii.ai"))


class MpassDisplayNameTest(ZulipTestCase):
    def test_local_part_is_the_name(self) -> None:
        self.assertEqual(display_name_for_email("jane.doe@example.com"), "jane.doe")
        self.assertEqual(display_name_for_email("1020010000019120@askii.ai"), "1020010000019120")

    def test_characters_check_full_name_rejects_are_dropped(self) -> None:
        self.assertEqual(display_name_for_email('"j*a`n>e"@example.com'), "jane")
        self.assertEqual(display_name_for_email("jane|12@example.com"), "jane")
        self.assertEqual(display_name_for_email("jane|1|2@example.com"), "jane")
        self.assertEqual(display_name_for_email("ja\x00ne@example.com"), "jane")

    def test_long_local_part_is_truncated(self) -> None:
        name = display_name_for_email("a" * 150 + "@example.com")
        self.assertEqual(len(name), UserProfile.MAX_NAME_LENGTH)

    def test_unusable_local_part_falls_back(self) -> None:
        self.assertEqual(display_name_for_email("***@example.com"), "at example.com")
        self.assertEqual(display_name_for_email("@"), FALLBACK_NAME)
        self.assertEqual(display_name_for_email("1020010000019120"), "1020010000019120")
        self.assertEqual(display_name_for_email(""), FALLBACK_NAME)


class MpassFirstSignInTest(MpassSSOTestCase):
    # The dev settings set TERMS_OF_SERVICE_VERSION, which adds a required
    # checkbox; the bundle leaves it unset, so registration needs no input.
    @override
    def mpass_settings(self, **overrides: object) -> override_settings:
        return super().mpass_settings(**{"TERMS_OF_SERVICE_VERSION": None, **overrides})

    def sign_in(self, email: str, headers: Mapping[str, str] = {}) -> "TestHttpResponse":
        return self.client_get(
            "/accounts/login/sso/",
            REMOTE_USER=email,
            headers={"X-Auth-Request-Email": email, **headers},
        )

    def complete_registration(self, result: "TestHttpResponse") -> "TestHttpResponse":
        self.assertEqual(result.status_code, 302)
        self.assertIn("/accounts/do_confirm/", result["Location"])
        confirm_page = self.client_get(result["Location"])
        self.assert_in_response('action="/accounts/register/"', confirm_page)
        key = result["Location"].split("/accounts/do_confirm/")[1].split("?")[0]
        return self.client_post("/accounts/register/", {"from_confirmation": "1", "key": key})

    def assert_nothing_created(self, email: str) -> None:
        self.assertFalse(PreregistrationUser.objects.filter(email__iexact=email).exists())
        self.assertFalse(UserProfile.objects.filter(delivery_email__iexact=email).exists())

    def test_first_sign_in_creates_the_account(self) -> None:
        realm = get_realm("zulip")
        email = "new.person@zulip.com"
        with self.mpass_settings():
            result = self.complete_registration(self.sign_in(email))
        self.assertEqual(result.status_code, 302)
        user = get_user_by_delivery_email(email, realm)
        self.assertEqual(user.full_name, "new.person")
        self.assertEqual(user.role, UserProfile.ROLE_MEMBER)
        self.assertFalse(user.has_usable_password())
        self.assert_logged_in_user_id(user.id)

        self.client_post("/accounts/logout/")
        with self.mpass_settings():
            result = self.sign_in(email)
        self.assertEqual(result.status_code, 302)
        self.assertNotIn("do_confirm", result["Location"])
        self.assert_logged_in_user_id(user.id)
        self.assertEqual(UserProfile.objects.filter(delivery_email__iexact=email).count(), 1)

    def test_unusable_local_part_registers_with_the_fallback(self) -> None:
        email = "***@zulip.com"
        with self.mpass_settings():
            self.complete_registration(self.sign_in(email))
        user = get_user_by_delivery_email(email, get_realm("zulip"))
        self.assertEqual(user.full_name, "at zulip.com")

    def test_refused_sign_in_creates_nothing(self) -> None:
        email = "refused.person@zulip.com"
        wrong_tenant = access_token({"custom:is_corporate": "true", "custom:corporate_id": "other"})
        with self.mpass_settings(MPASS_SMB_CORPORATE_ID="acme"):
            result = self.sign_in(email, {"X-Auth-Request-Access-Token": wrong_tenant})
        self.assertEqual(result.status_code, 403)
        with (
            self.mpass_settings(),
            mock.patch("zerver.lib.mpass_sso.configured_edge_secret", return_value=EDGE_SECRET),
        ):
            result = self.sign_in(email)
        self.assertEqual(result.status_code, 403)
        self.assert_nothing_created(email)
