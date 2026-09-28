# JARVIS 架构与模块依赖规则

> 本文档是分层架构的**强制约束**。任何新代码引入的 import 必须符合下述依赖方向;
> 违反即视为架构腐化,评审时直接打回。规则本身如需调整,先改本文档再改代码。

## 1. 分层模型(依赖只能自上而下)

```
┌────────────────────────────────────────────────────────────┐
│  L5  表现层        ui/                                      │
├────────────────────────────────────────────────────────────┤
│  L4  应用层        app/(组合根·生命周期·用例编排)          │
├───��────────────────────────────────────────────────────────┤
│  L3  编排层        agent/  planner/  workflow/  scheduler/  │
├────────────────────────────────────────────────────────────┤
│  L2  领域能力层    memory/ knowledge/ tools/ plugins/ mcp/  │
│                    prompt/ vision/ computer/                │
├────────────────────────────────────────────────────────────┤
│  L1  基础设施层    llm/ database/ vector/ ocr/ browser/     │
│                    wakeword/ vad/ asr/ tts/ config/ logging/│
├────────────────────────────────────────────────────────────┤
│  L0  共享内核      core/(异常·Result·常量)                 │
└────────────────────────────────────────────────────────────┘
```

**总规则:**

1. 上层可以 import 下层;下层**永远不允许** import 上层。
2. 同层之间原则上互不依赖;确需协作时依赖对方 `__init__.py` 导出的公共接口,禁止深入内部模块。
3. `core` 只依赖标准库,不依赖任何第三方库和任何 jarvis 包。
4. 第三方 SDK(openai、faiss、playwright、paddleocr……)只允许出现在对应的 L1/L2 包内部,严禁泄漏到编排层以上(依赖倒置:上层只见抽象)。
5. `app` 是唯一的组合根:对象装配、生命周期编排只发生在这里。
6. `ui` 只与 `app` 暴露的服务接口交互,不包含业务逻辑,不直接触碰 L1/L2/L3。

## 2. 各包职责与允许依赖

| 包 | 层 | 职责 | 允许依赖 | 交付阶段 |
|---|---|---|---|---|
| `core` | L0 | 异常体系、Result、常量、跨层事件契约(`events.py`: `PipelineEvent`) | 仅标准库 | ✅ 2 |
| `config` | L1 | YAML 分层配置加载与校验 | core | ✅ 3 |
| `logging` | L1 | 控制台+滚动文件、结构化指标字段、降噪、崩溃钩子 | core, config | ✅ 4 |
| `llm` | L1 | OpenAI 兼容多供应商客户端、流式、token 统计 | core, config, logging, prompt | ✅ 5 |
| `audio` | L1 | 音频采集抽象:格式校验 / 帧重组 / AudioSource 协议(供 wakeword·vad·asr 共用) | core | ✅ 6 |
| `wakeword` | L1 | OpenWakeWord(默认·离线·免Key)/ Porcupine(可选·AccessKey)唤醒 | core, config, audio | ✅ 6 |
| `vad` | L1 | Silero VAD 流式断句(端点状态机 + 流式端点) | core, config, audio | ✅ 7 |
| `asr` | L1 | SenseVoice / FunASR / Whisper 流式识别 | core, config, vad | ✅ 8 |
| `tts` | L1 | CosyVoice(离线·ONNX)/ Edge-TTS(云端),边合成边播、Barge-In 钩子 | core, config | ✅ 9 |
| `database` | L1 | SQLite 仓储、迁移、缓存 | core, config | 11+ |
| `vector` | L1 | FAISS 索引管理与相似检索 | core, config | 11 |
| `ocr` | L1 | PaddleOCR 封装 | core, config | 13 |
| `browser` | L1 | Playwright 浏览器自动化 | core, config | 14 |
| `prompt` | L2 | 提示词模板管理(禁止散落硬编码) | core, config | 5+ |
| `memory` | L2 | 短/长期记忆、画像、压缩/召回/排序 | core, config, database, vector, llm | 11 |
| `knowledge` | L2 | RAG 知识库 | core, config, database, vector, llm | 11 |
| `tools` | L2 | Tool 框架、自动注册、内置工具、危险确认(可按需引用能力包的公共接口) | core, config | 12 |
| `vision` | L2 | 截图、屏幕/图像理解、UI 识别 | core, config, ocr, llm | 13 |
| `computer` | L2 | 鼠标键盘窗口控制(经安全策略) | core, config, vision, ocr | 14 |
| `mcp` | L2 | MCP 客户端、配置驱动的服务器注册、工具桥接 | core, config, tools | 15 |
| `plugins` | L2 | 插件发现/装载/热更新 | core, config, tools | 18 |
| `agent` | L3 | LangGraph 多 Agent 编排(11 类 Agent) | core, config, llm, prompt, planner, memory, knowledge, tools | ✅ 10 |
| `orchestration` | L3 | 多 Agent 调度 + 单一麦克风语音流水线(唤醒→VAD→ASR→LLM→TTS,Barge-In) | core, config, llm, asr, tts, vad, wakeword, audio, agent | ✅ 10 |
| `planner` | L3 | 任务分解、计划模型、重规划 | core, config, llm, prompt | 10 |
| `workflow` | L3 | 触发-条件-动作工作流引擎 | core, config, scheduler, tools, database | 16 |
| `scheduler` | L3 | 定时/事件触发,持久化可恢复 | core, config, database | 16 |
| `app` | L4 | 组合根、Application 生命周期 | 所有下层 | ✅ 1 |
| `ui` | L5 | 桌面壳:pywebview 承载 Vue3 HUD(纯表现,经 JS 桥调 app 服务) | core, config, app | 17 |
| `qqbot` | 侧挂 | QQ 官方机器人通道:直接问 LLM,不经 app/编排层 | core, config, llm | 独立产品 |

**本表由 `tests/test_architecture_layers.py` 机器校验**(AST 扫描 `jarvis/**/*.py` 的每一条
first-party import,含函数内的懒导入):

- 表中"允许依赖"逐包硬校验 —— 新增 import 若不在表内,测试即红;新增子包若未入表,同样即红。
- `jarvis/__main__.py` 是唯一豁免(组合根的职责就是跨层装配);`jarvis/__init__.py` 不豁免,它必须保持零 import。
- 规则 2 的后半句(同层协作只依赖对方 `__init__.py`)**暂未机器校验**:现存 17 条同层深导入
  (`asr`→`jarvis.vad.types`、`wakeword`→`jarvis.audio.source`,以及大部分指向 `jarvis.config.schema`)
  方向合法、改名重导出无收益,继续作为约定维持。

## 3. 横切关注点

- **异常**:一律派生自 `jarvis.core.exceptions.JarvisError`;子系统内部可细化,跨层只捕获 core 导出的类型。
- **预期失败**:跨层返回 `jarvis.core.result.Result[T, E]`,不用异常做控制流。
- **配置**:任何行为参数来自 `config`(阶段三);代码中出现魔法值视为违规。
- **日志**(✅ 阶段四交付):统一走标准 `logging`,logger 名一律以 `jarvis.` 开头(非 `jarvis.*` 命名空间会被按第三方降噪);`print` 禁止出现在库代码中;计费/延迟记账用 `jarvis.logging.metrics()` 附加 `extra` 字段。
- **安全**:危险操作(删除/CMD/PowerShell/格式化/批量删除)必须走确认策略;密钥只进加密存储,不进日志、不进配置明文。

## 4. 目录总览(阶段二定稿)

```
jarvis/
  core/  config/  logging/  llm/  audio/
  prompt/  wakeword/  vad/  asr/  tts/
  agent/  planner/  memory/  knowledge/  vector/
  tools/  vision/  ocr/  browser/  computer/
  mcp/  plugins/  workflow/  scheduler/
  database/  app/  ui/
tests/    docs/    scripts/    assets/    logs/
```

说明:规格目录中的 `logs/ database/` 顶层目录为**运行期产物目录**(已在 .gitignore
管控);`jarvis/database` 是持久化**代码包**,二者不冲突。运行期数据实际落盘路径由
阶段三配置系统统一决定。
