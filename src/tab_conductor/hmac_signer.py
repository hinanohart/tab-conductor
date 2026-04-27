"""HMAC-SHA256 payload signer for tab-conductor worker state writes.

Provides an opt-in integrity layer: workers that have access to the shared
secret can sign their state-update payloads; the supervisor verifies the
signature before committing to state.json.

Security notes:
- The key is **never** embedded in source.  It must be supplied via the
  ``TAB_CONDUCTOR_HMAC_KEY`` environment variable or the ``key=`` constructor
  argument.
- Comparison uses :func:`hmac.compare_digest` (constant-time) to prevent
  timing-oracle attacks.
- Canonical JSON serialisation (``sort_keys=True``) ensures that field-order
  differences between serialisers produce identical signatures.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Any

from tab_conductor.exceptions import HmacKeyMissing
from tab_conductor.logging_config import get_logger, structured_event

_logger: logging.Logger = get_logger("tab_conductor.hmac_signer")

_SIG_FIELD = "_sig"


def _canonical_json(payload: dict[str, Any]) -> str:
    """Serialise *payload* to a canonical, deterministic JSON string.

    Fields are sorted alphabetically and whitespace is minimised so that
    any two dicts with the same key-value pairs produce the same bytes
    regardless of insertion order.

    Args:
        payload: The dict to serialise (must not contain ``"_sig"``).

    Returns:
        A compact UTF-8 JSON string.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class HmacSigner:
    """HMAC-SHA256 signer/verifier for dict payloads.

    Args:
        key: Raw HMAC key bytes.  If ``None``, the key is read from the
            environment variable named by *env_var*.  If neither is available
            the signer operates in *disabled* mode (see :attr:`enabled`).
        env_var: Name of the environment variable consulted when *key* is
            not passed explicitly.  Defaults to ``"TAB_CONDUCTOR_HMAC_KEY"``.

    Raises:
        HmacKeyMissing: Never raised in ``__init__``; raised by
            :meth:`sign` / :meth:`verify` when called in disabled mode.

    Example:
        >>> import os; os.environ["TAB_CONDUCTOR_HMAC_KEY"] = "s3cr3t-key-32b"
        >>> signer = HmacSigner()
        >>> signed = signer.sign({"worker": "w1", "status": "done"})
        >>> signer.verify(signed)
        True
    """

    def __init__(
        self,
        key: bytes | None = None,
        env_var: str = "TAB_CONDUCTOR_HMAC_KEY",
    ) -> None:
        """Initialise and optionally load key from environment.

        Args:
            key: Explicit key bytes.  Takes priority over *env_var*.
            env_var: Environment variable name to fall back to.
        """
        self._env_var = env_var
        resolved_key: bytes | None = key

        if resolved_key is None:
            raw_env = os.environ.get(env_var)
            if raw_env is not None:
                resolved_key = raw_env.encode("utf-8")

        self._key: bytes | None = resolved_key

        if self._key is not None:
            structured_event(_logger, "hmac.initialised", enabled=True)
        else:
            structured_event(_logger, "hmac.initialised", enabled=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """``True`` if a key is configured, ``False`` in disabled mode.

        When disabled, :meth:`sign` raises :class:`HmacKeyMissing` and
        :meth:`verify` returns ``False``.

        Returns:
            Whether a valid key is available.
        """
        return self._key is not None

    def sign(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *payload* with an ``_sig`` HMAC field appended.

        The signature is computed over the canonical JSON representation of
        *payload* **without** the ``_sig`` key.  Any pre-existing ``_sig``
        field in *payload* is ignored (stripped before signing).

        Args:
            payload: The dict to sign.

        Returns:
            A new dict identical to *payload* plus ``"_sig": "<hex>"``.

        Raises:
            HmacKeyMissing: If no key is configured.
            ValueError: If *payload* cannot be JSON-serialised.
        """
        if self._key is None:
            raise HmacKeyMissing(self._env_var)

        # Strip any previous signature before computing the new one
        clean: dict[str, Any] = {k: v for k, v in payload.items() if k != _SIG_FIELD}
        canonical = _canonical_json(clean)
        sig = hmac.new(self._key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

        signed = dict(clean)
        signed[_SIG_FIELD] = sig
        structured_event(_logger, "hmac.signed", fields=sorted(clean.keys()))
        return signed

    def verify(self, payload: dict[str, Any]) -> bool:
        """Verify the ``_sig`` field of *payload* using the configured key.

        Args:
            payload: The dict to verify.  Must contain ``"_sig"``.

        Returns:
            ``True`` if the signature is present and valid, ``False`` if the
            key is not configured, the ``_sig`` field is absent, or the
            computed signature does not match.
        """
        if self._key is None:
            structured_event(_logger, "hmac.verify_skipped", reason="no_key")
            return False

        provided_sig = payload.get(_SIG_FIELD)
        if not isinstance(provided_sig, str):
            structured_event(_logger, "hmac.verify_failed", reason="no_sig_field")
            return False

        clean: dict[str, Any] = {k: v for k, v in payload.items() if k != _SIG_FIELD}
        canonical = _canonical_json(clean)
        expected = hmac.new(self._key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

        ok = hmac.compare_digest(expected, provided_sig)
        structured_event(_logger, "hmac.verified", valid=ok)
        return ok
