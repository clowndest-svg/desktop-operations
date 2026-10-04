"""The LAN endpoint, driven over a real TLS socket.

Nothing here mocks the transport. Pairing, tokens and the method whitelist are
worthless if they only hold in unit-tested Python objects, so every case below is
one HTTPS request against a listener the gateway actually opened on this machine.
The M1 acceptance line — *pair from a client, reject a wrong token, close the port
and it must not be reachable* — is asserted exactly that way.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

from jarvis.app.mobile_pairing import PairingVault
from jarvis.ui.lan_server import (
    ALLOWED_METHODS,
    MAX_BODY_BYTES,
    MobileGateway,
    _plaintext_probe,
    ensure_certificate,
    fingerprint_of,
)

pytest.importorskip("cryptography", reason="the [mobile] extra is optional")

REPO_ROOT = Path(__file__).resolve().parent.parent


class _StubBridge:
    """Enough of :class:`HudBridge` to see which doors a phone can walk through.

    ``touched`` records every entry, including the ones the endpoint must refuse, so
    a test can tell "answered forbidden" apart from "answered forbidden *and then ran
    anyway*" -- which is the difference between a gate and a message.
    """

    def __init__(self) -> None:
        self.touched: list[str] = []
        self.received: list[dict[str, Any]] = []

    def chat_ask(self, text: str = "", **extra: Any) -> dict[str, object]:
        self.touched.append("chat_ask")
        self.received.append({"text": text, **extra})
        return {"question": text, "answer": f"电脑答：{text}", "error": ""}

    def app_info(self) -> dict[str, object]:
        self.touched.append("app_info")
        return {"name": "小夜", "version": "test"}

    def reminder_add(self, title: str = "", when: str = "") -> dict[str, object]:
        self.touched.append("reminder_add")
        self.received.append({"title": title, "when": when})
        return {"ok": True, "title": title}

    # Present so the whitelist test can prove these are *unreachable*, not merely
    # unused: a phone must not hide the window, arm the microphone, raise a
    # permission tier or kill a process.
    def window_hide(self) -> dict[str, object]:
        self.touched.append("window_hide")
        return {"visible": False}

    def voice_enable(self) -> dict[str, object]:
        self.touched.append("voice_enable")
        return {"phase": "running"}

    def process_kill(self, pid: int = 0, confirmed: bool = False) -> dict[str, object]:
        self.touched.append("process_kill")
        self.received.append({"pid": pid, "confirmed": confirmed})
        return {"killed": True}


def make_gateway(
    tmp_path: Path, bridge: Any, *, vault: PairingVault | None = None
) -> MobileGateway:
    """A gateway on ``127.0.0.1`` with a kernel-chosen port.

    Port 0 rather than 8737: the suite runs in parallel with everything else on this
    machine, and a fixed port would make a test fail because somebody's HUD happened
    to be open. The gateway reports the port it actually holds.
    """
    root = tmp_path / "mobile"
    store = vault or PairingVault(root / "devices.json")
    return MobileGateway(bridge, store, root, bind_host="127.0.0.1", port=0, version="test")


def call(
    port: int,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    token: str = "",
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    """One HTTPS request. Unverified on purpose — see :func:`test_pinnable`.

    A CA will never issue for ``127.0.0.1``, so verification here would only test
    that the client rejects its own host. What the phone really pins is the
    fingerprint, and that is asserted separately.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = raw_body if raw_body is not None else (json.dumps(body).encode() if body else None)
    connection.request(method, path, body=payload, headers=headers)
    response = connection.getresponse()
    text = response.read()
    connection.close()
    return response.status, json.loads(text.decode("utf-8") or "{}")


@pytest.fixture()
def gateway(tmp_path: Path) -> Any:
    bridge = _StubBridge()
    served = make_gateway(tmp_path, bridge)
    assert served.enable(), served.reason
    yield served, bridge
    served.disable()


def test_certificate_is_written_and_reused(tmp_path: Path) -> None:
    root = tmp_path / "tls"
    key, cert, first = ensure_certificate(root, addresses=("127.0.0.1",))
    assert key.exists() and cert.exists()
    assert first.startswith("sha256:")
    again = ensure_certificate(root, addresses=("127.0.0.1",))
    assert again[2] == first, "a stable address must not mint a new fingerprint"


def test_certificate_regenerates_when_an_address_is_missing(tmp_path: Path) -> None:
    """A phone dials the address on the screen; a certificate that misses it fails."""
    root = tmp_path / "tls"
    _, _, before = ensure_certificate(root, addresses=("127.0.0.1",))
    _, _, after = ensure_certificate(root, addresses=("127.0.0.1", "192.168.1.42"))
    assert after != before


def test_certificate_covers_the_address_it_serves(tmp_path: Path) -> None:
    from cryptography import x509

    _, cert, _ = ensure_certificate(tmp_path / "tls", addresses=("192.168.7.7",))
    loaded = x509.load_pem_x509_certificate(cert.read_bytes())
    san = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert "192.168.7.7" in {str(ip) for ip in san.value.get_values_for_type(x509.IPAddress)}


def test_hello_answers_without_a_token(gateway: Any) -> None:
    served, _ = gateway
    status, payload = call(served.port, "GET", "/hello")
    assert status == 200
    assert payload["name"] == "小夜"
    # Not in the advertisement: the fingerprint and the method list.
    assert "tls_fingerprint" not in payload
    assert "methods" not in payload


def test_the_peer_certificate_fingerprint_is_the_one_on_screen(gateway: Any) -> None:
    """Pinning works: what the desktop prints *is* what the socket presents."""
    served, _ = gateway
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection(("127.0.0.1", served.port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname="localhost") as tls,
    ):
        der = tls.getpeercert(True)
    assert der is not None
    assert fingerprint_of(der) == served.state()["fingerprint"]


def test_pairing_trades_a_code_for_a_token(gateway: Any) -> None:
    served, _ = gateway
    code = served.show_pair_code()["code"]
    status, payload = call(served.port, "POST", "/pair", {"code": str(code), "device": "Pixel 7"})
    assert status == 200
    assert payload["paired"] is True
    assert payload["token"]
    devices = served.state()["devices"]
    assert isinstance(devices, tuple)
    assert [row["name"] for row in devices] == ["Pixel 7"]


def test_pairing_with_a_wrong_code_is_refused(gateway: Any) -> None:
    served, _ = gateway
    served.show_pair_code()
    status, payload = call(served.port, "POST", "/pair", {"code": "000000", "device": "x"})
    assert status == 403
    assert "token" not in payload
    assert payload["paired"] is False


def test_a_pairing_code_works_once(gateway: Any) -> None:
    served, _ = gateway
    code = str(served.show_pair_code()["code"])
    first = call(served.port, "POST", "/pair", {"code": code, "device": "a"})
    second = call(served.port, "POST", "/pair", {"code": code, "device": "b"})
    assert first[0] == 200
    assert second[0] == 403


def test_rpc_without_a_token_is_401(gateway: Any) -> None:
    served, _ = gateway
    assert call(served.port, "POST", "/rpc/app_info", {})[0] == 401


def test_rpc_with_a_wrong_token_is_401(gateway: Any) -> None:
    served, _ = gateway
    assert call(served.port, "POST", "/rpc/app_info", {}, token="guess")[0] == 401


def test_rpc_with_another_scheme_is_401(gateway: Any) -> None:
    """Only ``Bearer`` counts, so a phone cannot smuggle the token in a Basic header."""
    served, _ = gateway
    token = _pair(served)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection("127.0.0.1", served.port, context=context, timeout=10)
    connection.request(
        "POST", "/rpc/app_info", body=b"{}", headers={"Authorization": f"Basic {token}"}
    )
    assert connection.getresponse().status == 401
    connection.close()


def _pair(served: MobileGateway) -> str:
    code = str(served.show_pair_code()["code"])
    status, payload = call(served.port, "POST", "/pair", {"code": code, "device": "测试机"})
    assert status == 200
    token = str(payload["token"])
    assert token
    return token


def test_rpc_reaches_the_computers_agent(gateway: Any) -> None:
    """The answer comes from the bridge on this machine — that is the whole design.

    Asserted on the stub's own recording, not just the response body, because a
    phone feature that answered locally would still look correct on screen.
    """
    served, bridge = gateway
    token = _pair(served)
    status, payload = call(served.port, "POST", "/rpc/chat_ask", {"text": "你好"}, token=token)
    assert status == 200
    assert payload["answer"] == "电脑答：你好"
    assert bridge.received == [{"text": "你好"}]
    assert bridge.touched == ["chat_ask"]


def test_a_method_off_the_whitelist_is_never_reached(gateway: Any) -> None:
    """``window_hide`` exists on the bridge and is still unreachable.

    The point is the second assertion: a response that *says* forbidden while the
    handler already ran would be a very convincing fake gate.
    """
    served, bridge = gateway
    token = _pair(served)
    status, payload = call(served.port, "POST", "/rpc/window_hide", {}, token=token)
    assert status == 200
    assert "未知方法" in str(payload.get("error"))
    assert bridge.touched == []


def test_process_kill_and_voice_enable_are_not_exposed(gateway: Any) -> None:
    """Killing a process, deleting a file, arming the microphone, editing settings.

    Named here so widening the whitelist later is a decision somebody has to make
    against this test, not an accident in a refactor.
    """
    for forbidden in (
        "process_kill",
        "disk_delete",
        "chat_delete",
        "memory_forget_all",
        "knowledge_forget",
        "voice_enable",
        "voice_mute",
        "settings_apply",
        "settings_get",
        "computer_set_tier",
        "shell_set_tier",
        "pet_toggle",
        "window_hide",
        "mobile_toggle",
        "mobile_pair_code",
    ):
        assert forbidden not in ALLOWED_METHODS


def test_confirmed_is_stripped_from_rpc_arguments(gateway: Any) -> None:
    """A caller may not hand the machine its own confirmation.

    Asserted on what the bridge actually received, not on what was sent: the rule is
    that the flag never arrives, and a whitelist entry added later has to inherit
    that guarantee rather than re-earn it.
    """
    served, bridge = gateway
    token = _pair(served)
    call(
        served.port,
        "POST",
        "/rpc/chat_ask",
        {"text": "删掉那个文件夹", "confirmed": True},
        token=token,
    )
    assert bridge.received == [{"text": "删掉那个文件夹"}]


def test_rpc_bad_json_is_a_400(gateway: Any) -> None:
    served, _ = gateway
    token = _pair(served)
    status, payload = call(
        served.port, "POST", "/rpc/app_info", raw_body=b"{ not json", token=token
    )
    assert status == 400
    assert "error" in payload


def test_a_declared_body_beyond_the_cap_is_refused(gateway: Any) -> None:
    """``Content-Length`` is checked before anything is read into memory.

    Written as a raw request because the point is the *declared* size: a client that
    claims 512 KB more than the cap must be refused without this process ever
    allocating for it.
    """
    served, _ = gateway
    token = _pair(served)
    request = (
        f"POST /rpc/chat_ask HTTP/1.1\r\nHost: x\r\nConnection: close\r\n"
        f"Authorization: Bearer {token}\r\nContent-Length: {MAX_BODY_BYTES + 1}\r\n\r\n"
    ).encode()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection(("127.0.0.1", served.port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname="localhost") as tls,
    ):
        tls.sendall(request)
        tls.settimeout(10)
        answer = b""
        while True:
            try:
                chunk = tls.recv(4096)
            except (TimeoutError, ssl.SSLError, OSError):
                break
            if not chunk:
                break
            answer += chunk
            if b"\r\n\r\n" in answer:
                break
    assert b"413" in answer


def test_unknown_route_is_404_only_for_a_paired_device(gateway: Any) -> None:
    """Unauthenticated callers get 401 even on paths that do not exist.

    Answering 404 there would let anyone on the Wi-Fi map the endpoint by probing
    names; the paired phone is the only audience for "no such route".
    """
    served, _ = gateway
    assert call(served.port, "GET", "/settings")[0] == 401
    assert call(served.port, "POST", "/exec", {})[0] == 401
    token = _pair(served)
    assert call(served.port, "GET", "/settings", token=token)[0] == 404
    assert call(served.port, "POST", "/exec", {}, token=token)[0] == 404


def test_methods_and_state_need_a_token(gateway: Any) -> None:
    served, _ = gateway
    assert call(served.port, "GET", "/methods")[0] == 401
    token = _pair(served)
    status, payload = call(served.port, "GET", "/methods", token=token)
    assert status == 200
    assert sorted(payload["methods"]) == sorted(ALLOWED_METHODS)
    status, state = call(served.port, "GET", "/state", token=token)
    assert status == 200
    assert state["device"]["name"] == "测试机"


def test_plaintext_gets_nothing_back(gateway: Any) -> None:
    """The claim is "TLS only", so ask the port in plain HTTP and check the answer."""
    served, _ = gateway
    assert _plaintext_probe(served.port, host="127.0.0.1", timeout=5.0) is False
    assert served.selftest()["plaintext_refused"] is True


def test_closing_the_switch_closes_the_port(tmp_path: Path) -> None:
    """「关掉开关后端口扫不到」 — after disable, nothing is listening."""
    bridge = _StubBridge()
    served = make_gateway(tmp_path, bridge)
    assert served.enable()
    port = served.port
    assert call(port, "GET", "/hello")[0] == 200
    served.disable()
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=3):
        pass
    assert served.running is False
    assert served.state()["url"] == ""


def test_disable_burns_the_outstanding_code(tmp_path: Path) -> None:
    bridge = _StubBridge()
    served = make_gateway(tmp_path, bridge)
    served.enable()
    code = str(served.show_pair_code()["code"])
    served.disable()
    served.enable()
    status, _ = call(served.port, "POST", "/pair", {"code": code, "device": "迟到的手机"})
    served.disable()
    assert status == 403


def test_pair_code_refuses_while_the_port_is_closed(tmp_path: Path) -> None:
    """A code the phone can never redeem is worse than no code: it reads like the
    phone's fault."""
    served = make_gateway(tmp_path, _StubBridge())
    assert served.running is False
    answered = served.show_pair_code()
    assert answered["code"] == ""
    assert "打开" in str(answered["error"])


def test_revoking_a_device_kills_its_next_request(gateway: Any) -> None:
    served, _ = gateway
    token = _pair(served)
    device_id = str(served.state()["devices"][0]["device_id"])
    assert served.revoke(device_id)["revoked"] is True
    assert call(served.port, "POST", "/rpc/app_info", {}, token=token)[0] == 401


def test_every_whitelisted_method_exists_on_the_bridge() -> None:
    """No phantom capability: the phone only ever names a real desktop method.

    This is the check that a whitelist entry added in a hurry ("sure, expose
    ``telemetry2``") cannot point at nothing and answer an error the app then has to
    explain from a phone screen.
    """
    from jarvis.ui.desktop import HudBridge

    missing = [name for name in ALLOWED_METHODS if not hasattr(HudBridge, name)]
    assert missing == []


def test_missing_cryptography_refuses_rather_than_going_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有证书生成能力时，这扇门直接不开——没有"先用明文顶着"这条路。

    The fallback would be silent and catastrophic: a phone that typed a pairing code
    into an HTTP endpoint would have just sent that code across the Wi-Fi in clear.
    """
    from jarvis.ui import lan_server

    def unmintable(*_: Any, **__: Any) -> NoReturn:
        raise lan_server.CertificateUnavailableError(
            "手机接入需要 cryptography：pip install .[mobile]"
        )

    monkeypatch.setattr(lan_server, "ensure_certificate", unmintable)
    served = make_gateway(tmp_path, _StubBridge())
    assert served.enable() is False
    assert "cryptography" in served.reason
    assert served.running is False
    assert served.state()["url"] == ""


def test_the_body_cap_still_covers_a_photo_question() -> None:
    """上限必须容得下电脑自己允许的附件量。

    不然手机上发一张照片得到 413，而电脑上同一个文件能发出去——这在用户眼里
    不是"太大"，是"手机版是坏的"，而且从手机屏幕上看不出来。
    """
    from jarvis.app.chat_service import MAX_ATTACHMENTS, MAX_IMAGE_DATA_CHARS

    assert MAX_BODY_BYTES >= MAX_ATTACHMENTS * MAX_IMAGE_DATA_CHARS + 8_192


def test_gateway_reports_the_port_it_holds_not_the_one_in_config(tmp_path: Path) -> None:
    served = make_gateway(tmp_path, _StubBridge())
    assert served.port == 0  # not listening: the configured request, verbatim
    served.enable()
    assert served.port > 0
    assert served.state()["port"] == served.port
    served.disable()


class TestDesktopWiring:
    """What ``desktop.run`` does with the config block, without opening a window."""

    def test_no_config_or_no_directory_means_no_menu_item(self, tmp_path: Path) -> None:
        from jarvis.ui.desktop import build_mobile_gateway

        bridge = _StubBridge()
        assert build_mobile_gateway(cast(Any, bridge), None, tmp_path) is None
        assert build_mobile_gateway(cast(Any, bridge), _section(), None) is None

    def test_the_config_numbers_are_the_numbers_the_gateway_uses(self, tmp_path: Path) -> None:
        from jarvis.ui.desktop import build_mobile_gateway

        served = build_mobile_gateway(cast(Any, _StubBridge()), _section(port=8799), tmp_path)
        assert served is not None
        assert served.port == 8799
        assert served.running is False, "enabled=false must not bind on construction"

    def test_the_bridge_answers_honestly_without_a_gateway(self, tmp_path: Path) -> None:
        """A page that asks before the shell wired the endpoint gets an error, not a hang."""
        from jarvis.app.disk_service import DiskService
        from jarvis.app.system_service import SystemService
        from jarvis.tools.disk_cleaner import DiskCleaner
        from jarvis.ui.desktop import HudBridge

        class _NoTelemetry:
            def __call__(self) -> Any:
                raise AssertionError("手机接入这几个调用不该读遥测")

        system = SystemService(_NoTelemetry())
        disk = DiskService(lambda: DiskCleaner(audit_log=tmp_path / "audit.jsonl", sources={}))
        bridge = HudBridge(system, disk)
        state = bridge.mobile_state()
        assert state["running"] is False
        assert state["devices"] == []
        assert state["error"]
        assert bridge.mobile_toggle(True)["running"] is False
        assert bridge.mobile_pair_code()["code"] == ""
        assert bridge.mobile_revoke("x")["revoked"] is False
        assert bridge.mobile_revoke_all()["revoked"] == 0
        assert bridge.mobile_selftest()["plaintext_refused"] is True

    def test_run_signature_accepts_the_mobile_block(self) -> None:
        """The composition root passes these two; a renamed parameter is a silent no-op."""
        import inspect

        from jarvis.ui.desktop import run

        parameters = inspect.signature(run).parameters
        assert "mobile" in parameters
        assert "mobile_dir" in parameters


def _frontend(relative: str) -> str:
    return (REPO_ROOT / "frontend" / relative).read_text(encoding="utf-8")


def test_the_phone_only_names_methods_the_computer_answers() -> None:
    """手机端源码里每一个 RPC 方法名，都必须在电脑端白名单上。

    存在的理由和插件契约那条一样：跨端的名字对不上，只有真点一次才会发现，
    而"真点一次"需要一台手机。写在这里就不需要了。
    """
    source = _frontend("src/mobile/MobileApp.vue")
    used = set(re.findall(r"pcCall(?:<[^>]*>)?\(\s*\w+\s*,\s*'([^']+)'", source))
    assert used, "手机端一个 RPC 都没调，这条检查就成了空话"
    assert used <= ALLOWED_METHODS


def test_the_pin_format_agrees_across_the_three_ends() -> None:
    """电脑打印的、手机存的、Kotlin 比对的是同一种写法，否则固定永远对不上。

    三处各自决定过一次格式，就会有一处不一致，而失败长得像"配对码错了"。
    """
    printed = fingerprint_of(b"demo")
    assert printed.startswith("sha256:")

    api = _frontend("src/mobile/api.ts")
    assert "sha256:" in api
    assert "trim().toLowerCase()" in api, "手机必须把指纹归成小写再去比"

    kotlin = _frontend("android/app/src/main/java/com/xiaoye/assistant/XyNetPlugin.java")
    assert 'startsWith("sha256:")' in kotlin, "原生端要把前缀去掉再比十六进制"
    assert "toLowerCase" in kotlin


def _section(port: int = 8737) -> Any:
    from jarvis.config.schema import MobileSection

    return MobileSection(
        enabled=False,
        bind_host="127.0.0.1",
        port=port,
        pair_minutes=5,
        max_pair_attempts=6,
        max_devices=8,
    )
