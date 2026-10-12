"""一个**真**的 stdio MCP 服务器，用来测真连接。

协议按 MCP 的形状说：newline 分隔的 JSON-RPC —— `initialize` →
`notifications/initialized` → `tools/list` → `tools/call`。

为什么不拿假对象测这一条：`jarvis/mcp/` 那套（transport / client / service）和这个进程之间
是**行分隔的 JSON 帧 + 子进程 stdin/stdout**，任何手搓的 dict 假体都会跳过真正会坏的那些东西
（换行编码、stderr 堵塞、响应乱序、超时、进程起不来）。这同一个坑在提醒那条链上踩过一次：
测试用 dict 抄了一遍接线，真机上提醒是哑的。

它会做两件可被外部核对的事：

- 把 `XIAOYE_PROBE_MARK` 指到的文件写一行 —— **证明这个进程真的起来了**，不是测试自己骗自己；
- 把 `XIAOYE_PROBE_ENV` 的值原样吐回工具结果 —— 证明配置里那个 `env_from` 真的传进了子进程。
"""

from __future__ import annotations

import json
import os
import sys


def _send(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(rid: object, result: object) -> None:
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def main() -> int:
    mark = os.environ.get("XIAOYE_PROBE_MARK", "")
    if mark:
        try:
            with open(mark, "a", encoding="utf-8") as handle:
                handle.write("started\n")
        except OSError:
            pass
    sys.stderr.write("probe server 上线了（这行走 stderr，客户端要能一边读一边不堵）\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = str(message.get("method") or "")
        rid = message.get("id")
        if method == "initialize":
            _result(
                rid,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "xiaoye-probe", "version": "1.0"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _result(
                rid,
                {
                    "tools": [
                        {
                            "name": "probe_echo",
                            "description": "回显一句话，并把这台机器上的探测变量带回来",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        },
                        {
                            "name": "坏 名字.带点和空格",
                            "description": "名字不合规范：客户端必须把它消毒而不是注册失败",
                            "inputSchema": {"type": "object", "properties": {}},
                        },
                    ]
                },
            )
        elif method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if name == "probe_echo":
                text = (
                    f"echo:{arguments.get('text')}"
                    f"|env:{os.environ.get('XIAOYE_PROBE_ENV', '缺')}"
                )
                _result(rid, {"content": [{"type": "text", "text": text}], "isError": False})
            else:
                _result(rid, {"content": [{"type": "text", "text": "未知工具"}], "isError": True})
        elif rid is not None:
            _result(rid, {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
