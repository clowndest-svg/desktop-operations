"""Files a complete portable local-feature delivery must actually contain."""

from pathlib import Path

FULL_COMPONENTS = {
    "离线聊天运行时": "_internal/jarvis/vendor/llama/llama-server.exe",
    "网页运行库": "运行库/WebView2/msedgewebview2.exe",
    "生图视频解释器": "本地引擎/python/python.exe",
    "生图视频依赖": "本地引擎/venv/Lib/site-packages/torch/__init__.py",
    "生图引擎": "本地引擎/ComfyUI/main.py",
    "生图模型": "本地引擎/ComfyUI/models/checkpoints/v1-5-pruned-emaonly-fp16.safetensors",
    "视频模型": "本地引擎/ComfyUI/models/animatediff_models/mm_sd15_v3.safetensors",
    "视频节点": "本地引擎/ComfyUI/custom_nodes/ComfyUI-AnimateDiff-Evolved/__init__.py",
    "视频编码节点": "本地引擎/ComfyUI/custom_nodes/ComfyUI-VideoHelperSuite/__init__.py",
    "离线语音识别": "语音模型/SenseVoiceSmall/model.pt",
    "离线语音检测": "语音模型/fsmn-vad/model.pt",
}


def full_bundle_problems(app: Path) -> list[str]:
    errors = [
        f"完整买家包缺少{label}：{relative}"
        for label, relative in FULL_COMPONENTS.items()
        if not (app / relative).is_file()
    ]
    if not any((app / "模型").glob("*.gguf")):
        errors.append("完整买家包缺少离线聊天权重")
    return errors
