# 桌面端操作指南（小夜 / JARVIS HUD）

面向：拿到这台机器要把它装好、演示好、出事能自己查的人。
所有数字都是本机实测值（Windows 11 / 12 核 / 32 GB），不是估算。

---

## 0. 一分钟版

### 0a. 日常启动（装好之后，不碰命令行）

**双击桌面上的「小夜」。** 就这一下。

- 没有黑窗口、没有终端、不需要记任何命令。
- 数据、日志、模型全在 `E:\BianChengGongJu\JarvisData`（由
  `scripts\setup_env_paths.ps1` 设成**用户级环境变量**，所以快捷方式启动的进程也能继承——
  这就是"免命令行"成立的原因，见 §10）。
- 要语音：**第一次**点窗口右上角「启用语音」，等指示灯从「加载中」变「待唤醒」
  （本机实测 31 秒），然后喊「你好小夜」。**之后不用再点**——那一次按压会被记进
  `<数据目录>/preferences.json`，下次开机自己回到待唤醒（见 §7「免点击待命」）。
- 不想让它听：点「释放麦克风」。这一步同时忘掉上面那个记忆，下次开机不会自己竖起耳朵。
- 不用了：点右上角 X。关窗即退出并**释放麦克风**，没有托盘、没有后台残留。

如果桌面上还没有这个快捷方式，只需建一次（这是安装动作，不是日常动作）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
```

### 0b. 首次安装（只有这一步需要命令行）

```bat
:: 1) 依赖（开发模式，含质量工具链）
.venv\Scripts\python -m pip install -e ".[dev,desktop]"

:: 2) 界面产物（Vue 构建，不入 git，必须自己生成一次）
.venv\Scripts\python scripts\build_desktop.py

:: 3) 把数据根和模型缓存指到非系统盘（一次性，之后双击即用）
powershell -ExecutionPolicy Bypass -File scripts\setup_env_paths.ps1 -Apply

:: 4) 一把 API Key（只进环境变量，绝不写进任何 yaml）
setx QWENAI_API_KEY "sk-..."

:: 5) 打包出可双击的 exe（一次性）
.venv\Scripts\python -m PyInstaller --noconfirm ^
  --distpath E:\BianChengGongJu\JarvisBuild\dist ^
  --workpath E:\BianChengGongJu\JarvisBuild\build ^
  packaging\jarvis.spec

:: 6) 建桌面快捷方式（一次性）
powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
```

> **`setx` 之后必须重开终端**，否则新变量对已存在的进程不可见——同一条命令在
> 旧终端里会静默地"没生效"。

开发期想直接跑源码（不打包）仍然可以：

```bat
.venv\Scripts\python -m jarvis --desktop --voice
```

或者双击仓库根目录的「启动小夜.bat」——它等价于上面这条，多做三件事：先检查 `.venv` 和
`jarvis\ui\web\index.html` 在不在（缺哪个就说什么缺，不会闪退），
`cd` 到脚本自己所在的目录（放桌面快捷方式也能找到路），
以及退出码非 0 时把日志路径打出来并按一下暂停——双击运行的人需要看到失败，
而不是看见一个黑框一闪而过。

缺 `.[desktop]` 的两个包会怎样：`pywebview` 缺 → 窗口打不开并打印构建提示；
`psutil` 缺 → 仪表盘显示「遥测读取失败：system monitoring requires the 'psutil' package」，
**窗口照常打开**，不会整个程序崩掉。

---

## 1. 安装顺序为什么是这样

`.[desktop]` 只解决 Python 侧。界面是 `frontend/` 用 Vite 构建到 `jarvis/ui/web/`，
这个目录**故意不进 git**（哈希文件名每次构建都变），但**在 `package-data` 里**，
所以打 wheel 前必须先构建，否则 wheel 里装出一个没有界面的桌面端。

`scripts/build_desktop.py` 会把这件事做完并自查：构建完重新读 `index.html`，
逐个确认它引用的 `assets/*.js|css` 真的在磁盘上。产物不完整比没有产物更坏——
`index.html` 在，页面是白的。

```bat
.venv\Scripts\python scripts\build_desktop.py            :: 装依赖 + 构建 + 校验
.venv\Scripts\python scripts\build_desktop.py --check    :: 只校验，不跑 npm
```

需要 Node.js（本机 v24 / npm 11）。没有 Node 的人只能拿 wheel，不能从源码构建。

---

## 2. 命令行

| 命令 | 作用 |
|---|---|
| `python -m jarvis` | 控制台模式：按配置装配语音链路，`Ctrl+C` 退出 |
| `python -m jarvis --desktop` | 打开 HUD 窗口（关窗即退出，见 §7） |
| `python -m jarvis --desktop --voice` | 同上，并允许页面申请麦克风 |
| `python -m jarvis --desktop -v` | 打开 DevTools（页面侧问题唯一的看法） |
| `python -m jarvis --desktop --speak "…"` | 语音栈一起来就把这句话朗读一遍。**验收用的**：不碰麦克风、不需要模型 key，却能跑完合成→音频桥→页面播放这一段（见 §7） |
| `python -m jarvis --wav in.wav --out ans.wav` | 离线演示：VAD→ASR→LLM→TTS，不碰麦克风 |
| `python -m jarvis --config my.yaml` | 指定用户配置文件 |
| `启动小夜.bat`（仓库根目录） | 等价于 `--desktop --voice`，带前置检查和失败留痕 |

打包出的 exe 等价于 `--desktop`（默认参数写在 `packaging/entry.py`），
`--voice` 可以加到快捷方式的「目标」后面。exe 那份的启动方式是
`dist\小夜\小夜.exe`，它没有 `.venv` 可查，所以不需要 bat。

---

## 3. 配置键速查

分层优先级：**内置 defaults.yaml < 用户 config.yaml < 环境变量 `JARVIS_段__键`**。
`JARVIS_HOME` / `JARVIS_CONFIG` 是例外，它们决定「在哪」而不是「是什么」。

用户配置默认位置：`%LOCALAPPDATA%\Jarvis\config\config.yaml`

| 键 | 默认 | 说明 |
|---|---|---|
| `llm.default_provider` | `qwenai` | 供应商；`api_key_env` 只写**变量名**，密钥本身绝不进 yaml |
| `orchestration.enabled` | `false` | 语音链路的总闸。桌面端 = `--voice` **或** 这个键为真 |
| `wakeword.engine` | `asr` | 中文唤醒走转写匹配；`openwakeword` 只认英文模型名 |
| `wakeword.keywords` | `你好小夜/小叶/晓叶/业` | 见 §6，变体是实测产物不是猜的 |
| `wakeword.enabled` | `false` | 独立唤醒环。桌面端**不注册**这个组件，见 §7 |
| `vad.threshold` | `0.5` | Silero 语音概率门限 |
| `vad.max_silence_ms` | `800` | 句尾要 800ms 静音才判「说完」。500 会把句中停顿当成句号，一句话劈成两轮独立应答（两边字都对，坏在断句）；800 的代价是每轮晚 0.3 秒开始回答。复现：`scripts/bench_asr_endpoint.py` |
| `asr.engine` | `sensevoice` | 离线，权重 ~897 MB |
| `tts.engine` | `edge` | 云端，合成一段回复要联网 |
| `computer.enabled` | `false` | 桌面控制的总闸。关掉时 6 个工具一条都不注册 |
| `computer.dry_run` | `true` | 只报告不真的动。返回的是「未真实执行」，不是「done」 |
| `computer.allow_mouse` / `.allow_keyboard` | `false` | 各自单独开：能移指针不等于能敲字 |
| `logging.level` | `INFO` | `-v` 会强制抬到 DEBUG |

改完配置不用重启窗口之外的东西：配置只在 `ConfigService.start()` 读一次。

---

## 4. 数据目录

```
%LOCALAPPDATA%\Jarvis\
  config\config.yaml      用户覆盖配置
  database\               SQLite / FAISS
  logs\jarvis.log         滚动日志；无窗口时的唯一现场
  models\                 模型权重（仅当环境里没预设缓存目录时由本程序指定）
  cache\                  可丢弃缓存 —— 清理功能有权扫这里
  audit\deletions.jsonl   删除审计（见 §5）
```

`audit\` **故意不放在 `cache\`**：`cache` 的语义是「随时可删」，而「上周你删了我什么」
是必须能回答的证据。同理，`%LOCALAPPDATA%\Jarvis` 整个目录被显式登记为受保护路径——
它和别的缓存只差一个分类，差一点就会被自己的清理功能吃掉。

**打包版另有一条默认值**（`packaging/entry.py`）：环境里没有 `JARVIS_HOME`、
而 `%LOCALAPPDATA%\Jarvis` 也还不存在时，数据根取 **exe 旁边的 `数据\` 目录**。
理由很直白：光语音权重就约 900 MB，一个刚解压完就往系统盘写 900 MB 的产品，
违背了「不占 C 盘」这条硬要求；而 onedir 本来就落在用户选的那个盘上。
两种情况**不会**搬家：`JARVIS_HOME` 已经设了（那是操作者给的答案，不是疏忽），
或者 `%LOCALAPPDATA%\Jarvis` 已经存在（老安装，改指向会让它当场失忆并重新下载权重）。
这条默认值有 5 条单元测试（`tests/test_packaging_entry.py`）。

> 2026-10-01 踩过的坑，记在这里别再踩：环境变量是 `setx` 设的**用户级**值，
> 但已经在运行的 Explorer 不一定刷新过。当晚从旧 Explorer 血统启动的 exe 就仍然
> 落到了 C:，日志写在 `%LOCALAPPDATA%\Jarvis\logs\`。判断依据只看一处：
> 日志里 `HUD bundle served at` 之前那几行的数据根。

---

## 5. 磁盘清理：流程与审计

**四道闸门，任何一道都能拒绝，没有一道能自动删。**

1. **扫描**（只读）→ 按类别列出可删条目，受保护路径连列都不列（2026-09-29 本机实测排除 38 条）
2. **勾选** → 每一项是一个「可决策单位」：一个目录一行；同一层里散落的临时文件
   超过 10 个就**聚合成一行**（2026-09-29 本机实测：整表 677 行、可释放 5.1 GB，
   其中 `%TEMP%` 的 1,893 个散落文件合成**一行**；按体积倒序，最大的排最前）。
   有「全选」，但按钮上直接写着**全选 N 项 / 多少体积**——要点下去之前就能看见代价。
   全选能选到的只有扫描列出来的那些，受保护路径根本不在清单里。
3. **二次确认** → 显示「N 项 / 约 M 个文件 / X GB」，明确写不可撤销
4. **删除** → 逐个成员**重新**做存在性、受保护前缀、mtime 三项检查

第 4 步的三个检查各挡一类事故：

- **受保护前缀**：`C:\Windows\win.ini` 即使被手工塞进请求里也会被拒（已实测），
  并且 `/` 与 `\` 混写挡不住——比较前统一分隔符。
- **mtime 上界**：你批准的是**那次扫描**的清单。扫描完成之后新产生的文件不在清单里，
  就不该被删。聚合条目还带 `scanned_at`，与当前扫描对不上直接拒（「清单已过期，请重新扫描」）。
- **展开上限** 20,000 个文件，超了整条拒绝而不是删一半。

审计每删一次追加一行 JSON：

```json
{"at":"2026-09-28T03:31:12+00:00","freed_bytes":2147483648,"count":1671,
 "approved":["C:\\Users\\me\\AppData\\Local\\Temp"],
 "paths":["C:\\Users\\me\\AppData\\Local\\Temp\\tmp0001.tmp", "..."]}
```

`approved` 是人勾的那一行，`paths` 是真正落盘删掉的每一个文件。只记前者，
就等于用「我清了 Temp」回答「你删了我什么」。

**「协助分析」不在这四道闸门里，它是第 0 步。** 扫描出来的清单可以交给模型读一遍，
它做的事只有三件：解释每个分类是什么、挑出看着异常大或名字不像垃圾的条目、在看不懂
某个目录时自己去 `list_directory` 看一眼。它**拿不到勾选权，也拿不到删除权**——提示词
里明写"不要建议删除任何具体路径，也不要给出删除命令"，而就算它建议了，第 2、3、4 步
一道都不会因此松动。这条按钮的产物是一段文字，不是动作。

---

## 6. 唤醒词

`wakeword.engine: asr` 的做法是：VAD 切出一句 → SenseVoice 转写 → 在文本里找关键词。
好处是不用训练模型、离线、并且**唤醒词和指令可以一句话说完**
（「你好小夜 今天星期几」只录一次）。代价是每句话都要跑一次识别（8 秒音频 ~1.5 秒 CPU）。

关键词为什么要写四个变体——这是测量结果：

```
$ .venv\Scripts\python scripts\verify_wake_words.py
engine=asr keywords=['你好小夜', '你好小叶', '你好晓叶', '你好小业']
  OK   '你好小夜' zh-CN-XiaoxiaoNeural +0%  -> 听到 '你好小叶' 命中 你好小叶
  OK   '你好小夜' zh-CN-YunjianNeural +20%  -> 听到 '你郝小叶' 命中 你好小叶
  ...
24/24 woke
```

四种写法，识别结果**全部**是「你好小叶」。只配「你好小夜」的话实测 0/2 命中，
也就是永远叫不醒，而代码、配置、日志看起来都完全正确。

`verify_wake_words.py` 不需要麦克风：Edge-TTS 合成 6 种音色/语速 → 真 VAD + 真 SenseVoice
→ 真 `AsrWakeWordEngine`。失败退出码非 0，可以当发布闸门。

一个坑值得写下来：合成素材必须**补 ≥900ms 尾部静音**，否则 VAD 等不到
`max_silence_ms` 的安静段，根本不会判定「说完」，引擎一次都没被调用——
这个假阴性长得跟「唤醒词不对」一模一样。

---

## 7. 麦克风与「关窗不退出」

- **点 X 是收进托盘，不是退出**（2026-10-02 起）。进程继续跑，麦克风继续待命，
  右下角托盘图标用颜色与悬停文字说清现在是不是在听：灰=没开麦，青=在等唤醒词，
  青亮=正在收你这句话，琥珀=正在识别与思考。真正的退出只有托盘右键「退出小夜」，
  它走 pywebview 自己的关窗路径，所以 `run()` 的 `finally` 照常释放麦克风与语音栈。
  托盘起不来（没装 pystray、或被 shell 拒绝）时 X 退回旧行为——直接退出，
  因为「藏起来且找不回来」比「没有这个特性」更糟。
  副作用：`taskkill`（不带 `/F`）等价于点 X，从此**停不掉**小夜，要用 `/F` 或托盘退出。
- **第一次要在任务栏点 `^`（Windows 11 的溢出区）才看得到托盘图标**。2026-10-02 在
  1920x1080 的实机上截图核对：可见的那一排只有 微信 / 麦克风 / 输入法 / 网络 / 音量 /
  电池，小夜被系统收进了溢出区——这是 Windows 对新图标的默认策略，不是没起来
  （日志里 `tray icon up` 是有的）。想让它常驻可见：设置 → 个性化 → 任务栏 →
  「其他系统托盘图标」里打开，或者点开 `^` 把图标直接拖到任务栏上。
- 桌面模式的装配是 Config → Logging → System → Disk → LLM → Chat → Voice。
  **`WakeWordService` / `VadService` 根本不注册**——它们各自跑一条采集环。
  抢麦这件事用「列表里没有」来保证，而不是靠某个配置值别被写错。
- 语音不常驻：加载 ~1.7 GB RSS、打包版本机实测**按压 → 待唤醒 31 秒**
  （03:14:22 → 03:14:53，权重已在 E: 缓存；首次运行还要下载约 900 MB，另计），
  而且麦克风是客户 IT 会当面问的东西。所以只有点「启用语音」才开——只开第一次。
- 加载期间界面不卡：`voice_enable()` 立即返回（实测 <1ms），模型在
  `jarvis-voice-boot` 线程里加载，状态经 `加载中 → 待唤醒/失败(原因)` 推给页面。
  GIL 争用会让 1.5s 的遥测轮询偶尔跳拍，`stores/system.ts` 有跳拍守卫。
- 「释放麦克风」= 停掉链路并把状态置为 `muted`，日志会写为什么停的。

状态有两条独立的轴，别混：`phase`（能不能跟我说话：off/loading/running/failed/muted）
和 `turn`（现在在听还是在想：idle/listening/processing）。合成一条，
失败就长得像「只是没在听」。

### 按一下说（免唤醒的一轮对话）

对话面板右上角的「按一下说」= `voice_talk()` → `VoicePipeline.speak_now()`：
把管线从 `IDLE` 直接推进 `LISTENING`，跳过唤醒词，后面走的还是同一条
VAD → ASR → agent → TTS。之所以**复用运行中的管线而不是另开一次录音**：
麦克风只有一个持有者，第二条采集环会直接抢麦。

三条边界：

- 只有 `IDLE` 能被按下。正在听时按它没有意义；正在回答时按它等于抢 barge-in 的活，
  两个功能同一个点击去夺麦克风，症状就是「听见了但答非所问」。所以这两种情况返回 `False`。
- 语音没起来（off / loading / muted / failed）时按它**不会静默失败**，
  按钮旁边会写出为什么、以及该点什么。
- 它不改 `phase`：一轮对话不是可用性变化，否则一次点击会让状态灯看起来「重新就绪」。

打字问答和语音问答共用一份记录（`StateBridge.history`）。界面自己再存一份的话，
面板上就会出现两份历史，看的人得自己分辨哪一份才是刚才那场对话；
「清空」因此同时清 Python 侧的上下文和这份记录，但**不碰麦克风**。

### 它说话时，声音是从界面里出来的（2026-10-01）

以前答案由 Python 直接写声卡。能响，但界面**拿不到任何样本**，所以声核只能照状态机
画个圈——它自己的注释里就写着这件事：没有电平却画波形，是"用最像真的方式撒谎说它在听"。

现在合成出的 PCM 走 `jarvis/ui/audio_bridge.py` → `evaluate_js` → 页面的
`AudioContext`，中间挂一个 `AnalyserNode` 再出声。**同一路信号**：律动、3D 人物的嘴、
和你耳朵听到的，不可能对不上，因为只有一个声音。

| 环节 | 位置 |
|---|---|
| 切片 + base64 + 顺序 | `jarvis/ui/audio_bridge.py`（0.5 秒一片） |
| 路由：界面还是扬声器 | `BridgePlayer`（`jarvis/orchestration/player.py`） |
| 解码、无缝排期、取电平 | `frontend/src/audio/speech.ts` |
| 谱冠 / 口型 | `VoiceCore.vue`、`avatar/` |

四条不能改的规矩：

- **默认是扬声器。** 页面必须先自己调 `audio_ready(true)`，在那之前一个字节都不推。
  webview 还没加载完是真实状态，而"助手不出声"是它最坏的失败方式。
- **答不上来 = 没就绪。** 没有声明就是没有，不是"大概可以"。
- **打断要两半一起停。** 样本已经进了页面的音频队列，Python 这边取消合成收不回来，
  所以唤醒词会顺手发一条 `flush`；「停下」按钮同理（`speech_stop()` 两头都做）。
- **量到的才是真的。** 页面不是输出设备时，声核退回状态驱动的圆环，
  并在下面写明「界面读不到电平」——不给一张假图。

**怎么验**（不需要模型 key，也不需要麦克风）：

```bat
.venv\Scripts\python -m jarvis --desktop --voice --speak "你好，我是小夜。"
```

`--speak` 等语音栈起来后把这句话交给朗读链路。日志里应当出现
`audio: N bytes in M slices delivered to the page`；只有 `switched to speaker`
就说明页面没接住，那是界面侧的问题，不是合成的问题。

### 朗读只念文字和数字，不念标点（2026-10-01 深夜）

"不要读标点符号，只要读数字和文字"。做法是 `jarvis/core/text.py` 里的 `speakable()`，
**只在一个地方调用**：`jarvis/tts/service.py:109 TtsService.synthesize`。

为什么是那里：语音回答和打字回答、演示用的 WAV，全都过这一个腰部；而**同一个字符串
还要进对话历史和屏幕**（`voice_pipeline.py` 里 `_append_history` 与 `synthesize` 拿的是
同一个对象），在源头删标点会让记录和界面一起变难看——听的人不要标点，看的人要。

它删什么，每一条都是单独一行而不是一整个字符类：

| 输入 | 念出来 | 为什么 |
|---|---|---|
| `45.0%（12 核）` | `百分之45.0 12 核` | `%` 在中文里要说在数字**前面**；小数点夹在数字中间必须留着，`45.0` 删成 `45 0` 就从四十五变成四十五和零 |
| `**重要**：见 [官网](https://…)` | `重要 见 官网` | markdown 的星号、方括号、网址本身都不该念，链接的文字要留 |
| ` ```powershell … ``` ` | （整段消失） | 念代码给耳朵听不是信息 |
| `19:32`、`3,000` | 原样 | 时间和千分位是数字的一部分 |
| `C:\Users\me` | `C Users me` | 路径要说，反斜杠不要 |
| `😀` | （消失） | 没有哪种读法比沉默更短 |

其余标点一律换成**空格**——不是删掉：逗号原来买的是一个换气，换成空格那个换气还在。
副作用写在这里：Edge-TTS / CosyVoice 自己靠标点断句，全删会让长句更赶。留了空格之后
实测停顿还在，但如果以后有人想"干脆全删干净"，那是把断句也删了。

**没有验到的**：这一轮没有能出声的模型 key，所以 `speakable()` 的行为是由
`tests/test_core_text.py`（14 条）钉住的，不是耳朵听过的。`--speak` 那条命令仍然可以
用来听（它只需要 TTS，不需要模型）。

### 3D 人物：全息投影是内置的，真模型是拖进来的

需求写的是"仿雏田的 3D 动漫人物"。版权是一半，另一半更难：**风格化人脸是图形学里
最难假的东西**，而这里没有美术、没有绑定、只有几何。第一版照直做了，截图里是一颗深色
的蛋上贴两个白眼窝加一道红口子。驱动没错，媒介错了。

所以 `frontend/src/avatar/character.ts` 画的是一个**全息胸像**：青色经纬线 + 微弱加色
填充 + 会发光的眼，剪影仍然是齐肩短发的人。它不假装是皮肤，就不会在"像不像皮肤"上失败。

嘴保留成五个 morph target，名字照 VRM 的 blendshape（`aa ih ou ee oh`）。**这条接缝
当天就被人用上了**：`avatar/vrm.ts` 现在会去 `avatar/` 找 `.vrm`，找到就把全息投影
换成那个模型，找不到（或加载失败）就继续用全息投影，并且**在面板左下角写明现在是哪
一个**（`赛博线条 · avatar/小夜.vrm` / `全息投影（内置）`）——静默回退是最容易被当成"这个功
能不能用"的失败。

**拖进来的模型会被重新画成线条**（2026-10-01 深夜，"这个 3D 人物太丑了，改成赛博线条的"）。
`vrm.ts` 不再给 mtoon 材质上色，而是**换掉材质**，一个网格两层：

- **线层**用原来的那个网格本体（`wireframe` + 加色混合，皮肤色），所以
  `expressionManager` 继续驱动它的 morph target，口型照旧；
- **身体层**是它的 `clone()`——同一个 geometry、同一副 skeleton，所以蒙皮跟着骨骼走
  （用 `EdgesGeometry` 拼 `LineSegments` 做不到这一点：那东西只跟网格节点，骨骼在
  它底下动它不动）。`expressionManager` 不认识这个副本，所以 `advance()` 每帧把
  影响值抄过去。
- 顺序是 `renderOrder` 定的：身体先画、写深度、并且 `polygonOffset` 往后推一点点
  （否则它和自己的线在同一深度上闪），线后画。**这一条是量出来的**：没有深度次序的
  第一版，脸背面那几十条线照样往脸上叠，截图里是一个白色的团。

材质被换掉之后原来的 mtoon 材质再没人引用，就地 `dispose()`：换一次皮肤会重建整个人物，
不释放就是每次换皮肤漏十几条编译好的 shader。也因为全是无光照材质，场景里不再需要灯
（`AvatarStage` 的那两盏灯跟着删了）。

放模型的地方是 `frontend/public/avatar/`，**故意不进 git**（10 MB 二进制；换形象 = 换
文件，不该改代码）。Vite 会把它原样拷进 `jarvis/ui/web/avatar/`，打包时整个目录跟着
`jarvis/ui/web` 一起进 exe。

两件事不是硬编码的，因为它们对"下一个模型"也必须成立：

- **取景**：`Character.framing` 由模型自己的包围盒算出来（头顶往下 50%、留 12% 余量），
  相机距离按面板的短边反解。所以竖面板裁的是腿，宽面板裁的是手，谁都不会把脸裁掉。
- **手臂**：VRM 都是 T-pose 绑定的，250 px 的面板里摆 T-pose 就是橱窗模特。
  `swing()` 不用 `rotation.z = -1.2` 这种写法——**骨骼的局部轴向是建模软件的导入链决定
  的，不是 VRM 规范保证的**，同一个数字在这个模型上是垂手，在下一个模型上就是把肘关节
  掰成膝盖。它量的是"肩到肘在世界空间朝哪"，绕垂直于（手臂、目标方向）的轴转，再把世界
  旋转换算回这根骨头的局部四元数。

口型不是音素识别：音频频带认不出 phoneme。它做的是"声音大就张得开、能量在哪张嘴型就
偏哪、声音停就闭上"——这是"口型同步"在肉眼能审的帧率下的样子。要音素级就得对同一段
文本跑对齐，是另一件更大的事，别对外那么说。

### 免点击待命：那一次按压会被记住

「双击打开、张嘴就说」中间只隔着一次点击，所以记的是**点击本身**：
`enable()` 成功进入加载就把 `voice.auto_arm: true` 写进 `<数据目录>/preferences.json`，
`mute()` 写回 `false`。桌面壳在 `voice.subscribe()` **之后**调
`VoiceService.arm_if_remembered()`——顺序是有意的，开机自启的那条状态事件也得有人
已经订阅着，否则页面会显示「未启用」而麦克风其实已经开了。

三条边界，每条都有测试钉着（`tests/test_app_voice_service.py::TestRemembersConsent`）：

| 情况 | 行为 | 为什么 |
|---|---|---|
| 按压被配置拒了（没 `--voice` 且 `orchestration.enabled=false`） | **不记** | 一次失败的点击不能让之后每次开机都自己开麦 |
| 记着的意图遇上未放行的一次启动 | 保持 `off`，只写一条 WARNING | 没人要求开机被拒绝，把面板刷红是骗人 |
| 关窗退出 | **不动**这个选择 | 退出和「我不想要语音」是两个人说的话 |

为什么不写进 `config.yaml`：那是人写的文件，`reject_unknown_keys` 让未登记的键直接启动
失败，而一个按钮不该去改一份可能正开在编辑器里的配置。同意属于 `preferences.json`，
一个只有两个布尔键的 JSON 文件，写失败只损失一次记忆、不损失这次启动
（`tests/test_app_preferences.py`，11 条）。

**这一条不等于 §「常驻待命」已经做完。** 关掉窗口后继续听、或者不打开界面也能被唤醒，
仍然只是 `docs/design/always-on-wake.md` 里的设计；现在做到的是「开着窗、免点击、
到待唤醒」。

### 空转 CPU：主因是遥测自己（但**当时的数字只量了半个程序**）

结论先说：**开销不在语音链路，也不在 pywebview 消息泵，而是 HUD 的进程排行表。**

`jarvis/tools/monitor.py` 每次快照都调
`psutil.process_iter(['pid','name','cpu_percent','memory_info'])` 再排序，而这要给
机器上**每一个进程**开一次查询句柄。本机 355 个进程 + 终端防护软件。

打包版 A/B（同一台机器、同一份配置、语音**关闭**、新旧交替跑防止环境漂移；
指标是 `Get-Process .CPU` 的累计秒数对墙钟秒数）：

| 打包产物 | 空转占一个核 | RSS | 进程句柄数 |
|---|---|---|---|
| 旧（每次都遍历进程表） | **60.06%**（15.9→124.0s / 180s） | 137→141 MB | 687–974，锯齿 |
| 新（进程表缓存 10s） | **0.22%**（4.1→4.5s / 180s） | 137.2 MB 平 | 657–664，稳定 |
| 新（再跑一遍复核） | **0.17%** | 137.1 MB 平 | 663–664，稳定 |

句柄数从锯齿变成一条直线，是"就是这次遍历"的旁证：遍历要给每个进程开句柄，
遍历完释放，所以它跟着轮询一起呼吸。

窗口每 1.5 秒拉一次快照，而"按内存排序的前 8 名"是分钟级才变的东西——两者本来就不该
共用一个心跳。现在拆开了：CPU/内存/磁盘照旧 1.5s，进程表 10s 一次；窗口不可见时前端
把轮询降到 10s（`stores/system.ts`），时钟也停走（`TopBar.vue`）。
`tests/test_monitor_refresh.py` 钉住"快轮询只遍历一次""TTL 到点才重走""遍历失败不进缓存"
"top_processes=0 完全不走"。

**顺带修掉一条从上线起就没说过真话的数字**：占用排行的 CPU 列整列 0.0%。
psutil 的 `cpu_percent()` 是"相对**同一个对象**上一次调用"的差值，而 `process_iter()`
每次遍历都发一批新对象，所以从 `info` 里读它永远是 0.0。现在 `SystemMonitor` 按 pid
留住句柄（`_cpu_since_last_walk`），这一列的含义变成**「距上次遍历这 10 秒内的占用率」**，
第一次见到某个 pid 时如实报 0.0（没有可比的过去），进程退出即丢弃句柄。
这两条都有测试钉住；旧代码没有，因为没有任何测试断言过这一列非零。

⚠️→✅ **这一条在打包版上也验通了，但过程值得记**：04:50 那次截图里 CPU 列**仍然整列 0.0%**，
我差点据此写成"打包版没修好"。04:58:52 同一窗口再截图，这一列是活的——
`小夜.exe 1.6 GB / 9.4%`、`Qoder CN.exe 738 MB / 48.8%`、`Qoder CN.exe 170 MB / 24.0%`，
而且本进程按内存排到了第一行。中间那次 0.0% 不是遍历坏了，是**仪表盘正卡在上一节那个
`snapshot()` 不返回的窗口里**：显示的是第一次遍历（只有 0.0 和加载前的 266 MB）的残留。
教训：**看到"数字不对"先确认它是不是根本没在更新**，否则会把一个卡死问题当成算法问题查。

**订正一条我自己写下的数字**：这一节早先版本给的是进程内测出的
「51.1% → 17.9%」（`perf_counter` 包住 9 次 `snapshot()`）。方向对，但那个微基准
**高估了修复之后的成本**——真实打包产物停在 0.2% 而不是 17.9%，
所以当时那句「剩下约 18% 一个核是有意的取舍」也是错的，已删。
进程内计时适合回答"这次遍历贵在哪"，不适合替真实产物报百分比。

### 顺带修掉的：funasr 覆盖 torch 线程数

机制真实存在（读源码确认）：`silero_vad` 在 import 时设 `torch.set_num_threads(1)`，
funasr 在 `AutoModel` 构造和**每次 `generate()` 之前**都把它抬回 `ncpu`（默认 4）
（`auto_model.py:533` 与 `_reset_runtime_configs` 的 `:1326`），抬完不还原。
采集环活在两次推理**之间**，所以 VAD 每 32ms 一帧走的是四线程池。
现在 `SenseVoiceAsrEngine` 在构造后和每次推理后把预算交还给 1
（`_return_threads_to_vad`），推理本身不受影响。
`tests/test_asr_engines.py` 钉了这条，含「推理抛异常也要交还」那一路。

**但它不是空转的主因**——上面那张表测的是语音完全关掉的状态。这条改动是对的，
量级上是次要的，别把它当成交付理由。

### ⚠️ 在这台机器上量 CPU 的坑（这次排查绕了三圈才出来）

1. **从 agent 终端里起的进程，`GetProcessTimes` / `GetThreadTimes` 返回冻结值**：
   一个跑满 10 秒的单线程忙等循环，psutil 读到 `user=0.0` 且两次采样完全不变。
   用 `Start-Process` 起的进程却能读到真实值——**是启动路径决定计不计时**。
2. **拿 `p.threads()` 的时间求差是错的**：线程会增减，新线程把整个生命周期算进来、
   死掉的算成负数，实测出现过 `-63.8%`。
3. `Get-Counter` 时好时坏（`c0000bc6` / `800007d5`）。
4. 我自己的第一版对照脚本因为闭包引用错了对象，把"旧行为"测成了 0.004s——
   **对照实验也要验，不然它会给你一个自洽的假结论。**
5. 同一份 A/B 的第二版把 CSV 里"样本序号"那一列当成了"构建名"来分组，
   于是三个构建被压成四组、跨构建相减，算出 0.02% / 30.9% / 43.8% 三个数——
   每一个都长得像结论。**分组键要先打印一遍 `keys()` 再相减。**

能用的办法：**量墙钟耗时，不量 CPU 百分比**。上面那张表就是 `perf_counter` 包住
`snapshot()` 测出来的，与进程计时坏不坏无关。
但**进程内的微基准不能替真实产物报百分比**——那张 A/B 表要起两个 exe、
同条件、交替跑；这次微基准把修复后的成本估成了 17.9%，真实产物是 0.2%。

---

## 8. 打包成 exe（交付形态：双击即用）

**给最终用户的是这个，不是 `启动小夜.bat`。** bat 是开发期从源码启动的入口，
需要 `.venv` 和一次前端构建；exe 是拷走就能用的产物。

> **不要在 PyInstaller 跑着的时候 `npm run build`。** 这一轮踩过：打包期间重建前端产物，
> Vite 会先清空 `jarvis/ui/web` 再写，而 PyInstaller 正好在那一刻枚举这棵树——结果那次的
> `_internal/jarvis/ui/web/` 里只剩 `index.html` 和 `avatar/`，**`assets/` 整个是空的**，
> 908 MB 的包打开就是一句「界面产物缺失」。两个动作不要同时动同一个目录。
> 打完包先数文件：`find <dist>/_internal/jarvis/ui/web -type f | wc -l` 应当等于仓库里
> `jarvis/ui/web` 的文件数（现在是 6），并且 `assets/` 里的文件名要和 `index.html` 里引的
> 那个哈希**逐字一致**——不一致就是拿了一份旧拷贝或半份拷贝。

> bat 里那串 `set "JARVIS_*"` 有一个坑：`JARVIS_ROOT` 只是 bat 自己拿来拼模型缓存路径的
> 中间变量，配置加载器却按 `JARVIS_` 前缀收集环境变量覆盖项，于是它变成一个顶层配置键
> `root`，schema 不认识 → **双击之后直接一条 `unknown key 'root'` 退出，窗口都不出现**。
> 现在 `env_overrides()` 只认带 `SECTION__KEY` 的名字，其余（`JARVIS_HOME`、
> `JARVIS_CONFIG`、`JARVIS_ROOT`）一律当"东西在哪"而不是"配置是什么"。
> 这条是这一轮从源码启动时撞上的，`tests/test_config_loader.py` 里留了回归测试。

```bat
.venv\Scripts\python -m pip install -e ".[build]"
.venv\Scripts\python scripts\build_desktop.py --check
.venv\Scripts\python -m PyInstaller ^
  --distpath E:\BianChengGongJu\JarvisBuild\r1\dist ^
  --workpath E:\BianChengGongJu\JarvisBuild\r1\build ^
  packaging\jarvis.spec
```

> **不要给 `--noconfirm`，也不要在有旧产物的目录上构建。** `--noconfirm` 会让
> PyInstaller 先 `rmtree` 目标目录；在批量删除被安全策略拦截的环境里，那一步会被
> 打断，构建以一条和打包本身毫无关系的错误结束。指向一个**全新的空目录**最省事。
> 这一条是实测踩出来的：一次构建在 `Including run-time hook` 之后直接失败，
> 原因就是它在清理上一轮的 `build\jarvis\`。

打包完把快捷方式放上桌面，用户就不需要碰命令行（`scripts\create_shortcut.ps1`）：

| 命令 | 作用 |
|---|---|
| `-File scripts\create_shortcut.ps1` | 在桌面放一个「小夜」 |
| `-File scripts\create_shortcut.ps1 -StartMenu` | 顺带放进开始菜单 |
| `-File scripts\create_shortcut.ps1 -Remove` | 撤销 |
| `-ExePath "D:\...\小夜.exe"` | 产物在别处时指定 |

（三条都要用 `-ExecutionPolicy Bypass` 执行。）

脚本用 `[Environment]::GetFolderPath("Desktop")` 取桌面路径 —— OneDrive 账号的桌面
是重定向过的，写死 `%USERPROFILE%\Desktop` 会指向一个不存在的地方。

**应用图标**由 `scripts/make_icon.py` 生成（`packaging/app.ico`，7 种尺寸，纯标准库
画的，不依赖 Pillow）。`scripts/build_desktop.py` 每次构建都会重新生成它，`--check`
会校验它在不在。图标刻意放在 `packaging/` 而不是 `jarvis/ui/web/`：后者不入 git，
图标放那儿会在全新检出时消失，构建静默退回 PyInstaller 的默认图标。

`--specpath` 不能和 `.spec` 文件同时给（PyInstaller 直接报
`makespec options not valid when a .spec file is given`）。spec 里的相对路径是按
`SPECPATH` 解析的，所以 `packaging/entry.py` 会变成 `packaging/packaging/entry.py`；
spec 顶部用 `ROOT = os.path.dirname(os.path.abspath(SPECPATH))` 锚回仓库根，
从哪个目录调用都对。

产物是 `dist\小夜\`（onedir 文件夹），拷走即用；建议压成 zip 交付。

四个必须知道的点：

1. **`package-data` 在冻结模式下不存在**。`defaults.yaml` 和 `jarvis/ui/web/`
   在 spec 的 `datas` 里显式带着，漏一个就是「exe 双击只弹一句构建提示」。
2. **本项目刻意懒导入**（`webview` 在 `desktop.run()` 里、`funasr`/`torch` 在引擎
   `__init__` 里、`AgentGraph` 在 `OrchestrationService.start()` 里），静态分析几乎
   什么都看不见 → 靠 `hiddenimports` + `collect_data_files` 补。`silero_vad` 的
   TorchScript 权重是随 wheel 分发的数据文件，不收集就没有 VAD。
3. **funasr 在冻结环境里注册不出模型**（本项最贵的一个坑，已修，实测通过）：
   funasr 的 `tables.model_classes / tokenizer_classes / frontend_classes` 不是靠
   import 副作用被动填的，而是靠 `funasr/__init__.py` 里的 `import_submodules(__name__)`
   **遍历自己的包目录**主动填的。冻结环境里 `_MEIPASS/funasr` 只有一个 `version.txt`，
   遍历结果为空 → 注册表空 → 点「启用语音」在 `auto_model.py:568` 得到
   `TypeError: 'NoneType' object is not callable`。
   那句 TypeError 指向的是 **tokenizer** 查表（`SenseVoiceTokenizer`，定义在
   `funasr/tokenizer/whisper_tokenizer.py`），不是模型查表——这是最误导人的地方：
   先按「模型没注册」去修，注册表里确实补出了 `SenseVoiceSmall`，**照样失败**。
   走过的弯路一并记下来，免得下次再踩：
     - `inspect.getsource` 在冻结环境抛 `OSError` 是真的（少了 35/48 个模型），
       但它只是**第二层**原因，单独修它不够。
     - `collect_data_files("funasr", includes=["**/*.py"])` 返回 **0 条**（本机实测）。
       这行看着像修复，其实什么都没发出去——PyInstaller 不把源码当数据。
       上一版 spec 里它就是空操作，而 `_internal/funasr` 下 `.py` 为 0 个才是真相。
   真正的修复只有一行：把 `site-packages/funasr` 整个目录作为 data 发到 `_MEIPASS/funasr`
   （424 个 `.py`）。funasr 自己的遍历随即恢复，代码仍由 PYZ 提供
   （PyInstaller 的 importer 排在 `sys.meta_path` 最前，磁盘上的 `.py` 不会抢加载），
   `inspect.getsource` 也顺手有了真实文件可读。
   运行时钩子因此瘦身为两件事：装 `inspect.getsource` 兜底包装（防第二层原因复发）、
   把「源码树在不在」写进 `<数据目录>/logs/funasr-hook.log`。它**不再在启动时 import funasr**
   ——那会在每次开机时为多数用户根本不会点的功能多花几秒。
   `jarvis.asr.engines._registry_hint()` 现在会在失败信息里点名缺的是哪个类。
   `tests/test_packaging_declared_deps.py` 钉了三条断言（源码树要发、`includes=["**/*.py"]`
   那种空操作不许回来、钩子文件必须在），且只看代码不看注释。
   **exe 实测**：点「启用语音」→ 约 2.5 分钟后日志给出 `voice stack running`，
   界面进入「待唤醒」；窗口冷启动 4 秒（16:59:30 → 17:03:37 是语音，17:03 之前窗口已经出来了）。
   **代价也要说清**：语音栈起来后常驻 RSS 约 2.4 GB。另外观察到的空转 CPU 已经结案，
   主因是 HUD 的进程排行表而不是语音链路（见 §7），打包版实测 60.06% → 0.22% 一个核；
   funasr 覆盖 torch 线程数那一条也一并处理了（§7 末），但那条是次要的。
   在这台机器上量进程计时有坑，§7 末列了四条，别再照着 `Get-Counter` 下结论。
   **仍然要留一手**：冻结环境比开发环境脆，演示当天优先用
   `python -m jarvis --desktop --voice`，exe 作为交付形态另测。
4. **未签名一定吃 SmartScreen**（「Windows 已保护你的电脑」→ 更多信息 → 仍要运行）。
   代码签名证书要钱，演示阶段先写文档，别让客户以为中病毒。

体积：**本机实测 882 MB / 7417 个文件**（`dist\小夜`，含 torch/funasr 运行时与 funasr
源码树，不含模型权重）。
   权重另算 ~1.7 GB，放在 `MODELSCOPE_CACHE` 指的目录里，**不打进 exe**——
   打进去体积翻倍，而且和「清理功能扫 %TEMP%」这件事正面冲突。
**不做 onefile**：每次启动要把
1.5–2 GB 解压到 `%TEMP%`，而那正是清理功能要扫的目录。

---

### 那个 0.22% 少算了一整半：WebView2 进程树才是大头（2026-10-01 中午补测）

上面 60.06% → 0.22% 的 A/B 结论仍然成立——它是**同一个进程上的差值**，遍历进程表确实是主因。
但 0.22% 这个绝对值是错的，因为它只统计了 `小夜.exe` 自己，而真正把界面画出来的是
WebView2 那一棵树（1 个 browser 进程 + 6 个 renderer/GPU/utility）。补测（本机，窗口开着、语音未起）：

| 数什么 | 核 |
|---|---|
| Python 侧 `小夜.exe` | 0.002 |
| WebView2 树 · 无 3D 人物 | 0.101 |
| WebView2 树 · 有 3D 人物（20 fps 空转） | 0.126 |
| WebView2 树 · 窗口最小化（全停） | 0.006 |

所以**整机空转的真实数字是 ~0.13 核**，其中 3D 人物约 0.025 核，剩下 ~0.1 核是页面被
每 1.5 秒的遥测驱动着重绘（ECharts 两条曲线 + 进程表 DOM）。对外别说"0.2%"，那是半个进程。
下一步该省的是那 0.1 核，不是 3D。

量法在 `scripts/measure_process.ps1`（对进程树做原始计数器差分，单位是"核"）；
`Get-Process` 的 CPU 是生命周期总量，`Get-Counter` 在八个同名 WebView2 进程下会撞名，
两个都不能用。

## 9. 故障排查

| 现象 | 先看哪里 | 原因 / 处理 |
|---|---|---|
| 双击 exe 只弹一句「找不到界面构建产物」 | `jarvis\ui\web\index.html` 在不在 | 没跑 `scripts/build_desktop.py`，或打包时没带 `datas` |
| 窗口一片白 | DevTools（`--desktop -v`）控制台 | `index.html` 在但 `assets/*.js` 缺 → `build_desktop.py --check` 会指名 |
| **窗口一片黑（只剩 `#04070d` 底色）** | 控制台有没有 `窗口已打开，但界面在 25 秒内没有加载完成` | 见下面「黑屏」一节；25 秒后一定会打出这段诊断 |
| exe 双击后窗口黑，且**没有控制台可看** | `%JARVIS_HOME%\logs\jarvis.log` 里的 `the desktop window never finished loading` | exe 是 `console=False`，诊断只进日志。见「黑屏」一节 |
| 仪表盘显示「遥测读取失败」 | 日志里 `system telemetry unavailable` | `psutil` 没装：`pip install -e ".[desktop]"` |
| **仪表盘数字看着正常但不再变化**（进程表里本进程的内存停在旧值、趋势图永远空白） | 日志里 `snapshot` 有没有第二次 | 已修（`d49b16f`）。真正的原因不是读取挂住，而是**轮询从来没被安排**：`reschedule()` 用 `if (timer === undefined) return` 表达"还没启动就别管"，可第一次启动时 `timer` 恰恰是 `undefined`，于是 `setInterval` 永远没被创建——整个界面只采一帧。`1f0494a` 那个 8 秒看门狗是真的需要的（它让"没有新数据"至少可见），但它当时被当成了病因。 |
| 它不出声，但日志说答案已经有了 | 日志 `desktop audio output switched to ...` | `switched to speaker` = 页面没接住音频，声音照样从本机扬声器出，只是界面读不到电平（谱冠会退回圆环并写明）。`switched to browser` 却没声 = 系统音量/输出设备问题，不是链路问题 |
| 它说话时画面不动 | `--desktop -v` 开 DevTools，控制台跑 `__jarvisAudioDebug()` | 看 `ready` / `bytes_received` / `levels.rms`。`ready:false` 是 AudioContext 没起来（自动播放策略）；`bytes_received:0` 是 Python 侧没推；有字节但 `rms:0` 才是渲染问题 |
| 3D 人物那块是空的、只有一行字 | 同一处 `__jarvisAudioDebug()` + 控制台报错 | 那块写的是「这台机器开不了 3D（原因）」——WebView2 拿不到 WebGL 时会这样。声核的电平不依赖 3D，仍然可信 |
| 想确认朗读链路通不通（没有模型 key） | `--speak "…"` | 见 §7「它说话时，声音是从界面里出来的」。不需要 key、不需要麦克风 |
| 文字问答报「API key environment variable ... is not set」 | 环境变量名 | 密钥只走环境变量；`setx` 后要**重开**终端 |
| 点了「启用语音」一直卡在加载中 | 日志 `voice stack failed to load` | 页面拿不到推送时状态仍在轮询里；失败原因一定在日志 |
| **喊了唤醒语没反应，但麦克风确实是开的** | 加 `-v` 重开，看 `no wake keyword in '…'` | 这一行把两种完全不同的故障分开：**有**这行 = 听见了但没匹配上（识别结果被写成了别的字，把实测到的变体加进 `wakeword.keywords`，见 §6）；**没有**这行也没有 `[听到说话]` = 根本没收到音频帧（设备被别的程序独占、系统播放/录音音量接近 0、或选错了输入设备）。默认日志级别下这条信息是 DEBUG，所以不看 `-v` 就永远分不清这两件事 |
| 语音报「拾音线程已退出」 | 麦克风被谁占了 | 会议软件/其他实例独占；关掉再点启用 |
| 叫不应 | `verify_wake_words.py` | 先分清是识别不到（加变体）还是 VAD 没判完（尾静音/音量） |
| 有声音但答得慢 | 日志 `rtf_avg` | SenseVoice 在 CPU 上 ~1.3× 实时；不是卡死 |
| 页面右上角显示「浏览器预览模式」 | 是否真在 pywebview 里 | 那是 `npm run dev` 的 mock 数据，**不能当演示效果** |
| exe 启动报缺 DLL | 是否 `pip install -e .` 后动过 venv | 冻结产物不认虚拟环境，重打 |

### 最小化 = 后台静音待命（打包版实测）

点标题栏「最小化」之后：`isMinimized=true`、窗口边界被挪到 `(-32000,-32000)`，
而语音链路照跑——60 秒里进程累计用了 2.44 CPU 秒（**4.1% 一个核**，RSS 1609 MB），
日志从 `voice stack running`（04:52:24）到恢复前台之间**没有一条 ERROR、也没有停链记录**。
也就是说"缩到后台、它还在听"这件事不需要任何新代码：拾音环在 Python 侧，
窗口可见与否不影响它；前端在 `document.hidden` 时把轮询降到 10 秒、时钟停走，
所以后台那份开销主要是 VAD 本身。

**这条不是"常驻待命"**：最小化仍然要求进程活着、要求那一次「启用语音」的按压（或其记忆）。
关掉窗口还是退出并释放麦克风。真正的"进程不在、窗口不开也能被喊醒"见
`docs/design/always-on-wake.md`，那一条还在纸上。

⚠️ **而且现在有时限**：待唤醒态实测每分钟涨 48.5 MB 内存（见 §11a），
所以"最小化挂机"只适合几十分钟，不适合过夜。查清之前不要对外承诺常驻。

### 曾经未结案的卡死：界面只有一帧数据（根因已找到，`d49b16f`）

**现象（打包版 build 17，pid 36964）**：04:33:07 与 04:35:53 两张截图里，
「占用排行」整块逐字节相同，其中 `小夜.exe` 仍写 266 MB，而它那一刻真实 RSS 是 1.7 GB。
同期：顶栏时钟在走；点「释放麦克风」Python 侧 04:36:37 立刻响应并写下停链日志。
所以**桥是活的，只有 `snapshot()` 这一条链停了**。

**已经修掉的是它的表现**（`frontend/src/stores/system.ts`，提交 `1f0494a`）：轮询用一个
`inFlight` 互量防重入，而一次永不 resolve 的调用会让 `finally` 永远不执行——于是此后每一拍
都被挡掉，界面安静地停在最后一次的数字上，灯还写着「遥测正常」。现在这一拍有 8 秒时限，
超时即放弃、置「遥测中断」并继续轮询；每次请求带序号，迟到的旧答案不许再覆盖屏幕。

**根因第二天中午找到了，而且不是 `snapshot()` 卡住**：`stores/system.ts` 里
`reschedule()` 的第一行是 `if (timer === undefined) return`——它想表达"还没启动就别管"，
可 `start()` 调它的时候 `timer` 恰恰还是 `undefined`，于是**第一个 `setInterval` 从来没被创建**。
界面从挂载起只采了一次，之后每一格数字都是那一帧的旧值。上面那条"两张截图逐字节相同、
`小夜.exe` 还写着 266 MB 而真实 RSS 已 1.7 GB"，就是这个 bug 的长相，不需要假设有任何一次
读取挂住。看门狗当时能"救回来"，是因为 `start()` 被再调一次（改遥测间隔）或
`visibilitychange` 会各补一拍——那是巧合，不是恢复。

**如果同类现象再出现**（现在应当不会再有），按这个顺序取证，别再靠猜：

1. `pip install py-spy`（换一台能上网的机器，或 `--find-links` 本地 wheel），
   `py-spy dump --pid <小夜.exe 的 PID>` —— 直接看 `jarvis-...` 线程卡在哪个 C 调用。
   这台机器上取不到栈，公司网络也装不了，这是本次没往下走的真实原因。
2. 分半隔离：把 `SystemMonitor(top_processes=0)` 跑一遍（不遍历进程表）。
   若不再卡，锁定 `_read_top_processes`；若还卡，看 `_read_disks`——
   Windows 上 `disk_usage()` 对一个已挂载但未就绪的卷会**无限期阻塞**，
   而这条路径每次快照都会走。本机当时挂着 GeoServer/minio，值得先怀疑。
3. 复现窗口很长：卡死发生在语音栈起来之后（04:32:36 起链，约 04:32:30–04:33:07 之间停更），
   GIL 争用最激烈的就是这一段。

在原因查清之前，这条只影响**界面新鲜度**：语音链路、桥的其他方法、关窗释放麦克风都照常。

### 黑屏：两个原因，都已修掉一个

窗口打开、标题栏正常、内容全黑，**日志里一个字都没有** —— 这是最难查的一类，因为
错误只存在于 WebView2 自己的控制台里，Python 侧完全看不见。已经踩过两次：

**原因一：页面用 `file://` 加载（已修复）。**
Vite 产出的是 ES module（`<script type="module">`），而 HTML 规范要求模块脚本必须走
CORS；`file://` 页面的 origin 是 `null`，本地文件又没有 `Access-Control-Allow-Origin`
响应头 → 脚本和样式全被拦，`#app` 永远是空的。

现在 `jarvis/ui/desktop.py` 通过 `jarvis/ui/static_server.py` **用回环 HTTP 承载界面**
（绑定 `127.0.0.1`、临时端口、只服务 `jarvis/ui/web`、禁目录列表、`no-store`），
不再用 `index.as_uri()`。

顺带去掉的坑：`vite.config.ts` 里的 `stripCrossorigin` 插件会把 Vite 默认加上的
`crossorigin` 属性删掉。它不是黑屏的**唯一**原因（模块脚本无论如何都走 CORS），
但它会让「双击 index.html 直接看」也变成黑屏。`bundle_hint()` 和
`tests/test_ui_bundle.py` 都会检查它。

**原因二：WebView2 渲染进程起不来（环境相关，不是代码问题）。**
Chromium 的沙箱需要进程/job-object 权限，某些受限环境（被限制的 shell、加固过的
杀软、部分 CI runner）拿不到，渲染进程一起来就死。表现是：`index.html` 被请求了一次，
但 `/assets/*.js` 一个都没请求，`loaded` 事件永不触发。

排查：

```bat
:: 先确认 WebView2 运行时在
dir "C:\Program Files (x86)\Microsoft\EdgeWebView\Application"

:: 再验证是不是沙箱问题
set WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--no-sandbox
.venv\Scripts\python -m jarvis --desktop
```

**`--no-sandbox` 是排障手段，不是配置项。** 它会摘掉渲染进程的沙箱保护，不要长期开着。
普通桌面环境不需要它 —— 需要它说明当前进程环境本身不正常。

判断"页面到底加载了没有"，看控制台里 pywebview 的这几行（`--desktop -v` 能看到）：

```
[pywebview] before_load event fired. injecting pywebview object
[pywebview] _pywebviewready event fired
[pywebview] loaded event fired
```

**有 `loaded` 就是页面加载成功了**；只有 `before_load` 而没有 `loaded`，就是渲染进程死了。

> 注：`--desktop -v` 的 `-v` 以前只开 DevTools、不改日志级别，所以这些 DEBUG 行看不到。
> 现在两处路径共用一个 `_logging_settings_factory`，`-v` 会同时把日志降到 DEBUG。

截图自查（不依赖外部工具）：

```bash
.venv\Scripts\python scripts\capture_window.py --title 小夜 --out logs\hud.png
.venv\Scripts\python scripts\capture_window.py --list   # 找不到窗口时先列一遍
```

---

## 10. 卸载与数据保留

删 `dist\小夜\`（或 `pip uninstall jarvis-assistant`）不会碰：

- `JARVIS_HOME` 指向的数据根（本机 `E:\BianChengGongJu\JarvisData\`）：配置、日志、
  **删除审计**、数据库、模型权重
- 权重实体在 `JarvisData\models\modelscope\`（~1.7 GB，含 SenseVoice / fsmn-vad / ct-punc）

想彻底清干净就手工删这个目录。反过来，重装时把它留着可以省掉一次 1.7 GB 的下载。

**环境变量用 `scripts\setup_env_paths.ps1 -Apply` 一次性指到非系统盘**，它写的是
用户级变量（`HKCU\Environment`），所以**双击桌面快捷方式也能继承**——这正是
"免命令行"能成立的原因：

```
JARVIS_HOME            E:\BianChengGongJu\JarvisData
MODELSCOPE_CACHE       E:\BianChengGongJu\JarvisData\models\modelscope
HF_HOME / TORCH_HOME   同一根下的 models\...
PIP_CACHE_DIR          同一根下的 cache\pip
PLAYWRIGHT_BROWSERS_PATH 同一根下的 cache\playwright
```

⚠️ **改这个根目录是一次迁移，不是改一个变量**。2026-09-30 踩过：变量指到 E: 之后
只改了"新数据往哪写"，**旧数据没搬**，后果有三个，每个都长得像别的毛病——

1. `config.yaml` 还在 C 盘 ⇒ 应用静默忽略全部用户配置，回落到内置默认的
   `default_provider: deepseek`（而它没有 key），**表现为"文字问答永远失败"**，
   日志里只有一行 `has no API key yet`，看不出配置根本没被读到。
2. `MODELSCOPE_CACHE` 指到一个**空目录** ⇒ 下次点「启用语音」重新下载 900 MB，
   而且第一次下载慢得像卡死。
3. 从**已开着的终端/IDE 里**启动的进程看不到 `setx` 的结果（环境是进程启动时
   捕获的），于是它继续往 C 盘写 —— 同一次迁移在同一个机器上呈现两种状态。
   **改完必须重开进程**，或者干脆用桌面快捷方式验证。

迁移做完要用这三条实测确认，别看配置就算完：加载模型看耗时（下载会是几十秒起）、
读 `ConfigService` 解析出的 `default_provider`、比对删除前后 C 盘可用空间。
本机已按此收口，C 盘回收 **1.76 GB**；`E:\BianChengGongJu\AiCache\modelscope`
现在是指向新位置的**目录联接**，不是第二份副本——两条路径都有效，只占一份空间。

---

## 11. 本轮验收记录（2026-10-01 通宵，build 18）

交付物：`E:\BianChengGongJu\JarvisBuild\dist2\小夜\`（onedir，898 MB + 数据目录）。
构建 build18：`exit=0`，耗时 532.4 秒。

### 已实测通过

| 事项 | 证据 |
|---|---|
| 双击 exe 免命令行、免 bat、无黑窗 | 04:48:59 无参数启动 → 04:49:01 `opening desktop window`，**约 2 秒**出窗；`console=False`，全程无控制台窗口；截图确认 HUD 渲染（不是只看日志） |
| 免点击待命 | 04:32:02 无参数启动 → 04:32:03 `re-arming the microphone from the remembered 「启用语音」 choice` → 04:32:36 `voice stack running`：**双击到待唤醒 34 秒，中间零点击** |
| 记忆的两个方向 | 点「释放麦克风」→ `preferences.json` 写 `false` → 下一次启动**不再**自动开麦（04:48:59 那次就是这样）；再点「启用语音」→ 写回 `true` |
| 打包版语音真的能醒（历史一轮） | 02:02:11 build 10 在真实麦克风下 `WAKE: 你好小叶`；本轮未复测声学唤醒，见下面「没验通」第 1 条 |
| 空转 CPU | 打包版 A/B 交替跑：旧 **60.06%** → 新 **0.22%**（复核 0.17%）一个核；句柄数从 687–974 锯齿变 657–664 直线 |
| 语音起来后的开销 | 待唤醒态 60 秒采样：**4.1% 一个核**，RSS 1609 MB（模型常驻） |
| 后台静音待命 | 最小化（`isMinimized=true`，边界 −32000）60 秒：链路不停、日志零 ERROR，见 §7 |
| 不占 C 盘（全新安装默认） | 把 `LOCALAPPDATA` 指到空目录、不设 `JARVIS_HOME` 启动 → 数据落在 `<exe>\数据\`（含 `funasr-hook.log`），`%LOCALAPPDATA%\Jarvis` **没有被创建** |
| 进程排行 CPU 列（打包版） | 04:58:52 截图：`小夜.exe 1.6 GB / 9.4%`、`Qoder CN.exe 48.8%`、`Qoder CN.exe 24.0%`，按内存降序且本进程居首 |
| 看门狗在打包版里生效 | 04:50 那次整列 0.0% 且表格与 04:33 逐字节相同（卡死），04:58 同一窗口自行恢复出数——8 秒超时后放弃旧请求继续轮询，正是 `1f0494a` 的行为 |
| C 盘清理停在人工确认前 | 04:58:52：`0 / 755 项已选 约 0 B`、`全选 755 项 / 5.8 GB`、`已自动排除 38 个受保护路径`、「清理选中(0)」按钮为禁用态。**未点删除** |
| 门禁 | ruff / black / mypy(strict, 222 files) 全绿；pytest **1211 条，0 失败，4 跳过**，exit 0 |

### 没验通 / 不要对外承诺

1. **本轮声学唤醒没复测**：03:16 麦克风探针还能录到播放的「你好小夜」（峰值 0.3154、
   Silero p=0.999），03:20 之后同一录音路径峰值掉到 0.0084——**这台机器的播放音量在测试
   中途接近静音**，是我的激励信号没了，不是助手变聋。要下结论需要人工对麦克风说一次，
   或在能可靠控制系统音量的环境里重跑。
2. **回答链路仍然缺一个可用 API Key**：`QWENAI_API_KEY` 未设置，语音/文字问答都会以
   `LlmAuthError` 失败（现在这句话会显示在界面上，不再只是 `processing failed`）。
   密钥只走环境变量，见 §3。
3. **`snapshot()` 偶发不返回的根因未查**（本轮至少发生一次，约 04:33–04:58 之间自行恢复或被
   看门狗跳过）。界面侧已加看门狗，所以它不再表现为"界面装成没坏"；但**为什么这次读取会挂住**
   仍然不知道。详见 §9「未结案的卡死」与任务清单。
5. **待唤醒态在漏内存，这条否决"过夜挂机"**——见下面 §11a。

## 11a. 通宵浸泡查到的泄漏（阻塞项，未修）

打包版 build 18，**没人说话、界面开着、待唤醒**状态下连续 60 秒一次采样：

| 时间 | RSS | 说明 |
|---|---|---|
| 05:01:21 | 1956.3 MB | 语音起来之后（04:52:24 起链） |
| 05:11:22 | 2441.3 MB | **11 个点，每分钟 +48.5 MB，几乎完全线性** |
| 05:11:46 | — | 点「释放麦克风」，日志 `voice pipeline stopped` |
| 05:12:22 → 05:19:22 | 1652.2 → 1651.4 MB | **此后 7 个点完全平**，CPU 0.19% 一个核 |

两个结论，一个排除、一个坐实：

- **HUD / 遥测侧是干净的**：释放麦克风后同一进程 7 分钟不动，说明 1.5 秒轮询、
  进程表 TTL、`_proc_handles` 那套都不在漏。
- **泄漏在语音链里**，速率折算约 **2.9 GB/小时**。挂 8 小时就是 +23 GB，
  这台机器只有 16 GB 物理内存。**所以"最小化后台静音待命"目前只能当短时能力用，
  不能当常驻卖。**

速率对不上任何一条音频缓冲：16 kHz / s16le / 单声道 = 32 KB/秒，而实测约 810 KB/秒，
是它的 **25 倍**。所以不是"某个音频 buffer 忘了清"这么简单。

**读代码排掉的**：`AsrWakeWordEngine._audio`（`_trim` 封顶 30 秒）、
`VoiceActivitySegmenter` 的 `events`（每次调用新建）、`SystemMonitor._proc_handles`
（按存活 pid 剪枝）。**剩下的头号嫌疑是 torch / silero / funasr 那一层每次推理或每帧
评分留下的东西**——需要 `tracemalloc` 或 `objgraph` 在真机上跑一次才能定，
本轮时间不够，没有动手。

**已在打包版上确认修好**（build 19，05:42:18 无参数启动 → 05:42:50 自动到待唤醒 →
每 45 秒采样）：`1509.6 → 1509.7 → 1509.8 → 1510.3 MB`，**+0.22 MB/分钟**（原来 48.5），
同时 VAD 确实在跑（CPU 3.8% 一个核）。根因是 `SileroVadEngine.process()` 调 TorchScript
模型时没包 `torch.no_grad()`，而 silero 的 wrapper 有状态（把上一帧 RNN state 喂回下一帧），
autograd 开着就每帧挂一张图，图链随开麦时长无限增长——**回归测试**见
`tests/test_vad_engines.py`（把 `no_grad` 摘掉它立刻红）。

复现命令（不用重新打包）：起 exe → 点「启用语音」→ 别说话 →
`soak.ps1 -ProcessId <pid> -EverySeconds 60 -Samples 15`，看 `soak_voice.csv` 的 rss 列。

## 11b. 本轮验收记录（2026-10-01 白天，功能清单 1–5）

用户这轮提了 8 件事，逐条留下"做到哪一步、怎么验的、什么没验"。

| 需求 | 状态 | 证据 |
|---|---|---|
| 去掉那块难看的黑 | 已修 | 见下：根因不止一个 |
| 语音要能对话、要能说话 | 已做 | `--speak` 真机走通，1.93 MB / 81 片送达页面 |
| 说话要符合音律动 | 已做 | 声核 44 根柱由 `AnalyserNode` 实测驱动 |
| 3D 人物对口型 + 动作 | 已做（原创全息） | 闭嘴一条线 / 说话一个洞，两张截图对比 |
| 垃圾清理要全选 | 已做 | `全选 N 项 / X GB` 在摘要行，列表滚动时它不动 |
| 对话区 token 弹窗（含缓存命中、按时间查、上限一个月） | 已做 | 今天/7天/一个月三档，缓存那格三态 |
| 右上角设置（地址 / key / 模型名 + 扩展） | 已做 | 逐字段回执，key 只进环境变量 |
| 全做完做全面测试 + 更新文档 | 进行中 | 本节 + 门禁 1305 条 |

### 那块"黑"其实有三层

1. **垃圾清理的空态**：1.5 fr 高的面板里只有一句话。现在填的是它本来就该讲的三步
   （扫描 / 勾选 / 确认删除）+ 保护路径那条硬规矩。
2. **负载趋势图是空的**——这条最严重，因为它不是美观问题。`stores/system.ts` 的
   `reschedule()` 第一行 `if (timer === undefined) return` 让**第一个 setInterval
   从来没被创建**：整个仪表盘从挂载起只采了一次数据，之后 CPU、内存、进程表全部停在
   那一帧上。修完之后同一个页面挂 12 秒，`cpuHistory` 从 0 → 12、`memoryHistory`
   从 1 → 13，两条曲线都动。
3. **窗口缩小到接近下限时面板互相盖**：`min-height` 比所在网格行还高、`.hud-btn` 被挤到
   按字断行（中文字符都是合法断行点）。现在面板裁切/滚动，标题行可换行。

### 实测到的数字（不要引用未测的）

- 门禁：`ruff` / `black` / `mypy --strict` 全绿，`pytest` **1305 条 0 失败 0 错误 4 跳过**（31–33 秒）。
- 传输无损：真实 edge-tts 中文一句 → 443,520 字节 → 19 片 → 页面解码后**逐字节相等**。
- 频带形状（真实语音，非合成信号）：`[0.42, 0.92, 0.67, 0.53, 0.54]`，能量落在 160–420 Hz
  的浊音带，符合中文女声该有的样子。
- 空转成本（含 WebView2 树）：见 §7 那张表，**~0.13 核**，其中 3D 约 0.025 核。

### 没验到 / 不要对外承诺

- **"打字也朗读"的端到端**：`chat_ask → 朗读` 需要模型 key，本机没有可用的。
  这一条目前只有单元测试覆盖（设置开关、失败答案不朗读、无 key 时文字照答），
  朗读链路本身用 `--speak` 验过了，但"打个问题它答完自己念出来"没在真机上跑通过。
- **3D 人物没有用真模型**：`.vrm` / `.glb` 拖进来这条路只留了接缝（五个 VRM 命名的
  morph target），加载器没写——因为这台机器上没有任何一个模型文件可以拿来验，
  公司网络也下不动。没有验过的加载器不算功能。
  **（这条当天晚上就作废了：加载器写了，真模型也验了，见 §11c 第 ⑤ 条。留着原文是
  因为"下不动所以没做"这个判断在当时是对的——文件最后是走 GitHub API 拿到的。）**
- **音素级口型**：做不到，也不吹。现在是"按能量和频带张嘴"。
- **长时间浸泡**：本轮改动（音频线程、3D 渲染循环）都没有跑过小时级观察。
  上一轮的泄漏教训就是浸泡抓到的，这轮的账还没还。

## 11c. 本轮验收记录（2026-10-01 傍晚，功能清单 ①–⑥）

第二轮 6 件事。逐条写"做到哪、怎么验的、什么没验"。

| 需求 | 状态 | 证据 |
|---|---|---|
| ① 语音识别"有点垃圾"，优化 | 已定位并改 | 根因是**断句不是识别**：`max_silence_ms` 500→800，`scripts/bench_asr_endpoint.py` 复现 |
| ② 垃圾清理让 AI 协助扫描 | 已做 | 「协助分析」按钮 → 扫描摘要交给模型，只读；删除仍要人勾 + 二次确认 |
| ③ 对话区能选模型 | 已做 | 标题行下拉，切完当场回一行「已切到 X，但它缺 KEY，问答会失败」 |
| ④ AI 直接控制电脑 | 已做 | 6 个工具（status/move/click/scroll/key/type），默认全关 + 演练模式 |
| ⑤ 3D 换女性人体 | 已做 | `avatar/小夜.vrm` 真机加载，说话时嘴在动（两张截图对比） |
| ⑥ 我自己扩展的 | 已做 | 「最近动作」面板：工具调了什么、被谁拒了 |
| 全测试 + 文档 + 快捷方式 + 记忆 | 见本节末 | 门禁 1347 条全绿 |

### ①：识别没这么差，是断句在切人

第一次测出来"模型吃了 61% 的字"，**那是我的测试台错了**：片段尾巴没补静音，而
`SPEECH_END` 只在 `max_silence_ms` 那么长的安静之后才触发，所以最后一段永远不闭合。
补上 1.2 秒尾巴之后，同一批样本的真实 CER 是 **1.0%**。这条坑记在
`scripts/bench_asr.py` 的注释里，别再拿"识别率"当结论。

真正的毛病在断句：一句"帮我把临时文件清理一下然后看看磁盘还剩多少空间"，在句中 700 ms
的停顿处被劈成两段，**两边文字都认对了**（CER 4.3%），坏在变成两轮独立应答——第二
半句被当成一个新问题。所以改的是 `vad.max_silence_ms`：500 → 800。代价是每轮回答晚
0.3 秒开始，这是实测出来的取舍，不是"感觉会好一点"。

**没有做**的事：给识别结果加语言模型纠错/同音字归一化。那是拿一个猜测盖住另一个猜测，
测不出收益就不做。

### ④：桌面控制的边界画在哪

- `confirmed` **不是任何工具的参数**，`tests/test_computer_tools.py` 逐条钉住这件事——
  只要有一个工具收这个布尔，模型就会传 `true`（看起来必填的布尔参数，模型一定填），
  确认门就变成装饰。
- 拒绝**当答案返回**，不当异常抛：`computer.allow_mouse=false` 时模型要能读到
  "被安全策略拒绝：……"并转告用户，而不是这一轮死掉。
- 演练模式（`dry_run`，默认开）返回的是"未真实执行"，不是"done"。
- 风险级别标的是 `caution` 而不是 `safe`——`--tools` 那一列是给操作者看要不要开开关的，
  给 `mouse_click` 标 `safe` 会让这份清单比没有更糟。也不是 `dangerous`：那一档要的是
  注册层的 `confirmed` 参数，而这正是本文件不许存在的东西。
- 分层：`tools` 不许 import `computer`，所以 `computer_tools.py` 用本地 `Protocol`
  声明它需要的桌面动作。规则没为这个功能让路。
- 诚实的上限：坐标是绝对像素，而**模型看不见屏幕**。"打开开始菜单、输入、回车"能用，
  "点那个蓝色按钮"在有视觉模型之前不能用。

### ②：AI 协助分析为什么在应用层而不是做成一个工具

面板直接调 `ChatService.ask(..., remembered=False)`。三点：

1. 复用整条工具循环——模型如果觉得某个目录看不出装了什么，它自己会去 `list_directory`；
2. 复用 token 账本，这次分析照样记账；
3. `remembered=False` 是关键：那份摘要是机器写的，不是人说的。留在对话历史里，它会在
   之后每一次回答里被重放，用十二条路径去带一个本来在问别的事的对话。

摘要只给**分类合计 + 全局最大的 12 条**，不给全表：几百行路径会让模型把注意力花在它
无从核对的东西上，而且每次点击都花真钱。

### ⑥：「最近动作」——被拒的那次也要看得见

工具注册表留最近 40 条调用（含被拒的），`HudBridge.activity_log()` 读给页面。这条是
④ 逼出来的：一个会点鼠标的助手，必须能回答"你刚才对我机器做了什么"，而不用操作者去
翻日志文件。**拒绝记录是这里最有价值的一行**——安全门响过而没人知道，从操作者角度看
和被撬开是同一种失败。

面板打开时才取，不轮询：40 行内存数据不会自己变。列表在内存里、重启即清空，弹窗底部
把这句写明了，因为"这里没记录"和"审计没做"是两件事，磁盘上那份删除审计才是长期的。

### 实测到的数字

- 门禁：`ruff` / `black` / `mypy --strict`（231 个文件）全绿；`pytest` **1347 条
  0 失败 0 错误 4 跳过**（35 秒）；`vue-tsc -b` 干净，Vite 产物 2,017 KB。
- 识别：补静音后真实 CER **1.0%**；500ms 断句时 **4.3% 但被劈成 2 轮**——**CER 一样
  低，体验完全不同**，这就是为什么这条改的是 VAD 不是 ASR。
- 口型：`--speak` 一句话 923,904 字节 / 39 片送达页面；同一句的 4 帧截图里，静默帧嘴是
  一条线，说话帧嘴是张开的洞。
- VRM 取景：模型 1.6 m 身高按包围盒折算进 250×288 的面板，头顶留 12% 余量；手臂垂到
  离身体约 24°。

### 没验到 / 不要对外承诺

- **「协助分析」的模型回复内容**：本机没有可用 key，按钮的链路（无 key → 明确报错、
  无扫描 → 提示先扫描、`remembered=False` 不进历史）有单测，但**模型实际吐出来的那段
  建议长什么样，一次都没看过**。别拿这个当已交付的演示项。
- **「最近动作」有数据时的排版**：弹窗、空态、按钮、取数路径都在真机上点过了；
  列表行（5 列网格）只有单测覆盖，没有截图——因为没有 key 就没有一次真实的工具调用。
- **切模型之后的真实问答**：切换本身验过（下拉框、回执、`set_section_override` 重建
  client），但"切到 A 回答一次、切到 B 再回答一次"需要两个 key。
- **桌面控制真的动了一次鼠标**：`dry_run` 默认开，全程没关过。所以"执行"这条路径只有
  单测（假控制器）覆盖。
- **换第二个 VRM 模型**：包围盒取景和 `swing()` 都是按"任何模型"写的，但手上只有这一个
  文件。接缝的可信度到此为止。

## 11d. 本轮验收记录（2026-10-01 夜，第二轮清单 + 体验项）

| 需求 | 状态 | 证据 |
|---|---|---|
| 人物换成蓝色赛博风 | 已做 | 皮肤「蓝 · 赛博」为默认；截图里人物整体蓝调，见下 |
| 语音多选 + 试听 | 已做 | 「音色」弹窗，16 个中文音色，试听走页面音频通道 |
| 电脑控制权限分档 | 已做 | 顶栏「控制 · 演练」，四档：关闭/演练/键盘/键鼠全开 |
| 思考中动画、别像卡死 | 已做 | 发送后立刻出现小夜气泡 + 三点动画；CPU 大数字未测量时显示 `--` |
| 对话传图片/视频/文件 | 已做 | 「附件」按钮；图片在页面里缩到 ≤512px 再传；多模态走 image_url |
| 历史长期保存 | 已做 | SQLite 两张表（chat_sessions / chat_messages），关窗不丢 |
| 多会话 | 已做 | 「历史」弹窗：打开/改名/删除/新对话 |
| 气泡背景 | 已做 | 用户右侧青色渐变、助手左侧深色，圆角带切角 |
| CPU 占用像有 bug | 已修 | 见下，两处"假零" |
| C 盘告警 | 已做 | 系统盘可用 < 20 GB 时顶栏告警 + 存储面板红字 |
| 皮肤更换 | 已做 | 顶栏「皮肤」循环三套：蓝/青/紫，CSS 与 3D 同步换 |
| 全量测试 + 提交推送 | 测试已全绿 | 推送范围待你确认，见文末 |

### CPU 那两处"假零"

用户说"cpu 占用率好像有 bug"，实测下来读数本身没坏（与 `psutil.cpu_percent(interval=1)`
逐轮对比，偏差在采样噪声内），坏的是**两个自信的错误数字**：

1. **大数字首位 0%**：psutil 第一次调用没有前一个样本，返回 0.0。趋势图从一开始就跳过
   这个点，但 30px 的大数字没跳——开机头一秒半，界面用最大的字号说"你这台机器是闲的"。
   现在大数字和图表共用同一个"已测量"标志，没测出来就显示 `--`。
2. **进程表前 10 秒全 0.0%**：每个进程的 CPU 是"距上次观测的增量"，第一次见到一个进程
   只能记基线；而进程表有 10 秒缓存，于是这份"全零"会在屏幕上冻 10 秒。现在基线那一轮
   **不进缓存**（多花一次遍历，只发生在启动时），并且未测量的格子显示 `—` 而不是 `0.0%`。

两条都是同一类毛病：**把"还没量"画成"量出来是零"**。

### 对话历史怎么存的

SQLite，和 token 账本、记忆同一个库文件，两张表：`chat_sessions`（id/标题/时间）与
`chat_messages`（会话/角色/内容/时间/附件 JSON）。迁移走现有 `Migration` 机制（namespace
`chat`，v1 建表、v2 加附件列）。

- **清空 ≠ 删除**：点「清空」是开一场新对话，旧的仍在「历史」里。要真删，去历史弹窗里
  选一条、再确认一次——和垃圾清理同一个形状。
- **说出来的和打出来的进同一场对话**：语音管线每记一条历史就写一条库，所以"上周我们
  聊了什么"只有一个答案。
- 模型上下文只重放当前会话最近 10 轮（和原来内存里的上限一致），整场对话在库里是全的。
- 附件里的图片存的是**页面缩好的缩略图**（≤512px 的 JPEG data URL，几十 KB），原图不进库。

### 没验到 / 不要对外承诺

- **试听和音色切换的真实声音**：本机无网到 Edge-TTS 的验证没做（公司网络），试听按钮的
  失败路径有单测，成功路径只有 mock。
- **图片真的被模型"看见"**：多模态消息体（`image_url` part）有单测，但没有可用 key 跑过
  一次真请求。
- **视频内容**：不解析，只带名字和大小——界面上写明了，别对外说能看视频。
- **换第二套皮肤后的 3D 重建**：蓝/青/紫三套的 CSS 与人物调色代码同一条路径，截图验了
  默认蓝；紫套没截。
- **历史弹窗的删除确认**在真机上点过「打开/新对话」，删除的二次确认只有单测。

## 11e. 设置里的模型清单与"保存即生效"（2026-10-01 深夜）

用户三问：设置里能不能设多个模型？对话下拉是不是就读这份清单？保存能不能不重启？
三件事原来各差一块，这轮补齐。

**清单**：设置面板现在列的是"有效模型表"= config.yaml 的 providers + 界面添加的行
（`llm.extras` 偏好），每行独立保存自己的地址和模型名（`llm.overrides`，按 provider 键控）。
界面添加的行可以删；配置文件里的行删不掉——那是文件的生意，界面上写得明明白白。
对话区下拉读的就是同一张表（`model_choices()` 走有效表），所以"设置里设的"和"对话里选的"
不可能不一致。

**修掉的一个真缺陷**：旧的覆盖是全局的（一个地址、一个模型名挂在"当前选中的 provider"上），
而且**切换 provider 时会把这两个值删掉**——给 deepseek 填了地址、转头看 kimi，deepseek 的地址
就没了。现在覆盖按行存，切换不丢任何东西；同时一行地址也绝不会落到另一行头上（有测试钉住）。
启动时旧的全局键会自动折叠进它当时所属的 provider 名下，只迁一次。

**保存即生效**，四条路径各自的原因：

| 改了什么 | 为什么不用重启 |
|---|---|
| 地址 / 模型名 / 当前模型 | `LlmService.set_section_override()` 会清空客户端缓存，下一个请求按新值建客户端 |
| API Key | 写进进程环境变量，客户端**每次请求**读环境 |
| 遥测间隔 | 保存回执里带着新间隔，页面立刻 `setIntervalMs()`，下一次等待就用新值 |
| 对话区下拉 | 设置关闭时派发事件，下拉重新拉表 |

**真机实测**（不重启）：设置里点 kimi「设为当前」→ 关掉设置 → 对话下拉已经是
`moonshot-v1-8k（缺 key）`；再点 deepseek  likewise，编辑区跟着换行。

**没验到**：界面「添加一个模型」表单的完整点击流程（自动化点不中那几个输入框，逻辑由
8 条单测覆盖：添加进下拉、可被选中、重名拒绝、配置行删不掉、删除连带清编辑）；
以及 §11d 里列的那几条（试听真声音、图片真被模型看见）。

---

## 11f. 线条人物与"一排一样大"（2026-10-01 深夜，第二轮视觉）

用户圈了六处：右上角那一排状态灯和按钮、对话标题那一排、附件、垃圾清理标题那一排、
全选、以及 3D 人物。要求三条：**人物改成赛博线条**、**一排的要大小一致**、
**全部改成椭圆边角**。

**人物**：见上面「3D 人物」一节——材质整个换掉，线层是原网格（口型照旧），身体层是它的
蒙皮副本（负责挡住背面的线），`renderOrder` 定次序，被换下的 mtoon 材质当场释放。
第一版没有深度次序，截图里是一白团；调过之后才是现在这个能看出头发、脸、手臂的线人。

**一排一样大**：以前是每个组件各写各的 `padding` + `font-size`，同一行里 22 / 24 / 26 px
高三种。现在 `hud.css` 有三个 token：`--hud-control-h: 26px`、`--hud-pill: 999px`、
`--hud-radius: 12px`。`.hud-btn` 定高 + 居中排版，`.hud-field`（下拉、输入框）和
`.hud-chip`（只读指示灯）用同一个高度，`.bar__entry`（控制/皮肤/动作/设置）改成直接用
`.hud-btn`。各组件里那些 `padding: 2px 10px; font-size: 11px` 的"我再小一点"全部删掉——
它们就是不一致的来源。⚙ 那个字符按钮换成了「设置」两个字：一行里全是中文标签，
混一个图标既难对齐也看不出能点。

**椭圆边角**：面板 12px（并给 `.hud-panel` 加了 `overflow: hidden`，否则圆角里面的画布
和列表还是方的；顶部那条描边从 `inset:-1px` 改成 `inset:0`，不然它会从圆角上探出去）；
按钮/指示灯/输入框/下拉/气泡/附件 chip 全是胶囊；六个弹窗外壳、AI 协助分析块、
删除确认块、C 盘告警块、用量卡片、列表行都跟着圆了。

**顺带修的两个真缺陷**：
1. `启动小夜.bat` 双击起不来（`unknown key 'root'`），根因见 §8 那个引用块。已修 + 回归测试。
2. `.hud-btn--primary` 这个类在两个模板上挂着，样式表里从来没有定义过——设置里的「保存」
   和用量弹窗里"当前选中的时间范围"长得和其他按钮一模一样。补了样式。

**实测到的**（`r8-lines.png` / `r8-lines2.png` / `r8-skin-cyan.png` / `r8-back-blue.png`，
PrintWindow 抓的窗口客户区）：三行按钮等高、面板圆角、人物是蓝线；点「皮肤」三次
蓝→青→紫→蓝，人物和界面一起换色（线色取自同一份 `theme.ts`）。
门禁：ruff / black(237) / mypy --strict(237) 干净，pytest 全绿（exit 0，收集 1404 条），
`vue-tsc -b` + `vite build` 干净。

**没验到 / 不要对外承诺**：
- 口型在**线条人物**上的实际开合（这一轮没有 API Key、也没有放声音进页面；
  线层用的仍是 `expressionManager` 驱动的原网格，机制与 §11d 相同，但没重测像素）。
- 趋势图的折线颜色**不跟随皮肤**（切到紫色时它还是青色）——图表在挂载时读一次颜色，
  这一轮没改它。
- 自动化点击仍然只点得中顶部那一排；设置面板里的输入框照旧点不中（同 §11e）。


---

## 11g. 命令行分档、放大对话、窗口铺满（2026-10-01 深夜，第三轮）

用户五件事：AI 要能用 PowerShell 并有管理员权限；要能动鼠标键盘；朗读别念标点；
对话框太小要能放大；页面没铺满，看着扩展。

### 命令行是第二把锁，不是第一把锁的第五档

`jarvis/app/command_access.py` 的四档：关闭 / 演练 / 当前用户 / 管理员，存
`shell.tier` 偏好，默认**关闭**。它和 `computer.tier`（鼠标键盘）**分开**，因为
"能打字"和"能执行命令"是两个风险，有人会想开一个关另一个——合成一把梯子就选不了了。

`run_powershell` 工具（`jarvis/tools/builtins/shell_tools.py`）：

- **脚本走 `-EncodedCommand`**（UTF-16LE + base64），argv 写死 `powershell.exe`，
  绝不 `shell=True`。这样脚本里的 `"` `$` `;` 都只是数据：测试直接断言解码回来的
  字符串和送进去的一模一样。
- **档位就是授权**，所以 `confirmed` 不参与（它仍然不是任何工具的参数，有测试钉住）。
  风险标 CAUTION 而不是 DANGEROUS：DANGEROUS 走的是"每次调用要人确认"那条闸门，
  而这条闸门在这个界面上没有对应的按钮，标了只会让每次调用都失败在一个没人能满足的
  条件上——看起来像工具坏了，不像策略生效。
- **管理员 = 每条命令弹一次 UAC**。`Start-Process -Verb RunAs -Wait`。提权进程和
  本进程之间**没有管道**（跨完整性级别不能继承句柄），所以输出走结果文件回读，
  退出码写在文件尾一行。这三条限制都印在返回给模型的话里，不假装能拿到拿不到的东西。
- **每条命令写两行审计**（`数据根/logs/commands.jsonl`）：执行前一行"开始"，之后一行
  带退出码/耗时/结果。先写"开始"是为了让**跑挂了的那条**也留下痕迹。台账在
  「控制」弹窗里直接看得见。
- 诚实的边界：**名单挡不住 base64**。真正的墙是 UAC + 这份台账 + 你看着它跑，
  不是模式匹配。

`run_shell`（WorkBuddy 那条配置文件路径）原样保留，仍然默认不可达；两个工具的区别
写在模块 docstring 里，免得下一个读者以为是重复实现。

### 放大对话

对话标题多一个「放大」，把**同一个** ChatPanel 元素 `position: fixed` 抬到整窗，
左边多一条会话栏（新对话 / 切换），底部多一条状态（当前模型、条数、Esc 收回）。

不是"再开一个对话组件"：两个 ChatPanel 各自持有 `seeded` / `consumed`，同一份历史
会被两个视图各自决定"存的部分到哪结束"，那正是两边开始各说各话的方式。

### 窗口铺满

以前 `create_window` 只传 `width=1280, height=800`，1920×1080 的屏幕上永远开一个小窗。
现在：没有记住的几何 → 最大化开；有 → 按记下的位置和大小开，并记住是否处于最大化。

**踩到的坑（实测，不是猜的）**：`x`/`y` 和 `maximized=True` 一起传，WinForms 后端会
在设完窗口状态之后再设位置，于是**取消最大化**——同一份代码一次开成全屏、一次开成
1280×800。所以最大化时不传位置。另外 `create_window(maximized=True)` 会被 pywebview
自己的 show 流程冲掉，因此在 `loaded` 事件上再补一次 `maximize()`。

顶栏多一个「铺满 / 还原」按钮，标签读的是 Python 那边的状态而不是页面自己记的点击，
所以你从标题栏或 Win+↑ 改的它也跟着变。

### 多铺出来的读数

`monitor.py` 以前采集了但界面从不显示的：`swap_percent`、`available_bytes`。
新加的：`net_io_counters` 差分出的上下行速率。**第一个采样点没有速率**（差分要两个点），
返回 `None` 让界面显示 `--`，不是 0——这就是 §7 里 CPU 那两个"假零"的同一个坑。

### 实测到的

- 门禁：ruff / black(240) / mypy --strict(240) 干净，pytest exit 0（收集 1460 条），
  `vue-tsc -b` + `vite build` 干净。新增测试：`test_core_text.py` 14 条、
  `test_app_command_access.py` 26 条、`test_monitor_refresh.py` 网络 3 条、
  桌面桥 + 几何 10 条。
- 像素（`r9-max3.png` 1906×1025、`r9-chat-open4.png`、`r9-chatmax-final.png`）：
  窗口最大化后铺满整屏；左栏多出「交换分区 6%」「网络 ↓/↑ 1.0 KB/s / 922 B/s」
  （这台机器当时的真实读数）；放大后的对话有会话栏、底部模型条、输入行贴着下沿。

### 没验到 / 不要对外承诺

- **`run_powershell` 没有真跑过一次**：验证全部是 monkeypatch 掉 `subprocess.run` 的
  单测（命令行拼装、编码往返、UAC 被拒、超时、审计）。UAC 弹窗、结果文件回读、
  跨完整性级别的退出码这三件事，只有真点一次「管理员」才知道。默认档位是关闭，
  所以这一步留给操作者自己开。
- 鼠标键盘 FULL 档这一轮**没有重新验**（§11c 验过一次）。顺带发现这台机器的
  `computer.tier` 现在是 3（键鼠全开）——不是我改的，但值得知道：**现在模型能真动指针**。
- 放大弹窗的"不透明底"是最后一次构建才改的 CSS，改完没再截图复验。
- 「看一眼屏幕」（把 `vision/capture.py` 接进对话）这一轮**没做**：`mss`/`Pillow`
  都没装，`vision.enabled` 默认 false，要动的是依赖清单 + 一个新的投放件级开关，
  半接比不接更糟。它是下一轮最值钱的一件事——文档里"模型看不见屏幕所以点不准按钮"
  那句话就等它。

---

## 11h. 进程排行榜可以结束进程（2026-10-01 深夜，第四轮）

用户圈了右边那块「占用排行」：**"我要可以选择对应的进程并结束"**。

这条和垃圾清理是同一类操作——不可逆、由人勾选、事后要有账——所以照同一套门走：
**勾选 → 「结束进程」 → 二次确认条 → 逐个复查 → 审计**（`jarvis/tools/process_control.py`
+ `jarvis/app/process_service.py`，桥 `process_kill(items, confirmed)`）。

### 一个 PID 不是一个身份

这是这条功能里唯一真正新东西，其余都是把已有的纪律再走一遍。

从界面画出这张表，到人按下「确认结束」，中间可能过了三分钟。三分钟里那个进程可以已经
退出，Windows 可以把**同一个号码**发给任何一个新进程——包括 `lsass.exe`。所以：

- 页面勾的不是 pid，而是 **`{pid, name}` 一对**（勾选时就地记住当时那行的名字，表每 1.5 秒
  重画也不改这个记忆）；
- 动手前重新读一次这个名字，**不一致就拒绝**并回一句"PID 已被复用（现在是 X），没有动它"；
- 名字比对不分大小写（`NotePad.EXE` 勾上、`notepad.exe` 才算同一个）。

测试里钉的就是这个场景：表里 900 号现在是 `lsass.exe`，勾的是 `updater.exe` → 不杀，
并写一条 `ok=false` 的审计。

### 名单

`OS_CRITICAL`：`lsass / svchost / csrss / wininit / services / dwm / winlogon / fontdrvhost /
smss / userinit / spoolsv / taskhostw / Memory Compression / System / Registry`。
`svchost` 在里面是**故意的**：一个宿主可能挂着网络或音频服务，真要重启服务的人会用
`services.msc` 或管理员命令行，那里看得见自己在重启什么。

`OWN_NAMES` + 自己的进程树：`小夜.exe`、`pythonw.exe`、`msedgewebview2.exe`，外加
`os.getpid()` 及其父进程和全部子孙——**动手的那一刻**算，不缓存，因为渲染这个窗口的
WebView2 是启动之后才出现的。一个能把自己的窗口杀掉的助手，没法告诉你它做了这件事。

`AccessDenied`（别人的进程 / 以管理员跑的进程）回一句"权限不够，小夜这里结束不了"，
不会偷偷提权——提权是「控制 → 命令行 → 管理员」那条路，每条都要过 UAC。

### 没有做成模型的工具

这是刻意的。结束进程**只从界面走**：一个能看见列表并勾选的人。注册成工具就等于允许
模型按名字杀任何进程，而它看不见屏幕（§11c 那条限制还在），这跟"能执行命令"也不是
一个量级的事。要留给模型的话，得先有截图进模型（§11g 末尾那条没做的）。

### 实测到的

- 门禁：ruff / black(243) / mypy --strict(243) 干净，pytest exit 0（收集 1481 条）。
  新增 `tests/test_tools_process_control.py` 19 条 + 桥面 2 条。
- 像素（`r10-procs.png`）：每行一个勾选框，标题行「占用排行 · TOP」+「结束进程」在一行内
  （原先标题写成 `TOP PROCESSES` 加一个「按内存」提示，在 320px 的右栏里挤成两行）。

### 没验到 / 不要对外承诺

- **二次确认条与结果列表没有点过**：验证到一半这台机器的主人在用（窗口被移动过、CPU
  突然 100%），就没有继续在他的屏幕上点。后端这一侧的每种拒绝都有单测，界面这一侧是
  状态机（`confirming` → `commit()` → `outcomes`），但**没看过它画出来的样子**。
- **一个进程都没真杀过**，包括自己的窗口。第一次真用请挑一个确定无所谓的行。
- 勾选状态在进程表刷新后按 pid 保留：如果一个被勾的进程退出、号码被复用，勾选会跟着
  号码留在原地——但**动手时会被名字比对挡下来**，这正是上面那条规则存在的理由。

---

## 11i. 负载趋势曲线看不懂 → 把读它的方法画在图上（2026-10-01 深夜，第四轮）

用户："负载均衡那个曲线我看不懂加上解释"。（面板上写的确实是"负载趋势 · LOAD TREND"，
所以"负载均衡"指的就是这张图。）

它以前只有两条线：没有图例、x 轴标签是关的、**tooltip 也是关的**
（`tooltip: { show: false }`）。也就是说一张图同时缺了"哪条是什么""横轴多长""某个点是
什么时候多少"这三样——只有写它的人看得懂。

改了三处，都是"把图自己说不出来的话说出来"：

| 加在哪 | 内容 | 为什么这样写 |
|---|---|---|
| 标题右边图例 | 青＝CPU、橙＝内存，各自带当前值 | 当前值直接放上去，不用从曲线高度反推 |
| 打开 tooltip | 悬停显示该时刻两条线的读数 | 原来关掉是"少一个浮层"的想法，代价是曲线上任何一个点都没有时间和数值 |
| 图下两行小字 | 横轴跨度 + 点数 + 采样间隔；以及"100% 是什么意思" | 见下 |

**横轴跨度和采样间隔是从点上算出来的，不是写死的。** 轮询间隔可以在设置里改，窗口
隐藏时还会自动降到 10 秒一次，中间慢了的读会被跳过而不是排队——所以任何印死的
"最近 90 秒"都是在猜别人的设置。跨度用首尾时间戳，间隔用**相邻间隔的中位数**（真实
发生了什么的唯一说法）。

解释文字里两条容易误读的要写明白：

- **CPU 100% 不是死机**：那是所有核心平均排满；一根尖峰就是一个进程在干活。逐核的
  细节在左栏那排小竖条。
- **内存那条线长期偏高是正常的**：它是已用÷物理内存，空闲内存会被 Windows 拿去做缓存，
  **它不是速度**。这条线被读成"内存快满了要卡"是最常见的误会。
- CPU 第一次读数显示 `--` 而不是 0%：`psutil.cpu_percent` 没有前一个样本可比（§7 那两处
  "假零"的同一个根因）。

**实测到的**（`r11-trend.png`，源码实例截图）：图例「CPU 100% / 内存 65%」带色块贴在标题右边；
横轴那行读作 **`最近 41 秒 · 29 点 · 约每 2 秒一点`**——注意那个"2 秒"：配置写的是 1.5 秒，
真实间隔是 ~2 秒，因为每次读要花约 1.4 秒走进程表。**如果按配置印成"每 1.5 秒"就是错的**，
这正是"从点上算"而不是"写死"的理由。

**没验到**：tooltip 的悬停样子（截图是静态的，没有鼠标停在上面）。

## 11j. 托盘常驻 + 桌面宠物（2026-10-02 凌晨，第五轮）

需求原话：「我叉掉页面时不能真退出，在右下角要有托盘图标并保持后台运行，我右击托盘
图标点击退出时才能真正推出」+「要有类似桌面宠物的功能，有个按钮显示和隐藏桌面宠物，
点显示时说出唤醒词电脑屏幕中间要有一个赛博女性人物从虫洞中散发光粒子显示出来并与我
和我沟通对话」。四个决定由用户选定：隐藏后**继续待命但托盘要标出来**、宠物**平时穿透
按住某处可拖**、托盘用 **pystray + Pillow**、清理只删 r10。

### 托盘与生命周期

| 模块 | 职责 |
| --- | --- |
| `jarvis/ui/tray.py` | Pillow 画四态图标（与 `scripts/make_icon.py` 同一套几何：圆角方 + 环 + 核心），pystray 菜单「显示主界面 / 桌面宠物 / 退出小夜」，`start()` 返回**是否真的挂上了** |
| `jarvis/ui/lifecycle.py` | X 与退出的全部决策：`on_closing()`、显隐、后台通知、语音态→图标态、音频归属、唤醒召唤宠物、`quit()` |
| `jarvis/ui/instance.py` | 一次启动只允许一个进程：锁文件 + loopback socket，第二次双击是闹钟不是助手 |

三条实测/源码坑：

1. **`window.events.closing` 的返回值里 `False` 才是"取消关闭"**（`webview/event.py`
   收集 `is False` 的返回值再 `args.Cancel = True`）。写成 `return True` 就等于没写，
   而且其余断言全都会绿。测试 `test_closing_returns_false_because_false_means_cancel`
   单独钉这一条。
2. **`closing` 处理器在 WinForms UI 线程内联执行**，所以里面不能 `evaluate_js`
   （那要等 UI 线程回话→自锁）。隐藏动作被推迟到一条一次性线程里。
3. **隐藏窗口时 `document.hidden` 仍是 `false`**：pywebview 从不设置
   `CoreWebView2Controller.IsVisible`。所以藏起来之前页面会一直每 1.5s 走一遍进程表。
   现在由 `lifecycle` 显式推 `window.__jarvisBackground(false)`，store 据此切到 10s。

真机验收（源码运行，pid 见日志）：

- `tray icon up: 小夜 · 麦克风未开启（后台待命）` → 图标确实出现在通知区域；
- 点标题栏 X → `window hidden to tray (voice keeps running)`，进程存活（143 MB），
  窗口从 `list_windows` 消失；
- 再跑一次 `python -m jarvis --desktop` → 第二个进程 0 退出，**第一个窗口回到前台**
  （`isForeground: true`），锁文件仍是赢家的；
- 硬杀（`taskkill /F`）留下的死锁文件被下一个启动探测并接管（日志里那条
  `FileExistsError` 已降级为静默，因为它是常态不是错误）。

### 桌面宠物

第二个 pywebview 窗口：`?mode=pet`，frameless + on_top + `focus=False` + 懒创建
（第一次显示才建，之后只隐藏不销毁——重建会把"说完唤醒词"和"看见人"之间塞进几秒）。

- **穿透已实测**：`WS_EX_TRANSPARENT` 平时为 ON；光标移到右上角那条把手上，
  8Hz 的指针轮询把它清掉（`ex=0x08050008`），移开又恢复（`0x08050028`）。
  按住把手拖动：窗口从 (750,220) 走到 (398,508)，与手指位移一致 → `easy_drag` +
  `pet_drag(true)` 冻结这条链路成立。
- **虫洞出场**：环 + 汇聚光粒子 + 人物从 0.22 长到 1.0；`wake` 事件且主界面在托盘时
  重放一次出场（`lifecycle.summon_pet`）。宠物开/关只由人决定，唤醒词不会自己把她变出来。
- **谁出声**：两个 WebView2 不能同时放同一段话。规则是"看得见的那个说话"：
  主界面在屏→主界面；主界面进托盘且宠物在屏→宠物。`AudioPusher` 一行没改，
  换的只是 `attach_window` 的指向。

### 没做到的那一半（写清楚，别让它变成下一次的惊喜）

`transparent=True` 传了，**没拿到真透明**。四次量测：

| 尝试 | 屏幕上量到 | 结论 |
| --- | --- | --- |
| 只给 `transparent=True` | 404x601 的 #28292a 方块 | 表单在填底 |
| `native.BackColor = Transparent` | #EFF0F0（WinForms 控件默认色） | 还是表单 |
| 表单涂黑 | #202020 | 请求的黑色到不了屏幕 |
| 按屏幕回读值上色键（`WS_EX_LAYERED` + `LWA_COLORKEY`，键值 0x202020 由 `_sample_surface` 实测得到，日志确认已应用） | 仍然 #202020 | 键只对**顶层窗口**有效，这些像素属于 WebView2 **子窗口** |

所以宠物现在是一块**设计过的面板**：portal 形渐变 + 一圈青色描边，底色与主界面同源，
而不是一个灰盒子。要做成真·桌面镂空，需要换宿主（WPF/WinUI 的 per-pixel alpha，或
`UpdateLayeredWindow` 自己逐帧喂位图）——那是一次架构改动，不是一轮打磨。

### 门禁与交付

- ruff / black / mypy（251 个文件）全绿；`pytest -q` 全绿；`vue-tsc -b` + `vite build` 全绿。
- 新增测试：`test_ui_tray.py`、`test_ui_lifecycle.py`、`test_ui_pet.py`、`test_ui_instance.py`，
  外加 `test_ui_desktop.py` 里的 `TestTraySurface` / `TestPetSurface`。
- 依赖：`pystray>=0.19.5` + `pillow>=10` 进 `[desktop]` extra；`packaging/jarvis.spec`
  显式列出 `pystray._win32` / `PIL.Image(Draw)`（pystray 按 `sys.platform` 在函数里挑后端）。
- 顺手修掉一个第五轮留下的缺陷：**「还原」会把窗口还原成比屏幕还大的一块**
  （`create_window(maximized=True)` 让 WinForms 没有更小的 normal size，实测
  1936x1048 @ (208,208)）。现在 `fit_to_screen()` 在打开与还原两处都按屏幕夹一遍。

## 11k. 全面体检 + 把已有能力接到人身上（2026-10-02 凌晨，第六轮）

先体检，再决定加什么。方案与爆炸半径写在 `docs/extension-plan.md`，这里只记结果。

### 体检量到的关键事实

装配表里有 28 个组件，而 `jarvis/ui/desktop.py` 里 `scheduler` / `memory` /
`knowledge` / `workflow` / `vector` / `planner` **出现 0 次**——调度器在跑（0 个任务）、
记忆里有 3 条、知识库里有 1 篇文档，用户看不见、管不着，语音也喊不动。
所以这一轮**不加新底层能力**，只把已经建好并测过的三样接到眼睛和嘴上。

### 加了什么

| 能力 | 接法 | 关键决定 |
| --- | --- | --- |
| 提醒 | `jarvis/app/reminder_service.py` + 工具 `add_reminder` / `list_reminders` / `cancel_reminder` + 顶栏「助手」弹窗「提醒」页 | 时间由**纯函数**解析（`十分钟后`/`明天 9 点半`/`19:40`/ISO），听不懂就拒绝并列出可用写法；不交给模型猜时间 |
| 播报 | `jarvis/app/announcer.py` + 工具 `speak` | 到点 = 出声 + 托盘气泡，两条通道分别报告；一条都没接上时明确说"没有可用的播报通道"，而不是回一句"已完成" |
| 记忆 | 桥面 `memory_list` / `memory_forget` / `memory_forget_all(confirmed)` | 能被看见、能被抹掉是记忆功能的前提；全清要二次确认 |
| 知识库 | 桥面 `knowledge_state` / `_ingest` / `_forget` / `_probe` | 「忘掉」只删索引，**不碰磁盘上的文件**——这句话写在按钮旁边，因为"删除"挨着一个文件名时每个人都会有别的理解。命中测试暴露片段与分数：看不见出处的引用等于没出处 |
| 全局热键 | `jarvis/ui/hotkeys.py`，`Ctrl+Alt+K` 开一轮免唤醒对话、`Ctrl+Alt+H` 显隐主界面 | **只用 `RegisterHotKey`，不装键盘钩子**：前者只在组合键按下时收到一条消息，后者看得见你敲的每一个键 |
| 开机自启 | 托盘菜单勾选项，写 `HKCU\...\Run` 的一个值 | 只写 HKCU；勾选状态每次从注册表回读，不另存一份真相 |

### 调度器多了一个 `DATE` 触发器（改了 L3，写清楚为什么）

原来只有 `cron` / `interval`，注释里说"一次性定时器属于上面那层"。做提醒时这条被推翻：
一次性任务同样需要**重启后还在**和**错过时间的处理策略**，而这两件正是手写
`threading.Timer` 会做错、`SchedulerService` 已经做对的。表结构是 `trigger + expression`
两个 TEXT，加一个枚举值不需要迁移。

退役规则（`_retire_one_shot`）比解析更值得测：

- 响过的提醒**置灰而不是删掉**——行留着才是"当初让我做什么"的记录；
- **跑失败的那条保持启用**：宁可下次再试，也不能让"提醒没来"和"列表说已经响了"同时成立；
- `run_now`（手动试跑）**不退役**：测试一次不等于它响了。

### 一个只有真机能发现的 bug

`RegisterHotKey(None, ...)` 把 `WM_HOTKEY` 投给**注册它的那个线程**。原先在
`run()` 里注册、另起线程去泵消息，于是：日志兴高采烈地写"hotkeys up"，按键**永远
没有反应**——因为注册的线程随后就卡在 `webview.start()` 里，那条队列没人泵。
修法是把注册搬进泵线程（注册 → 置就绪事件 → 泵）。所有单元测试都 fake 掉了 `user32`，
所以它们全绿而功能是死的；这条只能靠真按一次。

### 验收：量到的和没量到的

量到的：

- 热键：`IsWindowVisible` 从 True → False（日志同时写 `window hidden to tray`）→ 再 True，
  两次 `Ctrl+Alt+H` 之间没有点过任何鼠标；
- 托盘「退出小夜」：日志 `tray quit requested` → `component stopped: voice` →
  `application stopped`，麦克风随进程一起放掉（上一轮唯一没被像素验证的那条，这轮被真按了一次）；
- 宠物窗口在桌面上的最终形态（portal 渐变 + 描边 + 把手）；
- 门禁：ruff / black / mypy（258 文件）/ `pytest`（1602+ 条）/ `vue-tsc` / `vite build` 全绿。

没量到的（别当成已验证）：

- 「助手」弹窗的**像素**：验证时前台是用户正在用的 IDE，`SetForegroundWindow` 没能把
  小夜提到前面，于是停手没有继续点这台机器的屏幕。弹窗的逻辑有类型检查与桥面测试兜着，
  但它长什么样要等下一次人不在机器前时再看一眼；
- 提醒**真的到点出声**：集成测试用真 `SchedulerService` + 真工具表 + 录音 announcer 走完了
  `scheduler → speak → announcer → 退役`，但没有让一条提醒在真机上等满一分钟。
  （这一条在下一节被真机推翻了：链路上有一个单元测试和集成测试都碰不到的校验步骤。）

## 11l. 提醒在真机上哑了（2026-10-02 凌晨，第七轮）

上一轮留了两条"没量到"。这次去量，第一条就把那条测试打穿了。

### 怎么量的

调度器在启动时读库，所以提醒必须**在 exe 起来之前**写进真库：用 `ReminderService` 本体
（不是手写 SQL）往 `E:\BianChengGongJu\JarvisData\database\jarvis.db` 塞了一条
`add("站起来倒杯水", "两分钟后")` —— 连中文时间解析一起量。01:52:37 启动 r13，等它响。

量到的：

```
01:54:00 | WARNING | jarvis.scheduler.service | scheduled job reminder:78bf00c4
         failed after 0 ms: JarvisError: 参数不合法：未知参数 title
```

**时间是对的，触发是准的，工具拒了。** 人什么都没听到。任务终态记成
`ok=False / error='JarvisError: 参数不合法：未知参数 title'`（回读 `scheduler_runs` 看到的），
所以失败是可见的——但"可见"只是没骗人，不等于能用。

### 根因是一处没人对过的契约

提醒写进 job 的 `arguments` 是 `{"text": "提醒：…", "title": "提醒"}`，
而 `speak` 工具的 schema 只声明了 `text`。`validate_arguments`
（`jarvis/tools/types.py`）对**未声明的参数一律报错**——这条规则本身是对的，
模型乱传参数就该被挡在调用之前。错的是提醒这边多塞了一个对方没登记的字段。

修法：`speak` 把 `title` 登记为可选参数（只有托盘气泡看得到它），handler 有则透传、
没有就用 `Announcer` 自己的默认标题。

### 为什么 46 条测试全绿，而功能是死的

`TestAReminderActuallyFires` 名义上走的是"scheduler → speak → announcer → 退役"全链路，
但它的 `run()` 是**测试自己手搓的一个 dict**：

```python
handler = handlers.get(action)      # ← 这就是问题
return handler(dict(arguments))
```

真机上跑的是 `_tool_action_runner` → `tools.invoke()` → 注册表里的**参数校验 + 风险策略**。
测试把这一段"接线"重新实现了一遍，于是恰好跳过了唯一会出错的那一步。
现在测试里的那个 `run()` 改成用真 `ToolRegistry`（`registry.invoke`），
不再自己查表。

改完做了一次**变异验证**：把 `title` 从 schema 里删掉 → 那条测试立刻红；装回去 → 46 条全绿。
这条测试现在真的咬得住，而不是恰好路过。

> 教训一句话：**跨组件的端到端测试，"连接"本身必须是真件。** 名字对得上不代表参数对得上，
> 而手搓的 dict 连参数都不看。同类坑这轮是第二次（上一次是 `dict(build(...))` 因为
> `ToolSpec` 不可哈希而炸）。

### 顺手查了同一类 bug 的其它落点

`grep` 了所有产生 `action=` 的地方，只有一处可疑：`jarvis/workflow/service.py:341` 给调度器
注册的 job 写的是 `action=definition.name`（工作流自己的名字），而注册表里**没有任何地方**
按工作流名字注册工具（`__main__.py` 的 `.register(` 全是组件注册，没有动态注册）。
也就是说 cron 工作流一到点就会以"未知动作"失败。今天看不出来，因为
`workflow.definitions()` 恒为空（0 条定义）。这条记在 `docs/extension-plan.md`
的"本轮明确不做"里，并且把理由从"缺设计"改成"链本来就是断的"。

### r13 冻结包验收（真机日志，不是开发模式）

- `tray icon up: 小夜 · 麦克风未开启（后台待命）` → 随后 `re-arming the microphone from the
  remembered 「启用语音」 choice`；
- `global hotkeys up: 说一句话（Ctrl+Alt+K） · 显示/隐藏主界面（Ctrl+Alt+H）`；
- `pet window shown at 420x640`；
- 打包侧：`pystray`（含 `pystray._win32`、`pystray._util.win32`）与 `PIL` 都在 `PYZ-00.toc` 里，
  `_internal/jarvis/ui/web/` 的资源哈希与源目录一致（`index-DB4OKLVm.js` / `index-H_yWnyLj.css`）；
- 门禁：ruff / black(258 文件) / mypy(258 文件) / `pytest` **1679 passed, 4 skipped**；
  `vue-tsc -b` 本轮重跑（exit 0）。`vite build` 是 01:27 产出这份资源时跑的，
  之后前端源码零改动，所以包里的哈希与源目录一致（不是"相信它会一样"）。
- 截图核对（1920x1080）：宠物窗口在桌面上仍是**黑底矩形**（透明做不到的结论在 §11j），
  托盘图标被 Windows 11 收进 `^` 溢出区——已把"第一次要点开 ^ 找它"写进 §7。

### 不用碰别人的屏幕也能看像素

上面那句"整块黑底"是**全屏截图**看到的，而全屏截图拍到的是窗口背后的桌面。
项目里本来就有 `scripts/capture_window.py`（`PrintWindow` + `PW_RENDERFULLCONTENT`，
专为"窗口被挡住也能拿到自己的像素"写的）。用它直接拍 r14 的两个窗口：

```
python scripts/capture_window.py --title "小夜"          --out r14-hud.png   # 1920x1032
python scripts/capture_window.py --title "小夜 · 桌面宠物" --out r14-pet.png   # 404x601
```

第一次跑出来两张都是宠物——`EnumWindows` 按 z-order 走，宠物是 always-on-top，
而 `--title` 是**子串**匹配，"小夜" 两个窗口都含。于是给 `find_window` 加了一条：
**有完全相等的标题就优先取它**。修完两张各自正确。

看到的（这次是真的看到了，不是"应该长这样"）：

- 主窗口：表头 `小夜 v0.1.0 · pywebview · 构建 10-02 01:58`（构建时间戳生效），
  顶栏一排 `遥测正常 / 待唤醒 / 释放麦克风 / 运行 … / 控制·键鼠全开 / 皮肤 / 动作 / 铺满 /
  助手 / 宠物 / 收进托盘 / 设置` —— 本轮新加的「助手」「收进托盘」都在冻结包里；
  存储面板从 2 个分区变成 3 个（E: 也被扫到了）；进程排行首位 `小夜.exe 1.5 GB`。
- 宠物窗口：深蓝渐变的**圆角面板** + 描边 + 右上角 `小夜 / 收起` 把手，人物是青蓝线条。
  也就是说 §11j 说的"黑底矩形"要说得更准：**面板本身是有设计的深色板，
  做不到的是让面板以外的地方透明**——全屏截图里那块"黑"是面板本身，
  而它盖住了编辑器，所以看起来像一块黑。
- 主窗口最大化到 1920x1032 时，**内容只铺到约 y=800，下面一条是空的**。
  三列各自的行高按内容排，剩下的空间没被分掉。这是布局问题不是渲染问题，
  记在这里，下一轮决定是"行高等分"还是"给对话区吃掉剩余高度"。

### 还没量到的（别当成已验证）

- 「助手」弹窗的像素：这轮依然没有点它（前台是用户正在用的 IDE，不动别人的屏幕）。
  现在**看**它已经不是问题了——`capture_window.py` 能在窗口被挡住时拍到它自己的像素；
  缺的只是"有人点开它"这一下。要么下次人不在机器前时由我点，要么早上用户点一下我用
  `capture_window` 收尾。
- cron 工作流：按上面的分析它现在必然失败，本轮没有造一条定义去撞它（要撞也得先解决
  "名字从哪来"）。

### 修完再量一次（r14，同一个脚本、同一台机器）

同一个 `seed_reminder_test.py`（还是走 `ReminderService` 本体解析「两分钟后」），
02:07:42 启动 r14，等 02:09 那一条：

```
02:09:01 | INFO | jarvis.ui.audio_bridge | audio: 133632 bytes in 6 slices delivered to the page
```

回读 `scheduler_runs`（不是我口头说的"应该响了"）：

```
job reminder:e8f5eeec enabled=False action=speak at 2026-10-02T02:09
  args={'text': '提醒：站起来倒杯水', 'title': '提醒'}
  run ok=True  detail='提醒：站起来倒杯水 —— 已开口，已弹托盘'  error=''
```

四条结论一次拿到：`ok=True`（工具收了）、**已开口**（音频真的推给了页面，133632 字节 6 片）、
**已弹托盘**（pystray 的通知在冻结包里没抛异常）、`enabled=False`
（一次性任务响过之后自己置灰，正是 `_retire_one_shot` 的"响过才退役"）。

测试行与它的 run 记录都已删干净（`reminder rows left: 0` / `orphan reminder runs left: 0`）。
r14 启动侧照旧全绿：`tray icon up` → `global hotkeys up` → `pet window shown at 420x640` →
`voice stack running`。

## 11m. 桌面宠物真的没有背景板了（2026-10-02 凌晨，第八轮）

用户提的三件事：**去掉背景板只留人形**、**把脚做出来**、**别那么死板**。三条都做到了，
但第一条不是靠"再试一次透明"做到的。

### 先说清楚为什么颜色键是死路（第三次确认，别再试第四次）

§11j 量过：`LWA_COLORKEY` 打在顶层表单上，而像素属于 WebView2 **子窗口**，键不动它们。
这轮先补一个推论：`UpdateLayeredWindow`（唯一能给出真 per-pixel alpha 的调用）
**不接受带子窗口的窗口**——它要的是你自己提供的位图。所以只要像素还在 WebView2 里，
就没有第三条路。结论：**要透明，像素必须离开浏览器窗口，交给一个 Python 自建的窗口。**

### 那像素走不走得动？先量管道再写代码

单独写探针（`build/probe_alpha_transport.py`）量 canvas → PNG → base64 → 桥面 → 解码：

| 尺寸 | 编码 | 桥面+解码 | PNG | 上限 |
| --- | --- | --- | --- | --- |
| 260x400 | 4.2 ms | 4.6 ms | 80 KB | ~113 fps |
| 420x640 | 8.1 ms | 9.5 ms | 198 KB | ~57 fps |

管道扛得住，于是动工。

### 做了什么

- **`jarvis/ui/compositor.py`（新）**：一个 `WS_EX_LAYERED` 窗口，自己注册类、自己泵消息，
  `UpdateLayeredWindow` 吃**预乘 alpha** 的 BGRA。桥面 `pet_frame(dataUrl)` 把帧交给它。
  渲染用的 WebView2 窗口挪到 `-32000,-32000`（**不能 hide**：隐藏的 WebView2 会被 Chromium
  判定遮挡而停止出帧），它只负责画，画完把 PNG 递出来。
- **取景**：`Character.reframe(fraction)`。`BUST_FRACTION=0.5` 是 HUD 那块 250px 面板的
  取景，宠物窗口整个继承了这个默认值——所以没有脚。宠物要 `reframe(1)`，HUD 不动。
- **活起来**：`FaceDriver` 多一个 `pointer` 输入（-1..1），头与眼跟着鼠标转，
  **鼠标停 4 秒就淡出**（一直盯着一个不动的光标很像摄像头）；眼睛比头先动 1/3；
  `VrmCharacter.advance` 加了髋部重心慢摆 + 胸腔呼吸 + 躯干跟着头转 1/4。
  光标位置由宠物自己的轮询线程推给页面（`__jarvisPetPointer`）。
- **把手改由 shell 画**：合成窗口只吃 canvas，**页面上的 DOM 元素根本到不了桌面**。
  所以"按住拖动"那颗药丸由 `compositor.draw_handle` 用 PIL 画在帧上，
  位置就是页面报上来的那个矩形——标签和可点区域从此不可能对不上。

### 五个只有真机能抓到的 bug

1. `GetCurrentThreadId` 在 **kernel32**，不在 user32；
2. `GetModuleHandleW` 同上；
3. **创建时带 `WS_EX_LAYERED` 会失败（1400 ERROR_INVALID_WINDOW_HANDLE）**，
   而对一个已存在的窗口 `SetWindowLongW` 加上它每次都成功；
4. `WS_POPUP` 我写成了 `0`——那是 `WS_OVERLAPPED`，宠物带着标题栏出现在了桌面上；
5. **`PostThreadMessageW` 返回成功，但消息永远到不了窗口过程**：线程消息的 `hWnd` 是 0，
   `DispatchMessageW` 没有窗口可派发。改成共享槽位 + 80 Hz 轮询。

第 5 条最贵，因为**它每一步都报告成功**：桥面收帧、post 返回 1、窗口在、线程活着。
中间还夹着一个 `message.hwnd` 拼写（真名 `hWnd`），线程当场死掉——
如果 `_run` 没有那个 try/except，这条会表现成"宠物不见了但没有报错"。

### 代价（实测，不是估计）

同一份代码、麦克风都关着、都等 90 秒稳定之后采 15 秒：

| 状态 | 进程树 CPU 合计 |
| --- | --- |
| 宠物关 | **26.7%** of one core（python 7.6 + webview2 18.8） |
| 宠物开（合成中，10 fps） | **94.3%** of one core（python 22.7 + webview2 71.6） |

**开宠物 ≈ +68% 单核 ≈ 12 核机器的 5.7%。** 大头在 WebView2 那边：离屏渲染 +
`preserveDrawingBuffer` 的拷贝 + 每帧 PNG 编码。已经砍过两刀：
空闲帧率 20→10 fps（`FRAME_MS_IDLE`），把手文字从每帧排版改成缓存贴图。
还能再砍的两刀，本轮没做：宠物画布 404x601 → 300x460（少 1.8 倍像素），
空闲 10 fps → 6 fps。**要不要砍到这一档，是"要不要一直开着宠物"的决定，留给你。**

### 还没做到 / 没量到的

- **字幕**（她刚说的那句话）：同样是 DOM 元素，到不了桌面。本轮把手画回来了，
  字幕没有——alpha 模式下她只有声音，屏幕上看不到那行字。
- **跟视线**：代码路径通了（shell 每 0.12 秒推一次光标），但没有真的移动鼠标去验证
  她的头会不会跟——那要动你的鼠标，凌晨三点不该这么干。
- 出场动画（虫洞）在 10 fps 下没重新看过；`ring` 收束到 0 之后不再有灰色肩带，
  这一点在截图里确认了。

## 11n. 「她根本操控不了我的电脑」——查下来是真的（2026-10-02 凌晨，第九轮）

用户原话：让她打开微信给老妈发句你好，做不到；让她查本机 java 版本，也做不到；
并问右上角那个「控制」按钮**到底有没有用**。三条都成立，而且第一条低劣到超出预期。

### 四条实测结论（先给证据，再给结论）

| 现象 | 实测出来的真因 |
| --- | --- |
| **右上角「控制·键鼠全开」是装饰** | `pyautogui` 只在 `automation` extra 里，**桌面装的人根本没有它**。六个鼠标键盘工具在任何档位都返回 `未执行：桌面控制需要可选的 'pyautogui' 依赖`。档位、审计、演练全都写对了，底下是空的 |
| **命令行永远查不到东西** | 模型看到的是一条**永远失败**的路：`run_shell` 被注册了，但 `tools.allow_shell=false` 让它每次都返回"配置里 allow_shell 为 false"，`confirmed=True` 也救不了。而真正能用的那条 `run_powershell`（由档位授权）她**不优先选**，于是回答"我的命令行执行权限被禁用了，请你自己敲" |
| 管理员档位下命令像卡死 | `run_powershell` 在「管理员」档每条都要弹 UAC，没人点就 30 秒超时。用户把档位停在「管理员」，于是**每一次尝试都表现为超时** |
| 打开微信这种"找图标点它"确实做不了 | 视觉链路是关的（`vision disabled by configuration`），她看不见屏幕。能用的只有盲操作：`win` → 打字 → `enter` |

第 2 条最讽刺：项目自己的注释里就写着"不要 advertise 一个注册表够不到的能力"（`run_powershell`
没档位时就不注册），但同一条规则**没有应用到 `run_shell` 上**。

### 改了什么

1. **`pyautogui` 进 `[desktop]` extra** + 装上 + 进 PyInstaller `hiddenimports`
   （pygetwindow / pyscreeze / pymsgbox / pyrect / pytweening / mouseinfo 一并写死：
   pyautogui 按平台动态挑模块，hooks-contrib 里没有它的钩子）。
2. **`allow_shell=false` 时不再注册 `run_shell`**：`ToolPolicy.allows_shell` 读配置，
   `shell_tools.build(allow_shell=...)` 决定要不要把它交给模型。于是模型只剩一条能走通的路。
3. `run_powershell` 的说明里加了一句实测出来的坑：**别在脚本里写 `2>&1`**，
   stdout/stderr 本来就会分别交给她；写了会被回敬一段 CLIXML。
   （我先按"关掉 progress 流"修了一版，验证后发现**根本没修好**——CLIXML 来自
   `2>&1` 而不是 progress，于是把那版**退回了**，只留下这句给模型的提示。
   注释里不许留一个没被证据支持的解释。）

### 复测（同一份代码，真调真答）

```
advertised command tools: ['run_powershell']
tools used: ('run_powershell',)
answer: **Java 17.0.19**（Eclipse Temurin 发行版，build 17.0.19+10，64 位）
```

鼠标也真的动了：档位=只看不碰 时返回"被安全策略拒绝（allow_mouse=false）"，
档位=键鼠全开 时返回 `已执行：移动鼠标到 (827, 735)`，回读 `GetCursorPos` 确认在那儿。

### 两条要用户自己定的

- **档位默认值**：「管理员」=每条命令弹 UAC=表现为卡死。这轮把测试机上停在
  「当前用户」（能真跑、不弹框）。「管理员」应该只在真要改系统的动作上用。
- **发微信这种"替我按发送"**：机制现在通了，但**我没有替你给你妈发消息**——
  一是那需要你看着做一次，二是她看不见屏幕，盲点很容易点错地方。
  要让她能"看见"就是 #75（截图进模型），前提是先确认当前 provider 里有能吃图的模型。

### 还没验证

- 冻结包里的 `pyautogui` 是否真的可用（r17 打包中，验完再写结论）。
- 「打开微信」这种多步盲操作在真机上走不走得通。
