# 项目技术栈与依赖清单（toolchain）

> 这份表里的**每一个版本号都是从这台机器上读出来的**，不是从文档里抄的。
> 读法：`.venv/Scripts/python.exe -c "import importlib.metadata as m; m.version('xxx')"`。
> 最后核对：2026-10-02。

## 1. 运行环境

| 项 | 值 | 说明 |
| --- | --- | --- |
| 操作系统 | **Windows 10 / 11 x64** | 只做过 Windows。托盘、WinRT 热键、WebView2、UAC 分档都是 Win32 直调 |
| Python | **3.13.14**（AMD64） | 打包用的就是它；换小版本要重测 funasr/torch |
| 打包产物 | PyInstaller **onedir**，实测 **923 MB** | 不是单文件：onedir 启动快、杀软误报少 |
| Node（仅构建期） | 见 `frontend/package.json` | 运行时不需要 Node，产物是静态文件 |

## 2. 数据库与持久化

**只有一个数据库：SQLite**（项目自带的薄封装，没有 ORM）。

| 库/文件 | 位置（本机实测） | 装什么 |
| --- | --- | --- |
| `jarvis.db` | `E:\BianChengGongJu\JarvisData\database\jarvis.db` | 13 张表：`schema_migrations, vector_records, memory_records, memory_turns, kb_documents, kb_chunks, scheduler_jobs, scheduler_runs, workflow_runs, llm_usage, chat_sessions, chat_messages` |
| `preferences.json` | `…\JarvisData\preferences.json` | 人改的偏好：窗口位置、音色、权限档位、宠物开关。**API Key 永不进这里** |
| `config.yaml` | `…\JarvisData\config\config.yaml` | 配置。密钥只写**变量名**（`api_key_env: QWENAI_API_KEY`） |
| 审计 JSONL | `…\JarvisData\audit\*.jsonl` | 删除、命令执行、结束进程三类，各自一份，**执行前先落盘** |
| 日志 | `…\JarvisData\logs\jarvis.log` | 滚动日志 |

- 驱动：**标准库 `sqlite3`**，WAL 模式 + `busy_timeout=5000`，没有 `sqlitedict`（实测未安装）。
- 迁移：代码内声明式 migration（`schema_migrations` 记版本），加字段不需要人工跑 SQL。
- 向量检索：**没有引向量数据库**。`vector_records` 表 + 自带 hashing 编码器，512 维，
  纯 Python 算相似度。够用是因为语料是个人的几百条，不是几百万。

## 3. AI 大模型（文本）

**接入方式：OpenAI 兼容 `/chat/completions`**，一个 provider 一段 YAML，加厂商不改代码。

内置 provider（`jarvis/config/defaults.yaml`，全是厂商公网端点，**不含任何密钥**）：

| provider | base_url | 默认模型 | 环境变量 |
| --- | --- | --- | --- |
| deepseek | `https://api.deepseek.com/v1` | deepseek-chat | `DEEPSEEK_API_KEY` |
| openai | `https://api.openai.com/v1` | gpt-4o-mini | `OPENAI_API_KEY` |
| kimi | `https://api.moonshot.cn/v1` | moonshot-v1-8k | `MOONSHOT_API_KEY` |
| qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-plus | `DASHSCOPE_API_KEY` |

- 界面上可以**自己加任意多个**模型条目，每个条目一条环境变量名。
- **思考链**：实测当前使用的网关支持 `enable_thinking` + `thinking_budget`，
  返回 `message.reasoning_content` 与 `usage.completion_tokens_details.reasoning_tokens`。
  （代码目前还没发这两个字段，见任务 #91/#92。）
- 编排：**LangGraph 1.2.10**（语音链路的状态图：VAD → ASR → agent → TTS，带打断）。
- 成本核算：`llm_usage` 表按 provider 记 token，含**缓存命中**（prompt cache）与自算金额。

## 4. 语音（全部离线，除 TTS 合成）

| 环节 | 用什么 | 版本/模型 | 是否联网 |
| --- | --- | --- | --- |
| 语音活动检测 VAD | **Silero VAD** | `silero-vad==6.2.1` | 否 |
| 识别 ASR | **FunASR / SenseVoice** | `funasr==1.3.30`，模型 `iic/SenseVoiceSmall` | 首次下载权重 |
| 唤醒词 | 能量 + 拼音滑窗匹配（带**同音变体**表） | 纯 Python，无模型 | 否 |
| 合成 TTS | **edge-tts** | `edge-tts==7.2.8` | **是**（微软在线合成，免费无 key） |
| 可选离线 TTS | CosyVoice2-0.5B | 配置里有，默认不启用 | 否 |
| 音频 I/O | `sounddevice==0.5.5` + `soundfile==0.14.0` | 采集 16k / 播放 24k | — |
| 张量后端 | **torch 2.13.0** + `modelscope==1.39.0` | 模型缓存在 `…\JarvisData\models`，实测 **1.7 GB** | — |

> 卖给别人时必须讲清楚：**edge-tts 是联网的**（把文字送到微软的合成服务）。
> 要完全离线就得切 CosyVoice，代价是模型体积和首次下载。

## 5. 桌面与界面

| 层 | 用什么 | 版本 |
| --- | --- | --- |
| 桌面壳 | **pywebview**（EdgeChromium/WebView2） | `pywebview==6.2.1` |
| 前端 | **Vue 3.5 + Pinia 4 + Vite（rolldown）+ TypeScript** | vue `^3.5.42`, pinia `^4.0.3` |
| 图表 | **ECharts 6** | `echarts ^6.1.0` |
| 3D 人物 | **three.js 0.186 + @pixiv/three-vrm 3.5.5**（VRM 模型，赛博线条渲染） | — |
| 托盘 | **pystray + Pillow** | `pystray 0.19.5`, `pillow 12.3.0` |
| 桌面控制 | **pyautogui** | `pyautogui 0.9.54`（本轮才补进 `[desktop]`，之前缺它导致按钮无效） |
| 系统监控 | **psutil** | `psutil 7.2.2` |
| 定时任务 | **APScheduler**（BackgroundScheduler + SQLite 存任务） | `3.11.3` |
| 宠物镂空 | **Win32 直调**：自建 `WS_EX_LAYERED` 窗口 + `UpdateLayeredWindow`，吃页面推来的 PNG 帧 | numpy 2.4.6 做预乘 alpha |
| 全局热键 | **Win32 `RegisterHotKey`**（不是键盘钩子） | ctypes |

## 6. 工程与门禁

| 用途 | 工具 | 版本 | 门禁命令 |
| --- | --- | --- | --- |
| 风格/静态检查 | ruff | 0.16.0 | `ruff check jarvis tests scripts` |
| 格式化 | black | 26.5.1 | `black --line-length 100` |
| 类型 | **mypy（strict）** | 2.3.0 | `mypy jarvis tests`（260 文件） |
| 测试 | pytest | 9.1.1 | `pytest`（**1696 passed / 4 skipped**） |
| 前端类型 | vue-tsc | — | `npx vue-tsc -b` |
| 前端构建 | vite | — | `npm run build` |
| 打包 | PyInstaller | 6.22.3 | `python -m PyInstaller packaging/jarvis.spec` |
| 架构约束 | 自建测试 | — | `tests/test_architecture_layers.py`（L0→L5 依赖表，机器强制） |

## 7. 明确没用的东西（避免误会）

- 没有 Web 后端、没有 FastAPI/Flask、没有 Docker；界面是本机回环静态服务 + JS 桥。
- 没有 ORM、没有 Postgres/MySQL、没有 Redis、没有向量数据库。
- 没有 Electron、没有 Qt（早期用过 PySide6，已删除）。
- 没有装 playwright（浏览器自动化在 `automation` extra 里，默认不装）。
- 没有键盘钩子、没有后台常驻服务（只有托盘进程）、没有遥测上报。
