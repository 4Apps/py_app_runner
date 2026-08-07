"""The binary envelope shared with end-to-end encrypted clients.

    magic(3) | version(1) | nonce(12) | ciphertext(n) | tag(16)

This is the format `copasty-server`'s clients already write, where it exists only as
JavaScript in the share viewer. Having no Python side to it is why the server currently
validates an uploaded envelope by its length alone and would store arbitrary bytes just as
happily as a real one.

The magic and version are parameters rather than constants: `CPE` belongs to Copasty, and
a second application gets its own. Nothing here needs the key - `validate()` is the call a
server that will never hold one still wants, so that a blob which cannot possibly decrypt
is refused at upload rather than at read.
"""

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from py_app_runner.crypto.errors import CryptoError

MAGIC_BYTES = 3
NONCE_BYTES = 12
TAG_BYTES = 16

# magic + version + nonce + tag, i.e. an envelope around an empty plaintext. Anything
# shorter cannot be one whatever it contains.
MIN_ENVELOPE_BYTES = MAGIC_BYTES + 1 + NONCE_BYTES + TAG_BYTES


@dataclass(frozen=True)
class Envelope:
    magic: bytes
    version: int
    nonce: bytes
    # Ciphertext with the AEAD tag still appended. Kept joined because that is what both
    # WebCrypto's decrypt and `cryptography`'s AESGCM.decrypt expect to be handed, so
    # splitting it here would only mean rejoining it at both call sites.
    body: bytes


def b64url_decode(value: str) -> bytes:
    """Decode the base64url alphabet, restoring padding first.

    Keys travel to a browser in the URL fragment, which rules out `+` and `/`, and the
    padding is routinely dropped on the way. `base64.urlsafe_b64decode` rejects a string
    whose length is not a multiple of four rather than assuming the padding, so it has to
    be put back before decoding.
    """

    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError) as e:
        raise CryptoError(f"Value is not valid base64url: {e}") from e


def b64url_encode(raw: bytes) -> str:
    """Encode to base64url without padding, which is what a URL fragment wants."""

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse(blob: bytes, magic: bytes = b"CPE", version: int = 1) -> Envelope:
    """Read an envelope, or raise explaining which part of it is wrong."""

    if len(blob) < MIN_ENVELOPE_BYTES:
        raise CryptoError(f"Envelope is {len(blob)} bytes, and the header alone needs {MIN_ENVELOPE_BYTES}.")

    if len(magic) != MAGIC_BYTES:
        raise CryptoError(f"An envelope magic is {MAGIC_BYTES} bytes; {magic!r} is {len(magic)}.")

    found_magic = blob[:MAGIC_BYTES]
    if found_magic != magic:
        raise CryptoError(f"Envelope starts with {found_magic!r}, not {magic!r}.")

    found_version = blob[MAGIC_BYTES]
    if found_version != version:
        raise CryptoError(
            f"Envelope is version {found_version}, and this build reads version {version}. "
            f"It was written by a newer client."
        )

    return Envelope(
        magic=found_magic,
        version=found_version,
        nonce=blob[MAGIC_BYTES + 1 : MAGIC_BYTES + 1 + NONCE_BYTES],
        body=blob[MAGIC_BYTES + 1 + NONCE_BYTES :],
    )


def validate(blob: bytes, magic: bytes = b"CPE", version: int = 1) -> None:
    """Refuse a blob that cannot be an envelope, without needing the key.

    This is the check a server storing opaque ciphertext can still make. It says nothing
    about whether the payload decrypts - only the holder of the key can know that - but it
    does stop a client uploading arbitrary bytes into a column the whole system treats as
    an envelope.
    """

    parse(blob, magic, version)


def pack(nonce: bytes, body: bytes, magic: bytes = b"CPE", version: int = 1) -> bytes:
    if len(nonce) != NONCE_BYTES:
        raise CryptoError(f"An envelope nonce is {NONCE_BYTES} bytes; got {len(nonce)}.")

    if len(magic) != MAGIC_BYTES:
        raise CryptoError(f"An envelope magic is {MAGIC_BYTES} bytes; {magic!r} is {len(magic)}.")

    if not 0 <= version <= 255:
        raise CryptoError(f"An envelope version is a single byte; got {version}.")

    return magic + bytes([version]) + nonce + body


def encrypt(key: bytes, plaintext: bytes, magic: bytes = b"CPE", version: int = 1) -> bytes:
    """Build an envelope. Only for the case where the server legitimately holds the key.

    An end-to-end encrypted application never calls this: the point there is that the
    server cannot. It exists for the other case - a server-side blob that happens to use
    the same wire format, so a browser can be handed the key later and open it.
    """

    if len(key) != 32:
        raise CryptoError(f"An envelope key is 32 bytes (AES-256); got {len(key)}.")

    nonce = os.urandom(NONCE_BYTES)
    body = AESGCM(key).encrypt(nonce, plaintext, None)
    return pack(nonce, body, magic, version)


def decrypt(key: bytes, blob: bytes, magic: bytes = b"CPE", version: int = 1) -> bytes:
    if len(key) != 32:
        raise CryptoError(f"An envelope key is 32 bytes (AES-256); got {len(key)}.")

    parsed = parse(blob, magic, version)
    try:
        return AESGCM(key).decrypt(parsed.nonce, parsed.body, None)
    except InvalidTag as e:
        raise CryptoError("Could not decrypt an envelope: wrong key, or the value has been altered.") from e
