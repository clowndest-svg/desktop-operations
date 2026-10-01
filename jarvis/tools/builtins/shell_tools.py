"""The two ways this assistant can reach a command line, and why there are two.

``run_shell`` is the **configuration's** answer: it needs ``tools.allow_shell`` true
*and* a call that arrives with ``confirmed=True``. The first says "this machine may
run commands"; the second says "a human approved this particular command". A model
can produce the first by writing a config file; it cannot produce the second, because
``confirmed`` is only ever set by the layer that talked to a person. It runs through
the platform shell, so pipes and redirection work — that is the feature, and it is
also why it stays off by default.

``run_powershell`` is the **operator's** answer, set from the window: a four-level
switch (关闭 / 演练 / 当前用户 / 管理员) held by :class:`jarvis.app.command_access`.
It does not consult ``allow_shell`` or ``confirmed`` — the level *is* the
authorisation, it is changed by a hand and not by a model, and it is visible on the
window. Every run is written to the command audit before it happens.

Both paths hand ``subprocess`` an explicit argv rather than a string plus
``shell=True``: the script travels as one argument (base64'd UTF-16, which is the
only quoting-free channel PowerShell offers), so nothing in the script can break out
into the argument list.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Protocol

from jarvis.core.constants import DEFAULT_ENCODING
from jarvis.core.exceptions import ToolExecutionError
from jarvis.tools.types import (
    PERMISSION_SHELL,
    RiskLevel,
    ToolHandler,
    ToolSpec,
    integer_property,
    object_schema,
    string_property,
)

logger = logging.getLogger("jarvis.tools.builtins.shell_tools")

POWERSHELL: Final[str] = "powershell.exe"
"""The desktop PowerShell, not ``pwsh``: PowerShell 7 is not a guarantee, 5.1 is."""

GATE_OFF: Final[str] = "off"
GATE_REHEARSAL: Final[str] = "rehearsal"
GATE_ADMIN: Final[str] = "admin"

MAX_TIMEOUT_SECONDS: Final[int] = 120
"""Upper bound on how long a command may run.

Structural bound: a tool call sits between the user and their answer, so a
command that outlives a two-minute ceiling is not going to be part of a
conversation anyway.
"""

MAX_OUTPUT_CHARS: Final[int] = 20_000
"""Output cap before the registry's own ``max_result_chars`` truncation.

Kept here as well because reading 200 MB of command output into memory to then
throw most of it away is a way to run out of memory.
"""

_RUN_SHELL = ToolSpec(
    name="run_shell",
    description=(
        "在本机执行一条命令行指令并返回输出。属于危险操作，需要用户确认；"
        "配置 tools.allow_shell 必须为 true。"
    ),
    parameters=object_schema(
        {
            "command": string_property("要执行的命令，例如 dir 或 git status"),
            "timeout_seconds": integer_property("超时秒数", default=30, minimum=1),
        },
        required=["command"],
    ),
    risk=RiskLevel.DANGEROUS,
    permissions=frozenset({PERMISSION_SHELL}),
)


def _run_shell() -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("command")
        if not isinstance(raw, str) or not raw.strip():
            raise ToolExecutionError("缺少要执行的命令")
        command = raw.strip()
        raw_timeout = arguments.get("timeout_seconds")
        timeout = (
            min(raw_timeout, MAX_TIMEOUT_SECONDS)
            if isinstance(raw_timeout, int) and raw_timeout > 0
            else 30
        )
        logger.warning("running shell command: %s", command)
        try:
            completed = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError(f"命令超过 {timeout} 秒仍未结束，已终止") from exc
        except OSError as exc:
            raise ToolExecutionError(f"无法执行命令：{exc}") from exc

        stdout = completed.stdout.decode(DEFAULT_ENCODING, errors="replace")
        stderr = completed.stderr.decode(DEFAULT_ENCODING, errors="replace")
        parts = [f"退出码：{completed.returncode}"]
        if stdout.strip():
            parts.append(f"标准输出：\n{stdout[:MAX_OUTPUT_CHARS]}")
        if stderr.strip():
            parts.append(f"标准错误：\n{stderr[:MAX_OUTPUT_CHARS]}")
        if not stdout.strip() and not stderr.strip():
            parts.append("（命令没有产生输出）")
        return "\n".join(parts)

    return handler


def build(*, allow_shell: bool = True) -> list[tuple[ToolSpec, ToolHandler]]:
    """Return the tools this module contributes.

    ``allow_shell=False`` leaves ``run_shell`` out rather than registering it to
    refuse: the policy would say no on every call, and a model that keeps finding
    a locked door stops trying the one that is open.
    """
    return [(_RUN_SHELL, _run_shell())] if allow_shell else []


# ---------------------------------------------------------------------------
# The operator-authorised path: run_powershell
# ---------------------------------------------------------------------------


class ShellGate(Protocol):
    """What the tool asks of :class:`jarvis.app.command_access.CommandAccess`.

    Declared structurally because ``tools`` may not import ``app`` -- the same seam
    ``computer_tools`` uses for :class:`~jarvis.tools.builtins.computer_tools.DesktopControl`.
    """

    def mode(self) -> str:
        """``off`` / ``rehearsal`` / ``user`` / ``admin``, as the operator left it."""
        ...

    def audit(
        self,
        *,
        mode: str,
        script: str,
        exit_code: int | None,
        seconds: float | None,
        note: str,
    ) -> None:
        """Write one command-audit line. Called before and after every run."""
        ...


def _encoded(script: str) -> str:
    """PowerShell's ``-EncodedCommand`` form: UTF-16LE, base64.

    The only quoting-free channel there is. Passing a script through ``-Command``
    means the argument survives cmd, then PowerShell's own parser, then the
    operator's profile -- three places a ``"`` or a ``$`` can change what runs.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _single(value: str) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


def _argv(script: str) -> list[str]:
    return [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(script)]


def _elevated_wrapper(script: str, result: Path) -> str:
    """The script an elevated PowerShell runs: the user's, with its output to a file.

    A pipe cannot cross the integrity boundary -- an elevated child cannot inherit a
    handle from the medium-integrity parent that started it -- so the file *is* the
    channel. The trailer line is the exit code, and it is a line in a file rather
    than a process status for the same reason.
    """
    return (
        "$ErrorActionPreference = 'Continue'\n"
        f"$out = {_single(str(result))}\n"
        "& {\n"
        f"{script}\n"
        f"}} *> $out\n"
        'Add-Content -LiteralPath $out -Value ("__exit__:" + $LASTEXITCODE)\n'
    )


def _launcher_script(script: str, result: Path) -> str:
    """``Start-Process -Verb RunAs``: this is what makes Windows show a UAC prompt."""
    inner = _encoded(_elevated_wrapper(script, result))
    return (
        f"$p = Start-Process -FilePath {_single(POWERSHELL)} -ArgumentList "
        f"{_single('-NoProfile')},{_single('-NonInteractive')},{_single('-EncodedCommand')},"
        f"{_single(inner)} "
        "-Verb RunAs -Wait -PassThru -WindowStyle Hidden\n"
        "if ($p) { $p.WaitForExit() }\n"
    )


def _read_result(result: Path) -> tuple[str, int | None]:
    """The output file an elevated run left behind, split into text and exit code."""
    try:
        body = result.read_text(encoding=DEFAULT_ENCODING, errors="replace")
    except OSError:
        return "", None
    code: int | None = None
    kept: list[str] = []
    for line in body.splitlines():
        if line.startswith("__exit__:"):
            try:
                code = int(line.split(":", 1)[1].strip())
            except ValueError:
                code = None
            continue
        kept.append(line)
    return "\n".join(kept), code


def _run_plain(script: str, timeout: int) -> tuple[int, str, str]:
    completed = subprocess.run(
        _argv(script),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return (
        completed.returncode,
        completed.stdout.decode(DEFAULT_ENCODING, errors="replace"),
        completed.stderr.decode(DEFAULT_ENCODING, errors="replace"),
    )


def _run_elevated(script: str, timeout: int) -> tuple[int | None, str, str]:
    """Ask Windows for admin, wait for the human, read the result file.

    Returns ``None`` for the exit code when the elevated run did not report one --
    which includes the case that matters most: the operator clicked 取消 on the UAC
    prompt, and nothing ran at all.

    The result file lives in a directory this function owns and removes: an elevated
    child writes it, so it cannot be a handle inherited from here (that is exactly
    what the integrity boundary refuses), and a temp folder is the one place a file
    the assistant leaves behind can be found and deleted again without a second
    opinion.
    """
    with tempfile.TemporaryDirectory(prefix="xiaoye-cmd-") as folder:
        result = Path(folder) / "output.txt"
        completed = subprocess.run(
            _argv(_launcher_script(script, result)),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout = completed.stdout.decode(DEFAULT_ENCODING, errors="replace")
        stderr = completed.stderr.decode(DEFAULT_ENCODING, errors="replace")
        if completed.returncode != 0:
            # The outer launcher failed, which means the elevated child never ran:
            # a declined UAC prompt, or a machine where the consent dialog is
            # policy-disabled. Saying "退出码 1" alone would leave the model to
            # guess whether the script ran and failed, so the sentence is the answer.
            detail = stderr.strip() or stdout.strip()
            head = "提权窗口没有打开（UAC 被取消或被拒绝），这条命令没有执行。"
            return completed.returncode, head, detail
        body, code = _read_result(result)
        return code, body, stderr


def _truncate(text: str) -> str:
    return text[:MAX_OUTPUT_CHARS]


def _answer(code: int | None, stdout: str, stderr: str, *, elevated: bool) -> str:
    parts = [f"退出码：{'未知' if code is None else code}"]
    if stdout.strip():
        parts.append(f"标准输出：\n{_truncate(stdout)}")
    if stderr.strip():
        parts.append(f"标准错误：\n{_truncate(stderr)}")
    if not stdout.strip() and not stderr.strip():
        parts.append("（命令没有产生输出）")
    if elevated:
        parts.append("（管理员运行：输出经结果文件回读，退出码只在命令自己调用外部程序时可靠）")
    return "\n".join(parts)


_RUN_POWERSHELL = ToolSpec(
    name="run_powershell",
    description=(
        "用 PowerShell 执行一段脚本并返回输出。标准输出和标准错误都会分别交给你，"
        "所以不要在脚本里写 2>&1：那会让 PowerShell 把结果包成 CLIXML 还给你。"
        "权限由用户在界面上的「命令行」档位决定："
        "关闭=问不到，演练=只返回本来会执行什么，当前用户=真的执行，管理员=每条都弹 UAC。"
        "超时上限 120 秒。"
    ),
    parameters=object_schema(
        {
            "script": string_property(
                "要交给 PowerShell 的脚本，例如 Get-Volume | Sort-Object HealthStatus"
            ),
            "timeout_seconds": integer_property("超时秒数", default=30, minimum=1),
        },
        required=["script"],
    ),
    # CAUTION, not DANGEROUS: the DANGEROUS path asks for a per-call ``confirmed``
    # that this surface has no UI for, and the authorisation that *does* exist is the
    # level a person set and can see. Marking it DANGEROUS would make every call fail
    # a gate nobody can satisfy, which reads as a broken tool rather than a policy.
    risk=RiskLevel.CAUTION,
    permissions=frozenset(),
)


def _timeout_of(arguments: Mapping[str, object]) -> int:
    raw = arguments.get("timeout_seconds")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return min(raw, MAX_TIMEOUT_SECONDS)
    return 30


def _run_powershell(gate_provider: Callable[[], ShellGate | None]) -> ToolHandler:
    def handler(arguments: Mapping[str, object]) -> str:
        raw = arguments.get("script")
        if not isinstance(raw, str) or not raw.strip():
            raise ToolExecutionError("缺少要执行的脚本")
        script = raw.strip()
        timeout = _timeout_of(arguments)
        gate = gate_provider()
        mode = gate.mode() if gate is not None else GATE_OFF
        if mode == GATE_OFF:
            return (
                "命令行能力已关闭。要开：窗口右上角「控制」→「命令行」→ 选「演练」或「当前用户」。"
                "这一条没有执行。"
            )
        if mode == GATE_REHEARSAL:
            argv = " ".join(_argv(script))
            return "演练模式：下面这条本来会执行，但一条都没跑。\n" f"$ {argv[:MAX_OUTPUT_CHARS]}"
        elevated = mode == GATE_ADMIN
        started = time.monotonic()
        logger.warning("running powershell (mode=%s): %s", mode, script[:400])
        if gate is not None:
            gate.audit(mode=mode, script=script, exit_code=None, seconds=None, note="开始")
        try:
            if elevated:
                code, stdout, stderr = _run_elevated(script, timeout)
            else:
                value, stdout, stderr = _run_plain(script, timeout)
                code = value
        except subprocess.TimeoutExpired as exc:
            elapsed = round(time.monotonic() - started, 2)
            if gate is not None:
                gate.audit(mode=mode, script=script, exit_code=None, seconds=elapsed, note="超时")
            raise ToolExecutionError(f"脚本超过 {timeout} 秒仍未结束，已终止") from exc
        except OSError as exc:
            note = f"起不来：{exc}"[:80]
            if gate is not None:
                gate.audit(mode=mode, script=script, exit_code=None, seconds=None, note=note)
            raise ToolExecutionError(f"无法启动 PowerShell：{exc}") from exc
        elapsed = round(time.monotonic() - started, 2)
        if gate is not None:
            gate.audit(mode=mode, script=script, exit_code=code, seconds=elapsed, note="完成")
        return _answer(code, stdout, stderr, elevated=elevated)

    return handler


def build_with_gate(
    gate_provider: Callable[[], ShellGate | None],
    *,
    allow_shell: bool = True,
) -> list[tuple[ToolSpec, ToolHandler]]:
    """The shell tools, including the operator-authorised one.

    ``build()`` stays the no-argument form for callers that do not have a gate; a
    desktop build always has one, and without it ``run_powershell`` is simply not
    advertised.
    """
    tools: list[tuple[ToolSpec, ToolHandler]] = [(_RUN_POWERSHELL, _run_powershell(gate_provider))]
    if allow_shell:
        tools.insert(0, (_RUN_SHELL, _run_shell()))
    return tools


__all__ = ["build", "build_with_gate"]
