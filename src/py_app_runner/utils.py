import asyncio
import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import string
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from functools import partial
from typing import Any, ParamSpec, TypeGuard, TypeVar

from msgspec import json as mjson

P = ParamSpec("P")
R = TypeVar("R")

BASE64_RX = re.compile(r"^[A-Za-z0-9+/=\s]+$")


def generate_random_string(length: int = 32) -> str:
    """Generate a random string of fixed length"""

    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))


############
### Json ###
############
def json_encoder(obj: Any) -> str | float | None:
    if isinstance(obj, Decimal):
        return float(obj)

    if isinstance(obj, dt.datetime):
        # * NOTE: Could also do this:
        # * return obj.astimezone(dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
        return obj.strftime("%Y-%m-%dT%H:%M:%S")

    if isinstance(obj, dt.date):
        return obj.strftime("%Y-%m-%d")

    if isinstance(obj, Enum):
        return obj.value

    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None  # "NaN" or "Infinity"
        return obj

    if isinstance(obj, (int, str)):
        return obj

    return str(obj)


class CustomJSONEncoder(json.JSONEncoder):
    """Stdlib JSONEncoder subclass that delegates to json_encoder for non-standard types."""

    def default(self, o: Any) -> Any:
        result = json_encoder(o)
        if result is not None:
            return result
        return super().default(o)


ms_encoder = mjson.Encoder(enc_hook=json_encoder)
ms_decoder = mjson.Decoder()


def json_encode_bytes(value: Any, pretty: bool = False) -> bytes:
    json_bytes = ms_encoder.encode(value)

    if not pretty:
        return json_bytes

    return mjson.format(json_bytes, indent=2)


def json_encode(value: Any, pretty: bool = False) -> str:
    json_bytes = ms_encoder.encode(value)

    if not pretty:
        return json_bytes.decode("utf-8")

    pretty_bytes = mjson.format(json_bytes, indent=2)
    pretty_text = pretty_bytes.decode("utf-8")
    return pretty_text


def json_decode(value: bytes | str) -> Any:
    return ms_decoder.decode(value)


###########
### CPU ###
###########
def workers_auto() -> int:
    # honors CPU affinity in containers
    if hasattr(os, "sched_getaffinity"):
        return max(1, len(os.sched_getaffinity(0)))
    return max(1, os.cpu_count() or 1)


#############
### Other ###
#############
# Ignore exception decorator
def ignore_exception(IgnoreException: type[Exception]) -> Any:
    """Decorator for ignoring exception from a function
    e.g.   @ignore_exception(DivideByZero)
    e.g.2. ignore_exception(DivideByZero)(Divide)(2/0)
    """

    def dec(function: Callable[..., Any]) -> Callable[..., Any]:
        def _dec(*args: tuple[Any] | None, **kwargs: Any | None) -> Any:
            if len(args) > 1:
                newArgs, defaultValue = args[:-1], args[-1]
            else:
                newArgs, defaultValue = args, None

            try:
                return function(*newArgs, **kwargs)
            except IgnoreException:
                return defaultValue

        return _dec

    return dec


# Ignore exception if float conversion fails
sfloat = ignore_exception(ValueError)(float)
sint = ignore_exception(ValueError)(int)


def is_dict(data: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(data, dict)


def is_list(data: Any) -> TypeGuard[list[Any]]:
    return isinstance(data, list)


def pretty_print(data: Any) -> None:
    """Print a dictionary in a pretty way"""

    if is_dict(data):
        for key, value in data.items():
            print(f"{key}: {value}")

    elif is_list(data):
        for item in data:
            pretty_print(item)

    else:
        print(data)


# Fix floats
def fix_float(string_value: str, default_value: float | None = None) -> float:
    return sfloat(string_value.replace(" ", "").replace(",", ".").strip(), default_value)


def replace_strings(
    text: str,
    replace_strings: list[tuple[str, str]] | None = None,
) -> str:
    """Replace strings in a text"""

    if replace_strings is not None:
        text = text.strip(' "\r\n')  # Remove any extra spaces and quotes

        for old, new in replace_strings:
            pattern = re.compile(re.escape(old), flags=re.IGNORECASE)
            text = pattern.sub(lambda match, new=new: new, text)

    text = text.strip(' "\r\n')  # Remove any extra spaces and quotes

    return text


def create_valid_hostname(input_string: str) -> str:
    # Replace spaces with hyphens
    input_string = input_string.replace(" ", "-")
    input_string = input_string.replace("_", "-")
    input_string = input_string.replace(".", "-")

    # Remove characters that are not letters, digits, or hyphens
    input_string = re.sub(r"[^a-zA-Z0-9-]+", "", input_string)

    # Ensure that the first and last characters are not hyphens
    if input_string[0] == "-":
        input_string = input_string[1:]
    if input_string[-1] == "-":
        input_string = input_string[:-1]

    # Return the valid hostname
    return input_string


def round_decimal(
    value: Decimal,
    digits: int = 0,
    rounding: str = ROUND_HALF_UP,
) -> Decimal:
    """
    Rounds a Decimal to the specified number of digits.

    Args:
        value (Decimal): The Decimal to be rounded.
        digits (int, optional): The number of decimal digits to round to (default is 0).
        rounding (str, optional): The rounding method (default is ROUND_HALF_UP).

    Returns:
        Decimal: The rounded Decimal.
    """
    # Calculate the multiplier based on the number of digits
    multiplier = Decimal("10") ** (-digits)

    # Round the value using quantize with the specified rounding mode
    return value.quantize(multiplier, rounding=rounding)


def maybe_truncate(msg: str, maxLen: int = 600) -> tuple[str, str]:
    """
    Functions that truncates message if it is too long.

    Always returns tuple, that can be used in logger.debug function call
    as argument. Tuple contains (maybe truncated message, ellipsis)
    """
    msg_trun = msg[:maxLen]
    ellipsis = ""
    if len(msg) > maxLen:
        ellipsis = "\n...truncated..."

    return (msg_trun, ellipsis)


def safe_decode(raw: Any) -> str:
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return raw
    return repr(raw)


def truncate_middle(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    head = max_len // 2
    tail = max_len - head - 1
    return f"{s[:head]}…{s[-tail:]}"


def summarize_maybe_base64(s: str, preview: int = 32, hard_cap: int = 256) -> str:
    """
    Heuristic summary for very long strings. If it looks like base64 (charset + len%4==0),
    show head/tail and approximate decoded size WITHOUT decoding. Otherwise, truncate.
    """
    compact = "".join(s.split())
    if compact and len(compact) % 4 == 0 and BASE64_RX.match(compact):
        approx = (len(compact) * 3) // 4 - compact.count("=")
        head = compact[:preview]
        tail = compact[-preview:] if len(compact) > 2 * preview else ""
        return f"<base64 {len(compact)} chars (~{approx} bytes) {head}…{tail}>"
    return truncate_middle(s, hard_cap)


async def run_blocking(fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run a blocking callable in the default executor without blocking the loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


def redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for k, v in (headers or {}).items():
        kl = str(k).lower()
        if kl in ("authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"):
            redacted[k] = "<redacted>"
        else:
            redacted[k] = v
    return redacted


def sha1_prefix(b: bytes, n: int = 12) -> str:
    return hashlib.sha1(b).hexdigest()[:n]


def sha256_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def shrink_json(obj: Any, *, max_string: int, max_items: int) -> Any:
    """
    Traverse JSON-like structures and:
      - truncate any string longer than max_string (base64-aware summary),
      - cap lists to max_items with a "… N more items" marker,
      - leave numbers/bools/None untouched.
    No key-based assumptions.
    """
    if isinstance(obj, dict):
        return {k: shrink_json(v, max_string=max_string, max_items=max_items) for k, v in obj.items()}
    if isinstance(obj, list):
        trimmed = [shrink_json(x, max_string=max_string, max_items=max_items) for x in obj[:max_items]]
        if len(obj) > max_items:
            trimmed.append(f"<… {len(obj) - max_items} more items>")
        return trimmed
    if isinstance(obj, str):
        return summarize_maybe_base64(obj, hard_cap=max_string)
    return obj


def format_body_for_log(
    raw_body: Any,
    *,
    max_chars: int,
    shrink_strings_to: int,
    cap_list_items: int,
) -> str:
    text = safe_decode(raw_body)
    try:
        js = json_decode(text)
        js = shrink_json(js, max_string=shrink_strings_to, max_items=cap_list_items)
        return truncate_middle(json_encode(js, pretty=True), max_chars)
    except Exception:
        return truncate_middle(summarize_maybe_base64(text, hard_cap=shrink_strings_to), max_chars)


def format_response_for_log(
    response: Any,
    *,
    max_chars: int,
    shrink_strings_to: int,
    cap_list_items: int,
) -> str:
    if isinstance(response, (dict, list)):
        js = shrink_json(response, max_string=shrink_strings_to, max_items=cap_list_items)
        return truncate_middle(json_encode(js, pretty=True), max_chars)
    return truncate_middle(safe_decode(response), max_chars)
