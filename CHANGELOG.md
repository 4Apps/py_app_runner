# Changelog

One line per change, newest release first. **Breaking:** marks anything a downstream project
must act on. Versions missing from the list were never published.

## Unreleased

- A project service shadows the built-in of the same name; a project `cron` no longer imports `py_app_runner.cron` (needed the `cron` extra)

## v0.7.1 - 2026-10-27

- Dependencies: `database_wrapper == 0.4.0` (brings `aiosqlite >= 0.17.0`)

## v0.7.0 - 2026-10-27

- **Breaking:** a non-empty queue `handlers` map is an allow-list; a job name outside it fails instead of being imported
- **Breaking:** `max_body_size` defaults to 10 MiB instead of tornado's 100 MB
- **Breaking:** `@rate_limit` raises `RuntimeError` without `redis_con` instead of silently not limiting
- **Breaking:** `audit.delete()` needs UPDATE privilege, since the before-read is `SELECT ... FOR UPDATE`
- Security: unparseable WebSocket frames are logged as a bounded prefix; Sentry dedupe cost no longer grows with the event
- Security: `@rate_limit` and `@audited` take the client IP from `client_ip()`, not the spoofable `X-Real-IP`
- Security: a WebSocket's JWT is re-verified per message; expired tokens and disabled users drop within 30 s
- Security: an impersonation token is refused when the impersonator is missing, disabled or deleted
- Security: an incomplete WebSocket message is no longer echoed into the log; `msg_id` 0 is valid
- Security: `@require_auth_for_actions` also guards actions a subclass adds or overrides
- Security: audit `exclude` matches table names case-insensitively and schema-qualified; `record()` redacts too
- Security: more redaction markers; form-encoded and top-level JSON array bodies are redacted
- Security: a revoked API key reaches `key_can()` in services not requiring a key
- Queue: a job claimed with its attempts used up goes to failed instead of running again
- Queue: a worker whose claim was taken over can no longer complete, release or fail the job
- Migrations: `-- migrations:foreign-keys-off` header for SQLite table rebuilds (`defer_foreign_keys` still cascades)
- Crypto: `rotate` works on Postgres again and skips rows changed while it runs; malformed `pa1:` values raise
- Crypto: `PasswordHasher.verify()` returns False for passwords over 72 bytes instead of raising
- Cron: 64-bit advisory lock keys; lock files are 0600 and never followed through a symlink
- `@rate_limit` honours `config["throttle"]` (`prefix`, `fail_open`)
- Dependencies: floors exclude releases with security advisories (`tornado`, `pyjwt`, `msgpack`, `redis`, `python-dotenv`, `sentry-sdk`, `cryptography`); `sentry-sdk < 3`
- Versioning: the patch counts releases within a minor (0.7.0, 0.7.1, ...) instead of all commits

## v0.6.61 - 2026-10-22

- WebSocket: a failed user lookup is retried on the next message instead of leaving the socket anonymous
- Accept the WebSocket `open()` override under tornado 6.6

## v0.6.59 - 2026-10-14

- Main database can be SQLite (`driver: sqlite`); Redis is needed only by WebSocket routes, cache decorators and `@rate_limit`
- Queue database driver, audit, `api_keys` and `crypto` stay Postgres-only

## v0.6.58 - 2026-09-25

- Sentry: frame locals off; credentials masked at any depth in extras, breadcrumbs and request data
- More `SENSITIVE_KEY_MARKERS`, shared with request-log redaction

## v0.6.57 - 2026-09-23

- Scoped API keys: abilities, IP allow-list, expiry, acting-as user; `api_keys` CLI
- Legacy keys (static `api.key`, models without `abilities`) stay unscoped

## v0.5.56 - 2026-09-07

- **Breaking:** `cryptography`, `bcrypt` and `cronsim` moved to the `crypto` and `cron` extras

## v0.5.55 - 2026-09-07

- Cron service: scheduled `app.py` subcommands with a per-job advisory lock

## v0.5.53 - 2026-08-28

- **Breaking:** the API key is read from `X-API-Key` only; `?api_key=` is gone
- **Breaking:** service input comes from the JSON body; the query string may only name the action
- Request logs redact credentials by key
