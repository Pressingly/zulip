# mPass SSO (oauth2-proxy ForwardAuth)

How this fork authenticates on the Moneta FOSS platform, what a deployment has to provide,
and what Layer 2 needs to wire it into the bundle.

Activated by `MPASS_SSO = True` together with `ZulipRemoteUserBackend` as the only
authentication backend (`ZULIP_AUTH_BACKENDS=ZulipRemoteUserBackend` in the image). With
`MPASS_SSO` off, Zulip behaves like upstream with one exception: this image's nginx always maps
`X-Auth-Request-Email` to `REMOTE_USER` on `/accounts/login/sso/` (and marks the request with
the `MPASS_REMOTE_USER_FROM_EDGE` uwsgi parameter), and without `MPASS_SSO` there is no gate
behind it, so such a sign-in is refused with `403`. `REMOTE_USER` from any other source keeps
upstream's behaviour.

## How it works

Zulip never talks to the identity provider. Traefik asks oauth2-proxy (`mpass-auth`) whether
each request carries a valid mPass session; if not, the browser goes through the mPass QR login
and comes back. Requests that reach Zulip carry the user's identity in `X-Auth-Request-*`
headers.

```text
browser ──> traefik ──(ForwardAuth)──> oauth2-proxy ──> mpass-auth-proxy ──> Cognito
               │
               └─ request + X-Auth-Request-* ──> zulip nginx ──> uwsgi (Django) / Tornado
```

- **Sign-in.** With only `ZulipRemoteUserBackend` enabled (`ONLY_SSO`), a signed-out visitor is
  sent to `/accounts/login/sso/`. Zulip's nginx maps `X-Auth-Request-Email` to the uwsgi
  `REMOTE_USER` parameter on that endpoint only (`puppet/zulip/files/nginx/zulip-include-app.d/mpass-sso.conf`,
  installed by `zulip::profile::docker`). No other path, and no client header, can set
  `REMOTE_USER`.
- **Existing user:** signed in and sent to `next`.
- **New user:** the account is created on the first sign-in, with no "account not found" page
  and no name form. The display name starts as the part of the email before the `@`
  (`zerver/lib/mpass_names.py`); users change it in settings, so the organization must keep
  `name_changes_disabled` off. The request goes through Zulip's own signup checks
  (`invite_required=False`, allowed domains, no disposable addresses) and its standard
  confirmation step, a page that posts itself (`full_name_validated`, `is_signup`), so nothing
  asks the user anything. The corporate gate and the edge secret run before any of this: a
  refused sign-in creates no `PreregistrationUser` or `UserProfile`.
- **Identity switch.** `MpassIdentitySwitchMiddleware` (after Django's `AuthenticationMiddleware`)
  compares the edge identity with the session user on every request, Tornado's long-poll
  included. A different identity logs the Zulip session out, so the next page load signs the new
  person in. A missing header carries no signal (internal traffic sends none).

Zulip's native OIDC is not used: it would bypass oauth2-proxy and mpass-auth-proxy (mPass QR
sign-in only works through them), has no corporate gate, and keeps a session the portal's logout
cannot end.

## Configuration

All values are Django settings in `/etc/zulip/settings.py`; the image's entrypoint writes every
`SETTING_<NAME>` environment variable there and every `SECRETS_<name>` into
`zulip-secrets.conf`.

| Setting | Required | Meaning |
|---|---|---|
| `MPASS_SSO` | yes | `True` turns the integration on |
| `MPASS_DEFAULT_EMAIL_DOMAIN` | yes | Domain for a bare mPass id (`<id>@<domain>`). Empty, every bare-id sign-in is refused. Must match the other bundle apps (`DEFAULT_EMAIL_DOMAIN`) |
| `MPASS_SMB_CORPORATE_ID` | one of these two | Only principals whose access token has `custom:is_corporate=true` and this `custom:corporate_id` may sign in |
| `MPASS_ALLOW_ANY_TENANT` | | `True` admits every mPass principal when `MPASS_SMB_CORPORATE_ID` is empty. See [Trade-offs](#trade-offs) |
| `MPASS_LOGOUT_REDIRECT_URL` | yes | The portal the user menu's Log out navigates to |
| `SESSION_COOKIE_AGE` | no | Zulip session lifetime; the bundle passes `SESSION_COOKIE_MAX_AGE_SECONDS` (432000) |
| secret `mpass_edge_secret` | no | Optional edge secret; see [Header trust](#header-trust) |
| `EXTERNAL_HOST` | yes | The host users see (`ZULIP_HOST`). One root-domain organization |

Behind Traefik, Zulip must trust the proxy's `X-Forwarded-Proto`: the image bakes
`[loadbalancer] ips = 172.16.0.0/12,192.168.0.0/16,10.0.0.0/8` into `zulip.conf`. Without it
every request fails with Zulip's "reverse proxy misconfiguration" page.

## Header trust

Header trust is topology-based, the same as every bundle app:

1. Zulip's port is never published; Traefik is the only way in.
2. Every Zulip router runs `strip-auth-headers` first, so a client cannot inject
   `X-Auth-Request-*`; on the protected router `mpass-auth` then sets the verified values.
3. oauth2-proxy validates the upstream session on every ForwardAuth call.

**Optional edge secret.** Topology trust does not cover another container on the same Docker
networks, which can reach Zulip's nginx directly with any header. Setting the
`mpass_edge_secret` secret (at least 32 characters; `ZULIP_EDGE_SECRET` in the bundle) closes
that: the protected router adds `X-Mpass-Edge-Secret`, the bypass router strips a client copy, and
Zulip trusts identity headers only on a request carrying it (constant-time compare). Sign-in
without it gets `403`; the identity-switch middleware ignores the header. A configured secret
shorter than 32 characters trusts nothing.

## Trade-offs

- **Tenant binding fails closed.** With `MPASS_SMB_CORPORATE_ID` empty, every sign-in is refused
  unless `MPASS_ALLOW_ANY_TENANT=True`: Zulip holds the team's conversations, and an empty
  variable silently admitting the whole Cognito pool is the easy mistake. The access token is
  decoded without verification (oauth2-proxy validated it).
- **The gate runs at sign-in.** An existing Zulip session is not re-checked on every request; an
  identity switch at the edge ends it (above), and the session lifetime bounds the rest.
- **Search stemming.** The bundle's shared Postgres has no hunspell dictionary, so full-text search
  uses plain `pg_catalog.english` stemming.
- **Mobile apps.** `/api/*` stays behind mPass on the public host, so the mobile apps cannot sign
  in. Web and desktop work.

## Provisioning

- Identity is `X-Auth-Request-Email` only, trimmed and lowercased. A value with an `@` (not first)
  and a `.` at least one character after it is used as is; a bare value becomes
  `<value>@${MPASS_DEFAULT_EMAIL_DOMAIN}`. The check is index-based, never a regex
  (`zerver/lib/mpass_sso.py`). Stock `SSO_APPEND_DOMAIN` is not used: it appends to every value
  and breaks real emails.
- The starting display name is the email's local part with the characters `check_full_name`
  rejects removed (`*`, `` ` ``, `\`, `>`, `"`, `@`, control characters, a trailing `|<digits>`),
  cut to 100 characters. If nothing is left it falls back to `<local part> at <domain>`
  (an `@` is never valid in a name), then to `mPass user`. A bare mPass id's name is the id. The
  name is never used for roles or tenancy. In an organization with "unique names" on, a taken
  name sends the user to Zulip's name form instead.
- New users get Zulip's default role (member). Owners come from the organization's creation and
  `manage.py change_user_role <email> owner -r ''`.
- Mail to `@MPASS_DEFAULT_EMAIL_DOMAIN` placeholder addresses is never sent
  (`zerver/lib/send_email.py`): nobody receives mail there.
- **mPass owns the email address.** The account is bound to the edge identity by its email, so a
  changed Zulip email would end the session on every request and the next sign-in would create
  a second account. Under `MPASS_SSO` every email change is refused server-side, administrators
  and `can_change_user_emails` included (`zerver/views/user_settings.py`, `zerver/views/users.py`),
  and the web app hides the control (`server_email_changes_disabled` in the register response).
  Change the address in mPass instead.
- `MPASS_ALLOW_ANY_TENANT` opens the gate only when it is the boolean `True`; any other value,
  such as the string `"no"`, keeps it closed.

## Logout

Log out in the user menu ends the Zulip session and navigates to `MPASS_LOGOUT_REDIRECT_URL`,
the portal. It never touches the mPass session: ending that is the portal's "Log out of all apps".
Without the redirect, `ONLY_SSO` would sign the user straight back in.

## Logged-out tabs

Once the mPass session ends, `mpass-auth` answers the `/json/events` long-poll with a redirect to
the auth host. The browser refuses the cross-origin redirect and jQuery reports status 0, which
Zulip treated as a network error and retried forever. `web/src/edge_sign_in.ts` tells the two
apart on a status-0 events error with one probe, `fetch("/api/v1/server_settings", {redirect:
"manual"})`:

- `opaqueredirect`: the edge wants a sign-in, so the tab reloads (a top-level navigation goes
  through mPass and lands back in Zulip);
- a network failure or a normal response: Zulip's retry and backoff carry on;
- loop guard: one reload per 5 minutes (`sessionStorage`). Within that window, or without a
  usable `sessionStorage`, polling stops and a "Your sign-in has expired" banner offers a
  reload. While the probe runs, and once polling has stopped this way, Zulip's
  "events are not running" check (on sending a message) leaves polling stopped.

## ForwardAuth routing

| Router | Rule | Priority | Middlewares |
|---|---|---|---|
| `zulip-secure` | `Host(ZULIP_HOST)`: the UI, `/accounts/login/sso/`, `/json/*` (events included), `/api/*` | 10 | `strip-auth-headers`, `security-headers`, `mpass-auth`, launchpad `verify-gate` (when on), the edge-secret injector |
| `zulip-bypass` | `PathPrefix(/static/)`, `Path(/favicon.ico)`, `Path(/robots.txt)` | 20 | `strip-auth-headers`, `security-headers`, the edge-secret strip |

No route is framable, so `X-Frame-Options` stays everywhere. Internal API callers use
`http://zulip` on the backend network with `Host: <ZULIP_HOST>` and `X-Forwarded-Proto: https`.

## Storage (SeaweedFS over internal HTTP)

Zulip writes uploads to S3 itself and serves downloads through its nginx (`X-Accel-Redirect` to
`/internal/s3/`), so browsers only read avatars directly. Upstream assumes an `https://` S3
endpoint; this fork also accepts a plain-HTTP one on a private network
(`zerver/views/upload.py`, `uploads-internal.conf`): the signed URL keeps its host and port and
nginx proxies it over `http`, so Zulip talks to `http://seaweedfs:8333` on the backend network.

| Setting | Bundle value |
|---|---|
| `S3_ENDPOINT_URL` | `http://seaweedfs:8333` |
| `S3_AUTH_UPLOADS_BUCKET` / `S3_AVATAR_BUCKET` / `S3_EXPORT_BUCKET` | `zulip-uploads` / `zulip-avatars` / `zulip-exports` |
| `S3_AVATAR_PUBLIC_URL_PREFIX` | `https://s3api.<domain>/zulip-avatars/` |
| `S3_ADDRESSING_STYLE`, `S3_SKIP_CHECKSUM`, `S3_REGION` | `path`, `True`, `us-east-1` |
| `LOCAL_UPLOADS_DIR` | `None` |
| secrets `s3_key`, `s3_secret_key` | a key scoped to the three buckets |

Only `zulip-avatars` is anonymously readable (a SeaweedFS `anonymous` identity with
`Read:zulip-avatars`); uploads and exports refuse anonymous reads.

## Operations

**Image.** `docker/Dockerfile` (adapted from docker-zulip, Apache-2.0) builds the release tarball
from this checkout (`tools/build-release-tarball`), so the build needs `.git` with tags and a
clean committed tree; `.dockerignore` keeps `.git`. Build natively for the target host; the
frontend build is far too slow under QEMU:

```bash
docker build -f docker/Dockerfile -t <registry>/zulip:<tag> .
```

**What the entrypoint does at boot** (`docker/entrypoint.sh`; each phase logs `[zulip-boot]`
with its duration):

1. Django's log files are created and owned by `zulip` before anything starts: the email
   gateway runs as root, and upstream relied on a boot-time `manage.py check` to create them
   first.
2. `zulip.conf` from the baked copy plus `CONFIG_<section>__<key>` variables. The image is built
   with Puppet already applied for the bundle's `zulip.conf` (HTTP only, the load-balancer ranges,
   one queue-worker process, 4 uwsgi workers); Puppet runs again only when the runtime
   `zulip.conf` differs.
3. The nginx resolver (`/etc/resolv.conf`, or `CONFIG_application_server__nameserver`) and the
   go-camo user agent (`EXTERNAL_HOST`) are patched in place.
4. `settings.py` from `SETTING_*`, secrets from `SECRETS_*`; `generate_secrets` only on a new image.
5. `manage.py check` only when the configuration or image changed.
6. Migrations only when the database lacks one: the image carries a manifest of the migrations
   it ships (`/usr/local/share/zulip/migration-manifest`, written at build), and one `psql`
   query at boot checks that each is in `django_migrations` and that the
   `third_party_api_results` cache table exists. The schema lives in the shared database, so a
   reset, restore or move of the database is noticed; any mismatch or error runs `migrate`.
   Provisioning runs them explicitly: `docker exec zulip /sbin/entrypoint.sh app:migrate`.
7. supervisord: nginx, uwsgi, Tornado, the queue worker, cron, go-camo, smokescreen, tusd.

Measured on an arm64 devstack: a restart is healthy in about 9 s and a recreate in about 12 s
(entrypoint under 1 s, the rest is uwsgi and Tornado starting), against 17 s and 23 s with a
Puppet run, `manage.py check` and `migrate` on every boot. A first boot against an empty
database takes about 40 s.

A configuration change that needs no recreate:
`docker exec zulip /home/zulip/deployments/current/scripts/restart-server`.

**Volume.** `/data` holds `zulip-secrets.conf` (generated secrets such as `secret_key`) and the
boot markers. Back it up with the database.

**Health check:** `curl -sfL http://localhost/health` inside the container.

## Tests

- `zerver/tests/test_mpass_sso.py`: email normalization, the corporate gate, the edge secret,
  identity switch, logout, account creation on the first sign-in, refused email changes, the
  header mapping without `MPASS_SSO`, and placeholder mail.
- `zerver/tests/test_upload_s3.py::test_user_uploads_internal_http_endpoint`: the internal-HTTP
  download path.
- `web/tests/edge_sign_in.test.cjs` and `web/tests/server_events.test.cjs`: the logged-out tab fix.
