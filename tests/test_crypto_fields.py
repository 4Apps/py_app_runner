import base64

import pytest

from py_app_runner.crypto.errors import CryptoError
from py_app_runner.crypto.fields import (
    VERSION,
    FieldCrypto,
    generate_key,
    is_encrypted,
    key_id_of,
)

K1 = generate_key()
K2 = generate_key()
INDEX = generate_key()

ENV = {"APP_K1": K1, "APP_K2": K2, "APP_INDEX": INDEX}


def crypto(key_id: str = "k1", keys: dict[str, str] | None = None) -> FieldCrypto:
    return FieldCrypto(
        key_id=key_id,
        keys=keys if keys is not None else {"k1": "APP_K1", "k2": "APP_K2"},
        index_key_var="APP_INDEX",
        resolver=ENV.get,
    )


class TestRoundTrip:
    def test_round_trips_a_value(self):
        c = crypto()
        assert c.decrypt(c.encrypt("hunter2")) == "hunter2"

    def test_round_trips_an_empty_string(self):
        """An empty plaintext is a legal value and must survive, rather than being confused
        with the None/'' passthrough on the way back."""

        c = crypto()
        stored = c.encrypt("")
        assert is_encrypted(stored)
        assert c.decrypt(stored) == ""

    def test_round_trips_non_ascii(self):
        c = crypto()
        assert c.decrypt(c.encrypt("Gints Murāns — ĒČŠ")) == "Gints Murāns — ĒČŠ"

    def test_the_same_input_encrypts_differently_every_time(self):
        """A fresh nonce per encryption. Deterministic ciphertext would leak which rows hold
        the same value, which is the whole reason blind_index is a separate, keyed thing."""

        c = crypto()
        assert c.encrypt("same") != c.encrypt("same")

    def test_the_stored_format_carries_version_and_key_id(self):
        stored = crypto().encrypt("x")
        assert stored.startswith(f"{VERSION}:k1:")
        assert key_id_of(stored) == "k1"


class TestPassthrough:
    def test_none_and_empty_pass_through(self):
        c = crypto()
        assert c.decrypt(None) is None
        assert c.decrypt("") == ""

    def test_plaintext_passes_through_unchanged(self):
        """A column mid-backfill holds both. Treating a plaintext value as an error would
        mean the backfill could never start."""

        c = crypto()
        for value in ("plain", VERSION, f"{VERSION}:", f"{VERSION}:BADKEY:zzz", "nope:k1:zz"):
            assert c.decrypt(value) == value

    def test_is_encrypted_agrees_with_the_passthrough_rule(self):
        assert is_encrypted(crypto().encrypt("x")) is True
        assert is_encrypted("plain") is False
        assert is_encrypted(None) is False
        assert is_encrypted("") is False
        assert key_id_of("plain") is None


class TestTamperingAndKeys:
    def test_an_altered_value_raises_rather_than_returning_garbage(self):
        c = crypto()
        stored = c.encrypt("secret")
        head, key_id, payload = stored.split(":", 2)

        raw = bytearray(base64.b64decode(payload))
        raw[-1] ^= 0x01
        altered = f"{head}:{key_id}:{base64.b64encode(bytes(raw)).decode()}"

        with pytest.raises(CryptoError) as excinfo:
            c.decrypt(altered)
        assert "altered" in str(excinfo.value)

    def test_relabelling_the_key_id_is_rejected(self):
        """The version and key id are bound as associated data, so the label a value carries
        is authenticated rather than merely present. Left outside the envelope they would be
        editable by anyone with write access to the column."""

        c = crypto()
        _, _, payload = c.encrypt("secret").split(":", 2)

        with pytest.raises(CryptoError) as excinfo:
            c.decrypt(f"{VERSION}:k2:{payload}")
        assert "altered" in str(excinfo.value)

    def test_a_truncated_payload_says_so(self):
        c = crypto()
        short = base64.b64encode(b"tooshort").decode()

        with pytest.raises(CryptoError) as excinfo:
            c.decrypt(f"{VERSION}:k1:{short}")
        assert "truncated" in str(excinfo.value)

    def test_a_retired_key_still_decrypts_while_it_stays_listed(self):
        """This is the whole rotation mechanism: decrypt reads the key id off the value, so
        the current key setting is irrelevant to reading old rows."""

        old = crypto(key_id="k2").encrypt("written under k2")
        now_on_k1 = crypto(key_id="k1")

        assert now_on_k1.current_key_id() == "k1"
        assert now_on_k1.decrypt(old) == "written under k2"

    def test_removing_a_key_from_config_says_what_happened(self):
        old = crypto(key_id="k2").encrypt("x")
        without_k2 = crypto(key_id="k1", keys={"k1": "APP_K1"})

        with pytest.raises(CryptoError) as excinfo:
            without_k2.decrypt(old)
        assert "k2" in str(excinfo.value)

    def test_a_value_in_the_other_field_format_is_refused_rather_than_passed_through(self):
        """It uses a different cipher and nonce length, so it cannot be read here. Falling
        through to the plaintext passthrough would hand ciphertext to a user as though it
        were their data."""

        with pytest.raises(CryptoError) as excinfo:
            crypto().decrypt("sp1:k1:c29tZXRoaW5nIGxvbmcgZW5vdWdoIHRvIGxvb2sgcmVhbA==")

        message = str(excinfo.value)
        assert "sp1" in message
        assert "cannot open it" in message


class TestKeyMaterial:
    def test_generate_key_is_32_bytes_of_base64(self):
        assert len(base64.b64decode(generate_key(), validate=True)) == 32

    def test_an_unset_environment_variable_names_itself(self):
        c = FieldCrypto("k1", {"k1": "MISSING_VAR"}, resolver={}.get)

        with pytest.raises(CryptoError) as excinfo:
            c.encrypt("x")
        assert "MISSING_VAR" in str(excinfo.value)

    def test_a_wrong_length_key_is_refused(self):
        c = FieldCrypto("k1", {"k1": "SHORT"}, resolver={"SHORT": base64.b64encode(b"nope").decode()}.get)

        with pytest.raises(CryptoError) as excinfo:
            c.encrypt("x")
        assert "32 bytes" in str(excinfo.value)

    def test_an_unusable_key_id_is_refused(self):
        with pytest.raises(CryptoError) as excinfo:
            crypto(key_id="Not-Valid").encrypt("x")
        assert "not a usable key id" in str(excinfo.value)

    def test_no_current_key_says_which_config_key_is_empty(self):
        with pytest.raises(CryptoError) as excinfo:
            crypto(key_id="").encrypt("x")
        assert 'config["crypto"]["key"]' in str(excinfo.value)


class TestBlindIndex:
    def test_is_deterministic_so_equality_lookups_work(self):
        c = crypto()
        assert c.blind_index("a@b.c") == c.blind_index("a@b.c")

    def test_differs_per_value(self):
        c = crypto()
        assert c.blind_index("a@b.c") != c.blind_index("d@e.f")

    def test_depends_on_its_own_key_not_the_field_key(self):
        """Keyed separately so a dump-holder cannot simply hash candidate values to find
        them, the way they could against a plain sha256."""

        other = FieldCrypto("k1", {"k1": "APP_K1"}, "APP_K2", resolver=ENV.get)
        assert crypto().blind_index("a@b.c") != other.blind_index("a@b.c")

    def test_does_not_normalise_for_you(self):
        """Whether "  Anna " and "anna" should match is a question about the column, not
        about hashing, so the caller answers it."""

        c = crypto()
        assert c.blind_index("Anna") != c.blind_index("anna")

    def test_without_an_index_key_it_says_which_config_key_is_missing(self):
        c = FieldCrypto("k1", {"k1": "APP_K1"}, index_key_var="", resolver=ENV.get)

        with pytest.raises(CryptoError) as excinfo:
            c.blind_index("x")
        assert 'config["crypto"]["index_key"]' in str(excinfo.value)


class TestFromConfig:
    def test_reads_the_documented_config_shape(self):
        c = FieldCrypto.from_config(
            {"crypto": {"key": "k1", "keys": {"k1": "APP_K1"}, "index_key": "APP_INDEX"}},
            resolver=ENV.get,
        )
        assert c.decrypt(c.encrypt("x")) == "x"

    def test_an_absent_crypto_block_builds_but_refuses_to_encrypt(self):
        """Importing the module must not require configuring it - only using it does."""

        c = FieldCrypto.from_config({}, resolver=ENV.get)

        with pytest.raises(CryptoError):
            c.encrypt("x")
