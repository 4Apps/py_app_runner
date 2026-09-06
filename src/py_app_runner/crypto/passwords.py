"""Password hashing.

Thin on purpose. bcrypt already embeds its salt and cost in the `$2b$` string it returns,
so a schema needs one text column and nothing else - a separate `salt` column stores a
second copy of something the hash already carries, and has to be kept in step with it for
no benefit.
"""

try:
    import bcrypt
except ImportError as e:
    raise ImportError("py_app_runner.crypto requires the 'crypto' extra: pip install 'py_app_runner[crypto]'") from e

from py_app_runner.crypto.errors import CryptoError

DEFAULT_ROUNDS = 12

# bcrypt truncates at 72 bytes and, depending on the build, either ignores the rest
# silently or raises. Neither is a good outcome for a user whose passphrase is long, so the
# limit is checked here where the message can say what happened.
MAX_PASSWORD_BYTES = 72


class PasswordHasher:
    def __init__(self, rounds: int = DEFAULT_ROUNDS) -> None:
        self.rounds = rounds

    def hash(self, password: str) -> str:
        return bcrypt.hashpw(self._encode(password), bcrypt.gensalt(self.rounds)).decode("ascii")

    def verify(self, password: str, stored: str) -> bool:
        """False for a wrong password *and* for a malformed hash.

        A stored value that is not a bcrypt hash at all - a truncated column, a migration
        that put something else there - must not raise out of a login handler, because the
        answer to "may this person in" is still no.
        """

        try:
            return bcrypt.checkpw(self._encode(password), stored.encode("utf-8"))
        except (ValueError, TypeError):
            return False

    def needs_rehash(self, stored: str) -> bool:
        """True when a stored hash was made with fewer rounds than are configured now.

        Call it after a successful verify: that is the one moment the plaintext is in hand
        and the hash can be upgraded without asking the user for anything.
        """

        try:
            cost = int(stored.split("$")[2])
        except (IndexError, ValueError):
            # Unparseable means it is not a bcrypt hash this class wrote, so replacing it is
            # exactly what should happen next time there is a plaintext to do it with.
            return True

        return cost < self.rounds

    def _encode(self, password: str) -> bytes:
        raw = password.encode("utf-8")
        if len(raw) > MAX_PASSWORD_BYTES:
            raise CryptoError(
                f"bcrypt hashes at most {MAX_PASSWORD_BYTES} bytes and this password is "
                f"{len(raw)}. Everything past that is ignored, so accepting it would mean two "
                f"different passwords unlocking the same account."
            )

        return raw
