# 阶段十：多 Agent 编排(LangGraph) + 唤醒→VAD→ASR→LLM→TTS 全链路

> 状态:✅ 完成(架构 + 全部门禁 + 确定性单测全绿)| 真实麦克风/模型推理待本机验证
> 门禁:Ruff / Black / MyPy strict / pytest
> 新增依赖:`langgraph>=0.2.0`(已固化 pyproject,并加 mypy overrides)

## 1. 交付物

```
jarvis/agent/                      多 Agent(L3,新建)
  __init__.py       导出 Agent / AgentContext / AgentResult / ConversationalAgent / Tool / ToolAgent
  types.py          Agent 协议(name/run) / AgentContext(frozen) / AgentResult(frozen: text/used_tools)
  conversational.py  ConversationalAgent(name="chat"):拼 system+history+user 调 LlmClient.complete
  tools.py          ToolAgent(name="tools"):内置真实工具 get_time(时钟注入)/ calculate(ast 白名单安全求值)
                     + _detect 关键词/符号路由(未命中返回空文本→供 router 回退 chat)

jarvis/orchestration/              编排(L3,新建)
  __init__.py       惰性导出 OrchestrationService / AgentGraph / OrchestrationSettings / PipelineEvent / VoicePipeline
  types.py          GraphState(TypedDict) / PipelineEvent(frozen: kind/text/detail)
  player.py         AudioPlayer 协议 / NullAudioPlayer(headless 测试) / SounddevicePlayer(PortAudio 输出)
  graph.py          AgentGraph:StateGraph(supervisor→chat/tools→supervisor/END)
                     supervisor 用 _SUPERVISOR_PROMPT 让 LLM 返回 chat/tools 一词路由;worker 无答案回退
  voice_pipeline.py  VoicePipeline:单 daemon 线程状态机 IDLE→LISTENING→PROCESSING
                     独占单一 AudioSource;内部驱动 WakeWordDetector + VoiceActivitySegmenter
                     (不启动独立 wakeword/vad loop);Barge-In 钩子 should_stop
  service.py        OrchestrationService(name="orchestration"):生命周期组件
                     校验 asr.running/tts.running;懒加载引擎与 AgentGraph;组合 VoicePipeline

jarvis/config/
  schema.py         += OrchestrationSection(enabled/barge_in/default_agent)+ AppConfig.orchestration + _ALLOWED
  defaults.yaml     += orchestration 节(默认 enabled=false, barge_in=true, default_agent=chat)
  __init__.py       += 导出 OrchestrationSection
jarvis/__main__.py   注册 OrchestrationService + 独立 WakeWordService + VadService(全部惰性 provider,互斥靠配置契约)
pyproject.toml       runtime deps += langgraph>=0.2.0;mypy overrides += langgraph/langgraph.*/langchain_core/*
                    ruff ignore += RUF001(中文全角标点是有意设计)
requirements.txt     同步 += langgraph
tests/
  test_config_orchestration.py(3)  test_agent.py(8)  test_orchestration_voice_pipeline.py(6)
  test_orchestration_service.py(4)  # 新增 21,全部确定性(无真实模型/麦克风/网络)
```

## 2. 架构决策

### 2.1 单一音频源原则(关键)
阶段六/七/八/九的 `WakeWordService` / `VadService` 各自独占麦克风 loop,**不能同时启用抢设备**。
阶段十新增统一 `OrchestrationService`,由它**独占单一 `AudioSource`**,内部直接驱动
`WakeWordDetector` 与 `VoiceActivitySegmenter`,**不启动独立 wakeword/vad loop**。

`__main__.py` 中三个服务全部注册,但每个都靠自身 `enabled` 标志自门控:
- 编排模式(orchestration.enabled=true):编排服务跑流水线;wakeword/vad 保持 disabled(不抢麦)。
- 独立模式(orchestration.enabled=false):编排服务 no-op;wakeword/vad 按各自 enabled 跑。
互斥性由**配置契约**保证(编排模式须把 wakeword/vad 留 disabled),组合根不读配置、全惰性 provider。

### 2.2 语音状态机
```
IDLE        detector.feed(chunk) → 命中唤醒词 → reset segmenter + clear buffer → LISTENING
LISTENING   buffer.extend(chunk);segmenter.feed → SPEECH_END → 切片 ASR → PROCESSING
PROCESSING  ASR recognize → AgentGraph.run → 追加 history → TTS synthesize(边合成边播)→ 回到 LISTENING
```
进入 LISTENING 时 `segmenter.reset()` + `buffer.clear()` 同步执行,保证 `SpeechSegment`
样本索引与本轮累积缓冲区对齐,ASR 切片精确。

### 2.3 Barge-In(打断)
PROCESSING 态继续监听 VAD:`SPEECH_START` 且 `barge_in=true` 时置 `_interrupt` 事件,
`should_stop` 谓词在 TTS 合成循环中被轮询 → 取消本轮 TTS 播放。

### 2.4 LangGraph 编排
`AgentGraph` 用 `StateGraph`:节点 `supervisor` / `chat` / `tools`;
`START→supervisor`;supervisor 条件边 `{chat, tools, finish}`;worker 完成条件边回 supervisor/END。
supervisor 用 `_SUPERVISOR_PROMPT` 让 LLM 返回单词路由;`ToolAgent._detect` 未命中返回空文本,
worker 回退到 chat(与 router 约定一致)。

### 2.5 协议化依赖 + 懒加载
- `VoicePipeline` 对 asr/tts/graph 用 `_AsrPort`/`_TtsPort`/`_GraphPort` Protocol(含 `running`/方法),测试注入 Fake。
- `OrchestrationService` 对唤醒/VAD 引擎、graph 工厂、source/player 全部支持注入;重依赖
  (langgraph / openwakeword / silero)在 `start()` 内延迟导入,`orchestration/__init__.py` 也
  PEP 562 惰性导出,避免无关测试被迫加载重型依赖(沙箱内存受限时尤为重要)。

## 3. 配置

```yaml
orchestration:
  enabled: false        # 显式 opt-in 端到端模式
  barge_in: true        # 合成中用户开口即打断
  default_agent: chat   # AgentGraph 默认 worker
```

校验:`OrchestrationSection` 三键全类型校验,未知键/类型错 fail-fast(点分键路径)。
`OrchestrationService.start()` 在 enabled 时强制 `asr.running` / `tts.running`,否则抛
`ConfigurationError`(提示先开 asr/tts)。

## 4. 质量门禁

- **Ruff**(E/W/F/I/N/UP/B/C4/SIM/RUF + RUF001 豁免):全绿。
- **Black**:全绿。
- **MyPy strict**(含 no-implicit-reexport):jarvis(71 文件)+ tests(32 文件)全绿。
  注:默认增量缓存曾触发 mypy 2.3.0 内部错误,`--no-incremental` 即干净通过(非代码问题)。
- **pytest**:阶段十 21 测试全绿(状态机 IDLE→LISTENING→PROCESSING、Barge-In、ASR→Graph→TTS
  链路、service 生命周期/互斥/配置校验),全部用 Fake 确定性驱动,无需真实模型/麦克风/网络。

## 5. 实测说明

- **沙箱无麦克风、无 GPU、重型原生依赖(funasr/torch/silero/openwakeword)下载受限于网络**,
  真实唤醒/端点检测/识别/合成推理、真实麦克风采集均未在沙箱跑通 —— 与阶段六~九一致的受限说明。
  全部逻辑以 `ScriptedWakeWordEngine` / `ScriptedVadEngine` / `FakeAsr` / `FakeTts` /
  `FakeGraph` / `FakeLlmClient` / `ScriptedAudioSource` 覆盖,确定性单测全绿。
- **本机验证清单**(在用户 Windows 主机、装好 voice 依赖后):
  1. `JARVIS_ORCHESTRATION_ENABLED=true`(或 config 设 `orchestration.enabled: true`,
     `asr.enabled: true`, `tts.enabled: true`, `wakeword.enabled: true`, `vad.enabled: true`)。
  2. `python -m jarvis`,说唤醒词→说话→应听到 LLM 回复语音;合成中再说话应被打断。
  3. 若只想用独立唤醒/ASR/TTS 而不走编排,保持 `orchestration.enabled: false` 即可。
```
