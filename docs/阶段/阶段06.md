# 阶段六:唤醒词(Wake Word)

> 状态:✅ 完成(架构 + 全部门禁 + 冒烟验证全绿)| CPU 实测受限说明见 §5.4
> 门禁:Ruff / Black / MyPy strict / pytest 全绿(140 passed,mypy 68 文件)

## 1. 交付物

```
jarvis/audio/                      语音流水线公共采集层(L1,新建)
  __init__.py        导出 AudioFormat / AudioSource / FrameAssembler / SounddeviceSource
  format.py          AudioFormat(16kHz/单声道/s16le 不可变校验)+ DEFAULT_FORMAT
  frames.py          FrameAssembler:任意字节块 → 精确引擎帧(不丢不重)
  source.py          AudioSource 协议 + SounddeviceSource(PortAudio,延迟导入)
jarvis/wakeword/                   唤醒词(L1,新建)
  __init__.py        公共 API 导出 + 依赖说明(允许 core/config/audio)
  types.py           WakeWordEngine 协议 / WakeHit / WakeEvent
  engines.py         OpenWakeWordEngine(默认·离线·免Key) + PorcupineEngine(可选·AccessKey)
  detector.py        WakeWordDetector:阈值 + 冷却去抖(时钟可注入)
  service.py         WakeWordService:生命周期组件 + daemon 监听线程 + on_wake 回调
jarvis/config/
  schema.py          += VALID_WAKEWORD_ENGINES / PorcupineSection / WakeWordSection
  defaults.yaml      += wakeword 节(默认 disabled)
  __init__.py        += 导出 PorcupineSection / WakeWordSection
jarvis/__main__.py   注册第 4 个组件 WakeWordService
pyproject.toml       += [voice] extra + mypy overrides(无桩第三方)
requirements-voice.txt   语音原生依赖隔离清单
scripts/
  bench_wakeword_glue.py  无原生依赖的胶水开销基准(用户可在真机补引擎实测)
tests/
  test_audio.py(12)  test_wakeword_detector.py(8)
  test_wakeword_service.py(6)  test_config_wakeword.py(9)   # 新增 35,合计 140
```

## 2. 架构决策

### 2.1 独立的 `jarvis/audio` 采集层(供 wakeword/vad/asr 共用)
唤醒词、VAD、ASR 全部以 **16 kHz / 单声道 / s16le** 为输入。把格式定义、
帧重组、采集源抽象成独立包,转换**只在采集边界做一次**,避免三个消费者各自
实现一套。格式错误在 `AudioFormat.__post_init__` 阶段 fail-fast(拒 stereo /
非 16bit / 非法采样率)。

### 2.2 `WakeWordEngine` 协议:引擎只打分,去抖归 detector
```python
@runtime_checkable
class WakeWordEngine(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def frame_samples(self) -> int: ...                 # OpenWakeWord=1280, Porcupine=512
    def process(self, frame: bytes) -> tuple[WakeHit, ...]: ...   # 原始未阈值化
    def close(self) -> None: ...
```
引擎职责的边界划得很清:**只产出原始分数**,阈值判定与冷却去抖由
`WakeWordDetector` 独占。Porcupine 自带阈值(命中 score=1.0),detector 侧
阈值对其退化为 no-op,两种引擎共用同一上层逻辑。

### 2.3 双引擎:OpenWakeWord 默认,Porcupine 可选
- **OpenWakeWord**(默认):离线、免 Key、ONNX 推理,`hey_jarvis` 为预训练模型;
  中文短语(如「贾维斯」)放社区/自定义模型进 `models_dir` 即可。
- **Porcupine**(可选):Picovoice 商业级准确率,需要 AccessKey;经
  `wakeword.porcupine.access_key_env` 读取环境变量名,**密钥绝不落盘**。

### 2.4 延迟导入 + 精确报错,缺包不崩溃
`sounddevice` / `openwakeword` / `pvporcupine` / `numpy` 全部在 `__init__`
或 `open()` 内延迟导入;缺包时抛 `WakeWordError` / `AudioError`(带
`missing_package` 细节与安装命令),**不会在 import 阶段炸**。因为默认
`wakeword.enabled=false`,缺原生依赖完全不影响启动与门禁。

### 2.5 `FrameAssembler`:任意块 → 精确帧,不丢不重
麦克风口驱动返回的块大小不定,而引擎要精确帧长。`FrameAssembler` 缓冲到满帧
才输出,余量留给下次 `push`,**零样本丢弃/重复**。非线程安全(设计如此,每个
消费者在自己的音频线程独占一个 assembler)。

### 2.6 去抖:阈值 + 冷却,时钟可注入
一次口语"Jarvis"会产生一串高分管连续帧,不去抖会触发几十次唤醒事件。
`WakeWordDetector.feed()` 做两件事:
- `score < threshold` → 忽略;
- 命中后进入 `cooldown_seconds` 冷却,期间后续命中被抑制。
`clock` 参数默认 `time.monotonic`,**测试可注入假时钟**精确控制时间线。

### 2.7 配置:显式 opt-in + 密钥只存变量名
```yaml
wakeword:
  enabled: false          # 抓麦克风是显式选择,默认不抓
  engine: openwakeword
  keywords: [hey_jarvis]
  threshold: 0.5
  cooldown_seconds: 2.0
  porcupine:
    access_key_env: PICOVOICE_ACCESS_KEY   # 只存变量名,不存 Key
    sensitivity: 0.5
```
AccessKey 只存环境变量**名**(`access_key_env`),运行时从 `environ` 查值;
换 Key 无需改配置,且阶段十四(安全)可在同一查找点后接加密存储,接口不变。

### 2.8 服务:daemon 线程 + 异常隔离 + 幂等 stop
`WakeWordService`(第 4 个注册组件)拥有常驻麦克风循环:
- 单条 `daemon` 线程 `read → detector.feed → on_wake`;
- **异常隔离**:采集失败 / 回调抛错都只记日志、不杀循环(采集异常在 stop 竞态
  下优雅退出);
- `stop()` 幂等:`stop_flag` + `join(timeout=5s)` + 关源 + 关引擎;
- 工厂 `engine_factory` / `source_factory` 可注入,**全链路无硬件、无原生依赖
  即可单测**(FakeEngine / SilenceSource)。

### 2.9 依赖隔离:mypy overrides + voice extra
`pyproject.toml` 的 `[tool.mypy.overrides]` 对 `sounddevice / openwakeword /
openwakeword.* / pvporcupine / numpy` 放 `ignore_missing_imports`(无桩第三方);
`voice` extra 与 `requirements-voice.txt` 把原生二进制(PortAudio / ONNX Runtime)
与主依赖隔离,主包保持零原生依赖、可纯标准库跑通。

## 3. 配置(defaults.yaml 摘录)

```yaml
wakeword:
  enabled: false            # JARVIS_WAKEWORD__ENABLED=true 开启
  engine: openwakeword      # openwakeword | porcupine
  keywords: [hey_jarvis]
  threshold: 0.5            # detector 侧置信阈值 [0..1]
  cooldown_seconds: 2.0     # 命中后抑制时长
  porcupine:
    access_key_env: PICOVOICE_ACCESS_KEY
    sensitivity: 0.5
```
环境变量覆盖示例:`JARVIS_WAKEWORD__ENABLED=true`、`JARVIS_WAKEWORD__ENGINE=porcupine`、
`JARVIS_WAKEWORD__THRESHOLD=0.7`。

## 4. Requirements

- **主运行依赖**:零新增(沿用阶段三的 PyYAML)。
- **可选语音依赖**(不在主包,需 `pip install jarvis-assistant[voice]` 或
  `requirements-voice.txt`):`sounddevice>=0.4.6`、`openwakeword>=0.6.0`
  (Porcupine 注释备选)。二者拉入原生二进制(PortAudio / ONNX Runtime),故隔离。

## 5. 验证

### 5.1 质量门禁

| 项 | 结果 |
|---|---|
| Ruff | ✅ All checks passed |
| Black | ✅ 68 files would be left unchanged |
| MyPy strict | ✅ Success: no issues found in 68 source files |
| pytest | ✅ **140 passed**(新增 35) |

### 5.2 冒烟(默认 disabled)

```
$ .venv/Scripts/python.exe -m jarvis
jarvis 0.1.0 initialized (env=production, data dir: ..., 4 component(s) registered)
```
- 默认 `wakeword.enabled=false` → 日志 `wake word disabled by configuration; not listening`,
  4 组件干净 start/stop,**完全不碰麦克风**。
- 非法引擎:`JARVIS_WAKEWORD__ENGINE=snowboy` →
  `invalid configuration at 'wakeword.engine': must be one of: openwakeword, porcupine`。

### 5.3 单元测试覆盖(35 项,全绿)

- `test_audio`(12):默认 16k/mono/s16le、字节↔样本换算、时长、拒非法格式;
  FrameAssembler 精确帧 / 累加 / 大块多帧留尾 / 随机切分不丢字节 / reset / 拒非正帧。
- `test_wakeword_detector`(8):阈值触发 / 低于阈值忽略 / 冷却抑制爆发 / 冷却后放行 /
  部分块重组 / reset 清冷却 / 非法阈值 / 负冷却(ScriptedEngine + FakeClock)。
- `test_wakeword_service`(6):disabled no-op / enabled 触发事件并干净停止 / stop 幂等 /
  源打开失败关引擎 / 回调异常不杀循环 / 默认配置校验(PulseEngine + SilenceSource)。
- `test_config_wakeword`(9):默认校验 / 未知引擎 / 未知键 / 空关键词 / 非字符串关键词 /
  阈值>1 / 负冷却 / porcupine 灵敏度越界 / 缺 porcupine 节。

### 5.4 CPU 占用实测 —— **受沙箱网络限制,引擎推理项暂缺**(如实标注)

**已实测(无原生依赖即可运行,`scripts/bench_wakeword_glue.py`):**

| 指标 | 数值 |
|---|---|
| 引擎 | fake(仅测我们所拥有的胶水) |
| 处理帧数 | 100 帧(8s @ 16kHz) |
| 每帧胶水开销 | **~4.7 µs** |
| 实时预算 | 80 ms/帧(1280 样本 @ 16kHz) |
| 胶水占预算 | **0.006%** |

> 即:帧重组 + 阈值/冷却去抖的纯 Python 开销,相对 80 ms 实时预算可忽略不计。

**未实测(被阻塞):** OpenWakeWord 引擎自身的 ONNX 推理开销。本沙箱网络在下载
`onnxruntime`(13.8 MB)wheel 时反复 `IncompleteRead`(已尝试 3 次,含
`--retries 15 --timeout 120`,阿里云/清华源均偶发抽风),且 OpenWakeWord 首次
`Model()` 还会联网拉取预训练模型——两步都依赖外网,当前环境不可得。

**预期区间(供架构判断,非本次测量值):** OpenWakeWord 是专为 always-on / 边缘设备
设计的轻量 ONNX 网络,社区实测在 CPU 上对单关键词每帧推理通常 **< 2 ms**
(远小于 80 ms 预算);叠加本阶段 ~4.7 µs 胶水,常驻监听预期 **远低于单核 5%**。
设计上进一步做了:单 daemon 线程、默认 disabled 显式 opt-in、异常隔离不杀循环。

**你在真机补测的一行命令:**
```bash
# 先装语音依赖(需稳定网络),再跑带引擎的基准:
.venv/Scripts/python.exe -m pip install -e ".[voice]"
.venv/Scripts/python.exe scripts/bench_wakeword_glue.py --with-engine openwakeword --seconds 30
```
跑出来把每帧真实开销填进上表即可。

## 6. 下一阶段计划(阶段七:VAD)

1. `jarvis/vad`:`VoiceActivityDetector` 协议(帧输入 → 语音/静音判定 + 端点检测)
2. Silero VAD 为默认实现(ONNX,离线),沿用 `jarvis/audio` 的 16kHz/单声道帧流水线
3. 与唤醒词衔接:唤醒后切到 VAD 做"语音起点/终点"断句,喂给阶段八 ASR
4. 流式端点状态机(说话中 / 静音超时 → 句尾),供后续打断(Barge-In)复用
5. 复用本阶段 `FrameAssembler` / `AudioSource`,新增 `vad` 配置节(阈值、静音时长、padding),
   门禁全绿

---

## 7. 补充（2026-09-28，中文唤醒落地）

交付时本阶段的英文默认值已经不适用于这个产品（助手叫「小夜」，唤醒语是中文），
现状与判断记录在这里。

**做法**：新增 `wakeword.engine: asr` —— 不训练模型，而是用阶段八已加载的
SenseVoice 把每句话转写出来，再在文本里找关键词。代价是每句一次识别
（8 秒音频 ~1.5 秒 CPU），收益是中文立刻可用、且**唤醒语和指令能一句话说完**
（命中时把关键词之后的文字放进 `WakeHit.command`）。

**关键词为什么是四个变体**：`scripts/verify_wake_words.py` 实测 6 种音色/语速 ×
4 种写法共 24 条，识别结果**全部**是「你好小叶」，另有 1 条「你郝小叶」。
只配「你好小夜」精确匹配 0/2 命中。所以变体是测量产物，不是拍脑袋的同音表。

**什么时候才需要拼音折叠**：客户要**自己定**一个唤醒词（例如公司名、人名）时。
现在的方案是"穷举已知误识别"，换词就得重新测。做拼音/音节级折叠能覆盖未知词，
但要新增依赖（pypinyin 一类），而且 SenseVoice 在这条路径上是离散解码、
没有 n-best 概率可用，折叠出来的仍是一个需要人工确认的封闭集合。
**触发条件**：出现第一个"要自定义唤醒词"的真实客户，且该词的同音写法无法用
2-3 条变体穷举 —— 那时再评估，别提前做。

**素材的坑**：离线验证素材必须补 ≥900ms 尾部静音。VAD 要等满
`vad.max_silence_ms`（默认 500ms）才判定"说完"，语速快的素材句尾只剩 ~0.28s，
引擎一次都不会被调用，表现和"唤醒词写错了"完全一样。
