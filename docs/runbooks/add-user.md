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

## 1. Pocket-ID account

In the Pocket-ID admin UI at `https://${SPARK_ID_HOST}`:

1. **Users → Add user.** Username `alice`, their email, first and last name. Leave admin off.
2. **User groups → `${CHAT_GROUP}` → add `alice`.** Add `${TELEMETRY_GROUP}` only if they should see the dashboard.
3. **Users → alice → Login code / one-time link.** Send the link to the user over a channel you trust. Opening it lets them register a passkey. The link expires; create another if they miss it.

Scripted alternative (for several users at once): set a temporary `STATIC_API_KEY` in `/srv/webui/pocket-id.env`, restart Pocket-ID, call its admin API with the `X-API-KEY` header from Walter, then **remove the key and restart again**. That key is full admin while it exists.

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
- With their key: `curl -s https://${SPARK_API_HOST}/v1/models -H "Authorization: Bearer <key>"` lists the aliases.

## Remove a user

1. Pocket-ID: disable or delete the user (this ends new logins; also revoke their sessions).
2. Open WebUI: Admin Panel → Users → delete (or set to `pending` to keep their chats).
3. LiteLLM: `POST /key/delete` with their key, and remove `keys/<name>.key`.
4. Remove the name from `SPARK_USERS` and from `${TELEMETRY_GROUP}` if present.
