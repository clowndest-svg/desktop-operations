# JARVIS

一个面向 Windows 的语音优先、多 Agent 架构的 AI 助手(对标《钢铁侠》JARVIS)。

> 当前进度:**阶段十七 —— 桌面 HUD(pywebview + Vue3)已可用**;语音链路
> 唤醒→VAD→ASR→LLM→TTS 在**一台真机上用真模型跑通**(6 种音色/语速 24/24 命中唤醒、
> 磁盘扫描 3 秒 590 行)。注意口径:自动化测试跑的是 fake 引擎,真模型路径靠
> `scripts/verify_wake_words.py` 与 `docs/desktop-operations.md` 的实测清单保证,
> 不是靠 CI。共二十阶段,见 `docs/phases/`。
>
> 桌面端怎么装、怎么演示、出问题查哪里 → **[docs/desktop-operations.md](docs/desktop-operations.md)**

## 设计目标

- 全天后台运行、低资源占用
- 语音唤醒 + 全程流式语音对话(可打断)
- 多 Agent(LangGraph 调度)、Tool Calling、MCP
- 长期记忆(SQLite + FAISS)、视觉、Computer Use
- 插件化、可热更新、企业级架构(Clean Architecture / DDD / SOLID)

## 环境要求

- Windows 10/11
- Python **3.12+**

## 快速开始

```bash
# 1. 创建虚拟环境
python -m venv .venv

# 2. 安装(开发模式,含质量工具链)
.venv/Scripts/python -m pip install -e ".[dev]"

# 2b. 语音功能(唤醒词 / VAD / ASR / TTS)依赖原生二进制(PortAudio / ONNX Runtime),
#     默认不安装;启用语音前装此 extra,并通过配置显式开启(默认麦克风不抓):
.venv/Scripts/python -m pip install -e ".[voice]"

# 2c. 桌面 HUD(pywebview 壳 + WebView2 + 系统遥测采样)需要 pywebview 与 psutil,
#     默认不装;缺 psutil 只会让仪表盘显示"遥测读取失败",不影响窗口打开:
.venv/Scripts/python -m pip install -e ".[desktop]"

# 2d. 桌面界面是 Vue 构建产物,不入 git(哈希文件名每次变)。首次运行或打包前构建一次,
#     产物落在 jarvis/ui/web/(已在 package-data 里,会随 wheel 分发):
.venv/Scripts/python -m pip install -e ".[desktop]" && .venv/Scripts/python scripts/build_desktop.py
#     只想确认产物是否完整(例如打包前):加 --check

# 3. 运行
.venv/Scripts/python -m jarvis
.venv/Scripts/python -m jarvis --version
.venv/Scripts/python -m jarvis --config my.yaml   # 显式指定用户配置文件
.venv/Scripts/python -m jarvis --desktop          # 打开桌面聊天窗口(文字提问 + 语音回答)

# 4. 离线演示:用一段 16kHz/单声道/s16le 的 WAV 走通 VAD→ASR→LLM→TTS,
#    不占麦克风、不需要唤醒词,回复音频写到 --out(默认 <输入名>_reply.wav)
.venv/Scripts/python -m jarvis --wav assets/demo_question.wav --out assets/demo_answer.wav
.venv/Scripts/python -m jarvis --wav assets/demo_question.wav --skip-llm   # 只验识别+合成
```

## 配置

分层加载,优先级从低到高:

1. 内置默认值 `jarvis/config/defaults.yaml`(随包分发,含全部键与注释)
2. 用户文件 `%LOCALAPPDATA%\Jarvis\config\config.yaml`(或 `JARVIS_CONFIG` / `--config` 指定,只写想改的键)
3. 环境变量 `JARVIS_<SECTION>__<KEY>`,如 `JARVIS_LOGGING__LEVEL=DEBUG`

数据目录默认 `%LOCALAPPDATA%\Jarvis`(可用 `JARVIS_HOME` 重定向),布局:`config/ database/ logs/ models/ cache/`。

LLM:`llm.providers` 预置 deepseek / openai / kimi / qwen 四个 OpenAI 兼容端点,新增供应商纯 YAML 编辑即可。API Key **只**通过环境变量提供(如 `DEEPSEEK_API_KEY`),绝不写入配置文件;切换供应商:`JARVIS_LLM__DEFAULT_PROVIDER=kimi`。

## 质量门禁

所有代码提交前必须全部通过:

```bash
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m black --check .
.venv/Scripts/python -m mypy
.venv/Scripts/python -m pytest
```

## 项目结构(阶段二定稿)

```
jarvis/
  core/            L0 共享内核:异常体系 / Result / 常量(已实现)
  config/          L1 配置系统:分层 YAML + 类型化校验 + 数据目录(已实现)
  logging/         L1 日志系统:滚动文件 + 结构化指标 + 降噪 + 崩溃钩子(已实现)
  llm/             L1 LLM 访问层:OpenAI 兼容多供应商 + 流式 + 重试(已实现)
  audio/           L1 音频采集抽象:16kHz/单声道/s16le 帧流水线 + AudioSource 协议(已实现)
  app/             L4 组合根:Application 生命周期 + 离线演示入口 + 对话服务(供 UI 调用)(已实现)
  prompt/ vad/ asr/ tts/
  wakeword/        L1 唤醒词:OpenWakeWord(默认·离线·免Key)+ Porcupine(可选·AccessKey)
                   + asr(SenseVoice 转写后匹配关键词,中文唤醒走这条·需 orchestration)(已实现)
  vad/             L1 语音活动检测:Silero VAD(离线·TorchScript,模型随 wheel 分发无需下载)流式端点状态机(已实现)
  asr/             L1 语音识别:SenseVoice/FunASR(离线·ONNX)流式识别 + VAD→ASR 切片(已实现)
  tts/             L1 语音合成:Edge-TTS(默认·云端)/ CosyVoice(离线·需按官方指南手动安装);两引擎统一输出 pcm_s16le,边合成边播仅 CosyVoice 成立(已实现)
  agent/ planner/ memory/ knowledge/ vector/ database/
  tools/ vision/ ocr/ browser/ computer/ mcp/
  ui/              L5 桌面 HUD:pywebview 壳 + JS 桥(desktop.py)、语音状态桥(state_bridge.py)、Vue 构建产物(web/,不入 git)。原 PySide6 对话窗口已被 HUD 取代并删除(阶段 17)
  workflow/ scheduler/ plugins/        # 按阶段逐步实现
tests/             单元测试
docs/              architecture.md(模块依赖强制规则)+ phases/ 阶段报告
scripts/ assets/ logs/
```

模块依赖方向规则见 [docs/architecture.md](docs/architecture.md) —— 新增 import 必须符合分层约束。

## 开发原则

Clean Architecture · DDD · SOLID · DRY · KISS · 全类型注解 · 零硬编码 · 配置全 YAML
