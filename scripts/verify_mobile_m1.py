"""M1 验收：把电脑端的局域网服务真开起来，用另一个请求方当"手机"跑一遍。

不看代码看行为。每一条都要打印实测数字：端口、指纹、握手耗时、每个请求的状态码。
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app.disk_service import DiskService
from jarvis.app.system_service import SystemService
from jarvis.config.schema import MobileSection
from jarvis.tools.disk_cleaner import DiskCleaner
from jarvis.tools.monitor import SystemMonitor
from jarvis.ui.desktop import HudBridge, build_mobile_gateway
from jarvis.ui.lan_server import lan_addresses

SCRATCH = Path(__file__).resolve().parents[1] / "build" / "mobile-m1"


def request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    token: str = "",
) -> tuple[int, dict[str, object], float]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    started = time.perf_counter()
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    connection.request(
        method, path, body=json.dumps(body).encode() if body else None, headers=headers
    )
    response = connection.getresponse()
    payload = json.loads(response.read().decode() or "{}")
    connection.close()
    return response.status, payload, (time.perf_counter() - started) * 1000


def pinned(host: str, port: int, expected: str) -> str:
    """手机侧那一步：把连上的证书指纹和屏幕上读到的那一串比一下。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((host, port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname="localhost") as tls,
    ):
        der = tls.getpeercert(True)
    from jarvis.ui.lan_server import fingerprint_of

    got = fingerprint_of(der or b"")
    return "一致" if got == expected else f"不一致：{got}"


def plaintext_ask(host: str, port: int) -> str:
    """明文打过去，看它到底回不回。"""
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(f"GET /hello HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
            data = sock.recv(128)
        return f"回了 {len(data)} 字节：{data[:24]!r}" if data else "连接被挂断，没回任何东西"
    except OSError as exc:
        return f"直接被拒：{type(exc).__name__} {exc}"


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    # 每次从零开始：上一轮留下的设备会让"撤销"这条检查看着像是没生效。
    for stale in list(SCRATCH.glob("*.pem")) + list(SCRATCH.glob("*.json")):
        stale.unlink()
    # 一个采样器复用到底：每问一次就新建一个 SystemMonitor 的话，它没有上一个 CPU
    # 样本，读数永远是空的（这一版脚本一开始就踩中了这个坑）。
    sampler = SystemMonitor(top_processes=5)
    system = SystemService(lambda: sampler)
    system.start()
    bridge = HudBridge(
        system,
        DiskService(lambda: DiskCleaner(audit_log=SCRATCH / "audit.jsonl", sources={})),
    )
    section = MobileSection(
        enabled=False,
        bind_host="0.0.0.0",
        port=0,
        pair_minutes=5,
        max_pair_attempts=6,
        max_devices=8,
    )
    gateway = build_mobile_gateway(bridge, section, SCRATCH)
    assert gateway is not None
    if not gateway.enable():
        print(f"起不来：{gateway.reason}")
        return 1
    port = gateway.port
    state = gateway.state()
    print(f"监听端口      : {port}（bind 0.0.0.0，配置写的 0 = 让内核挑）")
    print(f"屏幕上的地址  : {state['url']}")
    print(f"证书指纹      : {state['fingerprint']}")
    print(f"证书文件      : {SCRATCH / 'mobile-cert.pem'}")

    local = "127.0.0.1"
    lan = next((address for address in lan_addresses() if not address.startswith("127.")), "")
    hosts = [local] + ([lan] if lan else [])
    print(f"可连的地址    : {hosts}")

    ok = True

    def check(label: str, condition: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and condition
        print(f"[{'过' if condition else '不过'}] {label}：{detail}")

    for host in hosts:
        status, hello, ms = request(host, port, "GET", "/hello")
        check(
            f"{host} 能连上并拿到 /hello（TLS 握手+请求 {ms:.0f}ms）",
            status == 200 and hello.get("name") == "小夜",
            f"{status} {hello}",
        )

    status, wrong, ms = request(
        lan or local, port, "POST", "/pair", {"code": "000000", "device": "假手机"}
    )
    check(
        "错的配对码被拒",
        status == 403 and not wrong.get("token"),
        f"{status} {wrong}（{ms:.0f}ms）",
    )

    code = str(gateway.show_pair_code()["code"])
    status, paired, ms = request(
        lan or local, port, "POST", "/pair", {"code": code, "device": "验证机"}
    )
    token = str(paired.get("token") or "")
    check(
        "对的配对码换一个 token",
        status == 200 and len(token) > 30,
        f"{status}，token {len(token)} 字符（{ms:.0f}ms）",
    )
    status, replay, _ = request(
        lan or local, port, "POST", "/pair", {"code": code, "device": "再试一次"}
    )
    check("同一个码不能用第二次", status == 403 and not replay.get("token"), f"{status} {replay}")

    status, _, ms = request(lan or local, port, "POST", "/rpc/app_info", {}, token=token)
    check("带 token 的 RPC 打到电脑上的桥", status == 200, f"{status}（{ms:.0f}ms）")
    status, body, _ = request(lan or local, port, "POST", "/rpc/app_info", {})
    check("没 token 的 RPC 一律 401", status == 401, f"{status} {body}")
    status, body, _ = request(
        lan or local, port, "POST", "/rpc/app_info", {}, token="zhang-gu-de-token"
    )
    check("乱填的 token 也 401", status == 401, f"{status} {body}")

    # 第一次读数没有 CPU 百分比（psutil 要两个样本才差得出），所以取两次。
    request(lan or local, port, "POST", "/rpc/snapshot", {}, token=token)
    time.sleep(1.0)
    status, snapshot, ms = request(lan or local, port, "POST", "/rpc/snapshot", {}, token=token)
    metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
    cpu = metrics.get("cpu") if isinstance(metrics.get("cpu"), dict) else {}
    memory = metrics.get("memory") if isinstance(metrics.get("memory"), dict) else {}
    check(
        "手机上能看到电脑的实时遥测（真实的 psutil 读数）",
        status == 200 and bool(cpu.get("cores")) and isinstance(cpu.get("percent"), (int, float)),
        f"{status}，CPU {cpu.get('percent')}% / {cpu.get('cores')} 核，"
        f"内存 {memory.get('percent', '?')}%（{ms:.0f}ms）",
    )

    stored_now = (SCRATCH / "devices.json").read_text(encoding="utf-8")
    check(
        "设备文件里只有摘要，没有可用的 token",
        token not in stored_now and "token_sha256" in stored_now,
        f"{len(stored_now)} 字节，里面能读到 sha256 那行",
    )

    status, body, _ = request(lan or local, port, "POST", "/rpc/window_hide", {}, token=token)
    check(
        "手机关不了电脑窗口（白名单外）", "未知方法" in str(body.get("error")), f"{status} {body}"
    )
    status, body, _ = request(
        lan or local, port, "POST", "/rpc/disk_delete", {"items": []}, token=token
    )
    check(
        "手机删不了电脑上的文件（白名单外）",
        "未知方法" in str(body.get("error")),
        f"{status} {body}",
    )
    status, body, _ = request(lan or local, port, "POST", "/rpc/mobile_pair_code", {}, token=token)
    check("手机不能自己发配对码", "未知方法" in str(body.get("error")), f"{status} {body}")

    device_id = str(gateway.state()["devices"][0]["device_id"])
    status, _, _ = request(lan or local, port, "POST", "/rpc/app_info", {}, token=token)
    check("撤销之前这台还好使", status == 200, f"{status}")
    gateway.revoke(device_id)
    status, body, _ = request(lan or local, port, "POST", "/rpc/app_info", {}, token=token)
    check("在电脑上点撤销，手机下一次请求就 401", status == 401, f"{status} {body}")

    check("明文请求拿不到东西", gateway.selftest()["plaintext_refused"], plaintext_ask(local, port))

    check(
        "手机固定指纹这一步对得上",
        pinned(local, port, str(state["fingerprint"])) == "一致",
        pinned(local, port, str(state["fingerprint"])),
    )

    served = request(local, port, "GET", "/hello")[1]
    check(
        "/hello 不泄指纹也不泄方法表",
        "tls_fingerprint" not in served and "methods" not in served,
        f"{served}",
    )

    gateway.disable()
    try:
        with socket.create_connection((local, port), timeout=3):
            refused = False
            detail = "居然还连得上"
    except OSError as exc:
        refused = True
        detail = f"{type(exc).__name__}"
    check("关掉开关之后端口扫不到", refused, detail)

    print()
    print("结论：", "M1 全部验收项通过" if ok else "有验收项没过，见上面 [不过]")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
