# 阶段八:ASR 语音识别(流式)

> 状态:✅ 完成(架构 + 全部门禁 + 冒烟验证全绿)| CPU 实测受限说明见 §5.4
> 门禁:Ruff / Black / MyPy strict / pytest 全绿(**202 passed, 1 skipped**,mypy 82 文件)

## 1. 交付物

```
jarvis/asr/                        语音识别(L1,新建)
  __init__.py       公共 API 导出 + 依赖说明(允许 core/config/vad)
  types.py          SpeechRecognizer 协议 / AsrStream 协议 / RecognitionResult
                    / AsrResultType(PARTIAL·FINAL) / slice_segment_audio 切片器
  engines.py        SenseVoiceAsrEngine(默认·离线·ONNX,延迟导入)+ BufferedAsrStream
  service.py        AsrService:生命周期组件(加载/释放模型)+ recognize /
                    transcribe_segment(接 VAD 切片)/ stream 流式 API
jarvis/config/
  schema.py         += VALID_ASR_ENGINES / AsrSection(7 键全校验)/ AppConfig.asr
  defaults.yaml     += asr 节(默认 disabled)
  __init__.py       += 导出 AsrSection
jarvis/__main__.py   注册第 6 个组件 AsrService(与 voice 服务同构)
pyproject.toml       [voice] extra += funasr;mypy overrides += funasr/funasr.*/torch/modelscope
requirements-voice.txt   同步 += funasr
tests/
  test_asr_types.py(8)  test_asr_engines.py(5)  test_asr_service.py(8)  test_config_asr.py(9)   # 新增 30,合计 202 passed / 1 skipped
```

## 2. 架构决策

### 2.1 与唤醒词 / VAD 严格对称的设计
阶段六(唤醒词)、阶段七(VAD)把 "引擎只做最小单元、上层做决策、工厂可注入、
延迟导入 + 精确报错、默认禁用" 这套范式落了地。阶段八**原样复用**同一套结构,
把它用在了语音识别上:

| 关注点 | 唤醒词(六) | VAD(七) | ASR(八) |
|---|---|---|---|
| 引擎协议 | `WakeWordEngine.process` → 命中 | `VoiceActivityDetector.process` → 概率 | `SpeechRecognizer.recognize` → 文本 |
| 上层决策 | `WakeWordDetector`(去抖) | `VoiceActivitySegmenter`(端点) | 由编排层(后续)做 VAD→ASR 串联 |
| 生命周期 | `WakeWordService`(daemon 线程) | `VadService`(daemon 线程) | `AsrService`(**被动**:加载/释放模型) |
| 默认禁用 | `wakeword.enabled=false` | `vad.enabled=false` | `asr.enabled=false` |
| 缺包不崩 | 延迟导入 + `WakeWordError` | 延迟导入 + `VadError` | 延迟导入 + `AsrError` |

关键点:ASR 不自己抓麦克风(那归 VAD 所有),而是**被动**地提供识别能力,由
后续编排层把 VAD 的 `SPEECH_END` 事件喂给它。这也避开了 "两套代码抢麦克风"
的架构陷阱。

### 2.2 `SpeechRecognizer` / `AsrStream` 协议:流式增量
```python
@runtime_checkable
class SpeechRecognizer(Protocol):
    name: str
    sample_rate: int
    def recognize(self, audio, *, segment=None, language=None) -> RecognitionResult: ...
    def stream(self) -> AsrStream: ...
    def close(self) -> None: ...

@runtime_checkable
class AsrStream(Protocol):
    def push(self, chunk: bytes) -> list[RecognitionResult]: ...   # 返回 PARTIAL
    def finish(self) -> RecognitionResult: ...                      # 返回 FINAL
```
- `RecognitionResult` 带 `type: AsrResultType`(`PARTIAL` / `FINAL`)与
  `is_final` 便捷属性;并可选携带 `language` / `confidence` / `segment`。
- 整句识别:`recognize(audio)` 直接回 `FINAL`。
- 流式识别:`stream()` 开会话 → 多次 `push(chunk)` 取 `PARTIAL` 用于实时显示 →
  `finish()` 取最终 `FINAL` 交给 LLM(阶段五)。这正是阶段七报告 §6 第 4 点要求的
  "partial 边出边显示,final 定稿后交给 LLM"。
- 默认 `BufferedAsrStream` 缓冲所有 chunk、在 `finish` 时整体识别(SenseVoice 整句
  解码)。需要原生 partial 的引擎可子类化 `push` 自行发射 `PARTIAL`,协议与契约不变。

### 2.3 VAD → ASR 的精确切片契约
`slice_segment_audio(audio, segment)` 把 VAD 检测到的 `SpeechSegment`(采样区间)
精确切成对应 PCM 字节:
- `start_sample * 2 .. end_sample * 2`(管道固定 s16le = 2 字节/样本);
- 对越界做 clamp(尾部 padding 偶尔溢出不致命),区间反转返回空缓冲而非报错。
- `AsrService.transcribe_segment(audio, segment)` 就是这一契约的落地:喂完整麦克
  风缓冲 + VAD 的 `SpeechSegment`,得到一句文本。阶段七的 `SPEECH_END` 事件天然携带
  这个 `segment`,编排层只需一行接线即可打通 "唤醒 → VAD 断句 → ASR 识音"。

### 2.4 `AsrService`:被动生命周期组件(与 voice 服务同构)
- **默认禁用**:`asr.enabled=false`(加载模型是重操作 + 拉原生 ML 依赖),显式
  opt-in。
- **启用即硬失败**:`start()` 构造引擎 = 懒加载模型;若用户显式启用却缺依赖 /
  模型加载失败,直接抛 `AsrError`,不静默降级。
- **工厂可注入**:`engine_factory` 参数化,无硬件、无原生依赖即可全测(用
  `FakeAsrEngine`)。
- **API**:`recognize()` / `transcribe_segment()` / `stream()`;未 `start()` 调用
  任何识别方法都抛 `AsrError("asr engine is not loaded; call start() first")`。
- **stop 幂等**:释放引擎模型,二次 `stop()` 安全。

### 2.5 延迟导入 + 精确报错,缺包不崩溃
`funasr` / `torch` / `numpy` 全部在 `SenseVoiceAsrEngine.__init__` 内延迟导入;
缺失时抛 `AsrError`(带 `missing_package` 细节与 `pip install` 命令)。因为默认
`asr.enabled=false`,缺原生依赖完全不影响启动与门禁——与阶段六/七一致。

## 3. 配置(`asr.*`)
| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `false` | 加载模型、允许识别(显式 opt-in) |
| `engine` | str(枚举) | `sensevoice` | 唯一引擎 |
| `model` | str | `iic/SenseVoiceSmall` | ModelScope/HF 模型 id(首次推理自动下载) |
| `language` | str | `auto` | `auto` 自动检测,或强制 `zh`/`en`/`ja`/`ko`/`yue` |
| `temperature` | float [0,1] | `0.0` | 解码温度,0 = 贪心 |
| `beam_size` | int ≥1 | `5` | beam 宽度,1 = 贪心 |
| `device` | str | `cpu` | 推理设备:`cpu` / `cuda` |

环境变量覆盖示例:`JARVIS_ASR__ENABLED=true`、`JARVIS_ASR__LANGUAGE=zh`、
`JARVIS_ASR__BEAM_SIZE=1`。

## 4. requirements / 依赖
- `pyproject.toml` 的 `[voice]` extra 新增 `funasr>=1.0.0`(SenseVoice 默认引擎;
  它按需拉 `torch` / `modelscope`);`[tool.mypy.overrides]` 的 module 列表新增
  `funasr` / `funasr.*` / `torch` / `modelscope`(无桩第三方,导入受限在 typed adapter 内)。
- `requirements-voice.txt` 同步新增 `funasr>=1.0.0`。
- 两项均属**可选**依赖;不安装时 `python -m jarvis` 仍可正常启动(门禁与默认禁用保证)。

## 5. 门禁与冒烟

### 5.1 全绿
```
ruff check .      -> All checks passed!
black --check .   -> 83 files would be left unchanged
mypy              -> Success: no issues found in 82 source files
pytest            -> 202 passed, 1 skipped
```
(跳过的 1 个是 `test_sensevoice_engine_when_available`:仅在已安装 `funasr` 的环境
下运行,验证真实引擎的 `name` / `sample_rate`。)

### 5.2 单测覆盖
- `test_asr_types.py`(8):`FINAL`/`PARTIAL` 标志、切片基础范围、越界 clamp、负起点
  clamp、区间反转返回空、sample_width 可配、结果携带 segment/language/confidence。
- `test_asr_engines.py`(5):Fake 引擎整句识别回 FINAL、Fake 流 partial→final、
  `BufferedAsrStream` 累积后 finish、**缺 `funasr` 时构造即抛精确 `AsrError`**、
  已装 `funasr` 时校验 name/sample_rate(条件跳过)。
- `test_asr_service.py`(8):disabled 不加载引擎(no-op)、enabled 加载并识别且 stop
  释放、未 start 识别抛 `AsrError`、`transcribe_segment` 精确按 VAD 区间切片并带入
  segment、流需已加载、开流→识别→释放、stop 幂等、未知引擎工厂硬失败。
- `test_config_asr.py`(9):默认值、`asr.engine` 非法、`asr.languge` 拼写未知键、
  `temperature` 上下界、`beam_size` 下界、空 `model`、空 `device`、默认配置禁用校验。

### 5.3 冒烟验证
- **默认禁用启动**:`python -m jarvis` 干净 start/stop,6 个组件
  (`config`/`logging`/`llm`/`wakeword`/`vad`/`asr`)全部注册,ASR 不加载模型。
- **非法引擎环境变量**:`JARVIS_ASR__ENGINE=whisper-x .venv/Scripts/python -m jarvis`
  启动即 fail-fast:
  `invalid configuration at 'asr.engine': must be one of: sensevoice`

### 5.4 CPU 实测受限(如实标注)
沙箱内 `funasr` / `torch` / `onnxruntime` 等大体积 wheel 反复因网络 `IncompleteRead`
未能装成,与阶段六 `openwakeword`、阶段七 `silero-vad` 相同的国内镜像问题。**因此
`SenseVoiceAsrEngine` 的真实推理尚未在本机跑通**;但架构已优雅处理缺包场景(延迟导入
+ 精确 `AsrError`),且协议、服务生命周期、VAD→ASR 切片契约、配置校验全部用
`FakeAsrEngine` + `FakeAsrStream` 做了确定性单测(无原生依赖、无硬件)。后续在能稳定
拉取 `funasr` 的环境中,`pip install -e ".[voice]"` 后即可用真引擎端到端验证。

## 6. 下一阶段计划(阶段九:TTS 语音合成)
1. `jarvis/tts/`:`SpeechSynthesizer` 协议(文本 → 音频流)。
2. 默认实现 CosyVoice(离线·ONNX)或 Edge-TTS(云端·免本地模型),沿用 16kHz/单声道。
3. 与 ASR 闭环:识别结果经 LLM 生成答复后,由 TTS 合成语音播放;**边合成边播**。
4. Barge-In 预留:播放 TTS 期间监听 VAD 的 `SPEECH_START` 以打断(本阶段状态机已就绪,
   VAD 已在阶段七提供 `VadState` / `SPEECH_START`)。
5. 新增 `tts` 配置节(音色/语速/音量/引擎),门禁全绿。

> 请确认本阶段交付(目录结构 / 代码 / 门禁 / 报告),确认后我开始阶段九。
