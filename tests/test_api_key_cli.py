import argparse
import logging

import pytest

from py_app_runner.api_keys._service import init_service
from py_app_runner.api_keys._service_args import reg_subparsers
from py_app_runner.registry import AppRegistry
from tests.api_keys_pg import live_bridge

pytestmark = pytest.mark.filterwarnings(
    "ignore:Exception ignored in. <socket.socket:pytest.PytestUnraisableExceptionWarning"
)


async def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    parser = argparse.ArgumentParser()
    reg_subparsers(parser.add_subparsers(dest="service"), None, logging.getLogger("test"))  # type: ignore[arg-type]
    args = parser.parse_args(["api_keys", *argv])

    with pytest.raises(SystemExit) as excinfo:
        await init_service(args, None, logging.getLogger("test"))  # type: ignore[arg-type]

    return int(excinfo.value.code or 0), capsys.readouterr().out


async def test_create_use_update_revoke_round_trip(capsys, tmp_path):
    async with live_bridge("par_test_key_cli") as bridge:
        code, out = await run_cli(["install", "--dir", str(tmp_path)], capsys)
        assert code == 0
        assert "CREATE TABLE api_keys" in next(tmp_path.glob("*-create-api-keys.sql")).read_text()

        user_id, _ = await bridge.add_user()
        code, out = await run_cli(
            ["create", "--name", "manslauks", "--ability", "reports:run", "--expires", "2099-12-31"]
            + ["--allowed-ip", "127.0.0.1", "--user-id", str(user_id)],
            capsys,
        )
        assert code == 0, out
        key = out.strip().splitlines()[-1]
        prefix, secret = key.split(".", 1)

        status, body = await bridge.call("reports", "run", key)
        assert status == 200
        assert body["data"]["user_id"] == user_id
        assert (await bridge.call("reports", "delete", key))[0] == 403

        code, out = await run_cli(["list"], capsys)
        assert code == 0
        assert prefix in out and "manslauks" in out
        assert secret not in out

        code, out = await run_cli(["update", prefix, "--add-ability", "reports:delete", "--no-user"], capsys)
        assert code == 0, out
        status, body = await bridge.call("reports", "delete", key)
        assert status == 200
        assert body["data"]["user_id"] is None

        code, out = await run_cli(["revoke", key], capsys)
        assert code == 0, out
        assert (await bridge.call("reports", "run", key))[0] == 401

        assert (await run_cli(["revoke", "999"], capsys))[0] == 1


async def test_bad_input_is_refused_before_touching_the_database(capsys):
    saved = AppRegistry.config()
    # Unreachable on purpose: a refusal that tried to connect would exit 1, not 2.
    config = {
        "api_key_pepper": "p",
        "db": {"main": {"hostname": "invalid.invalid", "username": "x", "password": "x", "database": "x"}},
    }
    cases = [
        ["create", "--name", "x", "--ability", "reports"],
        ["create", "--name", "x", "--ability", "a:b", "--allowed-ip", "10.0.0.5/8"],
        ["create", "--name", "x", "--ability", "a:b", "--expires", "2001-01-01"],
        ["update", "1"],
    ]
    try:
        AppRegistry.configure(config=config, users_model=object)
        for argv in cases:
            assert (await run_cli(argv, capsys))[0] == 2, argv

        AppRegistry.configure(config={**config, "api_key_pepper": ""}, users_model=object)
        code, out = await run_cli(["create", "--name", "x", "--ability", "a:b"], capsys)
        assert code == 2
        assert "api_key_pepper" in out
    finally:
        AppRegistry.configure(config=saved, users_model=object)
