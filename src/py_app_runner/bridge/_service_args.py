import logging
from argparse import ArgumentParser, _SubParsersAction  # type: ignore

from py_app_runner.pybridge import PyBridge


###########################
### Register Subparsers ###
###########################
def reg_subparsers(
    subparsers: "_SubParsersAction[ArgumentParser]",
    pybridge: PyBridge,
    baseLogger: logging.Logger,
) -> None:
    """Command line subparsers"""

    bridge_parser = subparsers.add_parser(
        "bridge",
        description="Bridge api handler service",
        help="Bridge api handler service",
    )
    bridge_parser.add_argument("--address", type=str, default="0.0.0.0", help="Address to bind to")
    bridge_parser.add_argument("--port", type=int, default=4100, help="Port")
    bridge_parser.add_argument("--debug-wbcm", action="store_true", help="Enable debug for WBCM")
