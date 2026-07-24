# Security And Privacy

This document describes the current local security/privacy model and known
risks. Do not overclaim security: this is a local prototype, not a hardened
multi-user service.

## Localhost Dashboard Model

The local dashboard/server runs on `127.0.0.1` by default. Static inspection of
`agent/local_server.py` confirms:

- the server rejects non-`127.0.0.1` bind hosts,
- the default port is `0`, meaning the OS chooses a local port,
- API calls require the `X-Controller-Token` header,
- token comparison uses `hmac.compare_digest`,
- Host headers are restricted to `127.0.0.1:<port>` or `localhost:<port>`,
- POST requests validate Origin when an Origin header is present,
- JSON body sizes are bounded by route,
- responses use `Cache-Control: no-store` and
  `X-Content-Type-Options: nosniff`.

The bootstrap URL includes the session token in the URL fragment:
`http://127.0.0.1:<port>/#token=<token>`. The fragment is not sent as an HTTP
request path, but any local browser/page code that can read the fragment can
use the token. Treat the dashboard as local-only.

## Token And Lease Protection

The local controller session token is generated with `secrets.token_urlsafe`.
ChatGPT UI leases use an opaque token returned to the acquiring process. Ledger
events currently store lease token fingerprints (`lease_token_sha256`) rather
than raw lease tokens for new lease events.

There is also a historical redaction helper for ChatGPT UI lease tokens in
`agent/ledger.py`. That redaction helper is specific to lease tokens; it is not
a general evidence redaction system.

## Local Ledger Evidence

The ledger stores runs and events in local SQLite at `data/agent_ledger.db`.
Event metadata is JSON. It can include sensitive information such as:

- user instructions and extracted prompts,
- Codex prompts and final messages,
- repository paths,
- git status/diff metadata,
- changed-file names and classifications,
- ChatGPT destination titles,
- ChatGPT submission markers,
- captured ChatGPT response text and hashes,
- Accessibility candidate summaries and visible UI text,
- command stdout/stderr summaries or hashes.

Assume ledger evidence is sensitive and local plaintext unless a field is
explicitly hashed or redacted.

## Clipboard And ChatGPT Desktop Risks

Clipboard use writes full feedback payloads or markers into the macOS clipboard
through `pbcopy`. Paste/submit helpers can send clipboard content into the
frontmost app through AppleScript/System Events.

Risks include:

- leaking sensitive repo or prompt content through clipboard history or other
  clipboard-aware apps,
- pasting into the wrong app if focus/frontmost checks are wrong or stale,
- submitting the wrong content if ChatGPT UI state changes between checks,
- recording sensitive ChatGPT response text in local evidence.

## macOS Accessibility And CoreGraphics Risks

Accessibility readers can inspect visible UI structure and text from ChatGPT
Desktop. AXPress helpers can perform actions on Accessibility elements.
CoreGraphics helpers can post mouse clicks and scroll events at screen
coordinates.

Risks include:

- reading visible titles, prompt text, response text, and UI metadata from the
  desktop,
- clicking or scrolling the wrong target if geometry is stale,
- interacting with another app if focus, window, or coordinate assumptions fail,
- requiring powerful macOS Accessibility/input permissions for the terminal or
  Python process.

Diagnostic and synthetic input commands should remain manual-only unless
explicitly promoted.

## Plaintext Retention Risk

The project currently lacks a general retention, redaction, or encryption
policy for ledger evidence, prompt artifacts, Codex final-message artifacts, and
diagnostic outputs. Some artifacts are written under temporary directories, and
the ledger persists locally until removed.

Future security work needs explicit decisions for:

- retention duration,
- redaction of prompts, diffs, ChatGPT responses, titles, and command output,
- encryption at rest for local evidence,
- operator-controlled purge/export flows,
- minimum evidence needed for debugging versus privacy,
- handling of historical plaintext evidence.

## Security Boundary

Current protections are useful for a local single-user dashboard, but they are
not a remote access security boundary. Do not expose the local server to a
network. Do not assume localhost token checks protect against malicious local
software, compromised browser context, or a user who shares the bootstrap URL.

## Opt-In Remote Mode

Remote mode preserves the loopback-only Python bind and is intended to sit
behind Tailscale Serve. Tailscale provides private device reachability and TLS;
CRAX adds a second application-level device credential. Remote mode is enabled
only when `--remote-base-url` is supplied.

Remote controls include:

- exact trusted Host and Origin validation for the configured HTTPS URL,
- one-time pairing codes with a short expiration,
- random device credentials stored only as SHA-256 fingerprints,
- Secure, HttpOnly, SameSite cookies in the phone browser,
- read/control/admin authorization checks per route,
- expiring and revocable paired devices,
- remote mutation audit records,
- repository authorization under explicit `--repository-root` directories,
- remote Full Access disabled unless the Mac owner opts in,
- a fresh typed confirmation for every remote Full Access run.

The local bootstrap token remains available for the browser opened directly on
the Mac. It is not returned to or stored by paired remote devices.

Remote mode does not make public-internet exposure safe. In particular:

- do not use Tailscale Funnel, a public reverse proxy, router port forwarding,
  ngrok, or a public Cloudflare Tunnel for this server;
- anyone controlling a paired admin device can start Codex runs and drive the
  ChatGPT Desktop handoff;
- a malicious process on the Mac remains outside this threat boundary;
- Tailscale account and device security are part of the trusted computing base;
- the remotely displayed run data can contain sensitive repository and prompt
  information described above.

If a phone is lost, remove it from the tailnet and revoke its CRAX device
credential. Stop remote serving with `tailscale serve reset`. Removing the
device does not delete historical remote audit records or run evidence.
