"""Key material and network checks.

A key is `<prefix>.<secret>`. The prefix is stored in clear and finds the row; only a
peppered sha256 of the secret is stored. Prefixes always start with a letter, so the CLI
can tell a prefix from a numeric id.
"""

import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

PREFIX_MARKER = "ak"


def generate_key() -> tuple[str, str]:
    """A fresh (prefix, secret). The secret alphabet is URL-safe base64, so no '.'."""

    return f"{PREFIX_MARKER}{secrets.token_hex(6)}", secrets.token_urlsafe(32)


def hash_secret(secret: str, pepper: str) -> str:
    h = hashlib.sha256()
    h.update(pepper.encode())
    h.update(secret.encode())
    return h.hexdigest()


def secret_matches(secret: str, pepper: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret, pepper), stored_hash)


def split_key(key: str) -> tuple[str, str] | None:
    prefix, sep, secret = key.partition(".")
    if not sep or not prefix or not secret:
        return None

    return prefix, secret


def parse_network(raw: str) -> str:
    """Canonical CIDR text for a `--allowed-ip` value; a bare address becomes a /32 or /128.

    Strict: `10.0.0.5/8` is refused rather than widened to `10.0.0.0/8`, since Postgres
    refuses it too and a silently widened allow-list is the worse of the two outcomes.
    """

    try:
        return str(ipaddress.ip_network(raw.strip(), strict=True))
    except ValueError as e:
        raise ValueError(f"allowed IP {raw!r}: {e}") from None


def ip_allowed(ip: str | None, networks: Iterable[Any]) -> bool:
    """Whether `ip` falls in any of `networks` (strings or ipaddress networks, as psycopg
    returns cidr[]). An unknown client address matches nothing."""

    if not ip:
        return False

    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False

    # A dual-stack listener reports IPv4 clients as ::ffff:a.b.c.d.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped

    for network in networks:
        try:
            if address in ipaddress.ip_network(str(network), strict=False):
                return True
        except ValueError:
            continue

    return False


def expired(expires_at: datetime | None, now: datetime | None = None) -> bool:
    if expires_at is None:
        return False

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    return expires_at <= (now or datetime.now(UTC))
