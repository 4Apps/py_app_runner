"""Field encryption: encrypt a column the application has to read back.

Explicit at every call site. Nothing hooks into the database layer, so a value cannot be
encrypted twice or written in the clear because a hook did not fire - the two failure modes
that make transparent column encryption so unpleasant to debug.

Stored values look like:

    pa1:<key_id>:<base64 of nonce(12) || ciphertext || tag(16)>

carrying a version and a key id, which is what lets a retired key keep decrypting old rows,
lets a column hold plaintext and ciphertext while a backfill runs, and lets `crypto rotate`
tell what still needs rewriting.
"""

import base64
import hashlib
import hmac
import os
import re
from collections.abc import Callable
from typing import Any

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as e:
    raise ImportError("py_app_runner.crypto requires the 'crypto' extra: pip install 'py_app_runner[crypto]'") from e

from py_app_runner.crypto.errors import CryptoError

VERSION = "pa1"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16

# Another field-encryption format in use across the estate, on a different cipher with a
# 24-byte nonce. Recognised only so that a value in it is refused with an explanation rather
# than passed through as though it were plaintext, which is what a column mid-backfill would
# otherwise make it look like.
_FOREIGN_VERSION = "sp1"

# Lowercase, no dash, no dot, and above all no colon: the id sits next to the ":"
# separators in the stored value, so a colon in it would make the value unsplittable.
# Constrained rather than escaped, for the same reason a table name is whitelisted rather
# than quoted.
_KEY_ID_RE = re.compile(r"^[a-z0-9_]{1,16}$")

# The blind index key is cached under a reserved id that the pattern above cannot match,
# so it can never collide with a real key id.
_INDEX_CACHE_ID = "@index"

Resolver = Callable[[str], str | None]


class FieldCrypto:
    """Holds the configured keys. Build one per process; it caches decoded key material.

    An instance rather than a module of functions, so the decoded-key cache has an obvious
    lifetime: it is dropped by rebuilding the object. A module-level cache would need an
    explicit invalidation call that nothing would remember to make after a key change.
    `AppRegistry` already owns the process-global config this is built from.
    """

    def __init__(
        self,
        key_id: str,
        keys: dict[str, str],
        index_key_var: str = "",
        resolver: Resolver | None = None,
    ) -> None:
        self.key_id = key_id
        self.keys = keys
        self.index_key_var = index_key_var
        # Indirection so a test - or a deployment reading from a secrets manager rather than
        # the environment - can supply key material without setting real environment
        # variables in the process.
        self.resolver: Resolver = resolver if resolver is not None else os.environ.get
        self._cache: dict[str, bytes] = {}

    @classmethod
    def from_config(cls, config: dict[str, Any], resolver: Resolver | None = None) -> "FieldCrypto":
        settings = config.get("crypto") or {}
        raw_keys = settings.get("keys") or {}
        if not isinstance(raw_keys, dict):
            raise CryptoError('config["crypto"]["keys"] must be a mapping of key id -> env var name.')

        return cls(
            key_id=settings.get("key") or "",
            keys={str(k): str(v) for k, v in raw_keys.items()},
            index_key_var=settings.get("index_key") or "",
            resolver=resolver,
        )

    ####################
    ### Key material ###
    ####################

    def current_key_id(self) -> str:
        if not self.key_id:
            raise CryptoError(
                'config["crypto"]["key"] does not name a key to encrypt with. '
                "Generate one with: python3 src/app.py crypto key"
            )

        return self._assert_key_id(self.key_id)

    def _assert_key_id(self, key_id: str) -> str:
        if _KEY_ID_RE.match(key_id) is None:
            raise CryptoError(f"{key_id!r} is not a usable key id. Use up to 16 of a-z, 0-9 and underscore.")

        return key_id

    def _key(self, key_id: str) -> bytes:
        self._assert_key_id(key_id)

        cached = self._cache.get(key_id)
        if cached is not None:
            return cached

        variable = self.keys.get(key_id)
        if not variable:
            raise CryptoError(
                f'No key {key_id!r} in config["crypto"]["keys"]. A value encrypted under a key id '
                f"can only be read while that id is still listed, so removing one is permanent."
            )

        material = self._material(variable, f"key {key_id!r}")
        self._cache[key_id] = material
        return material

    def _index_key(self) -> bytes:
        cached = self._cache.get(_INDEX_CACHE_ID)
        if cached is not None:
            return cached

        if not self.index_key_var:
            raise CryptoError(
                'config["crypto"]["index_key"] is not set, so blind_index() has no key. '
                "Generate one with: python3 src/app.py crypto key"
            )

        material = self._material(self.index_key_var, "the blind index key")
        self._cache[_INDEX_CACHE_ID] = material
        return material

    def _material(self, variable: str, label: str) -> bytes:
        value = self.resolver(variable)
        if not value:
            raise CryptoError(
                f"The environment variable {variable}, holding {label}, is not set. "
                f"Generate one with: python3 src/app.py crypto key"
            )

        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as e:
            raise CryptoError(
                f"{variable}, holding {label}, must be {KEY_BYTES} bytes of base64. "
                f"Generate one with: python3 src/app.py crypto key"
            ) from e

        if len(raw) != KEY_BYTES:
            raise CryptoError(
                f"{variable}, holding {label}, must be {KEY_BYTES} bytes of base64, "
                f"and decoded to {len(raw)}. "
                f"Generate one with: python3 src/app.py crypto key"
            )

        return raw

    ###################
    ### Encrypt/dec ###
    ###################

    def encrypt(self, plaintext: str, key_id: str | None = None) -> str:
        """Encrypt under the current key, or under `key_id` when rotating onto a new one."""

        chosen = key_id if key_id else self.current_key_id()
        key = self._key(chosen)

        nonce = os.urandom(NONCE_BYTES)
        # The version and key id are bound as associated data, so the label a value carries
        # is authenticated rather than merely present. Without this an attacker with write
        # access to the column could relabel a value's key id; that only ever degrades to a
        # failed decrypt rather than a forgery, but binding it costs nothing.
        body = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), self._aad(chosen))

        return f"{VERSION}:{chosen}:{base64.b64encode(nonce + body).decode('ascii')}"

    def decrypt(self, value: str | None) -> str | None:
        """Decrypt a stored value, passing anything that is not one straight through.

        A column being backfilled holds both plaintext and ciphertext at once, so a value
        that does not carry the format is returned verbatim rather than treated as an error.
        A value that *does* carry it and still will not open is always an error.
        """

        if value is None or value == "":
            return value

        parts = split(value)
        if parts is None:
            if value.startswith(f"{_FOREIGN_VERSION}:"):
                raise CryptoError(
                    f"This value is in the {_FOREIGN_VERSION!r} field-encryption format, which uses "
                    f"a different cipher and nonce length. This module reads {VERSION!r} "
                    f"(AES-256-GCM) and cannot open it."
                )

            return value

        key_id, payload = parts

        try:
            raw = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError) as e:
            raise CryptoError(f"Encrypted value under key {key_id!r} is not base64") from e

        if len(raw) < NONCE_BYTES + TAG_BYTES:
            raise CryptoError(
                f"Encrypted value under key {key_id!r} is truncated: {len(raw)} bytes, and the "
                f"nonce and tag alone need {NONCE_BYTES + TAG_BYTES}."
            )

        nonce = raw[:NONCE_BYTES]
        body = raw[NONCE_BYTES:]

        try:
            plaintext = AESGCM(self._key(key_id)).decrypt(nonce, body, self._aad(key_id))
        except InvalidTag as e:
            # Wrong key or an edited value, and AES-GCM cannot say which. Both mean the same
            # thing to the caller: do not trust this row.
            raise CryptoError(
                f"Could not decrypt a value under key {key_id!r}: wrong key, or the value has been altered"
            ) from e

        return plaintext.decode("utf-8")

    def _aad(self, key_id: str) -> bytes:
        return f"{VERSION}:{key_id}".encode("ascii")

    ###################
    ### Blind index ###
    ###################

    def blind_index(self, value: str) -> str:
        """A keyed, deterministic index that restores equality lookups on an encrypted column.

        Store it in a second column and query that. Ranges and LIKE do not come back, and
        equality across rows becomes visible to anyone holding the table - that is inherent
        to a deterministic index, and the separate key is what stops a dump-holder simply
        hashing candidate values to find them.

        Nothing is normalised here. Whether "  Anna " and "anna" should match is a question
        about the column, not about hashing, so the caller answers it.
        """

        return hmac.new(self._index_key(), value.encode("utf-8"), hashlib.sha256).hexdigest()


#########################
### Format inspection ###
#########################


def split(value: str) -> tuple[str, str] | None:
    """Pull the key id and payload out of a stored value, or None when it is not one."""

    if not value.startswith(f"{VERSION}:"):
        return None

    parts = value.split(":", 2)
    if len(parts) != 3 or parts[1] == "" or parts[2] == "":
        return None

    if _KEY_ID_RE.match(parts[1]) is None:
        return None

    return (parts[1], parts[2])


def is_encrypted(value: str | None) -> bool:
    return value is not None and value != "" and split(value) is not None


def key_id_of(value: str | None) -> str | None:
    """Which key a stored value is under, without needing that key to be configured.

    This is what lets `crypto rotate` report on values it cannot decrypt.
    """

    if value is None or value == "":
        return None

    parts = split(value)
    return None if parts is None else parts[0]


def generate_key() -> str:
    """Fresh key material, base64 encoded, for an environment variable."""

    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")
