# 桌面端操作指南（小夜 / JARVIS HUD）

面向：拿到这台机器要把它装好、演示好、出事能自己查的人。
所有数字都是本机实测值（Windows 11 / 12 核 / 32 GB），不是估算。

---

## 0. 一分钟版

```bat
:: 1) 依赖（开发模式，含质量工具链）
.venv\Scripts\python -m pip install -e ".[dev,desktop]"

:: 2) 界面产物（Vue 构建，不入 git，必须自己生成一次）
.venv\Scripts\python scripts\build_desktop.py

:: 3) 一把 API Key（只进环境变量，绝不写进任何 yaml）
setx QWENAI_API_KEY "sk-..."

:: 4) 打开窗口
.venv\Scripts\python -m jarvis --desktop

:: 5) 要演示语音唤醒，再加 --voice（麦克风只在窗口里点「启用语音」时才开）
.venv\Scripts\python -m jarvis --desktop --voice

:: 6) 或者什么都不用记：双击仓库根目录的「启动小夜.bat」
```

`启动小夜.bat` 就是第 4~5 步的那条命令，多做三件事：先检查 `.venv` 和
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
| `vad.max_silence_ms` | `500` | 句尾要 500ms 静音才判「说完」——离线素材必须补尾静音，见 §6 |
| `asr.engine` | `sensevoice` | 离线，权重 ~897 MB |
| `tts.engine` | `edge` | 云端，合成一段回复要联网 |
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

---

## 5. 磁盘清理：流程与审计

**四道闸门，任何一道都能拒绝，没有一道能自动删。**

1. **扫描**（只读）→ 按类别列出可删条目，受保护路径连列都不列（实测本机排除 36 条）
2. **勾选** → 每一项是一个「可决策单位」：一个目录一行；同一层里散落的临时文件
   超过 10 个就**聚合成一行**（实测 `%TEMP%`：1,671 个散落文件 → 1 行，
   整表从 2,212 行降到 590 行，按体积倒序，最大的排最前）
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

## 7. 麦克风与「关窗即退出」

- **没有托盘**。点 X 就是退出进程并释放麦克风。pywebview 没有托盘 API，
  而「窗口没了但麦克风还开着」是隐私缺陷，不是省事的特性。
- 桌面模式的装配是 Config → Logging → System → Disk → LLM → Chat → Voice。
  **`WakeWordService` / `VadService` 根本不注册**——它们各自跑一条采集环。
  抢麦这件事用「列表里没有」来保证，而不是靠某个配置值别被写错。
- 语音不常驻：加载 ~1.5 GB RSS、本机实测 **约 2 分钟**（首次还要下载权重），
  而且麦克风是客户 IT 会当面问的东西。所以只有点「启用语音」才开。
- 加载期间界面不卡：`voice_enable()` 立即返回（实测 <1ms），模型在
  `jarvis-voice-boot` 线程里加载，状态经 `加载中 → 待唤醒/失败(原因)` 推给页面。
  GIL 争用会让 1.5s 的遥测轮询偶尔跳拍，`stores/system.ts` 有跳拍守卫。
- 「释放麦克风」= 停掉链路并把状态置为 `muted`，日志会写为什么停的。

状态有两条独立的轴，别混：`phase`（能不能跟我说话：off/loading/running/failed/muted）
和 `turn`（现在在听还是在想：idle/listening/processing）。合成一条，
失败就长得像「只是没在听」。

---

## 8. 打包成 exe

```bat
.venv\Scripts\python -m pip install -e ".[build]"
.venv\Scripts\python scripts\build_desktop.py --check
.venv\Scripts\python -m PyInstaller --noconfirm ^
  --distpath E:\BianChengGongJu\JarvisBuild\dist ^
  --workpath E:\BianChengGongJu\JarvisBuild\build ^
  packaging\jarvis.spec
```

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
   **代价也要说清**：语音栈起来后常驻 RSS 约 2.4 GB，待唤醒空转吃掉约半个核
   （Silero VAD 每 32 ms 一帧、torch 多线程反复起停）。演示无妨，长期挂着不划算。
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

## 9. 故障排查

| 现象 | 先看哪里 | 原因 / 处理 |
|---|---|---|
| 双击 exe 只弹一句「找不到界面构建产物」 | `jarvis\ui\web\index.html` 在不在 | 没跑 `scripts/build_desktop.py`，或打包时没带 `datas` |
| 窗口一片白 | DevTools（`--desktop -v`）控制台 | `index.html` 在但 `assets/*.js` 缺 → `build_desktop.py --check` 会指名 |
| 仪表盘显示「遥测读取失败」 | 日志里 `system telemetry unavailable` | `psutil` 没装：`pip install -e ".[desktop]"` |
| 文字问答报「API key environment variable ... is not set」 | 环境变量名 | 密钥只走环境变量；`setx` 后要**重开**终端 |
| 点了「启用语音」一直卡在加载中 | 日志 `voice stack failed to load` | 页面拿不到推送时状态仍在轮询里；失败原因一定在日志 |
| 语音报「拾音线程已退出」 | 麦克风被谁占了 | 会议软件/其他实例独占；关掉再点启用 |
| 叫不应 | `verify_wake_words.py` | 先分清是识别不到（加变体）还是 VAD 没判完（尾静音/音量） |
| 有声音但答得慢 | 日志 `rtf_avg` | SenseVoice 在 CPU 上 ~1.3× 实时；不是卡死 |
| 页面右上角显示「浏览器预览模式」 | 是否真在 pywebview 里 | 那是 `npm run dev` 的 mock 数据，**不能当演示效果** |
| exe 启动报缺 DLL | 是否 `pip install -e .` 后动过 venv | 冻结产物不认虚拟环境，重打 |

---

## 10. 卸载与数据保留

删 `dist\小夜\`（或 `pip uninstall jarvis-assistant`）不会碰：

- `%LOCALAPPDATA%\Jarvis\`：配置、日志、**删除审计**、模型权重
- `MODELSCOPE_CACHE` 指向的目录（本机 `E:\BianChengGongJu\AiCache\modelscope`，~1.7 GB）

想彻底清干净就手工删这两个目录。反过来，重装时把这两个目录留着可以省掉
一次 1.7 GB 的下载——但前提是环境变量还在，否则程序会往
`%USERPROFILE%\.cache\modelscope` 再下一份（这正是 `AppPaths.models_dir`
以前「声明了却没人用」造成的后果，现在由 `ConfigService` 在启动时接管：
环境里已有 `MODELSCOPE_CACHE`/`HF_HOME`/`TORCH_HOME` 就尊重，没有才指定，
并且落在系统盘时打一条 WARNING）。
