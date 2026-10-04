"""The LAN endpoint a phone talks to: TLS, tokens, and a closed list of methods.

Layer note: this is L5, and it is allowed to hold a :class:`HudBridge` because the
bridge is L5 too. What it is *not* allowed to do is invent capability. Every RPC
here resolves to a method that already exists on the bridge, so a phone cannot
reach anything the window cannot, and the whitelist below is the only door.

Why TLS with a self-signed certificate
--------------------------------------
The assistant can move the mouse, run a command line and read the disk. Sending
that over plain HTTP on a home Wi-Fi network would put every instruction in the
clear for anything else attached to it, and a neighbour's laptop could answer the
phone. A real certificate authority is not an option: the address is a private
``192.168.x.x`` that no CA will issue for, and the product's stance is that no
third-party service ever sees this traffic. So the computer generates its own
certificate once, and the **phone pins its SHA-256 fingerprint** — the operator
reads the fingerprint off the screen the same way they read the pairing code.
Pinning replaces the CA; it does not weaken the transport.

Plaintext is refused, not upgraded. ``--insecure`` does not exist here because a
fallback that "just works when the certificate is annoying" is the exact hole this
module was written to close.

Why the surface is a whitelist and not a namespace
--------------------------------------------------
The bridge also has ``window_hide``, ``pet_toggle``, ``voice_enable`` (which opens
the computer's microphone), ``settings_apply`` (which holds the API key variable
name), ``computer_set_tier``, and the delete-class methods ``disk_delete`` /
``process_kill`` / ``chat_delete`` / ``memory_forget_all``. A phone is a screen that
left the house. It gets read-only state, conversation, and reminder creation;
raising a permission tier, arming a microphone or deleting anything stays on the
machine with the person standing in front of it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import http.server
import ipaddress
import json
import logging
import socket
import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from jarvis.app.mobile_pairing import PairingVault

logger = logging.getLogger("jarvis.ui.lan_server")

APP_NAME = "小夜"

MAX_BODY_BYTES = 2 * 1024 * 1024
"""Largest request body accepted.

Sized by a real cross-file fact, not a guess: a photo question is an attachment
list, and the application layer allows ``MAX_ATTACHMENTS`` × ``MAX_IMAGE_DATA_CHARS``
(4 × 400 000 ≈ 1.6 MB) of base64. A cap below that would reject the phone's photo
with a 413 while the desktop accepts the same file, which reads as "the phone
version is broken" and is a pain to diagnose from a phone screen.
"""

CERT_VALID_DAYS = 824
"""Self-signed leaf lifetime.

Not a round number: Android refuses network certificates valid for more than 825
days, and a 3-year certificate would pair once and then fail on the phone only,
with no error the desktop side can explain.
"""

CERT_RENEW_BEFORE_DAYS = 60
"""Regenerate when the certificate has less than this left. Expiring in silence is
how a paired phone ends up with a handshake error nobody can reproduce."""

#: The bridge methods a paired device may call. Read-only state, conversation, and
#: creating a reminder. Anything that deletes, raises a permission tier, opens the
#: microphone or edits settings is absent on purpose — see the module docstring.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "app_info",
        "state_snapshot",
        "voice_status",
        "snapshot",
        "activity_log",
        "computer_levels",
        "shell_levels",
        "chat_models",
        "chat_sessions",
        "chat_messages",
        "chat_ask",
        "chat_new_session",
        "chat_switch",
        "chat_rename",
        "reminders",
        "reminder_add",
        "memory_list",
        "knowledge_state",
        # Voice. The phone is where a spoken conversation actually happens, so the
        # spoken round trip and the voice list belong here. What does *not* is
        # ``voice_enable`` (that arms the computer's microphone) or
        # ``voice_set_config``; a phone asking to talk must not open a different
        # machine's mic.
        "tts_voices",
        "tts_pick",
        # ``tts_preview_pcm`` and not ``tts_preview``: the latter plays through the
        # *computer's* speakers, which a phone user cannot hear. This one hands the
        # samples back so the phone plays them itself.
        "tts_preview_pcm",
        "call_readiness",
        "call_warmup",
        "call_turn",
        "call_speak",
        # Recorded voices. ``voice_clone_list`` and ``voice_clone_add`` are how a
        # phone makes a voice of its own; ``voice_clone_remove`` is allowed because
        # the recording is a file the phone created moments ago and the desktop
        # cannot see it. Contrast ``chat_delete`` / ``disk_delete`` / ``process_kill``:
        # those end something the operator was already living with, and stay home.
        "voice_clone_list",
        "voice_clone_add",
        "voice_clone_remove",
    }
)

FINGERPRINT_PREFIX = "sha256:"


def fingerprint_of(der: bytes) -> str:
    """The string the pairing screen prints and the phone pins."""
    return FINGERPRINT_PREFIX + hashlib.sha256(der).hexdigest()


def lan_addresses() -> tuple[str, ...]:
    """Every IPv4 address this machine answers on, for the certificate's SAN.

    Got from a connected UDP socket rather than by resolving the hostname: the
    latter returns ``127.0.0.1`` on a Windows box with a hosts entry, which is
    precisely not the address a phone needs. No packet leaves the machine — an
    unconnected UDP socket only asks the routing table which interface would be
    used, so this works with no internet and with the Wi-Fi unplugged (in which
    case it simply yields loopback).
    """
    found: list[str] = []
    probes: list[tuple[str, int]] = [("192.0.2.1", 53), ("10.255.255.255", 53)]
    for host, port in probes:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.5)
                sock.connect((host, port))
                address = str(sock.getsockname()[0])
        except OSError:
            continue
        if address not in found:
            found.append(address)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if address not in found:
                found.append(address)
    except OSError:
        pass
    if not found:
        found.append("127.0.0.1")
    return tuple(found)


def _san_hosts() -> tuple[str, ...]:
    """DNS names worth putting in the certificate."""
    names = ["localhost", socket.gethostname()]
    with contextlib.suppress(OSError):  # a machine that cannot resolve its own name
        names.append(socket.getfqdn().split(".")[0])
    return tuple(dict.fromkeys(name for name in names if name))


class CertificateUnavailableError(RuntimeError):
    """No usable TLS certificate, so the endpoint stays shut.

    Raised rather than logged-and-continued: the alternative is a listener that
    silently falls back to plaintext, which is the failure mode this whole module
    exists to prevent.
    """


def _new_key() -> Any:
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def ensure_certificate(
    directory: Path,
    *,
    addresses: tuple[str, ...] | None = None,
    now: dt.datetime | None = None,
) -> tuple[Path, Path, str]:
    """Create or reuse the self-signed certificate; returns ``(key, cert, fingerprint)``.

    Reuses when the file exists, is still valid, and already covers every address
    we would advertise. The last condition matters: an Android client pins the
    fingerprint *and* checks the name, so a certificate minted before the laptop
    roamed to a different Wi-Fi would hand back a fingerprint the phone accepts and
    a hostname it rejects. Regenerating changes the fingerprint, which means one
    re-pair — the honest cost of not shipping a key that outlives the network it
    was made for.

    Raises:
        CertificateUnavailableError: if ``cryptography`` is not installed. Install with
            ``pip install .[mobile]``.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    except ImportError as exc:  # pragma: no cover - the extra is optional
        raise CertificateUnavailableError(
            "手机接入需要 cryptography：pip install .[mobile]"
        ) from exc

    key_path = directory / "mobile-key.pem"
    cert_path = directory / "mobile-cert.pem"
    hosts = addresses or lan_addresses()
    moment = now or dt.datetime.now(dt.UTC)

    if cert_path.exists() and key_path.exists():
        reused = _reuse(cert_path, key_path, hosts, moment)
        if reused is not None:
            return key_path, cert_path, reused

    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, APP_NAME)])
    subject_alt_names: list[Any] = [x509.DNSName(entry) for entry in _san_hosts()]
    for host in hosts:
        try:
            subject_alt_names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            continue
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - dt.timedelta(days=1))
        .not_valid_after(moment + dt.timedelta(days=CERT_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(subject_alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    )
    certificate = builder.sign(key, hashes.SHA256())

    directory.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows maps this to the read-only bit
        logger.debug("could not tighten permissions on %s", key_path)
    digest = fingerprint_of(certificate.public_bytes(serialization.Encoding.DER))
    logger.info("mobile TLS certificate ready at %s", cert_path)
    return key_path, cert_path, digest


def _reuse(
    cert_path: Path, key_path: Path, hosts: tuple[str, ...], moment: dt.datetime
) -> str | None:
    """The fingerprint of a certificate that is still fit to serve, else ``None``."""
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, load_pem_private_key

    try:
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        load_pem_private_key(key_path.read_bytes(), password=None)
    except Exception:
        logger.warning("existing mobile certificate is unreadable; regenerating")
        return None
    if certificate.not_valid_after_utc - moment < dt.timedelta(days=CERT_RENEW_BEFORE_DAYS):
        return None
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return None
    covered = {str(entry) for entry in san.value.get_values_for_type(x509.DNSName)}
    covered |= {str(entry) for entry in san.value.get_values_for_type(x509.IPAddress)}
    for host in hosts:
        try:
            address = str(ipaddress.ip_address(host))
        except ValueError:
            continue
        if address not in covered:
            logger.info("certificate misses %s; regenerating", address)
            return None
    return fingerprint_of(certificate.public_bytes(Encoding.DER))


def ssl_context(key_path: Path, cert_path: Path) -> ssl.SSLContext:
    """The server context. TLS 1.2 floor, no client certificates.

    Client certs would be the stronger authentication, but a phone has no way to
    get one issued without the CA machinery this design refuses, and the bearer
    token already says which device is speaking.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


class _MobileRequestHandler(http.server.BaseHTTPRequestHandler):
    """Five routes, all of them JSON, none of them a directory listing."""

    server: _MobileHttpServer

    def do_GET(self) -> None:
        """``/hello`` for anyone; ``/methods`` and ``/state`` for a paired device."""
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/hello":
            self._answer(200, self.server.hello())
            return
        device = self.server.authorize(self)
        if device is None:
            self._answer(401, {"error": "未配对或 token 无效"})
            return
        if path == "/methods":
            self._answer(200, {"methods": sorted(ALLOWED_METHODS)})
            return
        if path == "/state":
            self._answer(200, self.server.state_for(device))
            return
        self._answer(404, {"error": "没有这个地址"})

    def do_POST(self) -> None:
        """``/pair`` is the unauthenticated enrolment door; everything else needs a token.

        An unknown path answers 401 to an unauthenticated caller rather than 404:
        a neighbour on the Wi-Fi probing names should not learn which routes exist by
        watching the status codes change.
        """
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/pair":
            body = self._body()
            if body is None:
                return
            answered = self.server.pair(body)
            self._answer(200 if answered.get("paired") else 403, answered)
            return
        if self.server.authorize(self) is None:
            self._answer(401, {"error": "未配对或 token 无效"})
            return
        if path.startswith("/rpc/"):
            self._rpc(path[len("/rpc/") :])
            return
        self._answer(404, {"error": "没有这个地址"})

    def _rpc(self, method: str) -> None:
        """One whitelisted bridge call. The caller already proved it is paired."""
        body = self._body()
        if body is None:
            return
        self._answer(200, self.server.rpc(method, body))

    def _body(self) -> dict[str, Any] | None:
        """Read a bounded JSON object, or answer 400/413 and return ``None``."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._answer(400, {"error": "Content-Length 无法解析"})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._answer(413, {"error": f"请求体超过 {MAX_BODY_BYTES} 字节"})
            return None
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._answer(400, {"error": "请求体不是合法 JSON"})
            return None
        if not isinstance(parsed, dict):
            self._answer(400, {"error": "请求体必须是 JSON 对象"})
            return None
        return parsed

    def _answer(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Access lines go to our logger at DEBUG, never to stderr."""
        logger.debug("%s %s", self.client_address[0], format % args)


class _MobileHttpServer(http.server.ThreadingHTTPServer):
    """The socket, the vault, and the bridge the whitelisted methods live on."""

    daemon_threads = True
    # Not the stdlib default (``http.server`` sets 1). On Windows SO_REUSEADDR lets
    # a bind succeed even when another process already holds the port, so a leftover
    # 小夜 would not fail loudly -- two listeners would split the phones on 8737.
    # Refusing the reuse turns the conflict into an error on screen, which is right.
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        bridge: Any,
        vault: PairingVault,
        *,
        version: str,
    ) -> None:
        self._bridge = bridge
        self._vault = vault
        self.version = version
        # Set by the gateway before the first request: which device just spoke.
        self.on_request: Callable[[str], None] = lambda device_id: None
        super().__init__(address, _MobileRequestHandler)

    # -- endpoints -------------------------------------------------------

    def hello(self) -> dict[str, object]:
        """Unauthenticated advertisement: enough to pick a machine, nothing else.

        Deliberately does not list the methods or the certificate fingerprint. A
        fingerprint has to be read by a human off the desktop screen; serving it to
        anyone on the subnet would let an attacker already sitting on the network
        point the phone at a *different* port with *their* certificate, which is
        precisely the attack pinning is supposed to defeat.
        """
        return {
            "name": APP_NAME,
            "version": self.version,
            "pairing_open": bool(self._vault.pairing_state().get("active")),
        }

    def pair(self, body: dict[str, Any]) -> dict[str, object]:
        """Trade a typed code for a token. The handler answers 403 when not paired."""
        token = self._vault.exchange(str(body.get("code") or ""), str(body.get("device") or ""))
        if token is None:
            return {"paired": False, "error": "配对码不对、已过期或已用完"}
        return {
            "paired": True,
            "token": token,
            "name": APP_NAME,
            "version": self.version,
            "methods": sorted(ALLOWED_METHODS),
        }

    def authorize(self, handler: _MobileRequestHandler) -> Any:
        """The device behind ``Authorization: Bearer …``, or ``None``."""
        header = handler.headers.get("Authorization") or ""
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return None
        device = self._vault.authenticate(value.strip())
        if device is not None:
            self.on_request(device.device_id)
        return device

    def state_for(self, device: Any) -> dict[str, object]:
        """What the app shows after it connects: who am I, and what is there."""
        return {
            "device": device.public(),
            "methods": sorted(ALLOWED_METHODS),
            "server": {"name": APP_NAME, "version": self.version},
        }

    def rpc(self, method: str, body: dict[str, Any]) -> dict[str, object]:
        """Call one whitelisted bridge method.

        A method outside the list is answered as *unknown* rather than "forbidden":
        telling a caller which names exist on the bridge is information a phone has
        no use for, and an error that distinguishes the two turns the endpoint into
        a directory of the desktop surface.
        """
        if method not in ALLOWED_METHODS:
            return {"error": f"未知方法：{method}"}
        handler = getattr(self._bridge, method, None)
        if handler is None:  # pragma: no cover - the whitelist names real methods
            return {"error": f"未知方法：{method}"}
        # A phone never supplies a confirmation. Nothing on today's whitelist takes
        # the flag, but this is the same rule the tool layer enforces ("confirmed is
        # never a caller-supplied value"), and a whitelist that grows is exactly
        # when a rule like that has to already be true.
        arguments = {key: value for key, value in body.items() if key != "confirmed"}
        try:
            result = handler(**arguments)
        except TypeError as exc:
            return {"error": f"参数不合法：{exc}"}
        except Exception as exc:  # pragma: no cover - the bridge contract says it does not raise
            logger.exception("lan rpc %s failed", method)
            return {"error": f"执行失败：{exc}"}
        if not isinstance(result, dict):
            return {"result": result}
        return result

    # -- plumbing --------------------------------------------------------

    def handle_error(self, request: Any, client_address: tuple[str, int]) -> None:
        """A refused handshake is not a bug to print a traceback about.

        ``http.server`` would otherwise dump a stack for every plaintext probe, and
        a scanner that gets one traceback per attempt is a scanner that fills a log.
        """
        del request
        logger.debug("mobile connection from %s ended", client_address[0])


def _plaintext_probe(port: int, host: str = "127.0.0.1", timeout: float = 3.0) -> bool:
    """Whether an HTTP request without TLS still gets an answer.

    Used by the self-test the tray runs after enabling, because "it works from the
    browser" does not prove a plaintext request is refused — browsers upgrade
    silently and hide exactly the hole this checks for.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b"GET /hello HTTP/1.1\r\nHost: x\r\n\r\n")
            return bool(sock.recv(64))
    except OSError:
        return False


def _lan_ip_for_display() -> str:
    """The address to print in the pairing screen."""
    for address in lan_addresses():
        if not address.startswith("127."):
            return address
    return "127.0.0.1"


def firewall_hint(port: int) -> str:
    """The ``netsh`` line that opens this one port, for the screen to copy.

    Not executed. Windows normally prompts when the app first listens, and a
    silent firewall edit made by an assistant is not something an operator should
    discover later.
    """
    return (
        f'netsh advfirewall firewall add rule name="小夜手机接入" '
        f"dir=in action=allow protocol=TCP localport={port}"
    )


class MobileGateway:
    """Owns the listener: on/off, the certificate, the device list, the pairing code.

    Nothing in here runs until :meth:`enable` is called. The tray's 「手机接入」 is
    the only thing that calls it in the shipped shell, and the default configuration
    says off — so the process that starts on a double-click holds no port.
    """

    def __init__(
        self,
        bridge: Any,
        vault: PairingVault,
        certificate_dir: Path,
        *,
        bind_host: str = "0.0.0.0",
        port: int = 8737,
        version: str = "",
        touch: Callable[[str], None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._vault = vault
        self._certificate_dir = certificate_dir
        self._bind_host = bind_host
        self._port = int(port)
        self._version = version
        self._server: _MobileHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._reason = ""
        self._fingerprint = ""
        self._touch = touch or vault.touch

    # -- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def reason(self) -> str:
        """Why it is not running, in one line for the screen."""
        return self._reason

    @property
    def port(self) -> int:
        """The port actually held.

        Read off the bound socket rather than the config, because ``port: 0`` is a
        legal way to ask for any free port -- and the pairing screen has to print the
        number the phone will dial, not the number somebody wrote in a file.
        """
        server = self._server
        if server is None:
            return self._port
        address = server.server_address
        return int(address[1]) if isinstance(address, tuple) else self._port

    def enable(self) -> bool:
        """Start listening. Returns whether the port is actually open.

        A ``False`` here must be shown to the operator. A tray tick that survived a
        failed bind would be the UI lying about a network listener, which is the one
        thing in this feature a person cannot afford to get wrong.
        """
        if self._server is not None:
            return True
        self._reason = ""
        try:
            key_path, cert_path, digest = ensure_certificate(self._certificate_dir)
        except CertificateUnavailableError as exc:
            self._reason = str(exc)
            logger.warning("mobile endpoint disabled: %s", exc)
            return False
        except OSError:
            logger.exception("could not prepare the mobile certificate")
            self._reason = "证书写入失败，看日志"
            return False
        self._fingerprint = digest
        try:
            server = _MobileHttpServer(
                (self._bind_host, self._port),
                self._bridge,
                self._vault,
                version=self._version,
            )
        except OSError as exc:
            logger.warning("could not bind %s:%s (%s)", self._bind_host, self._port, exc)
            self._reason = f"端口 {self._port} 绑定失败：{exc}"
            return False
        server.on_request = self._touch
        try:
            server.socket = ssl_context(key_path, cert_path).wrap_socket(
                server.socket, server_side=True
            )
        except (OSError, ssl.SSLError):
            logger.exception("could not put TLS on the mobile socket")
            server.server_close()
            self._reason = "TLS 装载失败，看日志"
            return False
        thread = threading.Thread(target=server.serve_forever, name="jarvis-mobile", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        logger.info(
            "mobile endpoint listening on https://%s:%d (pin %s)",
            _lan_ip_for_display(),
            self._port,
            digest[:23],
        )
        return True

    def disable(self) -> None:
        """Close the port and stop answering. Idempotent."""
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        self._vault.cancel_pairing()
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
        logger.info("mobile endpoint closed")

    def toggle(self) -> bool:
        """Flip the listener; returns the state after the flip."""
        if self.running:
            self.disable()
            return False
        return self.enable()

    # -- what the HUD asks for -------------------------------------------

    def state(self) -> dict[str, object]:
        """Everything the 「手机接入」 panel shows. The code itself is not here."""
        address = _lan_ip_for_display()
        port = self.port
        return {
            "running": self.running,
            "url": f"https://{address}:{port}" if self.running else "",
            "port": port,
            "fingerprint": self._fingerprint if self.running else "",
            "devices": self._vault.devices(),
            "pairing": self._vault.pairing_state(),
            "error": self._reason,
            "firewall": firewall_hint(port) if self.running else "",
        }

    def show_pair_code(self) -> dict[str, object]:
        """Issue a code for the screen. Refuses while the listener is off.

        A code minted against a closed port would be a number the operator reads to
        nobody, and the phone's failure would look like the phone's fault.
        """
        if not self.running:
            return {"code": "", "error": "先把「手机接入」打开"}
        return {"code": self._vault.start_pairing(), "error": ""}

    def revoke(self, device_id: str) -> dict[str, object]:
        """Cut one phone off, and hand back the list so the panel can redraw it."""
        return {"revoked": self._vault.revoke(str(device_id)), "devices": self._vault.devices()}

    def revoke_all(self) -> dict[str, object]:
        """Cut every phone off at once — the answer to a lost device."""
        return {"revoked": self._vault.revoke_all(), "devices": self._vault.devices()}

    def selftest(self) -> dict[str, object]:
        """Does this machine's listener really refuse plaintext? Run it, do not assume.

        Called from the HUD so the claim "only TLS" is a measurement rather than a
        sentence in a docstring.
        """
        if not self.running:
            return {"plaintext_refused": True, "note": "没有在监听"}
        host = "127.0.0.1" if self._bind_host in {"0.0.0.0", "::"} else self._bind_host
        refused = not _plaintext_probe(self.port, host=host)
        return {"plaintext_refused": refused, "note": "" if refused else "明文请求得到了回应"}


__all__ = [
    "ALLOWED_METHODS",
    "APP_NAME",
    "CertificateUnavailableError",
    "MobileGateway",
    "ensure_certificate",
    "fingerprint_of",
    "lan_addresses",
    "ssl_context",
]
