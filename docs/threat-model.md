# Threat model

This service watches a private home. It holds a description of who comes and
goes, a Home Assistant token that can control the house, and an API key that
costs money. It is worth being explicit about what it defends against and what
it does not.

## What is being protected

| Asset | Why it matters | Where it lives |
|---|---|---|
| Home Assistant long-lived token | grants full control of the house | `.env` only |
| Anthropic API key | billable | `.env` only |
| Webhook secret | authenticates event injection | `.env`, and HA's `secrets.yaml` |
| Event history | a record of household activity and visitors | `data/hermes-home.db` |
| Event images | pictures of people at the door | **not retained** |

## Trust boundaries

```
   Internet ─── (no path in; nothing is exposed, no port forwarding)
                                │
   Home LAN (private) ──────────┼── Home Assistant
                                │        │ webhook, authenticated by shared secret
                                │        ▼
                                └── hermes-home  <LAN_BIND_IP>:8099
                                         │  (also 127.0.0.1:8099)
                                         │ MCP over localhost
                                         ▼
                                    Hermes Agent
                                         │
   Anthropic API ◄───── outbound HTTPS ──┘   (event stills, transiently)
```

The LAN is treated as **semi-trusted**: reachable by every device on the home
network, including ones with no business talking to this service. Hence the
webhook secret rather than an IP allowlist alone.

## Controls

**Network exposure is two explicit addresses, not a wildcard.** Docker publishes
`127.0.0.1:8099` (for Hermes) and `$LAN_BIND_IP:8099` (for Home Assistant) and
nothing else — not `0.0.0.0`, so no VPN adapter or secondary interface exposes
it. Verified: exactly two listening sockets.

**No inbound path from the internet.** No port forwarding, no tunnel, no cloud
ingress. Do not add one; if remote access is ever needed, put it behind a VPN
rather than exposing this port.

**Webhook authentication.** A 43-character random secret in the
`X-Hermes-Webhook-Secret` header, compared with `hmac.compare_digest` so the
comparison is constant-time. A wrong or missing secret gets 401 and nothing is
written. The secret was rotated after an earlier development placeholder was
echoed to a terminal; the old value is rejected.

**MCP DNS-rebinding protection** is enabled: a request whose `Host` header is not
in the allowlist gets 421. This matters because a browser on the LAN could
otherwise be induced by a malicious page to reach the endpoint. Verified with a
forged Host header.

**Request size limits.** Bodies over `WEBHOOK_MAX_BODY_BYTES` (64 KB) are
rejected with 413 before parsing.

**Input validation.** Event types are checked against a registry; timestamps must
carry a UTC offset; unknown cameras are rejected. Payloads are Pydantic-validated
at the write boundary.

**Container hardening.** Runs as non-root uid 10001, `no-new-privileges`, config
mounted read-only, only `/data` writable, and nothing of value in the container
filesystem.

## Secret handling

Secrets come from `.env` and nowhere else. They are:

- **not in the repository** — `.env` is gitignored, and `.env.example` holds only
  placeholders;
- **not in the image** — `.dockerignore` excludes `.env`, and the built image was
  scanned for the live token, key and webhook secret. One apparent hit was
  checked and proved to be pyjwt's documentation example, which shares the
  standard `{"alg":"HS256"}` JWT header — not our token;
- **not in build args or layer history**, which would persist even if the file
  were deleted;
- **not in logs** — a structlog processor redacts known secret-shaped keys as a
  second line of defence, and no call site passes one;
- **not in exception messages** — the Home Assistant client raises "credentials
  rejected", never the token.

Home Assistant holds its own copy of the webhook secret in `secrets.yaml`,
referenced as `!secret hermes_home_webhook_secret`, so it is not inline in
`configuration.yaml`.

**Backups contain secrets.** `make backup` copies `.env`, so
`~/hermes-home-backups/` is created mode `700` with `env.backup` at `600`. Do not
put it on shared or cloud storage without encryption.

## Privacy

Minimising what is kept is the main privacy control.

**Images are not retained.** Retrieve, analyze, discard. What survives is the
derived observation, a content hash, and pixel dimensions. This is irreversible
per event — a frame cannot be re-analyzed later with a better model — and that
tradeoff is accepted deliberately.

**No identity, by construction.** No facial recognition, no biometric matching,
no licence-plate database, no persistent identity across events. The
`SceneObservation` schema is `extra="forbid"` and contains no identity field, so
a model has nowhere to record one even if it tried. Enforcing this in the schema
rather than only in the prompt is the point: schemas outlive prompts.

Observations describe appearance — "a person in a grey t-shirt" — which is
useful and not identifying.

**Event stills are sent to Anthropic** when `VISION_PROVIDER=anthropic`. That is
the one point where a picture of your doorstep leaves the LAN. It is transient
(not retained by us) but it is a real disclosure, and it is why the provider is
pluggable: a local model can be added behind the same interface with no change
to ingest or storage.

**Retained history is a behavioural record.** Timestamps and observations of who
came and went are kept indefinitely by design — that is the product. Raw webhook
bodies are pruned after 14 days.

## What this does not defend against

Stated plainly, because an unstated limitation is worse than a known one.

- **A compromised device on the LAN.** With the webhook secret it can inject
  fabricated events; without it, it can still reach the port. There is no
  network-level allowlist beyond the interface binding.
- **Anyone with read access to this Mac.** `.env` is protected by filesystem
  permissions only; there is no encryption at rest. A user who can read the
  home directory has the HA token and can control the house.
- **A compromised Home Assistant.** hermes-home trusts what HA tells it.
- **Denial of service.** An attacker who knows the secret can flood the queue.
  Retries are bounded and images are size-capped, but there is no rate limiting.
- **Tampering with the database.** No integrity signing; anyone who can write
  `data/hermes-home.db` can rewrite history.
- **Automatic login** (if enabled for unattended reboot) means an unlocked Mac.
  That is a genuine tradeoff and why it is not enabled by default.

## If a secret is exposed

1. **Home Assistant token** — revoke it in HA (Profile → Security), create a new
   one, update `.env`, `make restart`.
2. **Webhook secret** — generate a new one
   (`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`), update
   `.env` *and* HA's `secrets.yaml`, restart both.
3. **Anthropic key** — revoke in the Anthropic console, issue a new one, update
   `.env`, `make restart`.

Rotating the webhook secret takes effect immediately: the old value returns 401.
