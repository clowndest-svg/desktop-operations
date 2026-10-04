"""手机上那 18 个白名单方法，逐个用"手机会发的形状"打一遍。

不是给 M1 再加一遍测试，而是回答另一个问题：**手机界面里的每一个入口，
点下去电脑这边到底答不答**。方法名对不上、参数名打错、返回形状不像话——
这些在真机上都会变成"点了没反应"，而真机不是随时都在。

方法清单和参数形状直接从 `frontend/src/mobile/` 的源码里读出来，
所以改了手机界面忘了改电脑端，这边会红。
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._fakes import FakeLlmClient  # noqa: E402

from jarvis.app.chat_service import MAX_IMAGE_DATA_CHARS, ChatService  # noqa: E402
from jarvis.app.disk_service import DiskService  # noqa: E402
from jarvis.app.reminder_service import ReminderService  # noqa: E402
from jarvis.app.system_service import SystemService  # noqa: E402
from jarvis.app.transcript_service import TranscriptService  # noqa: E402
from jarvis.config.schema import MobileSection, ModelSpec  # noqa: E402
from jarvis.database.store import SqliteStore  # noqa: E402
from jarvis.tools.disk_cleaner import DiskCleaner  # noqa: E402
from jarvis.tools.monitor import SystemMonitor  # noqa: E402
from jarvis.ui.desktop import HudBridge, build_mobile_gateway  # noqa: E402
from jarvis.ui.lan_server import MAX_BODY_BYTES, fingerprint_of  # noqa: E402

SCRATCH = Path(__file__).resolve().parents[1] / "build" / "mobile-contract"
MOBILE_SRC = ROOT / "frontend/src/mobile"


def oversized_answer(phone: Phone) -> tuple[int, str]:
    """报一个超过上限的 Content-Length、正文一个字节都不发，看门怎么答。

    为什么不用真正文：电脑答 413 的时候不会先把正文读干，还趴在 socket 上写的客户端
    收到的是"连接被重置"（实测 `ConnectionAbortedError: 10053`），不是那句人话。
    这条路手机自己走不到——`photo.ts` 在出门前就把压不下的图丢了，所以这里量的是门框，
    不去复现那条走廊。
    """
    connection = http.client.HTTPSConnection(
        "127.0.0.1", phone.port, context=phone.context, timeout=10
    )
    connection.putrequest("POST", "/rpc/chat_ask")
    connection.putheader("Content-Type", "application/json")
    connection.putheader("Content-Length", str(MAX_BODY_BYTES + 8192))
    connection.putheader("Authorization", f"Bearer {phone.token}")
    connection.endheaders()
    response = connection.getresponse()
    status, text = response.status, response.read()
    connection.close()
    body = json.loads(text or b"{}")
    return status, str(body.get("error") or "")


def tiny_jpeg() -> str:
    """一张真的、能解码的 JPEG，形状和手机 `photo.ts` 发出去的那份一模一样。

    现场生成而不是抄一串 base64 常量：抄的"图"解码不了的时候，测到的是校验逻辑，
    不是"她收不收得下这张图"。
    """
    import base64
    from io import BytesIO

    from PIL import Image

    box = BytesIO()
    Image.new("RGB", (24, 24), (12, 34, 56)).save(box, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(box.getvalue()).decode()


def read_keys() -> dict[str, list[str]]:
    """手机界面真正读的键 —— 从 `types.ts` 的 READ_KEYS 读，不在这里再抄一份。

    抄一份的后果已经中过一次：`chat_messages` 那边写的是 `content`，这边手抄成 `text`，
    自检照样绿，装机翻开历史会话才发现每条气泡都是空的。
    """
    source = (MOBILE_SRC / "types.ts").read_text(encoding="utf-8")
    block = source.split("READ_KEYS", 1)[1]
    table: dict[str, list[str]] = {}
    for name, keys in re.findall(r"(\w+):\s*\[([^\]]*)\]", block):
        table[name] = re.findall(r"'([^']+)'", keys)
    assert table, "types.ts 里没有 READ_KEYS，这份检查就没有意义了"
    return table


def _real_model_choices_keys() -> list[str]:
    """真 `SettingsService.model_choices()` 的顶层键集 —— 用它给替身当尺子。

    用真服务跑一次，而不是在这里再抄一份键名：抄一份就等于又养了一个会过期的
    替身，而这份自检要抓的正是"两边不同步"。依赖全是最小假件（内存偏好 +
    记录用的假 LlmService + 一段配置），不联网、不读用户目录。
    """
    from jarvis.app.preferences import Preferences
    from jarvis.app.settings_service import SettingsService
    from jarvis.config.schema import LlmSection, ProviderSection

    def section() -> LlmSection:
        provider = ProviderSection(
            name="deepseek",
            base_url="https://api.deepseek.com/v1",
            models=(ModelSpec(id="deepseek-chat"),),
            default_model="deepseek-chat",
            api_key_env="DEEPSEEK_API_KEY",
            cost_input_per_1m=0.0,
            cost_output_per_1m=0.0,
        )
        return LlmSection(
            default_provider="deepseek",
            timeout_seconds=30.0,
            max_retries=0,
            retry_backoff_seconds=0.0,
            providers={"deepseek": provider},
        )

    class _NoopLlm:
        def set_section_override(self, _section: LlmSection | None) -> None:
            """真服务 `start()` 会调它清缓存；这里只需要它存在且可调用。"""

    prefs = Preferences(SCRATCH / "model-choices-probe.json")
    service = SettingsService(
        prefs,
        _NoopLlm(),  # type: ignore[arg-type]
        section,
        environ={},
        persist_env=lambda _name, _value: True,
    )
    return sorted(service.model_choices())


class Phone:
    """一个不带证书的 HTTPS 客户端：指纹自己比，比不过就不发第二个字节。"""

    def __init__(self, port: int, pin: str) -> None:
        self.port = port
        self.pin = pin.lower()
        self.token = ""
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # 证书链在这一层没有意义（自签、也没有 CA 可验），身份由下面的指纹比对负责。
        self.context.check_hostname = False
        self.context.verify_mode = ssl.CERT_NONE

    def call(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """一次请求。指纹在建立连接时就比掉，比不过连请求都不会发出去。"""
        connection = http.client.HTTPSConnection(
            "127.0.0.1", self.port, context=self.context, timeout=30
        )
        connection.connect()
        actual = fingerprint_of(connection.sock.getpeercert(True)).lower()
        if actual != self.pin:
            connection.close()
            raise AssertionError(f"指纹不一致：{actual} != {self.pin}")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = json.dumps(body).encode() if body else None
        connection.request("POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        status, text = response.status, response.read()
        connection.close()
        return status, json.loads(text or b"{}")


_SOURCES = ("MobileApp.vue", "MobilePanels.vue", "api.ts", "photo.ts")

_FILL = {
    "text": "合同自检",
    "when": "10 分钟后",
    "attachments": [],
}


def _args_of(inline: str, session_id: str) -> dict[str, Any]:
    """把 `pcCall` 第二参数的键抄成一份真参数。

    手机那边写的是 `{ text, attachments }`（简写）和 `{ session_id: id }`（带值）两种，
    所以按键名取值，不去解析值表达式。
    """
    shape: dict[str, Any] = {}
    for piece in inline.split(","):
        match = re.match(r"\s*(\w+)\s*(?::|$)", piece)
        if not match:
            continue
        key = match.group(1)
        if key == "session_id":
            shape[key] = session_id
        else:
            shape[key] = _FILL.get(key, "")
    return shape


def phone_calls(session_id: str) -> dict[str, dict[str, Any]]:
    """从手机源码里读出它会怎么调：{方法名: 参数形状}。

    参数名就是电脑那边方法的形参名：手机写 `sessionId`、电脑签名是 `session_id` 这种错，
    在真机上只会表现成"点了没反应"，而这套自检不花一次真模型调用就能撞出来。
    """
    sources = "".join((MOBILE_SRC / name).read_text(encoding="utf-8") for name in _SOURCES)
    calls: dict[str, dict[str, Any]] = {}
    for name, inline in re.findall(
        r"pcCall(?:<[^>]*>)?\(\s*[\w.]+\s*,\s*'([^']+)'(?:\s*,\s*\{(.*?)\})?", sources, re.S
    ):
        calls[name] = _args_of(inline or "", session_id)
    # 面板页把方法名藏在 grab('snapshot') 这一层后面，只找 pcCall 会漏掉一整页。
    for name in re.findall(r"grab(?:<[^>]*>)?\('(\w+)'\)", sources):
        calls.setdefault(name, {})
    assert calls, "手机源码里一个 RPC 都没写，这份检查就没有意义了"
    return calls


class _MemoryStub:
    def list_memories(self) -> list[dict[str, Any]]:
        return [{"id": 1, "kind": "preference", "text": "合同自检记的一条"}]


class _LedgerEntry:
    """台账里的一条。桥面是 `call.to_dict()`，不是字典，替身必须照这个形状来。"""

    def to_dict(self) -> dict[str, Any]:
        return {"at": 1.0, "tool": "system_report", "ok": True, "detail": "合同自检"}


class _LedgerStub:
    def recent(self) -> list[_LedgerEntry]:
        return [_LedgerEntry()]


class _KnowledgeStub:
    """知识库替身：手机「这台电脑」页要读文档表和技术计数。

    给一份真文档而不是空表 —— 空表下 `title` / `chunk_count` 这些**行内**键
    在 JSON 里根本不会出现，`READ_KEYS` 的核对会变成假绿（这正是上一轮
    `content` 写成 `text` 却没被发现的原因）。
    """

    def documents(self) -> list[dict[str, Any]]:
        return [
            {
                "doc_id": "contract-1",
                "source": "合同自检.md",
                "title": "合同自检",
                "media_type": "text/markdown",
                "size_bytes": 128,
                "chunk_count": 3,
                "ingested_at": "2026-10-03T00:00:00",
            }
        ]

    def stats(self) -> dict[str, Any]:
        return {
            "running": True,
            "enabled": True,
            "documents": 1,
            "chunks": 3,
            "vector_records": 3,
        }


class _SettingsStub:
    """设置替身：手机要读"电脑上现在用的是哪个模型"。

    只回变量的**名字**，不回值 —— 桥面本来就是这么做的，替身照着来。

    `speaks_typed` 是 `chat_ask` 路上要问的第二个问题（打完字要不要念出来）。
    这个替身一加进来，`_should_read_aloud()` 就从"没接设置所以直接 False"
    变成真的会去问它 —— 少这一个方法，整条 `chat_ask` 会以
    `AttributeError` 收场，而屏幕上看起来和"方法没接线"一模一样。

    形状**必须跟着 `SettingsService.model_choices()` 走**，不能抄旧版：
    上一轮服务商改成两级（providers[] → models[]）时这个替身还停在一层的
    `choices[]`，于是这份自检报出「chat_models 缺 providers, models」——
    一个看起来像代码坏了、实际是自检自己过期了的假红。真桥面返回什么，
    这里就返回什么（含 `thinking_levels` / `turns_bounds`，手机目前不读，
    但少写就意味着下次有人照着替身改真桥面）。
    """

    def model_choices(self) -> dict[str, Any]:
        return {
            "error": "",
            "providers": [
                {
                    "name": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "models": [
                        {"id": "deepseek-chat", "label": "deepseek-chat"},
                        {"id": "deepseek-reasoner", "label": "deepseek-reasoner"},
                    ],
                    "default_model": "deepseek-chat",
                    "key_set": True,
                    "key_variable": "DEEPSEEK_API_KEY",
                    "current": True,
                }
            ],
            "provider": "deepseek",
            "model": "deepseek-chat",
            "thinking": "medium",
            "turns": 8,
            "thinking_levels": ["off", "low", "medium", "high"],
            "turns_bounds": [0, 50],
        }

    def speaks_typed(self) -> bool:
        """自检里不念：念出来要占一个真语音通道，而这里要验的是"电脑答不答"。"""
        return False


class _InMemoryScheduler:
    """调度器的内存替身：只实现提醒用到的那五个动作。"""

    def __init__(self) -> None:
        self.jobs: dict[str, Any] = {}

    def add_job(self, spec: Any) -> Any:
        self.jobs[spec.job_id] = spec
        return spec

    def remove_job(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        spec = self.jobs.get(job_id)
        if spec is None:
            return False
        import dataclasses

        self.jobs[job_id] = dataclasses.replace(spec, enabled=enabled)
        return True

    def list_jobs(self) -> list[Any]:
        return list(self.jobs.values())

    def next_run_time(self, job_id: str) -> str:
        spec = self.jobs.get(job_id)
        return str(getattr(spec, "expression", "")) if spec else ""


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    for stale in (
        list(SCRATCH.glob("*.pem")) + list(SCRATCH.glob("*.json")) + list(SCRATCH.glob("*.db"))
    ):
        stale.unlink()
    sampler = SystemMonitor(top_processes=5)
    system = SystemService(lambda: sampler)
    system.start()
    # 模型用桩件：要验的是"手机点下去，电脑上那条链路答不答"，不是再花一次真调用。
    # 会话本用真库：手机的「会话」页读的就是它，没有它那一页永远是"对话历史未启用"，
    # 而这条错误在自检里长得和"方法没接线"一模一样。
    store = SqliteStore(SCRATCH / "contract.db")
    store.start()
    transcript = TranscriptService(store)
    transcript.start()
    chat = ChatService(lambda: FakeLlmClient(), transcript=transcript)
    chat.start()
    # 提醒用真服务 + 内存调度器：要验的是手机写的那句话能不能变成电脑上的一条排程。
    scheduler = _InMemoryScheduler()
    reminders = ReminderService(lambda: scheduler)
    bridge = HudBridge(
        system,
        DiskService(lambda: DiskCleaner(audit_log=SCRATCH / "audit.jsonl", sources={})),
        chat=chat,
        reminders=reminders,
        # 记忆和动作台账用替身：这里要验的是"手机读的那几个键电脑给不给"，
        # 真库里的记忆由桌面端自己的测试覆盖。知识库和设置同理。
        memory=_MemoryStub(),
        tools=_LedgerStub(),
        knowledge=_KnowledgeStub(),
        settings=_SettingsStub(),
    )
    section = MobileSection(
        enabled=False,
        bind_host="127.0.0.1",
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
    state = gateway.state()
    phone = Phone(int(state["port"]), str(state["fingerprint"]))

    ok = True

    def check(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and passed
        print(f"[{'过' if passed else '不过'}] {label}：{detail}")

    # 替身过期 = 这份自检自己坏了。服务商改成两级那次，`_SettingsStub` 没跟着改，
    # 于是它报「chat_models 缺 providers, models」——看起来像桥面断了，实际是
    # 替身还在一层的 `choices[]` 上。真服务的键集和替身的键集比一次，漂移当场红灯。
    real_keys = set(_real_model_choices_keys())
    stub_keys = set(_SettingsStub().model_choices())
    check(
        "设置替身与真 model_choices 同形（替身过期会让这份自检假红）",
        real_keys == stub_keys,
        (
            f"真 {sorted(real_keys)} vs 替身 {sorted(stub_keys)}"
            if real_keys != stub_keys
            else f"{len(real_keys)} 个键一致"
        ),
    )

    code = str(gateway.show_pair_code()["code"])
    status, paired = phone.call("/pair", {"code": code, "device": "合同自检"})
    phone.token = str(paired.get("token") or "")
    check("配对", status == 200 and bool(phone.token), f"{status}，token {len(phone.token)} 字符")

    # 先真问一句，电脑上才会有一场会话；空的历史读出来是 `[]`，
    # 那时候 `content` 这种**行内**键永远搜不到，自检会变成假绿。
    _, seeded = phone.call("/rpc/chat_ask", {"text": "合同自检", "attachments": []})
    _, board = phone.call("/rpc/chat_sessions", {})
    rows = board.get("sessions") or []
    session_id = str(rows[0]["id"]) if rows else ""
    check(
        "手机上问的一句进了电脑上的历史",
        bool(rows) and not seeded.get("error"),
        f"{len(rows)} 场会话，取第一场的 id={session_id[:12]}",
    )

    # 拍照问她：一张真 JPEG 走一遍这条路。电脑那边收不下图的时候，
    # 手机界面收到的必须是一句人话，不是一根转到天荒地老的进度条。
    status, photographed = phone.call(
        "/rpc/chat_ask",
        {
            "text": "这张图里是什么？",
            "attachments": [
                {
                    "name": "拍照-自检",
                    "kind": "image",
                    "mime": "image/jpeg",
                    "data": tiny_jpeg(),
                }
            ],
        },
    )
    check(
        "手机上拍的一张图能问到电脑上的她",
        status == 200 and not photographed.get("error"),
        f"{status} " + json.dumps(photographed, ensure_ascii=False)[:120],
    )
    # 答"ok"是桩件无条件给的，证明不了图真的到了模型那一路。图有没有被丢掉，
    # 看它有没有跟着那一轮存进电脑上的历史——那是清理之后剩下什么的唯一凭据。
    _, with_photo = phone.call("/rpc/chat_messages", {"session_id": session_id})
    carried = any(
        row.get("attachments")
        for row in (with_photo.get("messages") or [])
        if isinstance(row, dict)
    )
    check(
        "那张图真的跟着这一轮存进了电脑上的历史",
        carried,
        f"{len(with_photo.get('messages') or [])} 条，带图的：{carried}",
    )
    status, reason = oversized_answer(phone)
    check(
        "超过电脑上限的正文被挡在门口，还说得出人话",
        status == 413 and bool(reason),
        f"{status} {reason[:60]}",
    )
    check(
        "手机自己压完的图，不会撞上这道门",
        len(tiny_jpeg()) < MAX_IMAGE_DATA_CHARS,
        f"自检图 {len(tiny_jpeg())} 字符，单图上限 {MAX_IMAGE_DATA_CHARS}，"
        f"整封正文上限 {MAX_BODY_BYTES}",
    )

    keys = read_keys()
    for method, args in phone_calls(session_id).items():
        status, body = phone.call(f"/rpc/{method}", args)
        error = str(body.get("error") or "")
        refused = "未知方法" in error
        # 答了但带 error，对手机界面来说就是"点了没反应"，一样算没过。
        check(
            f"手机调用 {method}",
            status == 200 and not refused and not error,
            f"{status} " + json.dumps(body, ensure_ascii=False)[:120],
        )
        # 手机读的键电脑必须真的给：改个名字没通知手机，装机后就是空白面板。
        dumped = json.dumps(body, ensure_ascii=False)
        wanted = keys.get(method, [])
        if wanted:
            absent = [key for key in wanted if f'"{key}"' not in dumped]
            check(
                f"{method} 给了手机要读的键",
                not absent,
                "都在" if not absent else "缺：" + ", ".join(absent),
            )

    gateway.disable()
    print()
    print("结论：", "手机会发的每个方法电脑都答了" if ok else "有方法电脑没答，见上")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
