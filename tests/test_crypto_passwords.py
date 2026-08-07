import pytest

from py_app_runner.crypto.errors import CryptoError
from py_app_runner.crypto.passwords import PasswordHasher

# bcrypt cost is exponential and the default of 12 is ~250ms per call. These tests care
# about the wrapper's behaviour, not about how slow bcrypt is, so they use the floor.
hasher = PasswordHasher(rounds=4)


class TestHashing:
    def test_verifies_the_right_password(self):
        assert hasher.verify("hunter2", hasher.hash("hunter2")) is True

    def test_rejects_the_wrong_password(self):
        assert hasher.verify("wrong", hasher.hash("hunter2")) is False

    def test_the_same_password_hashes_differently_every_time(self):
        assert hasher.hash("hunter2") != hasher.hash("hunter2")

    def test_the_hash_carries_its_own_salt(self):
        """Which is why a schema needs one column, not two. copasty-server stores a second
        copy in a `salt` column that has to be kept in step for no benefit."""

        assert hasher.hash("hunter2").startswith("$2b$")

    def test_handles_non_ascii(self):
        assert hasher.verify("pārbaude", hasher.hash("pārbaude")) is True


class TestMalformedInput:
    def test_a_stored_value_that_is_not_a_hash_returns_false_rather_than_raising(self):
        """A truncated column or a migration that put something else there must not raise
        out of a login handler - the answer to "may this person in" is still no."""

        assert hasher.verify("hunter2", "") is False
        assert hasher.verify("hunter2", "not a hash") is False
        assert hasher.verify("hunter2", "$2b$truncated") is False

    def test_a_password_past_bcrypts_limit_is_refused_loudly(self):
        """bcrypt silently ignores everything past 72 bytes, so accepting a longer one means
        two different passwords unlock the same account."""

        with pytest.raises(CryptoError) as excinfo:
            hasher.hash("x" * 73)
        assert "72 bytes" in str(excinfo.value)


class TestNeedsRehash:
    def test_false_for_a_hash_at_the_configured_cost(self):
        assert hasher.needs_rehash(hasher.hash("hunter2")) is False

    def test_true_when_the_configured_cost_has_risen(self):
        """Call it after a successful verify: that is the one moment the plaintext is in
        hand and the hash can be upgraded without asking the user for anything."""

        old = PasswordHasher(rounds=4).hash("hunter2")
        assert PasswordHasher(rounds=6).needs_rehash(old) is True

    def test_true_for_something_that_is_not_a_bcrypt_hash(self):
        assert hasher.needs_rehash("legacy-md5-thing") is True
