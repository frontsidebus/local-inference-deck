# Add a user

A user gets up to four things, independently:

| Access | Granted by |
|---|---|
| Chat (Open WebUI) | Pocket-ID account + membership of `${CHAT_GROUP}` |
| Telemetry dashboard | Pocket-ID account + membership of `${TELEMETRY_GROUP}` |
| Digest (optional app) | Pocket-ID account + membership of `${DIGEST_GROUP}` |
| API (harnesses on their own machine) | A LiteLLM virtual key |

Signup is off everywhere. Pocket-ID is passkey-only: the user never has a password.

You need the user's **email address** (Pocket-ID requires one) and a short username, e.g. `alice`.

## How site access works

Each web app has its own Pocket-ID OIDC client, and each client admits only one group:

| Site | Pocket-ID client id | Allowed group | Enforced by |
|---|---|---|---|
| `https://${SPARK_CHAT_HOST}` | `open-webui` | `${CHAT_GROUP}` (`chat-users`) | Pocket-ID |
| `https://${SPARK_TELEMETRY_HOST}` | `${OAUTH2_PROXY_CLIENT_ID}` (`telemetry`) | `${TELEMETRY_GROUP}` (`telemetry-viewers`) | Pocket-ID, and oauth2-proxy `allowed_groups` on the edge |
| `https://${SPARK_DIGEST_HOST}` (optional) | `${OAUTH2_PROXY_DIGEST_CLIENT_ID}` (`digest`, or the UUID Pocket-ID generated) | `${DIGEST_GROUP}` (`digest-viewers`) | Pocket-ID, and oauth2-proxy-digest `allowed_groups` on the edge |

So **granting access to a site means adding the user to that site's group**, and nothing else.
Clients, the edge and the apps need no change. Group membership is read at login: a user added
while signed in gets access at their next login. A user removed from a group keeps an existing
session until it expires (12 hours for telemetry and the digest).

## 1. Pocket-ID account

In the Pocket-ID admin UI at `https://${SPARK_ID_HOST}`:

1. **Users → Add user.** Username `alice`, their email, first and last name. Leave admin off.
2. **Add them to the groups for the sites they should use** (next section). `${CHAT_GROUP}` is the
   usual minimum.
3. **Users → alice → Login code / one-time link.** Send the link to the user over a channel you
   trust. Opening it lets them register a passkey. The link expires; create another if they miss it.

## Add a user to a site's group

This works the same for a new user and for an existing one who needs another site.

**UI:** User groups → the site's group (table above) → select the user in the member list → Save.
Check the result on the same page: the group lists its members.

**Scripted** (several users at once): `sudo /srv/webui/pocketid-bootstrap.py --users FILE` on
Walter, with lines `username email [group,group]`. It creates missing users, adds them to the
listed groups, and writes one-time login links for new users to `/srv/webui/oidc/login-links.txt`
(0600; delete it after sending the links). It only knows `${CHAT_GROUP}` and `${TELEMETRY_GROUP}`,
and warns `unknown group` for any other, so add digest members in the UI. The script works through
a temporary `STATIC_API_KEY`, which recreates the `pocket-id` container twice, so every login
pauses for a few seconds. See [walter/README.md](../../walter/README.md#pocket-id--oidc-bootstrap).

If you call the Pocket-ID API yourself instead, note that `PUT /api/user-groups/<group id>/users`
**replaces** the member list with the `userIds` you send. Read the current members first
(`GET /api/user-groups/<group id>`) and send them plus the new one, as the script does. Remove the
temporary `STATIC_API_KEY` as soon as you are done: it is full admin while it exists.

**Check** that it worked: the user signs in at the site. A user outside the group is either refused
by Pocket-ID at login, or gets 403 from oauth2-proxy (telemetry, digest).

## 2. Open WebUI account

Nothing to do. The account is created on the user's first OIDC login, with role `user`. They see the models that have public-read access grants (`coder`, `coder-fast`, `big`, `vision`, `hermes`); admin-only models such as `hermes-agent` stay hidden.

Tell them to pick **Dark** in Settings → General to get the theme; there is no server-side default.

To make someone an Open WebUI admin: Admin Panel → Users → change role. Do this sparingly, since admins can use `hermes-agent`, which runs tools on the operator's workstation.

## 3. LiteLLM key (API access)

On Walter:

```bash
cd /srv/gateway
set -a; . ./.env; set +a
curl -s http://${BACKEND_WG_IP}:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
  -d '{"key_alias":"alice","rpm_limit":60,"max_parallel_requests":4}' \
  | jq -r .key | sudo install -m 600 /dev/stdin keys/alice.key
```

All keys use the same limits (60 rpm, 4 parallel requests); spend and usage are attributed per key. Add the name to `SPARK_USERS` in your local `site.env` so `scripts/gen-secrets.sh` and the docs stay in sync with reality.

Hand the key over once, out of band. The user stores it in a file (mode 600) and points their harness at `https://${SPARK_API_HOST}/v1` (see [clients/](../../clients/README.md) for each harness's settings).

## 4. Verify

- The user logs in at `https://${SPARK_CHAT_HOST}` with their passkey and sends a chat.
- If they are in `${TELEMETRY_GROUP}` or `${DIGEST_GROUP}`, they also open that site and see the dashboard.
- With their key: `curl -s https://${SPARK_API_HOST}/v1/models -H "Authorization: Bearer <key>"` lists the aliases.

## Remove a user

1. Pocket-ID: disable or delete the user (this ends new logins; also revoke their sessions). To take away one site only, remove them from that site's group instead.
2. Open WebUI: Admin Panel → Users → delete (or set to `pending` to keep their chats).
3. LiteLLM: `POST /key/delete` with their key, and remove `keys/<name>.key`.
4. Remove the name from `SPARK_USERS`.

Existing telemetry and digest sessions last until their cookie expires (12 hours). To end them at
once, restart that oauth2-proxy instance after rotating its cookie secret (see
[rotate-secrets](rotate-secrets.md)).
