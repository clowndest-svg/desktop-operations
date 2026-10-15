"""Portable buyer installs never inherit an author's application environment."""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from pathlib import Path

_CREDENTIALS = frozenset(
    {
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "MOONSHOT_API_KEY",
        "QWENAI_API_KEY",
        "BAIDU_TRANSLATE_APPID",
        "BAIDU_TRANSLATE_KEY",
        "XIAOYE_IMAGE_API_KEY",
        "JARVIS_IMAGE_API_KEY",
    }
)


def prepare_buyer_environment(env: MutableMapping[str, str], install: Path) -> Path:
    """Set process-local paths before ML/WebView imports; do not edit the registry."""
    install = install.resolve()
    system_drive = env.get("SystemDrive", "C:").upper().rstrip("\\/")
    if install.drive.upper() in {"C:", system_drive}:
        raise OSError("请先完整解压到可写的非系统盘再启动，小夜不会在C盘下载模型或创建数据。")
    root = install / "用户数据"
    for name in list(env):
        if (
            name.upper().startswith("JARVIS_")
            or name.upper() in _CREDENTIALS
            or name.upper().endswith("_API_KEY")
        ):
            env.pop(name, None)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / "install.json"
    if not marker.exists():
        import uuid

        marker.write_text(
            json.dumps({"id": uuid.uuid4().hex, "edition": "buyer"}), encoding="utf-8"
        )
    env["JARVIS_HOME"] = str(root)
    from jarvis.config.credentials import read_credentials

    try:
        env.update(read_credentials(root))
    except (OSError, ValueError):
        # A portable folder moved to another PC/user must still boot without cloud keys.
        env["XIAOYE_CREDENTIAL_NOTICE"] = "凭据不能在此Windows用户解密，请在设置重新填写"
    for name, relative in {
        "TEMP": "cache/tmp",
        "TMP": "cache/tmp",
        "HF_HOME": "cache/huggingface",
        "HUGGINGFACE_HUB_CACHE": "cache/huggingface/hub",
        "TORCH_HOME": "cache/torch",
        "MODELSCOPE_CACHE": "cache/modelscope",
        "PIP_CACHE_DIR": "cache/pip",
        "XDG_CACHE_HOME": "cache",
        "NUMBA_CACHE_DIR": "cache/numba",
        "CUDA_CACHE_PATH": "cache/cuda",
        "WEBVIEW2_USER_DATA_FOLDER": "cache/webview",
    }.items():
        target = root / relative
        target.mkdir(parents=True, exist_ok=True)
        env[name] = str(target)
    return root
