# 阶段七:语音活动检测(VAD)流式断句

> 状态:✅ 完成(架构 + 全部门禁 + 冒烟验证全绿)| CPU 实测受限说明见 §5.4
> 门禁:Ruff / Black / MyPy strict / pytest 全绿(**173 passed**,mypy 75 文件)

## 1. 交付物

```
jarvis/vad/                        语音活动检测(L1,新建)
  __init__.py       公共 API 导出 + 依赖说明(允许 core/config/audio)
  types.py          VoiceActivityDetector 协议 / VadEvent / SpeechSegment / VadState / VadEventType
  engines.py        SileroVadEngine(默认·离线·ONNX,延迟导入)
  segmenter.py      VoiceActivitySegmenter:流式端点状态机(阈值+min/max silence+padding+max speech)
  service.py        VadService:生命周期组件 + daemon 监听线程 + on_event 回调
jarvis/config/
  schema.py         += VALID_VAD_ENGINES / VadSection(7 键全校验)
  defaults.yaml     += vad 节(默认 disabled)
  __init__.py       += 导出 VadSection
jarvis/__main__.py   注册第 5 个组件 VadService(与 WakeWordService 同构)
pyproject.toml       [voice] extra += silero-vad;mypy overrides += silero_vad / onnxruntime
requirements-voice.txt   同步 += silero-vad
tests/
  test_vad_segmenter.py(17)  test_vad_service.py(7)  test_config_vad.py(9)   # 新增 33(含 7 个参数化),合计 173
```

## 2. 架构决策

### 2.1 与唤醒词严格对称的设计
阶段六把 "引擎只打分、去抖归上层" 这条法则用在了唤醒词上。阶段七**原样复用**同
一套结构,把它用在了语音端点检测上:

| 关注点 | 唤醒词(阶段六) | VAD(阶段七) |
|---|---|---|
| 引擎协议 | `WakeWordEngine.process` → 原始命中 | `VoiceActivityDetector.process` → 语音概率 [0,1] |
| 上层状态机 | `WakeWordDetector`(阈值+冷却) | `VoiceActivitySegmenter`(端点状态机) |
| 生命周期 | `WakeWordService`(daemon 线程) | `VadService`(daemon 线程,同构) |
| 默认禁用 | `wakeword.enabled=false` | `vad.enabled=false` |
| 缺包不崩 | 延迟导入 + 精确 `VadError` | 延迟导入 + 精确 `VadError` |

这条对称性的价值:零原生依赖、无硬件即可全测,且后续 `asr` / `tts` 阶段能照葫芦
画瓢,不引入新的架构模式。

### 2.2 `VoiceActivityDetector` 协议:引擎只打分
```python
@runtime_checkable
class VoiceActivityDetector(Protocol):
    @property
    def name(self) -> str: ...                       # silero
    @property
    def frame_samples(self) -> int: ...               # Silero = 512
    def process(self, frame: bytes) -> float: ...     # 语音概率 in [0, 1]
    def close(self) -> None: ...                      # 释放原生句柄(幂等)
```
引擎职责的边界划得和唤醒词引擎一样清:**只产出每帧的语音概率**,端点决策
(起点/终点、padding、最小/最大时长过滤)全部由 `VoiceActivitySegmenter` 独占。
这样引擎可被任意脚本化打分器替换,也便于单测。

### 2.3 流式端点状态机(本阶段核心)
`VoiceActivitySegmenter` 把概率流变成 "句子边界",状态转移:

```
SILENCE --(prob >= threshold,持续 min_speech)--> SPEECH_START
SPEECH  --(prob <  threshold,持续 max_silence)-> SPEECH_END
SPEECH  --(spoken >= max_speech,硬上限)--------> SPEECH_END(强制分段,max_speech_ms=0 关闭)
```

关键行为(均有单测覆盖):
- **Min-speech 过滤**:短于 `min_speech_ms` 的 "blip"(按键声/呼吸)在发任何
  `SPEECH_START` 之前就被丢弃 → 下游永远不会收到 "有头无尾" 的事件。
- **Padding**:`speech_pad_ms` 把段向两侧各延展一小段,让 ASR 拿到干净的起音/收音,
  而不是被砍掉的半个音素。
- **Max-speech 硬上限**:单句超过 `max_speech_ms`(默认 0=不限制)强制切段,防止
  用户一直不喘气导致整段无限堆积。
- **可注入时钟**:`clock: Callable[[], float]` 默认 `time.monotonic`,测试用
  `FakeClock` 完全确定化时间。
- **跨块帧重组**:内部复用阶段六的 `FrameAssembler(engine.frame_samples * 2)`,
  任意大小的麦克风块都能精确还原成引擎帧,不丢不重。

`SpeechSegment` 同时带采样空间(`start_sample/end_sample`)与墙钟空间
(`start_time/end_time`),并暴露 `duration_samples` / `duration_ms`,供 ASR 切片与
UI 显示直接使用。

### 2.4 `VadService`:生命周期组件(与唤醒词同构)
- **默认禁用**:`vad.enabled=false`,抓取麦克风是显式 opt-in;禁用时 `start()`
  直接 no-op,引擎/源根本不构造。
- **启用即硬失败**:若用户显式启用却缺依赖 / 麦克风不可用,`start()` 抛
  `VadError` / `AudioError` 并关闭已开的引擎 —— 静默降级一个被明确要求的功能
  只会掩盖真问题。
- **工厂可注入**:`engine_factory` / `source_factory` 参数化,使无硬件、无原生
  依赖即可全测。
- **异常隔离**:`on_event` 回调抛异常只丢当前事件、不杀监听循环。
- **stop 幂等 + flush**:`stop()` 设停标志、`join`、关源、关引擎三步幂等;循环
  退出时 `segmenter.flush()` 把仍在开的尾段收尾,消费者不会丢掉最后一句话。

### 2.5 延迟导入 + 精确报错,缺包不崩溃
`silero_vad` / `numpy` / `onnxruntime` 全部在 `SileroVadEngine.__init__` 内延迟
导入;缺失时抛 `VadError`(带 `missing_package` 细节与 `pip install` 命令)。因为
默认 `vad.enabled=false`,缺原生依赖完全不影响启动与门禁。

### 2.6 与唤醒词的衔接(为阶段八 ASR 铺路)
唤醒命中后,应用应切换到 VAD 做 "语音起点→终点" 断句,再把这段 PCM 喂给阶段八
的 ASR。`VadService` 的 `on_event` 回调已经在 `SPEECH_END` 时吐出完整
`SpeechSegment`(带正确的采样区间),ASR 只需按区间切片即可。Barge-In(打断)也可
复用 `VadState` / `SPEECH_START` 事件:播放 TTS 时监听 VAD,一旦检测到新的
`SPEECH_START` 即中断播报。

## 3. 配置(`vad.*`)
| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 显式 opt-in 才抓麦克风 |
| `engine` | str(枚举) | `silero` | 唯一引擎 |
| `threshold` | float [0,1] | `0.5` | 语音概率门限 |
| `min_speech_ms` | int ≥0 | `250` | 低于此长的 blip 丢弃 |
| `max_silence_ms` | int ≥1 | `500` | 静音持续这么久判定句尾 |
| `speech_pad_ms` | int ≥0 | `100` | 首尾各填充,给 ASR 干净边缘 |
| `max_speech_ms` | int ≥0 | `0` | 单句硬上限,0=不限 |

环境变量覆盖示例:`JARVIS_VAD__ENABLED=true`、`JARVIS_VAD__THRESHOLD=0.6`。

## 4. requirements / 依赖
- `pyproject.toml` 的 `[voice]` extra 新增 `silero-vad>=4.0.0`(VAD 默认引擎);
  `[tool.mypy.overrides]` 的 module 列表新增 `silero_vad`、`onnxruntime`
  (无桩第三方,导入受限在 typed adapter 内)。
- `requirements-voice.txt` 同步新增 `silero-vad>=4.0.0`。
- 两项均属**可选**依赖;不安装时 `python -m jarvis` 仍可正常启动(门禁与默认
  禁用保证)。

## 5. 门禁与冒烟

### 5.1 全绿
```
ruff check .      -> All checks passed!
black --check .   -> 76 files would be left unchanged
mypy              -> Success: no issues found in 75 source files
pytest            -> 173 passed
```

### 5.2 单测覆盖
- `test_vad_segmenter.py`(12):状态机全分支 —— 正常起止、blip 过滤、padding 向
  前延伸、max-speech 强制分段、flush 收尾(确认段发 END / 未确认 blip 丢弃)、
  跨任意块边界的帧重组、空块安全、reset 清状态、构造参数越界校验(7 类)。
- `test_vad_service.py`(7):disabled no-op、enabled 正常起止且 stop 干净、stop 时
  flush 尾段、stop 幂等、源 `open()` 失败仍关引擎、回调异常不杀循环、默认配置
  校验。
- `test_config_vad.py`(9):默认值、未知引擎、未知键、threshold 上下界、各 ms 越界。

### 5.3 冒烟验证
- **默认禁用启动**:`python -m jarvis` 干净 start/stop,5 个组件
  (`config`/`logging`/`llm`/`wakeword`/`vad`)全部注册,VAD 不抓麦克风。
- **非法引擎环境变量**:`JARVIS_VAD__ENGINE=snowboy .venv/Scripts/python -m jarvis`
  启动即 fail-fast:
  `invalid configuration at 'vad.engine': must be one of: silero`

### 5.4 CPU 实测受限(如实标注)
沙箱内 `silero-vad` 依赖的 `onnxruntime` 等大体积 wheel 反复因网络
`IncompleteRead` 未能装成,与阶段六 `openwakeword`/`sounddevice` 相同的国内镜像
问题。**因此 `SileroVadEngine` 的真实 ONNX 推理尚未在本机跑通**;但架构已优雅处理
缺包场景(延迟导入 + 精确 `VadError`),且端点状态机、service 生命周期、配置校验
全部用 Fake 打分器 + Fake 源做了确定性单测(无原生依赖、无硬件)。后续在能稳定拉
取 `onnxruntime` 的环境中,`pip install -e ".[voice]"` 后即可用真引擎端到端验证。

## 6. 下一阶段计划(阶段八:ASR 流式识别)
1. `jarvis/asr/`:`SpeechRecognizer` 协议(音频段/流 → 文本 + 可选流式增量)。
2. 默认实现 SenseVoice / FunASR(离线 ONNX)或 Whisper;沿用 16kHz/单声道帧流水线。
3. 接入本阶段产物:`VadService.on_event` 在 `SPEECH_END` 时按 `SpeechSegment` 采样
   区间切片,喂给 ASR 得到一句文本(唤醒→VAD 断句→ASR 识音 的完整链路打通)。
4. 流式增量:`partial` 结果边出边显示,`final` 结果定稿后交给 LLM。
5. 复用 `FrameAssembler` / `AudioSource`,新增 `asr` 配置节(模型、语言、温度等),
   门禁全绿。
6. 为 Barge-In 预留:播放 TTS 期间监听 VAD 的 `SPEECH_START` 以打断(本阶段状态机
   已就绪)。

> 请确认本阶段交付(目录结构 / 代码 / 门禁 / 报告),确认后我开始阶段八。
