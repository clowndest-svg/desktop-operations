# 阶段九:TTS 语音合成(流式 · 边合成边播)

> 状态:✅ 完成(架构 + 全部门禁 + 冒烟验证全绿)| CPU 实测受限说明见 §5.4
> 门禁:Ruff / Black / MyPy strict / pytest 全绿(**226 passed, 3 skipped**,mypy 89 文件)

## 1. 交付物

```
jarvis/tts/                        语音合成(L1,新建)
  __init__.py       公共 API 导出 + 依赖说明(允许 core/config)
  types.py          SpeechSynthesizer 协议(text → Iterator[AudioChunk])
                    / AudioChunk(frozen:audio/sample_rate/is_final/format)
                    / FORMAT_* 编码标签 / ShouldStop 谓词类型别名
  engines.py        CosyVoiceTtsEngine(默认·离线·ONNX,延迟导入)
                    / EdgeTtsEngine(云端·纯 Python·延迟导入)
                    / _missing_dependency 精确报错工厂
  service.py        TtsService:生命周期组件(加载/释放引擎)
                    + synthesize(流式生成器·边合成边播)
                    + synthesize_to_bytes(便捷聚合)
                    + should_stop(Barge-In 打断钩子)
jarvis/config/
  schema.py         += VALID_TTS_ENGINES / TtsSection(7 键全校验)/ AppConfig.tts
  defaults.yaml     += tts 节(默认 disabled)
  __init__.py       += 导出 TtsSection
jarvis/__main__.py   注册第 7 个组件 TtsService(与 voice 服务同构)
pyproject.toml       [voice] extra += edge-tts;mypy overrides += edge_tts/cosyvoice/cosyvoice.*
requirements-voice.txt   同步 += edge-tts
tests/
  test_tts_types.py(3)  test_tts_engines.py(7)  test_tts_service.py(7)  test_config_tts.py(9)
                        # 新增 26(24 passed / 2 skipped),合计 226 passed / 3 skipped
```

## 2. 架构决策

### 2.1 与唤醒词 / VAD / ASR 严格对称的设计
阶段六(唤醒词)、阶段七(VAD)、阶段八(ASR)把 "引擎只做最小单元、上层做决策、
工厂可注入、延迟导入 + 精确报错、默认禁用" 这套范式落了地。阶段九**原样复用**
同一套结构,把它用在了语音合成上:

| 关注点 | 唤醒词(六) | VAD(七) | ASR(八) | TTS(九) |
|---|---|---|---|---|
| 引擎协议 | `WakeWordEngine.process` → 命中 | `VoiceActivityDetector.process` → 概率 | `SpeechRecognizer.recognize` → 文本 | `SpeechSynthesizer.synthesize` → `Iterator[AudioChunk]` |
| 上层决策 | `WakeWordDetector`(去抖) | `VoiceActivitySegmenter`(端点) | 编排层做 VAD→ASR 串联 | 编排层做 播放 + Barge-In |
| 生命周期 | `WakeWordService`(daemon) | `VadService`(daemon) | `AsrService`(**被动**:加载/释放) | `TtsService`(**被动**:加载/释放) |
| 默认禁用 | `wakeword.enabled=false` | `vad.enabled=false` | `asr.enabled=false` | `tts.enabled=false` |
| 缺包不崩 | 延迟导入 + `WakeWordError` | 延迟导入 + `VadError` | 延迟导入 + `AsrError` | 延迟导入 + `TtsError` |

关键点:TTS 同样**不自己抓麦克风、也不自己播音**——它只负责"文本 → 音频流",
是一个被动的能力提供方。播放(交给音频输出)与打断(监听 VAD 的 `SPEECH_START`)
都留给后续编排层,避免 "两套代码抢资源" 的架构陷阱(与阶段八同构)。

### 2.2 `SpeechSynthesizer` 协议:流式增量 + 边合成边播
```python
@runtime_checkable
class SpeechSynthesizer(Protocol):
    name: str
    sample_rate: int
    def synthesize(self, text, *, voice=None, speed=None, volume=None,
                   should_stop=None) -> Iterator[AudioChunk]: ...
    def close(self) -> None: ...
```
- `AudioChunk` 携带 `audio: bytes` + `sample_rate` + `format`(pcm_s16le/mp3/wav)
  + `is_final`。之所以把 `format`/采样率打在 chunk 上,是因为合成引擎的**原生输出
  格式各异**:CosyVoice 吐 24kHz s16le PCM,Edge-TTS 吐 24kHz MP3。播放层(后续阶段)
  据此解码 / 重采样,责任不泄漏进 TTS 包内。
- **整句 / 流式统一**:`synthesize(text)` 本身就是一个生成器,边产边 yield。这正是
  阶段八报告 §6 第 3 点要求的 "边合成边播"——调用方拿到第一个 chunk 即可开始播放,
  无需等整句合成完。
- `synthesize_to_bytes(text)` 是便捷封装:收集全部 chunk 成一段 buffer,返回
  `(audio, format, sample_rate)`,给不需要流式、只想"合成完再播"的场景用。

### 2.3 Barge-In 预留:`should_stop` 打断钩子
`synthesize` 接受一个 `should_stop: Callable[[], bool] | None`。引擎在产出每个 chunk
**之间**轮询它;一旦返回 `True`,立刻停止 yield(取消剩余合成)。这正是阶段八报告 §6
第 4 点要求的 "播放 TTS 期间监听 VAD 的 `SPEECH_START` 以打断" 的接入点:编排层把
VAD 的 `SPEECH_START` 事件接到这个谓词上即可实现"用户插话 → 立即停止当前播报"。
本阶段只把钩子协议准备好并单测验证(用 `FakeTtsEngine` + 计数器谓词断言只产出首
chunk 后即取消),真实接线在音频播放 / 编排阶段落地。

### 2.4 引擎选择:CosyVoice(默认·离线) / Edge-TTS(云端备选)
- 默认 `cosyvoice`:与 wake/VAD/ASR 一致的**离线优先**原则(语音管线的默认引擎
  历史上全是离线 ONNX),且 `CosyVoice2` 是 tech-stack 与架构文档首列引擎。
- 备选 `edge_tts`:纯 Python `edge-tts` 客户端,对接微软免费云端 TTS,**无任何原生
  二进制**(`pip install edge-tts` 即装即用),作为轻量 / 无本地模型的下落路径。
- `voice` extra 把 `edge-tts`(干净 PyPI 包)纳入,`cosyvoice` 因非干净 wheel,仅在
  `pyproject.toml` / `requirements-voice.txt` 中以**注释**说明手动安装方式,以便
  `pip install -e ".[voice]"` 仍能成功且默认引擎可一键切换。
- 两者皆**延迟导入**:`CosyVoiceTtsEngine.__init__` 不碰 `torch`/`cosyvoice`,
  `EdgeTtsEngine.__init__` 不碰 `edge_tts`;只有真正 `synthesize` 时才 import。
  缺包时抛精确 `TtsError`(带 `missing_package` + `pip install` 提示),不崩溃。

### 2.5 `TtsService`:被动生命周期组件(与 voice 服务同构)
- **默认禁用**:`tts.enabled=false`(加载模型 / 开云端会话是重操作 + 拉原生依赖 /
  联网),显式 opt-in。
- **启用即硬失败**:`start()` 构造引擎 = 懒加载模型;若用户显式启用却缺依赖 /
  模型加载失败,直接抛 `TtsError`,不静默降级。
- **工厂可注入**:`engine_factory` 参数化,无硬件、无原生依赖即可全测(用
  `FakeTtsEngine`)。
- **API**:`synthesize()`(流式生成器,边合成边播)+ `synthesize_to_bytes()`;`sample_rate`
  属性暴露当前引擎原生采样率(加载前为 16kHz 占位);未 `start()` 调用合成方法抛
  `TtsError("tts engine is not loaded; call start() first")`。
- **stop 幂等**:释放引擎,二次 `stop()` 安全。

### 2.6 `TtsError` 已在异常体系预定义
`jarvis.core.exceptions.TtsError(AudioError)` 在阶段二即预留(与 `WakeWordError`/
`VadError`/`AsrError` 并列),本阶段直接复用,无需新增异常类。

### 2.7 配置(`tts.*`)
| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 加载引擎、允许合成(显式 opt-in) |
| `engine` | str(枚举) | `cosyvoice` | `cosyvoice`(离线 ONNX) / `edge_tts`(云端) |
| `voice` | str | `中文女` | 引擎专属音色:CosyVoice `中文女`/Edge-TTS `zh-CN-XiaoxiaoNeural` |
| `speed` | float ≥0 | `1.0` | 语速倍率,1.0 = 正常 |
| `volume` | float [0,1] | `1.0` | 音量增益,1.0 = 满 |
| `device` | str | `cpu` | 离线引擎推理设备:`cpu` / `cuda` |
| `model` | str | `iic/CosyVoice2-0.5B` | 离线模型 id(首次推理自动下载) |

环境变量覆盖示例:`JARVIS_TTS__ENABLED=true`、`JARVIS_TTS__VOICE=zh-CN-XiaoxiaoNeural`、
`JARVIS_TTS__ENGINE=edge_tts`。

## 3. requirements / 依赖
- `pyproject.toml` 的 `[voice]` extra 新增 `edge-tts>=1.1.0`(TTS 云端备选,纯 Python);
  `cosyvoice` 因非干净 PyPI wheel,仅在注释中给出手动安装指引。`[tool.mypy.overrides]`
  的 module 列表新增 `edge_tts` / `cosyvoice` / `cosyvoice.*`(无桩第三方,导入受限在
  typed adapter 内;`torch`/`numpy` 已在 earlier 阶段加入)。
- `requirements-voice.txt` 同步新增 `edge-tts>=1.1.0` + `cosyvoice` 手动安装注释。
- 两项均属**可选**依赖;不安装时 `python -m jarvis` 仍可正常启动(门禁与默认禁用保证)。

## 4. 门禁与冒烟

### 4.1 全绿
```
ruff check .      -> All checks passed!
black --check .   -> 90 files would be left unchanged
mypy              -> Success: no issues found in 89 source files
pytest            -> 226 passed, 3 skipped
```
(跳过的 3 个是真实引擎条件检查:仅在已安装 `funasr`(ASR)/ `cosyvoice` / `edge_tts`
的环境下运行,分别校验 `name` / `sample_rate`。)

### 4.2 单测覆盖
- `test_tts_types.py`(3):`AudioChunk` 默认值、final+mp3 组合、格式标签互异。
- `test_tts_engines.py`(7):Fake 引擎产出 3 chunk(末 chunk `is_final`)、`should_stop`
  取消后续 chunk、`cosyvoice` 引擎构造不触发模型加载(`name`/`sample_rate`)、`cosyvoice`
  缺包 `synthesize` 抛精确 `TtsError`(环境无包时执行)、`edge_tts` 引擎构造校验、
  `edge_tts` 缺包抛 `TtsError`、协议结构一致性。
- `test_tts_service.py`(7):disabled 不加载引擎(no-op)、enabled 加载并合成且 stop
  释放、未 start 合成抛 `TtsError`、`synthesize_to_bytes` 聚合全 chunk 为 buffer、
  `should_stop` 取消仅产出首 chunk、stop 幂等、未知引擎工厂硬失败。
- `test_config_tts.py`(9):默认值、`tts.engine` 非法、`tts.voce` 拼写未知键、
  `speed` 下界、`volume` 上界、空 `voice`、空 `device`、空 `model`、默认配置禁用校验。

### 4.3 冒烟验证
- **默认禁用启动**:`python -m jarvis` 干净 start/stop,**7 个组件**
  (`config`/`logging`/`llm`/`wakeword`/`vad`/`asr`/`tts`)全部注册,TTS 不加载引擎。
- **非法引擎环境变量**:`JARVIS_TTS__ENGINE=gpt-sovits .venv/Scripts/python -m jarvis`
  启动即 fail-fast:
  `invalid configuration at 'tts.engine': must be one of: cosyvoice, edge_tts`

### 4.4 CPU 实测受限(如实标注)
沙箱内 `cosyvoice` / `torch` / `onnxruntime` 等大体积 wheel 反复因网络 `IncompleteRead`
未能装成,与阶段六 `openwakeword`、阶段七 `silero-vad`、阶段八 `funasr` 相同的国内镜像
问题。**因此 `CosyVoiceTtsEngine` 的真实推理尚未在本机跑通**;但架构已优雅处理缺包场景
(延迟导入 + 精确 `TtsError`),且协议、服务生命周期、流式产出、`should_stop` 打断钩子、
配置校验全部用 `FakeTtsEngine` 做了确定性单测(无原生依赖、无硬件)。后续在能稳定拉取
依赖的环境中,`pip install -e ".[voice]"` 后即可用真引擎端到端验证。

> 注:`edge-tts` 是纯 Python 客户端,联网即可用;本机仍可验证其缺包报错路径与协议
> 一致性,真实合成需在能访问微软 TTS 端点(且已 `pip install edge-tts`)的环境运行。

## 5. 下一阶段计划(阶段十:多 Agent 编排核心 / LangGraph)

1. 进入 L3 编排层:`jarvis/agent` —— LangGraph 多 Agent 调度骨架(11 类 Agent 的
   抽象与注册)。
2. `jarvis/planner` —— 任务分解 / 重规划(依赖 llm / prompt)。
3. 打通 **VAD → ASR → LLM → TTS** 全链路:用阶段七/八/九的契约把"唤醒 → 断句 → 识音
   → 生成 → 合成语音"串成一次完整对话轮次(音频播放与 Barge-In 接线同阶段)。
4. `jarvis/prompt` —— 提示词模板管理(禁止散落硬编码),供 planner / agent 复用。
5. 质量门禁全绿,并按二十阶段规范逐步推进 工具调用 / MCP / 记忆 / 视觉 / Computer Use。

> 请确认本阶段交付(目录结构 / 代码 / 门禁 / 报告),确认后我开始阶段十。
