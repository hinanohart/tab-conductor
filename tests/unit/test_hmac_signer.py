"""Unit tests for tab_conductor.hmac_signer.

Covers:
- sign → verify round-trip returns True
- tampering any field makes verify return False
- key rotation: old-key signature fails verification with new key
- disabled mode (no key): enabled=False, sign raises HmacKeyMissing
- canonical JSON: field-order differences produce identical signatures
- _sig field is excluded from the data being signed (strip before sign)
- verify with missing _sig field returns False (not raise)
"""

from __future__ import annotations

import pytest

from tab_conductor.exceptions import HmacKeyMissing
from tab_conductor.hmac_signer import HmacSigner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY_A = b"key-alpha-32-bytes-padded-here!!"
_KEY_B = b"key-beta-32-bytes-padded-here!!!"


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestSignVerifyRoundtrip:
    """sign() then verify() on the same signer should succeed."""

    def test_round_trip_true(self) -> None:
        """sign + verify with the same key → True."""
        signer = HmacSigner(key=_KEY_A)
        payload = {"worker_id": "w1", "status": "done", "cost": 0.42}
        signed = signer.sign(payload)
        assert "_sig" in signed
        assert signer.verify(signed) is True

    def test_sign_preserves_all_fields(self) -> None:
        """signed dict contains all original fields plus _sig."""
        signer = HmacSigner(key=_KEY_A)
        payload = {"a": 1, "b": "hello"}
        signed = signer.sign(payload)
        assert signed["a"] == 1
        assert signed["b"] == "hello"
        assert "_sig" in signed

    def test_verify_on_unsigned_payload_returns_false(self) -> None:
        """verify() returns False (not raise) when _sig is absent."""
        signer = HmacSigner(key=_KEY_A)
        assert signer.verify({"field": "value"}) is False


# ---------------------------------------------------------------------------
# Tampering detection
# ---------------------------------------------------------------------------


class TestTamperingDetection:
    """Modifying any field after signing must cause verify to return False."""

    def test_tamper_value_fails(self) -> None:
        """Changing a field value invalidates the signature."""
        signer = HmacSigner(key=_KEY_A)
        signed = signer.sign({"worker_id": "w1", "status": "done"})
        signed["status"] = "hacked"
        assert signer.verify(signed) is False

    def test_tamper_sig_field_directly_fails(self) -> None:
        """Changing _sig itself fails verification."""
        signer = HmacSigner(key=_KEY_A)
        signed = signer.sign({"x": 1})
        signed["_sig"] = "deadbeef" * 8  # 64 hex chars, wrong value
        assert signer.verify(signed) is False

    def test_add_extra_field_fails(self) -> None:
        """Adding an extra field after signing invalidates the signature."""
        signer = HmacSigner(key=_KEY_A)
        signed = signer.sign({"x": 1})
        signed["injected"] = "evil"
        assert signer.verify(signed) is False


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


class TestKeyRotation:
    """Signatures from one key must not verify against a different key."""

    def test_different_key_fails(self) -> None:
        """Signature created with key A cannot be verified with key B."""
        signer_a = HmacSigner(key=_KEY_A)
        signer_b = HmacSigner(key=_KEY_B)
        signed = signer_a.sign({"data": "payload"})
        assert signer_b.verify(signed) is False

    def test_same_key_different_instance_passes(self) -> None:
        """Two HmacSigner instances with the same key can cross-verify."""
        signer1 = HmacSigner(key=_KEY_A)
        signer2 = HmacSigner(key=_KEY_A)
        signed = signer1.sign({"msg": "hello"})
        assert signer2.verify(signed) is True


# ---------------------------------------------------------------------------
# Disabled mode
# ---------------------------------------------------------------------------


class TestDisabledMode:
    """When no key is provided, signer operates in disabled mode."""

    def test_enabled_false_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """enabled is False when neither key= nor env var is set."""
        monkeypatch.delenv("TAB_CONDUCTOR_HMAC_KEY", raising=False)
        signer = HmacSigner()
        assert signer.enabled is False

    def test_sign_raises_hmac_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sign() raises HmacKeyMissing when no key is configured."""
        monkeypatch.delenv("TAB_CONDUCTOR_HMAC_KEY", raising=False)
        signer = HmacSigner()
        with pytest.raises(HmacKeyMissing):
            signer.sign({"data": "value"})

    def test_verify_returns_false_when_no_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """verify() returns False (not raise) when no key is configured."""
        monkeypatch.delenv("TAB_CONDUCTOR_HMAC_KEY", raising=False)
        signer = HmacSigner()
        assert signer.verify({"data": "value", "_sig": "abc"}) is False

    def test_enabled_true_when_env_var_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """enabled is True when TAB_CONDUCTOR_HMAC_KEY env var is set."""
        monkeypatch.setenv("TAB_CONDUCTOR_HMAC_KEY", "env-key-32-bytes-padded-here!!")
        signer = HmacSigner()
        assert signer.enabled is True

    def test_env_var_key_used_for_signing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Key loaded from env var produces a verifiable signature."""
        test_key = "env-key-32-bytes-padded-here!!"
        monkeypatch.setenv("TAB_CONDUCTOR_HMAC_KEY", test_key)
        signer = HmacSigner()
        signed = signer.sign({"worker": "w1"})
        assert signer.verify(signed) is True


# ---------------------------------------------------------------------------
# Canonical JSON / field-order invariance
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    """Signatures must be identical regardless of Python dict insertion order."""

    def test_field_order_invariant(self) -> None:
        """Two dicts with same content but different order produce the same sig."""
        signer = HmacSigner(key=_KEY_A)
        p1 = {"a": 1, "b": 2, "c": 3}
        p2 = {"c": 3, "a": 1, "b": 2}
        s1 = signer.sign(p1)
        s2 = signer.sign(p2)
        assert s1["_sig"] == s2["_sig"]

    def test_re_sign_strips_old_sig(self) -> None:
        """Re-signing a payload strips the existing _sig before computing."""
        signer = HmacSigner(key=_KEY_A)
        original = signer.sign({"x": 42})
        # Sign again — the new sig should be the same as the original
        re_signed = signer.sign(original)
        assert re_signed["_sig"] == original["_sig"]
