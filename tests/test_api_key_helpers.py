import ipaddress

import pytest

from py_app_runner.api_keys.abilities import ability_allows, normalize_ability
from py_app_runner.api_keys.keys import ip_allowed, parse_network, split_key
from py_app_runner.request_handler.client_ip import resolve_client_ip


@pytest.mark.parametrize(
    ("granted", "ability", "expected"),
    [
        (["reports:run"], "reports:run", True),
        (["reports:run"], "reports:delete", False),
        (["reports:*"], "reports:delete", True),
        (["reports:*"], "users:list", False),
        (["*"], "users:list", True),
        ([], "reports:run", False),
        (None, "reports:run", False),
        (["sudat_reports:report"], "sudat-reports:report", True),
    ],
)
def test_ability_allows(granted, ability, expected):
    assert ability_allows(granted, ability) is expected


def test_normalize_ability():
    assert normalize_ability(" sudat-reports:report ") == "sudat_reports:report"
    assert normalize_ability("reports:*") == "reports:*"
    assert normalize_ability("*") == "*"

    for bad in ("", "reports", "reports:", ":run", "rep*:run", "reports:ru*", "a:b:c", "reports:run now"):
        with pytest.raises(ValueError):
            normalize_ability(bad)


PROXY = [ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("172.16.0.0/12")]


@pytest.mark.parametrize(
    ("peer", "forwarded_for", "real_ip", "trusted", "expected"),
    [
        # Headers from a peer that is not a proxy are the client's own claim.
        ("203.0.113.9", "10.1.2.3", "10.1.2.3", PROXY, "203.0.113.9"),
        ("127.0.0.1", "10.1.2.3", None, [], "127.0.0.1"),
        # Rightmost untrusted hop wins; the spoofed leftmost entry is ignored.
        ("127.0.0.1", "1.1.1.1, 10.1.2.3, 172.16.0.5", None, PROXY, "10.1.2.3"),
        # Every hop is a proxy: the leftmost is as far as the chain goes.
        ("127.0.0.1", "172.16.0.7, 172.16.0.5", None, PROXY, "172.16.0.7"),
        ("127.0.0.1", None, "10.1.2.3", PROXY, "10.1.2.3"),
        ("127.0.0.1", None, None, PROXY, "127.0.0.1"),
        ("127.0.0.1", "garbage, 10.1.2.3", None, PROXY, "10.1.2.3"),
        ("127.0.0.1", "10.1.2.3, garbage", None, PROXY, None),
        ("::ffff:203.0.113.9", None, None, PROXY, "203.0.113.9"),
        (None, "10.1.2.3", None, PROXY, None),
    ],
)
def test_resolve_client_ip(peer, forwarded_for, real_ip, trusted, expected):
    assert resolve_client_ip(peer, forwarded_for, real_ip, trusted) == expected


def test_networks():
    assert parse_network("10.0.0.0/8") == "10.0.0.0/8"
    assert parse_network("10.1.2.3") == "10.1.2.3/32"
    with pytest.raises(ValueError):
        parse_network("10.0.0.5/8")

    assert ip_allowed("10.1.2.3", [ipaddress.ip_network("10.0.0.0/8")])
    assert ip_allowed("::ffff:10.1.2.3", ["10.0.0.0/8"])
    assert not ip_allowed("11.1.2.3", ["10.0.0.0/8"])
    assert not ip_allowed(None, ["0.0.0.0/0"])


def test_split_key():
    assert split_key("ak12.sec.ret") == ("ak12", "sec.ret")
    assert split_key("nodot") is None
    assert split_key(".secret") is None
