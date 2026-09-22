"""The address a request really came from.

The bridge's HTTPServer runs with `xheaders=True`, so Tornado's `request.remote_ip` is
whatever `X-Forwarded-For` / `X-Real-IP` said, from anyone. Anything that grants access by
address starts from the socket peer instead and believes forwarding headers only when that
peer is a configured proxy.
"""

import ipaddress
import logging
import os

from py_app_runner.registry import AppRegistry

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

logger = logging.getLogger(__name__)


def trusted_proxies() -> list[Network]:
    """`config["trusted_proxies"]` (list or comma separated), falling back to the
    `TRUSTED_PROXIES` env var. Empty means forwarding headers are never believed."""

    configured = AppRegistry.config().get("trusted_proxies")
    if configured is None:
        configured = os.environ.get("TRUSTED_PROXIES", "")

    if isinstance(configured, str):
        configured = configured.split(",")

    networks: list[Network] = []
    for item in configured:
        text = str(item).strip()
        if not text:
            continue

        try:
            networks.append(ipaddress.ip_network(text, strict=True))
        except ValueError as e:
            logger.error("Ignoring trusted_proxies entry %r: %s", text, e)

    return networks


def _parse(raw: str | None) -> IPAddress | None:
    if not raw:
        return None

    try:
        address = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped

    return address


def _is_trusted(address: IPAddress, trusted: list[Network]) -> bool:
    return any(address in network for network in trusted)


def resolve_client_ip(
    peer: str | None,
    forwarded_for: str | None,
    real_ip: str | None,
    trusted: list[Network],
) -> str | None:
    """Client address, or None when it cannot be established.

    Walks `X-Forwarded-For` from the right, skipping trusted proxies: every hop appends
    the address it received from, so only the entries our own proxies wrote are reliable,
    and the first untrusted one is the client. Anything left of it is client supplied.
    """

    peer_address = _parse(peer)
    if peer_address is None:
        return None

    if not _is_trusted(peer_address, trusted):
        return str(peer_address)

    if forwarded_for:
        hop_address: IPAddress | None = None
        for hop in reversed(forwarded_for.split(",")):
            hop_address = _parse(hop)
            if hop_address is None:
                return None
            if not _is_trusted(hop_address, trusted):
                return str(hop_address)

        return str(hop_address) if hop_address else None

    if real_ip:
        real_address = _parse(real_ip)
        return str(real_address) if real_address else None

    return str(peer_address)
