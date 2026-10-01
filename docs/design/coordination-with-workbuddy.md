# 与 WorkBuddy 的文件边界与接口契约

> 2026-09-30 深夜。写这份文档时，WorkBuddy 正在**同一个工作树**上未提交地改动
> 38 个文件 / +2,789 行，并新增十几个后端子系统，权限是「允许完全访问」。
> 本文的目的是：我们两个 agent 不要互相覆盖，且早上有人能看懂谁做了什么。

## 1. 当前实际分工（按谁在改哪，不是按理想设计）

| 归属 | 范围 | 依据 |
|---|---|---|
| **WorkBuddy** | `jarvis/**`（`browser/ computer/ database/ knowledge/ mcp/ memory/ ocr/ planner/ plugins/ scheduler/ vector/ vision/ workflow/` + 已改的 `__main__.py`、`ui/desktop.py`、`ui/static_server.py`、`core/`、`config/`、`llm/`、`agent/`、`app/`、`tools/`） | 未提交改动与新增文件全在这里 |
| **WorkBuddy** | `tests/**`（测试从 440 涨到 1149）、`docs/architecture.md`、`docs/phases/phase-11-to-18.md`、`README.md`、`.gitignore`、`scripts/capture_window.*`、`scripts/setup_env_paths.ps1`、`frontend/vite.config.ts` | 同上 |
| **Qoder（本次夜间）** | `frontend/src/**`、`docs/design/**` | 它未触碰 `frontend/src` 任何一个文件（已用 mtime 核实） |

划分不是审美问题，是**物理避让**：`frontend/src/**` 是唯一确认零交集的区域。

## 2. 夜间这次改了什么（全在 `frontend/src/**`）

- **`styles/hud.css` 重写**：删掉"每个面板 1px 青边 + 四角括号"这套 2015 仿钢铁侠套路
  （它正是"老套"的来源：所有表面装饰相同 ⇒ 没有视觉重心）。换成
  分层半透明 + 顶部一道语义描边 + 真实投影纵深；网格用 mask 向边缘淡出，
  加一条 16 秒的极慢扫光作为唯一的环境动效。`prefers-reduced-motion` 下只关动效、
  **不关状态颜色**（麦克风是否在听不该因为动效偏好而变得不可读）。
- **新增 `components/VoiceCore.vue`**：界面唯一的视觉重心，把语音状态画成一个有纵深的核。
  状态映射：dormant 暗环 / waking 琥珀倾斜轨道快转 / armed 青色慢呼吸 /
  listening 绿色正圆大呼吸 / thinking 紫色高倾斜快轨 / broken 琥珀断裂 / held 灰虚 / error 红。
  **刻意只画几何体，不画波形**——见 §3 的诚实约束。
- **`App.vue` 左栏重构**：声核吃掉整栏剩余高度（老布局 168px 封顶，下方留一大片空），
  指标改用显示级数字（原来全是 11–12px，没有层级，得靠找才知道哪个数重要）。
- **删除 `components/HudGauge.vue`**：声核取代了它的位置，且无任何外部引用。

## 3. 一条不能让步的约束（请两边都遵守）

**声核绝不能画成"看起来像麦克风电平"的样子，除非后端真的推了电平。**

页面不是持有采集设备的一方，拿不到振幅。画一条活波波形 = 在**最需要说真话的位置**
（用户判断"它到底有没有在听"的唯一依据）上造假。所以：

- 现在：几何体表达**状态**，状态全部来自后端推送的真实 `phase` / `turn`。
- 将来要真波形：需要 §4 的 `mic_level` 接口先落地。

同理，任何读数缺失时继续显示「无读数」，不要用 0 冒充"空闲"。

## 4. 我需要 WorkBuddy 提供的接口（前端等这个）

| 接口 | 用途 | 详细设计 |
|---|---|---|
| `mic_level`（0..1 RMS，随 pipeline 事件推） | 声核从"状态几何体"升级成**真波形** | [[always-on-wake]] §9 |
| `wakeword.engine: onnx` + `voice.always_on`（默认 false） | 免点击冷唤醒、常驻待命 | [[always-on-wake]] §2 §7 |
| `wakeword.preroll_ms: 2000` | 一级检出后不能丢掉关键词前半句 | [[always-on-wake]] §4 |
| 托盘图标 + 全局热键（**真正关闭采集设备**，不是置 muted 标志） | 后台静默运行 + 隐私硬断 | [[always-on-wake]] §6 §7 |
| `speaking` 状态（TTS 正在出声） | 声核目前无法区分"它在回答"和"它在想" | —— |

## 5. 它若继续改前端，请注意（bundle 约束）

`tests/test_ui_bundle.py` 已经把这些钉住了，改坏了它自己会红，这里只是提醒为什么：

- 产物 `index.html` 里资源必须是**相对路径**（`./assets/...`），不能出现 `src="/`。
- **不能带 `crossorigin`**：`file://` 下会黑屏（现在改成本地回环 HTTP 了，但约束仍在）。
- 不能退化成内联脚本，要保留 `type="module"`。
- 夜间改完已复跑：`tests/test_ui_bundle.py` **28 项全绿**，产物路径相对、无 `crossorigin`。

## 6. 待办归属（避免明早重复劳动）

**归 WorkBuddy（后端）**
- §4 全部接口
- 空转 CPU 真因：已确认**不在语音链路**（麦克风释放、链路停掉后进程仍读到一个核的约七成），
  下一个嫌疑是桌面壳 pywebview / WinForms 消息泵。
  ⚠️ 复测前先看 `docs/desktop-operations.md` §7 末——本机部分环境下**进程 CPU 计时是坏的**，
  之前"降了 6 倍"的结论就是这么来的，已作废。

**归 Qoder（前端）**
- 拿到 `mic_level` 后把声核换成真实振幅驱动
- 角落 pill 形态（`window.hide()` 保活 + 小窗常驻）的前端部分
- 视觉细节：dormant 态偏暗、可读性可以再提一档

**谁都没做，需要决定**
- 「不做托盘、关窗即退」这条旧决策已被用户的"要后台常驻"**实质推翻**。
  推翻的理由和补偿措施写在 [[always-on-wake]] §7，需要用户确认接受
  （尤其：`voice.always_on` 建议**默认关**，由他显式打开）。

---

## 2026-10-01 通宵之后的交接（Qoder 侧已做完的）

**免点击待命已经落地并被打包实测**（`f8e7ec3` / `518e4c6`）：那一次「启用语音」的按压记进
`<数据根>/preferences.json`，下次启动由 `desktop.run()` 在 `voice.subscribe()` 之后调
`VoiceService.arm_if_remembered()` 自己回到待唤醒。打包版实测**双击 → 34 秒 → 待唤醒，零点击**。
`always-on-wake` §7 里"需要用户确认"的那条旧决策，**兑现方式变了**：不是加托盘、也不是
`voice.always_on` 默认开，而是「记住一次真实按压」+「最小化而不是关窗」。默认仍然不自动开麦，
第一次必须有人按。

三条你们改到相关代码时要知道的事：

1. **`packaging/hooks/` 现在有顺序依赖**。`runtime_hook_data_root` 必须是第一个 runtime hook：
   它决定数据根，而 `runtime_hook_funasr` 会 `mkdir(<数据根>/logs)` 写自己的报告。
   把它挪到后面、或者在入口脚本里重做这个决定，都会让打包版重新往 `%LOCALAPPDATA%` 写。
   规则本体在 `jarvis/config/paths.bundle_data_root()`，别再复制第二份。
2. **`启动小夜.bat` 顶部那句「需要语音唤醒时点窗口右上角「启用语音」；麦克风不会自己打开」
   已经不完整**：第一次成立，之后会被记忆自动武装。这句话现在是你们（`4bb76f` 之后由你们改着）
   的文件，我没动。改法建议：「第一次点一下「启用语音」，之后开机自动回到待唤醒；
   点「释放麦克风」会忘掉这个选择。」
3. **`snapshot()` 偶发不返回，根因未查**（`1f0494a` 只修了"卡住之后界面装成没卡"）。
   取证顺序写在 `docs/desktop-operations.md` §9「未结案的卡死」。要动
   `jarvis/tools/monitor.py` 或 `_read_disks` 之前先看那一段——Windows 上
   `disk_usage()` 对未就绪的卷会无限阻塞，是当前的头号嫌疑，而本机挂着 GeoServer/minio。

**空转 CPU 已结案，别再引用旧数字**：主因是 HUD 的进程排行表（`process_iter` + `memory_info`
每轮给每个进程开一次查询句柄），打包版 A/B **60.06% → 0.22%** 一个核。
`docs/desktop-operations.md` §7 里"剩下 18% 是有意取舍"那句已经删掉了——那是我进程内微基准
给的错数。语音起来的实测值是 **4.1% 一个核 / RSS 1.6–1.9 GB**。

---

## 2026-10-01 中午：语音改走界面，但**接线还压在你们未提交的改动上**

`0e8ebf8` 提交了这件事能自洽的那一半：`jarvis/ui/audio_bridge.py`、`BridgePlayer`、
`VoicePipeline.utter()`、`VoiceService.speak_text()`、`SettingsService.speaks_typed()`，
以及前端 `src/audio/{speech,channel}.ts` + 声核的谱冠。**没提交的**是三个文件里的接线，
因为它们和你们工作区里未提交的改动长在同一个文件上：

| 文件 | 我加的东西 | 为什么不能现在提交 |
|---|---|---|
| `jarvis/ui/desktop.py` | `HudBridge(audio=...)`、`audio_ready` / `audio_output` / `speech_stop` 三个桥方法、`run(audio=)`、`_fan_out()`、`allow_autoplay()` | 这个文件未提交的部分引用 `jarvis.ui.static_server`，而那个文件还没进版本库。只提交 desktop.py 会让 `main` 变成 `ImportError` |
| `jarvis/__main__.py` | `AudioPusher` 的创建、`player_factory=lambda: BridgePlayer(pusher.emit)`、`--speak` 诊断开关 | 同上（+520 行是你们的） |
| `tests/test_ui_desktop.py` | `TestAudioSurface`、`TestTypedAnswerIsReadAloud`、`TestAutoplayFlag`（13 条） | 测的就是上面那两个文件 |

**这三处别丢**：`git diff` 里有，工作区脏着就是它的临时存放处。你们提交 static_server 那条链
的时候，把这三处一起带上；不带的话功能不会坏（默认走扬声器），但「打字也朗读」和谱冠就都不生效。

两条改这段代码时的硬约束：

1. **默认必须是扬声器。** `AudioPusher.ready` 初值 `False`，页面不主动 `audio_ready(true)`
   就一个字节都不推。谁把它改成"默认往页面推"，就等于把"webview 没起来"变成"助手不说话"。
2. **`allow_autoplay()` 只在桌面模式设 `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`**，而且看到
   操作者自己填过的值只做追加不做覆盖。全平台自动播放对浏览器是保护，对这个窗口是必需品。

已实测（真机 `python -m jarvis --desktop --voice --speak <203 字>`）：`12:13:49 audio output
switched to browser` → `12:14:15 audio: 1928448 bytes in 81 slices delivered to the page`，
截图里谱冠 44 根柱随音量起伏，「停下」出现。同一批 payload 在浏览器回放，五个频带
`[0.42, 0.92, 0.67, 0.53, 0.54]`——中文语音该有的形状。**没验到的一环**：`chat_ask → 朗读`
需要模型 key，本机没有，所以那半只有单测覆盖。
