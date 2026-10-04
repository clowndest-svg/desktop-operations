"""一台假的 OpenAI 兼容模型服务器，专门用来在**没有真 Key** 的情况下验流式。

为什么要有它：流式那套东西的失败方式全是"看起来正常"——半个汉字变 、
一帧被切成两半就少一句、tool_calls 的参数只拼进来一半就去动手机。
这些拿真模型测要花钱、要人配合看屏幕，而且**复现不了**（分帧时机每次不同）。
这里把分帧时机做成确定的：

* 故意把一个汉字的三个字节**拆成两次 write**，中间不合并 —— 逼 Java 那侧的
  UTF-8 累加器表态。它要是直接 `new String(bytes, UTF_8)`，屏幕上就会出现 。
* `data:` 行也拆成两次发，逼前端的 SSE 解析器表态。
* 问到"音量"就吐一个 `tool_calls`，参数一个字符一帧 —— 验工具环 + 确认闸
  走的是真 socket，不是我在 Node 里自说自话。
* 问到"慢"就一个字等 0.6 秒 —— 给"停止"按钮留出可点的时间窗。

证书走的是电脑那台端点**同一套**代码（`ensure_certificate`），
所以手机上的指纹固定那条路也是真的。

跑法：`.venv/Scripts/python.exe scripts/fake_model_server.py`
然后手机上「设置」里 Base URL 填它打出来的那行、模型名随便填、Key 填 `test`。
"""

from __future__ import annotations

import json
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.ui.lan_server import ensure_certificate, fingerprint_of, lan_addresses  # noqa: E402

SCRATCH = ROOT / "build" / "fake-model"
PORT = 8790
# 一个汉字三个字节：把第一个字的中间切开，最能暴露"按字节块解码"的错。
SLOW_SENTENCE = "流量是一帧一帧拼回来的，不是等你说完了才一次贴上来。"


def frame(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def delta(text: str) -> bytes:
    return frame({"choices": [{"delta": {"content": text}}]})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # 不写 Content-Length：靠关连接收尾，最接近模型端点的行为

    def log_message(self, fmt: str, *args: Any) -> None:
        print("  " + (fmt % args), flush=True)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            asked = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_error(400, "不是 JSON")
            return
        history = asked.get("messages") or []
        text = ""
        for row in reversed(history):
            if row.get("role") != "user":
                continue
            content = row.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            break
        print(f"  问题：{text[:60]}", flush=True)
        if not asked.get("stream"):
            self._whole(text)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            if "音量" in text or "调低" in text:
                self._tool_stream()
            elif "慢" in text:
                self._slow_stream()
            else:
                self._byte_split_stream()
        except (BrokenPipeError, ConnectionResetError):
            # 手机上按了「停止」就会是这样：这是预期的结局，不是故障。
            print("  客户端中途走了（这就是「停止」生效）", flush=True)

    def _whole(self, text: str) -> None:
        answer = f"（整块回答）{text[:20]}"
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": answer}}]},
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _byte_split_stream(self) -> None:
        """逐字发，并且**真的把一个汉字的三个字节从中间切开**。

        切法不是猜的：先整帧编好字节，再定位这个字的字节起点，在它后面 1 字节处切。
        两次 write 之间 flush + 睡 120ms，TCP 不会替我们把它俩合回去。
        Java 那侧要是按块 `new String(bytes, UTF_8)`，屏幕上这里必然出现 。
        """
        for index, char in enumerate(SLOW_SENTENCE):
            payload = delta(char)
            if index % 3 == 1:
                start = payload.rindex(char.encode("utf-8"))
                self.wfile.write(payload[: start + 1])
                self.wfile.flush()
                time.sleep(0.12)
                self.wfile.write(payload[start + 1 :])
            else:
                self.wfile.write(payload)
            self.wfile.flush()
            time.sleep(0.04)
        self.wfile.write(frame({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _slow_stream(self) -> None:
        """一个字 0.6 秒：给"停止"留出可点的时间窗。"""
        for char in "这一句会一直说下去，说到你不想听为止。":
            self.wfile.write(delta(char))
            self.wfile.flush()
            time.sleep(0.6)

    def _tool_stream(self) -> None:
        """先说一句，再吐一个 tool_calls，参数一字符一帧。"""
        self.wfile.write(delta("好，我把音量调到四成。"))
        self.wfile.flush()
        time.sleep(0.1)
        pieces = [
            {"index": 0, "id": "call-fake-1", "function": {"name": "set_volume", "arguments": ""}},
            *[{"index": 0, "function": {"arguments": char}} for char in '{"percent":40}'],
        ]
        for piece in pieces:
            self.wfile.write(frame({"choices": [{"delta": {"tool_calls": [piece]}}]}))
            self.wfile.flush()
            time.sleep(0.02)
        self.wfile.write(frame({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def serve() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    key, cert, _ = ensure_certificate(SCRATCH, addresses=tuple(lan_addresses()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    host = (lan_addresses() or ("127.0.0.1",))[0]
    der = Path(cert).read_bytes()
    print(f"Base URL   https://{host}:{PORT}/v1", flush=True)
    print("证书指纹   " + fingerprint_of(der), flush=True)
    print("模型名     随便填（假服务器不看）", flush=True)
    print("API Key    test", flush=True)
    print()
    print("这台是**自签证书**：手机上「设置 → 证书指纹」那一栏必须填上面那串，", flush=True)
    print("否则走系统信任链会直接拒——手机拒自签模型端点这件事，正是这台的用途之一。", flush=True)
    print("问「把音量调低」验工具环；问「慢慢说」验停止；其他验逐字和半个汉字。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
