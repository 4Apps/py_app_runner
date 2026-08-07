import base64
import os

import pytest

from py_app_runner.crypto import envelope
from py_app_runner.crypto.errors import CryptoError


def a_copasty_envelope(plaintext: bytes = b"hello") -> tuple[bytes, bytes]:
    key = os.urandom(32)
    return (key, envelope.encrypt(key, plaintext))


class TestFormat:
    def test_the_layout_matches_what_the_share_viewer_reads(self):
        """magic(3) | version(1) | nonce(12) | ciphertext | tag(16). The JavaScript in
        copasty-server's viewer.html slices at exactly these offsets, so a Python writer
        that disagrees produces blobs no client can open."""

        _, blob = a_copasty_envelope(b"hello")

        assert blob[0:3] == b"CPE"
        assert blob[3] == 1
        assert len(blob) == 3 + 1 + 12 + len(b"hello") + 16

    def test_round_trips(self):
        key, blob = a_copasty_envelope(b"some clipboard text")
        assert envelope.decrypt(key, blob) == b"some clipboard text"

    def test_parse_exposes_the_body_with_the_tag_still_attached(self):
        """Both WebCrypto and cryptography's AESGCM want the tag appended to the ciphertext,
        so splitting it here would only mean rejoining it at both call sites."""

        _, blob = a_copasty_envelope(b"x")
        parsed = envelope.parse(blob)

        assert len(parsed.nonce) == 12
        assert len(parsed.body) == len(b"x") + 16

    def test_pack_and_parse_are_inverse(self):
        # A body is never shorter than the 16-byte tag, so the sample has to be at least
        # that or parse rejects it on the minimum-length check - correctly.
        nonce = os.urandom(12)
        body = os.urandom(16) + b"ciphertext"
        packed = envelope.pack(nonce, body)
        parsed = envelope.parse(packed)

        assert parsed.nonce == nonce
        assert parsed.body == body


class TestValidation:
    def test_rejects_a_blob_that_is_too_short_to_be_one(self):
        with pytest.raises(CryptoError) as excinfo:
            envelope.validate(b"CPE\x01short")
        assert "header alone" in str(excinfo.value)

    def test_rejects_foreign_magic(self):
        """This is the check a server holding no key can still make. Without it, share
        upload validates by length only and stores whatever it is handed."""

        _, blob = a_copasty_envelope()
        with pytest.raises(CryptoError) as excinfo:
            envelope.validate(b"XXX" + blob[3:])
        assert "not b'CPE'" in str(excinfo.value)

    def test_rejects_a_newer_version_and_says_so(self):
        _, blob = a_copasty_envelope()
        newer = blob[:3] + bytes([2]) + blob[4:]

        with pytest.raises(CryptoError) as excinfo:
            envelope.validate(newer)
        assert "newer client" in str(excinfo.value)

    def test_a_tampered_body_fails_to_decrypt(self):
        key, blob = a_copasty_envelope(b"secret")
        altered = bytearray(blob)
        altered[-1] ^= 0x01

        with pytest.raises(CryptoError) as excinfo:
            envelope.decrypt(key, bytes(altered))
        assert "altered" in str(excinfo.value)

    def test_the_wrong_key_fails_to_decrypt(self):
        _, blob = a_copasty_envelope(b"secret")

        with pytest.raises(CryptoError):
            envelope.decrypt(os.urandom(32), blob)

    def test_a_wrong_length_key_is_refused_before_use(self):
        with pytest.raises(CryptoError) as excinfo:
            envelope.encrypt(os.urandom(16), b"x")
        assert "32 bytes" in str(excinfo.value)

    def test_a_custom_magic_is_a_parameter_not_a_constant(self):
        """CPE is Copasty's. A second application gets its own rather than borrowing it."""

        key = os.urandom(32)
        blob = envelope.encrypt(key, b"x", magic=b"ABC")

        assert blob[0:3] == b"ABC"
        assert envelope.decrypt(key, blob, magic=b"ABC") == b"x"

        with pytest.raises(CryptoError):
            envelope.decrypt(key, blob, magic=b"CPE")


class TestBase64Url:
    def test_decodes_the_fragment_alphabet_without_padding(self):
        """Keys reach the viewer in the URL fragment, which rules out + and /, and the
        padding is routinely stripped. urlsafe_b64decode rejects both on its own."""

        raw = os.urandom(32)
        unpadded = base64.urlsafe_b64encode(raw).decode().rstrip("=")

        assert envelope.b64url_decode(unpadded) == raw

    def test_round_trips_through_the_encoder(self):
        raw = os.urandom(32)
        assert envelope.b64url_decode(envelope.b64url_encode(raw)) == raw

    def test_the_encoder_emits_no_padding(self):
        assert "=" not in envelope.b64url_encode(os.urandom(32))

    def test_rubbish_raises_rather_than_returning_short_bytes(self):
        with pytest.raises(CryptoError):
            envelope.b64url_decode("!!!not base64!!!")
