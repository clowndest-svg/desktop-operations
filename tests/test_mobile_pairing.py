"""The pairing vault: codes, tokens, digests, and the counter that stops guessing.

These are the rules the phone feature lives or dies by, so each one is stated as
behaviour a person could notice:

* a code is short-lived and **single use**;
* wrong guesses burn it, so 10^6 combinations is not a searchable space;
* the store holds digests, never the token that authenticates;
* revoking is immediate, and survives a restart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jarvis.app.mobile_pairing import CODE_DIGITS, PairingVault, clean_name


class _Clock:
    """A clock the test advances, so "expired" is not a ``sleep``."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


def make_vault(
    path: Path,
    clock: _Clock,
    *,
    pair_minutes: int = 5,
    max_pair_attempts: int = 6,
    max_devices: int = 8,
) -> PairingVault:
    """A vault on a fake clock, with the test's own limits."""
    return PairingVault(
        path,
        pair_minutes=pair_minutes,
        max_pair_attempts=max_pair_attempts,
        max_devices=max_devices,
        clock=clock,
    )


def test_start_pairing_issues_a_plain_six_digit_code(tmp_path: Path, clock: _Clock) -> None:
    vault = make_vault(tmp_path / "devices.json", clock)
    code = vault.start_pairing()
    assert len(code) == CODE_DIGITS
    assert code.isdigit()
    state = vault.pairing_state()
    assert state["active"] is True
    assert state["attempts_left"] == 6


def test_state_never_shows_the_code(tmp_path: Path, clock: _Clock) -> None:
    """The screen prints the code from ``start_pairing``; the state object does not.

    Matters because ``pairing_state`` is what the HUD polls every second and what the
    LAN could in principle echo. A code that travels on a poll is a code on a wire.
    """
    vault = make_vault(tmp_path / "devices.json", clock)
    code = vault.start_pairing()
    assert code not in json.dumps(vault.pairing_state())


def test_a_new_code_replaces_the_old_one(tmp_path: Path, clock: _Clock) -> None:
    vault = make_vault(tmp_path / "devices.json", clock)
    first = vault.start_pairing()
    second = vault.start_pairing()
    assert vault.exchange(first, "Pixel") is None
    assert vault.exchange(second, "Pixel") is not None


def test_exchange_is_single_use(tmp_path: Path, clock: _Clock) -> None:
    vault = make_vault(tmp_path / "devices.json", clock)
    code = vault.start_pairing()
    token = vault.exchange(code, "小米13")
    assert token
    assert vault.exchange(code, "小米13") is None
    # The token from the first exchange still works; only the code was consumed.
    assert vault.authenticate(token) is not None


def test_code_expires(tmp_path: Path, clock: _Clock) -> None:
    vault = make_vault(tmp_path / "devices.json", clock, pair_minutes=5)
    code = vault.start_pairing()
    clock.advance(5 * 60 + 1)
    assert vault.pairing_state()["active"] is False
    assert vault.exchange(code, "Pixel") is None


def test_wrong_codes_burn_the_current_one(tmp_path: Path, clock: _Clock) -> None:
    """Six guesses, then the code is gone even if the seventh guess is right.

    This is the whole defence against a LAN neighbour brute-forcing six digits; the
    math is not.
    """
    vault = make_vault(tmp_path / "devices.json", clock, max_pair_attempts=3)
    code = vault.start_pairing()
    guesses = [guess for guess in ("000000", "111111", "222222", "333333") if guess != code][:3]
    for guess in guesses:
        assert vault.exchange(guess, "Pixel") is None
    assert vault.pairing_state()["active"] is False
    assert vault.exchange(code, "Pixel") is None


def test_token_authenticates_and_unknown_tokens_do_not(tmp_path: Path, clock: _Clock) -> None:
    vault = make_vault(tmp_path / "devices.json", clock)
    token = vault.exchange(vault.start_pairing(), "Pixel 7")
    device = vault.authenticate(token or "")
    assert device is not None
    assert device.name == "Pixel 7"
    assert vault.authenticate("not-a-token") is None
    assert vault.authenticate("") is None


def first_row(vault: PairingVault) -> dict[str, Any]:
    """The oldest device as a typed row, so the assertions below stay readable."""
    rows = vault.devices()
    assert rows
    row: dict[str, Any] = dict(rows[0])
    return row


def test_store_holds_a_digest_not_the_token(tmp_path: Path, clock: _Clock) -> None:
    """Read the file a person can open and check the claim in the docstring."""
    path = tmp_path / "devices.json"
    vault = make_vault(path, clock)
    token = vault.exchange(vault.start_pairing(), "Pixel") or ""
    text = path.read_text(encoding="utf-8")
    assert token not in text
    assert str(first_row(vault)["device_id"]) in text
    assert "token_sha256" in text


def test_a_second_vault_on_the_same_file_still_answers(tmp_path: Path, clock: _Clock) -> None:
    """Restart: the phone keeps its token, so pairing survives closing the app."""
    path = tmp_path / "devices.json"
    first = make_vault(path, clock)
    token = first.exchange(first.start_pairing(), "Pixel") or ""
    second = make_vault(path, clock)
    assert second.authenticate(token) is not None
    assert len(second.devices()) == 1


def test_revoke_is_immediate_and_persisted(tmp_path: Path, clock: _Clock) -> None:
    path = tmp_path / "devices.json"
    vault = make_vault(path, clock)
    token = vault.exchange(vault.start_pairing(), "Pixel") or ""
    device_id = first_row(vault)["device_id"]
    assert vault.revoke(str(device_id)) is True
    assert vault.authenticate(token) is None
    assert make_vault(path, clock).authenticate(token) is None


def test_revoke_all_clears_every_device(tmp_path: Path, clock: _Clock) -> None:
    vault = make_vault(tmp_path / "devices.json", clock)
    for name in ("Pixel", "iPhone"):
        vault.exchange(vault.start_pairing(), name)
    assert vault.revoke_all() == 2
    assert vault.devices() == ()


def test_device_ceiling_refuses_a_new_pairing(tmp_path: Path, clock: _Clock) -> None:
    """More phones than the cap is a configuration problem, not an invitation to
    push an old device out from a phone."""
    vault = make_vault(tmp_path / "devices.json", clock, max_devices=1)
    assert vault.exchange(vault.start_pairing(), "Pixel") is not None
    assert vault.exchange(vault.start_pairing(), "第二台") is None


def test_corrupt_store_starts_empty_instead_of_refusing_to_run(tmp_path: Path) -> None:
    path = tmp_path / "devices.json"
    path.write_text("{ not json", encoding="utf-8")
    vault = PairingVault(path)
    assert vault.devices() == ()
    assert vault.start_pairing()


def test_rows_missing_a_digest_are_dropped(tmp_path: Path) -> None:
    """A device nobody can authenticate must not sit in the list as if it were real."""
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps({"devices": [{"device_id": "x", "name": "半条记录"}]}), encoding="utf-8"
    )
    vault = PairingVault(path)
    assert vault.devices() == ()


def test_touch_records_the_device_without_rewriting_every_request(
    tmp_path: Path, clock: _Clock
) -> None:
    path = tmp_path / "devices.json"
    vault = make_vault(path, clock)
    token = vault.exchange(vault.start_pairing(), "Pixel") or ""
    device_id = str(first_row(vault)["device_id"])
    added = float(first_row(vault)["added_at"])
    vault.touch(device_id)
    assert float(first_row(vault)["last_seen"]) == added
    clock.advance(400)
    vault.touch(device_id)
    assert float(first_row(vault)["last_seen"]) > added
    assert vault.authenticate(token) is not None


def test_touch_on_an_unknown_device_does_nothing(tmp_path: Path, clock: _Clock) -> None:
    vault = make_vault(tmp_path / "devices.json", clock)
    vault.touch("nope")
    assert vault.devices() == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Pixel 7", "Pixel 7"),
        ("  小米 13 Pro  ", "小米 13 Pro"),
        ("<script>alert(1)</script>", "scriptalert1script"),
        ("", "未命名设备"),
        ("a" * 300, "a" * 40),
    ],
)
def test_clean_name_squeezes_a_label_from_an_untrusted_phone(raw: str, expected: str) -> None:
    assert clean_name(raw) == expected
