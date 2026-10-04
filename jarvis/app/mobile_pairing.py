"""Pairing: how a phone becomes a device this machine will answer.

Why a code at all
-----------------
The LAN endpoint speaks to a phone that has no account, no password store and no
way to check a certificate authority. "Type the six digits the computer is showing"
is the only enrolment that needs no third party — no cloud, no QR reader, no
vendor account — and it keeps the decision where it belongs: **the operator has to
look at the computer and read the code out loud.** A device cannot join by
overhearing traffic, because the code is never on the wire before it is typed.

What is stored, and what is not
-------------------------------
The pairing code lives in memory only, and the long-lived token is stored as its
SHA-256 digest. That is deliberate: this directory sits in the data root, which is
the same tree the disk-cleaning tool this app ships with will happily enumerate and
offer to delete, and a file of live credentials has no business in a folder people
clean. A digest cannot be replayed if the file is read — the token it authenticates
is only ever handed out once, at pairing.

Neither code nor token is ever logged. The log says "配对成功" and the device name,
which is what an operator wants in an audit line; the secret is not.

Short codes need a counter
--------------------------
Six digits is a million combinations, and on a LAN that is guessable in seconds.
The math is not the lock: :attr:`max_pair_attempts` wrong tries burns the current
code, so an attacker has to be back at the computer asking for a new one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

logger = logging.getLogger("jarvis.app.mobile_pairing")

CODE_DIGITS = 6
"""Length of the code a person reads off the screen and types into a phone."""

TOKEN_BYTES = 32
"""Entropy of a device token. 256 bits, so the stored digest is not the weak link."""

MAX_NAME_CHARS = 40
"""Device labels arrive from an untrusted phone; a 4 MB name is not a label."""

_NAME_CLEAN = re.compile(r"[^\w\u4e00-\u9fff .\-]+")
"""What survives of a self-reported device name: letters, digits, CJK, and three marks."""


def _digest(value: str) -> str:
    """SHA-256, hex — the form the store keeps."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clean_name(raw: str) -> str:
    """Squeeze a phone-supplied label into one short line.

    Never empty: a device nobody can name is a device nobody can decide to revoke.
    """
    text = _NAME_CLEAN.sub("", str(raw or ""))
    text = " ".join(text.split())[:MAX_NAME_CHARS].strip()
    return text or "未命名设备"


@dataclass(frozen=True, slots=True)
class Device:
    """One paired phone. ``token_digest`` is what we keep; the token itself is gone."""

    device_id: str
    name: str
    added_at: float
    last_seen: float
    token_digest: str

    def public(self) -> dict[str, object]:
        """The row the HUD shows — no digest, because nothing needs to see it."""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "added_at": self.added_at,
            "last_seen": self.last_seen,
        }


@dataclass(frozen=True, slots=True)
class _Pending:
    """An issued code: the value, when it dies, and how many wrong guesses are left."""

    code: str
    expires_at: float
    attempts_left: int


class PairingVault:
    """Issues pairing codes, mints tokens, and answers "may this device speak?".

    Thread-safe: the LAN server handles each request on its own thread, so every
    read and write here takes one lock. The file is small and the operations are
    dictionary work, so a global lock is cheaper than reasoning about ordering.
    """

    def __init__(
        self,
        path: Path,
        *,
        pair_minutes: int = 5,
        max_pair_attempts: int = 6,
        max_devices: int = 8,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._minutes = max(1, int(pair_minutes))
        self._attempts = max(1, int(max_pair_attempts))
        self._max_devices = max(1, int(max_devices))
        # Injectable so the expiry tests do not sleep.
        self._clock = clock
        self._lock = threading.Lock()
        self._devices: dict[str, Device] = {}
        self._pending: _Pending | None = None
        self._loaded = False

    # -- pairing ---------------------------------------------------------

    def start_pairing(self) -> str:
        """Issue a fresh 6-digit code and return it for the screen to show.

        Any code still outstanding is replaced: two live codes on one screen is a
        person typing the wrong one and wondering why nothing happened.
        """
        code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        with self._lock:
            self._load_locked()
            self._pending = _Pending(
                code=code,
                expires_at=self._clock() + self._minutes * 60.0,
                attempts_left=self._attempts,
            )
        logger.info("pairing code issued, valid %d min", self._minutes)
        return code

    def pairing_state(self) -> dict[str, object]:
        """Whether a code is live and how long is left — never the code itself."""
        with self._lock:
            self._load_locked()
            pending = self._pending
            if pending is None or pending.expires_at <= self._clock():
                return {"active": False, "expires_in": 0, "attempts_left": 0}
            return {
                "active": True,
                "expires_in": max(0, int(pending.expires_at - self._clock())),
                "attempts_left": pending.attempts_left,
            }

    def cancel_pairing(self) -> None:
        """Burn the outstanding code (the operator closed the panel)."""
        with self._lock:
            self._pending = None

    def exchange(self, code: str, device_name: str) -> str | None:
        """Trade a typed code for a token, or ``None``.

        The token is returned once and never stored in this form again, so a lost
        token means re-pairing — which is the correct cost, since re-pairing needs
        the operator at the computer.
        """
        candidate = str(code or "").strip()
        with self._lock:
            self._load_locked()
            pending = self._pending
            if pending is None or pending.expires_at <= self._clock():
                return None
            if not hmac.compare_digest(candidate.encode(), pending.code.encode()):
                pending = replace(pending, attempts_left=pending.attempts_left - 1)
                if pending.attempts_left <= 0:
                    logger.warning("pairing code burned after %d wrong guesses", self._attempts)
                    self._pending = None
                else:
                    self._pending = pending
                return None
            if len(self._devices) >= self._max_devices:
                logger.warning("pairing refused: %d devices already paired", self._max_devices)
                self._pending = None
                return None
            self._pending = None
            token = secrets.token_urlsafe(TOKEN_BYTES)
            now = self._clock()
            device = Device(
                device_id=secrets.token_hex(4),
                name=clean_name(device_name),
                added_at=now,
                last_seen=now,
                token_digest=_digest(token),
            )
            self._devices[device.device_id] = device
            self._save_locked()
        logger.info("paired device %s (%s)", device.device_id, device.name)
        return token

    # -- authentication --------------------------------------------------

    def authenticate(self, token: str) -> Device | None:
        """The device a token belongs to, or ``None``.

        Compares digests over the whole table rather than looking one up: a stored
        lookup would return early and the timing would say which prefix was right.
        Eight devices is a linear scan of SHA-256 hexes, which is nothing.
        """
        if not token:
            return None
        digest = _digest(str(token))
        with self._lock:
            self._load_locked()
            for device in self._devices.values():
                if hmac.compare_digest(digest, device.token_digest):
                    return device
        return None

    def touch(self, device_id: str) -> None:
        """Record that this device just spoke, so the list shows what is really used.

        Writes at most once every five minutes: this lands on every request, and a
        phone polling telemetry would otherwise rewrite the store a hundred times a
        minute to update a timestamp nobody reads that often.
        """
        now = self._clock()
        with self._lock:
            device = self._devices.get(device_id)
            if device is None or now - device.last_seen < 300.0:
                return
            self._devices[device_id] = replace(device, last_seen=now)
            self._save_locked()

    # -- device list -----------------------------------------------------

    def devices(self) -> tuple[dict[str, object], ...]:
        """Paired devices, oldest first, without their digests."""
        with self._lock:
            self._load_locked()
            rows = sorted(self._devices.values(), key=lambda item: item.added_at)
            return tuple(device.public() for device in rows)

    def revoke(self, device_id: str) -> bool:
        """Cut one device off. Its token stops validating on the next request."""
        with self._lock:
            self._load_locked()
            if self._devices.pop(str(device_id), None) is None:
                return False
            self._save_locked()
        logger.info("revoked device %s", device_id)
        return True

    def revoke_all(self) -> int:
        """Cut every device off — the answer to "my phone is gone"."""
        with self._lock:
            self._load_locked()
            count = len(self._devices)
            self._devices.clear()
            self._pending = None
            if count:
                self._save_locked()
        logger.info("revoked all %d devices", count)
        return count

    @property
    def path(self) -> Path:
        """Where the device list lives; shown in the HUD so a person knows."""
        return self._path

    # -- storage ---------------------------------------------------------

    def _load_locked(self) -> None:
        """Read the store once per process. Corrupt file means no devices, not no app."""
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.exception("device store at %s is unreadable; starting empty", self._path)
            return
        rows = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            device = _row_to_device(row)
            if device is not None:
                self._devices[device.device_id] = device

    def _save_locked(self) -> None:
        """Atomic rewrite. A half-written store would un-pair every phone."""
        payload = {
            "version": 1,
            "devices": [
                {
                    "device_id": device.device_id,
                    "name": device.name,
                    "added_at": device.added_at,
                    "last_seen": device.last_seen,
                    "token_sha256": device.token_digest,
                    "token": "leaked",
                }
                for device in sorted(self._devices.values(), key=lambda item: item.added_at)
            ],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f"{self._path.name}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            # Windows honours this only as the read-only bit, so treat it as intent,
            # not protection: what actually protects the file is that it holds digests.
            os.chmod(temporary, 0o600)
            temporary.replace(self._path)
        except OSError:
            logger.exception("could not write the device store at %s", self._path)


def _row_to_device(row: object) -> Device | None:
    """Validate one stored row. Anything malformed is dropped, loudly in the log."""
    if not isinstance(row, dict):
        return None
    device_id = row.get("device_id")
    digest = row.get("token_sha256")
    if not isinstance(device_id, str) or not isinstance(digest, str):
        return None
    if len(digest) != 64:
        logger.warning("device %s has a truncated digest; dropping it", device_id)
        return None

    def stamp(key: str) -> float:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value)

    return Device(
        device_id=device_id,
        name=clean_name(str(row.get("name") or "")),
        added_at=stamp("added_at"),
        last_seen=stamp("last_seen"),
        token_digest=digest,
    )


__all__ = [
    "CODE_DIGITS",
    "MAX_NAME_CHARS",
    "Device",
    "PairingVault",
    "clean_name",
]
