/**
 * The only channel between the page and Python.
 *
 * Outside the desktop shell (plain browser during development) there is no
 * `pywebview`, so this falls back to a synthetic feed. That keeps `npm run dev`
 * usable without faking the whole backend — but the mock is clearly labelled so
 * nobody mistakes a browser screenshot for a real reading.
 */

import { ref } from 'vue'
import { pageMode } from '../page'
import type { Skin } from '../theme'

export interface CpuMetrics {
  percent: number
  cores: number
  per_core: number[]
}

export interface MemoryMetrics {
  percent: number
  total_bytes: number
  used_bytes: number
  available_bytes: number
  swap_percent: number
}

export interface DiskMetrics {
  mount: string
  fstype: string
  percent: number
  total_bytes: number
  used_bytes: number
  free_bytes: number
}

export interface ProcessMetrics {
  pid: number
  name: string
  cpu_percent: number | null
  memory_bytes: number
}

export interface NetMetrics {
  sent_bytes: number
  recv_bytes: number
  /**
   * ``null`` means "no previous sample to difference against", not "idle". The
   * first reading after launch has no rate, and showing 0 there is the same lie the
   * CPU panel made for its first ten seconds.
   */
  send_bps: number | null
  receive_bps: number | null
}

export interface Metrics {
  taken_at: number
  uptime_seconds: number
  cpu: CpuMetrics
  memory: MemoryMetrics
  disks: DiskMetrics[]
  top_processes: ProcessMetrics[]
  net?: NetMetrics | null
  warnings: string[]
}

export interface Report {
  metrics: Partial<Metrics>
  warnings: string[]
  error: string
  /**
   * What the alert centre has open after this reading.
   *
   * Carried on the telemetry report rather than pushed on its own because the dashboard
   * already polls this every second and a half. Optional because an older shell does not
   * send it, and a missing line must read as "no alerts", not as "the machine is fine".
   */
  alerts?: AlertRow[]
}

/** One configurable alert rule, as the settings panel receives it. */
export interface AlertRuleSetting {
  code: string
  label: string
  unit: string
  help: string
  low: number
  high: number
  direction: 'above' | 'below'
  enabled: boolean
  threshold: number
  default_threshold: number
}

/** The alert centre's own section of the settings snapshot. */
export interface AlertSettings {
  rules: AlertRuleSetting[]
  cooldown_minutes: number
  cooldown_bounds: number[]
  speak_critical: boolean
  sustain_seconds: number
}

/** One open alert. See ``jarvis/app/alerts.py``. */
export interface AlertRow {
  code: string
  rule: string
  label: string
  severity: 'warn' | 'critical'
  message: string
  value: number
  threshold: number
  unit: string
  since: number
  last_seen: number
  count: number
  acknowledged: boolean
}

export interface AppInfo {
  name: string
  version: string
  engine: string
  /** When this artifact was packaged, or `source`. See ``jarvis.build_stamp``. */
  built?: string
  /**
   * Whether the notification-area icon is up, which is what decides whether the X
   * hides the app or ends it. Absent from an older shell, and `false` means the X
   * quits -- so a 「收进托盘」 button must not be offered when it would trap the app.
   */
  tray?: boolean
}

/** Where the pet's drag handle is, as fractions of the pet window's own viewport. */
export interface GripRect {
  x: number
  y: number
  width: number
  height: number
}

/** One reminder row, as Python renders it. */
export interface ReminderRow {
  job_id: string
  text: string
  when: string
  enabled: boolean
  next_run: string
  created_at: string
}

export interface ReminderBoard {
  rows: ReminderRow[]
  /** Which announcement channels are wired up right now: voice / tray. */
  channels: string[]
  error: string
}

export interface ReminderWrite {
  ok: boolean
  row: ReminderRow | null
  error: string
}

export interface MemoryRow {
  memory_id: number
  kind: string
  scope: string
  content: string
  source: string
  importance?: number
  created_at?: string
  accessed_at?: string
  access_count?: number
}

export interface MemoryBoard {
  rows: MemoryRow[]
  error: string
}

export interface KnowledgeBoard {
  documents: Record<string, unknown>[]
  stats: Record<string, unknown>
  error: string
}

/**
 * 她自学这一层现在什么样 —— 「她」那一格的读数，不是页面的记忆。
 *
 * Python 那一侧**永远回一整份**：没有接这一层时回 ``enabled: false`` + 一句原因，而不是
 * ``undefined``。这不是客气 —— 界面一旦把缺字段读成"没有这一格"，这条能力就再也看不见
 * 第二次，和"注册了一条永远失败的路"是同一笔账。
 *
 * 开关那扇门回的是**读回来**的那一份（``growth_set`` 之后再去盘上取一次），所以按钮不许
 * 拿"我刚才点了什么"去猜状态。
 */
export interface GrowthState {
  enabled: boolean
  /** 今天已经存了几条。 */
  today: number
  /** 每天的上限，后端说了算。 */
  limit_per_day: number
  /** 去重清单里一共记过几条。 */
  total_seen: number
  /** 自学条目落在知识库的哪一个子目录（``_自学``）。没接这一层时为空。 */
  folder: string
  /** 这台机器有没有接知识库 —— 没有的话她自学无处可记。 */
  has_knowledge: boolean
  error: string
}

/**
 * ``growth_forget``：只删她自学记下的那些条（索引和盘上的文件一起删）。
 *
 * 没有套 ``ActionResult`` 是因为这里多一个"删了几条"，而那个数字必须能念给人听 ——
 * 一颗说"清掉了"却没说清掉多少的按钮，和没说一样。
 */
export interface GrowthForget {
  ok: boolean
  removed: number
  error: string
}

/* ------------------------------------------------------------------ 扩展市场（插件页）
 * 这三格的类型全部照抄 Python 那一侧：`ExtensionsService.state()`、`jarvis/extensions/types.py`
 * 和 `jarvis/skills/types.py`。字段名不是页面自己发明的 —— 一发明就分成「界面说的」和
 * 「后端做的」两套，而这类漂移是本仓库反复付过账的那笔。
 */

/** 搜索结果里的一行：一条能装的东西。 */
export interface ExtensionCatalog {
  kind: string
  /** 装它时报给后端的凭据。后端只认「刚才搜出来的那批 id」，所以没搜过就装不了。 */
  id: string
  name: string
  summary: string
  /** 哪一路给的：``curated`` / ``npm`` / ``github``。分组和失败都按它说。 */
  source: string
  /**
   * 装这一条要落什么东西。形状由 ``kind`` 决定（技能是 repo/ref/path，MCP 是
   * transport/command/args），Python 那边故意不铺平 —— 铺平了就会有人忘了查 kind 就走错
   * 另一半。页面只挑要给人看的那几样：装之前那条命令行必须摊开写在屏上。
   */
  install: Record<string, unknown>
  homepage: string
  stars: number
  downloads: number
  /** 这一条要跑起来本机得有什么命令（``npx`` / ``uvx`` / ``python``）。缺了就不许装。 */
  needs: string[]
  /** 技能：来源的 ref。钉了 commit 的那几条不会跟着上游改版漂走。 */
  pinned: string
  /**
   * 图标：emoji 那一两个字符，或一个 https 的 GitHub 头像地址（后端已过白名单）。
   *
   * 空串是**正常值**不是失败：npm 的搜索响应里就没有图标这一栏。界面退成名字首字母方块，
   * 并在详情页写明「来源没给图标」。
   */
  icon: string
  /** 详情页那一段长说明。``summary`` 是一行，这一栏是「它到底干什么、有什么前提」。 */
  description: string
  /** 来源自己报的版本。技能的「有没有新版」不看它 —— 看取回来的文件内容。 */
  version: string
}

/** 一路搜索的交代：命中几条、花了多少毫秒、失败的话原文是什么。 */
export interface ExtensionSource {
  source: string
  label: string
  hits: number
  error: string
  milliseconds: number
}

export interface ExtensionSearch {
  query: string
  items: ExtensionCatalog[]
  sources: ExtensionSource[]
  /** 「这一路没通」的那几句（``标签：原文``）。它和「市场里没有」是两句不同的话。 */
  failures: string[]
  error: string
}

/** 打开页面就能看到的那一格。``error`` 非空时「空」是读不到，不是没有推荐。 */
export interface ExtensionRecommended {
  items: ExtensionCatalog[]
  error: string
  at: number
  where: string
}

/** 一条记录在**这台机器上**现在是什么样子。三颗按钮画什么全看它。 */
export interface ExtensionInstalled {
  yes: boolean
  kind: string
  name: string
  slug: string
  path: string
  at: number
  files: string[]
  digest: string
  enabled: boolean
  /** 盘上有没有那份来历记录。false = 更早那版装的，更新判断不了，只能删掉重装。 */
  provenance: boolean
  /** 清单上这一条和盘上登记的还是不是同一份（MCP 的「更新」就按这个决定给不给）。 */
  changed: boolean
  connected: boolean
  words: string
}

/** 「能不能 + 为什么」。``can=false`` 时 ``words`` 就是屏上那句原话，不是提示文案。 */
export interface ExtensionVerdict {
  can: boolean
  words: string
}

/** 点图标弹出来的那张卡要的全部东西。后端永远回完整形状。 */
export interface ExtensionDetail {
  found: boolean
  id: string
  item: Partial<ExtensionCatalog>
  installed: ExtensionInstalled
  install: ExtensionVerdict
  update: ExtensionVerdict
  remove: ExtensionVerdict
  runtime: ExtensionRuntime
  log: ExtensionLogRow[]
  directory: string
  servers_file: string
  error: string
}

/** 一条记录的四句判词：装了没、能不能装、能不能更新、能不能删。后端算，页面不重复算。 */
export interface ExtensionRowVerdicts {
  installed: ExtensionInstalled
  install: ExtensionVerdict
  update: ExtensionVerdict
  remove: ExtensionVerdict
}

/** 装 / 更新 / 删 三件事的结果行。``reason`` 必须能直接上屏。 */
export interface ExtensionResultRow {
  ok: boolean
  kind: string
  name: string
  path: string
  reason: string
  detail: string
  connect?: string
}

/** 已装的一份技能。``error`` 非空 = 盘上有这个目录但读不过来，那一行仍然要有删除钮。 */
export interface ExtensionSkill {
  slug: string
  name: string
  description: string
  path: string
  body_chars: number
  version: string
  license: string
  homepage: string
  error: string
  enabled: boolean
}

/** ``servers.json`` 里的一条 MCP server。 */
export interface ExtensionServer {
  name: string
  transport: string
  command: string
  args: string[]
  url: string
  enabled: boolean
  source: string
  homepage: string
  needs: string[]
  /** 这一条要哪些环境变量。密钥本身永远不上屏（只写名字），同 ``api_key`` 那条规矩。 */
  env_from: string[]
  note: string
}

/**
 * MCP 那一层现在连着什么样。
 *
 * ``servers`` 是**条数**（一个数字），每一台自己的情况在 ``statuses`` 里 —— 后端
 * ``McpService.stats()`` 两个都给。以前它只给总数，页面于是只能在每一行里写「这一台的
 * 工具数后端没单独回」；后端明明知道（``servers()`` 早就是逐台的），那是后端的缺口，
 * 不是页面的设计。读总数请走 ``mcpCount`` 那种类容错写法：它对老形状也不会崩。
 */
export interface ExtensionMcpStatus {
  name: string
  transport: string
  connected: boolean
  tool_count: number
  error: string
}

export interface ExtensionMcpState {
  enabled: boolean
  running: boolean
  servers: number
  connected: number
  tools: number
  statuses: ExtensionMcpStatus[]
  errors: Record<string, string>
  error: string
}

/** 本机运行时：装 npm/uvx 那类条目前先确认那条命令真在。 */
export interface ExtensionRuntime {
  present: string[]
  paths: Record<string, string>
  labels: Record<string, string>
  missing: string[]
  error: string
}

/** 扩展审计的一行。``at`` 是 epoch 秒，屏上必须写成时间（``stamp()``），不许贴裸数字。 */
export interface ExtensionLogRow {
  at: number
  event: string
}

export interface ExtensionState {
  directory: string
  servers_file: string
  /** 那一大颗「允许她运行外部扩展程序」，这一层的总开关。 */
  layer_enabled: boolean
  servers: ExtensionServer[]
  skills: ExtensionSkill[]
  mcp: ExtensionMcpState
  runtime: ExtensionRuntime
  /** 后端记着的那一次搜索。页面刷新也看得见，所以它是状态不是本地变量。 */
  search: {
    query: string
    items: ExtensionCatalog[]
    sources: ExtensionSource[]
    failures: string[]
  }
  /** 读 ``servers.json`` 本身失败的那一句 —— 和「一条 server 都连不上」不是一件事。 */
  store_error: string
  log: ExtensionLogRow[]
  /**
   * 按 catalog id 索引的判词。搜索结果那一行和详情卡读的是**同一份**，
   * 所以同一个东西不会在两个位置上说出两句话。
   */
  verdicts: Record<string, ExtensionRowVerdicts>
  error: string
  /** 只有刚装过那一下才带。``reason`` 是能直接上屏的一句人话。 */
  install?: ExtensionResultRow
  /** 只有刚更新过那一下才带。「没动」也是一种 ok=true，靠 ``detail`` 那句话区分。 */
  update?: ExtensionResultRow
  /** 只有刚按条目删过那一下才带（从搜索结果或详情卡删的，不是「已装」那一格）。 */
  removed?: ExtensionResultRow
}

/** One execution record, as both the scheduler and the workflow engine render it. */
export interface RunRow {
  ok: boolean
  detail?: string
  error: string
  started_at: string
  finished_at: string
}

/** One scheduled job. ``last`` is null when it has never fired. */
export interface ScheduledJobRow {
  job_id: string
  name: string
  action: string
  trigger: string
  expression: string
  enabled: boolean
  next_run: string
  last: RunRow | null
  created_at?: string
}

export interface ScheduledJobsBoard {
  rows: ScheduledJobRow[]
  error: string
}

/**
 * The three counters the tab opens with.
 *
 * Each block is ``{}`` when this process has no such engine -- the Python side
 * reports absence as an empty object rather than an error, because "this build has
 * no planner" is a fact about the build, not a malfunction.
 */
export interface AutomationOverview {
  scheduler: Record<string, unknown>
  workflow: Record<string, unknown>
  planner: Record<string, unknown>
  planner_enabled: boolean
  error: string
}

/** One workflow definition, as ``WorkflowDef.to_dict`` renders it. */
export interface WorkflowRow {
  name: string
  description: string
  trigger: string
  schedule: string
  steps: Record<string, unknown>[]
  path: string
}

export interface WorkflowRunRow {
  run_id: number
  workflow: string
  started_at: string
  finished_at: string
  ok: boolean
  steps: { name: string; ok: boolean; skipped: boolean; output: string; error: string }[]
  error: string
}

export interface WorkflowBoard {
  rows: WorkflowRow[]
  runs: WorkflowRunRow[]
  error: string
}

/** One step of a plan. ``status`` is pending / running / done / failed / skipped. */
export interface PlanStepRow {
  step_id: string
  title: string
  action: string
  status: string
  notes: string
  depends_on: string[]
}

export interface PlanRow {
  goal: string
  status: string
  rationale: string
  revision: number
  steps: PlanStepRow[]
}

export interface PlanAnswer {
  ok: boolean
  plan: PlanRow | null
  error: string
}

export interface JobRunAnswer {
  ok: boolean
  run: RunRow | null
  error: string
}

export interface WorkflowRunAnswer {
  ok: boolean
  run: WorkflowRunRow | null
  error: string
}


/** One figure the operator generated from a photograph, as the picker sees it. */
export interface PetFigureRow {
  id: string
  name: string
  /** Vertices left after simplification -- what 「细线 / 粗线」 actually counts. */
  lines: number
  aspect: number
  coverage: number
  /** Slab thickness in units of the figure's own height. */
  depth: number
  /** outline | photo | cartoon. */
  style: string
  /** The fur colour a cartoon was tinted with, as ``#rrggbb``. */
  colour: string
  /** Which authored shape a cartoon uses; empty for the traced styles. */
  template: string
  /** True when this figure keeps a thumbnail clipped from your photo. */
  has_texture: boolean
  created_at: string
  /** Milliseconds the segmentation pass took on this machine. */
  cost_ms: number
  /** base64 PNG, no data-URL prefix. Empty when the thumbnail went missing. */
  preview: string
}

/** One of the rigged models that ship with the program (see ``pet_gallery.py``). */
export interface GalleryModel {
  /** ``builtin:<Key>`` -- the value stored as the chosen figure. */
  id: string
  key: string
  name: string
  /** 动物 / 幻想 / 人物. */
  kind: string
  note: string
  /** Bytes of the shipped ``.glb``, read off disk. */
  bytes: number
  /** Animation clips in the file, read off disk. */
  clips: number
  url: string
}

export interface PetAssets {
  ok: boolean
  pets: PetFigureRow[]
  /** The built-in models on this machine: only the ones whose file actually shipped. */
  models?: GalleryModel[]
  /** The figure on the desktop. Empty string means the built-in one. */
  current: string
  can_generate?: boolean
  reason?: string
  error: string
}

export interface PetGenerated extends PetAssets {
  /** The four ids this photo produced, in candidate order. */
  made: string[]
  cost_ms: number
  segment_ms: number
  /** Which style was asked for; the shell echoes it so the note can name it. */
  style?: string
  colour?: string
}

/** What the pet page asks for: which body to draw, and its data. */
export interface SilhouetteModel {
  /** ``builtin`` (a shipped model), ``silhouette`` (a traced figure), or ``""``. */
  kind: string
  /** Set when ``kind`` is ``builtin``; the page hands it straight to the loader. */
  model?: GalleryModel
  id: string
  name: string
  depth: number
  style: string
  colour: string
  template: string
  aspect: number
  /** base64 PNG clipped to the silhouette. Only set for the 照片 style. */
  texture: string
  points: [number, number][]
}

export interface PetState {
  shown: boolean
  /** 她现在多大一档：small / normal / large / giant。 */
  scale?: string
  scales?: { id: string; label: string; multiplier: number }[]
  width?: number
  height?: number
  /** 「她会自己动一动」那颗开关。关掉只是不再自己动，戳她照样理人。 */
  idle_motion?: boolean
  ok?: boolean
  error?: string
}

/** The bundled llama.cpp server: what it is doing and why it is not doing more. */
/** 一个随包（或买家自己放进 ``模型`` 目录）的离线模型，连同它在这台机器上的实测代价。 */
export interface BundledModel {
  file: string
  label: string
  size_mb: number
  /** 文件在不在这台机器的 ``模型`` 目录里 —— 删掉了就选不了，但列表里仍要看得见。 */
  present: boolean
  selected: boolean
  license: string
  /** false = 这个文件没实测过，下面几个数是按大小估的，界面上要这么说。 */
  measured: boolean
  /** 默认开思考的模型（Qwen3 全系）：不关掉它，回答会看着是空的。 */
  thinking: boolean
  /** 带那 34 个工具的真清单，实测见过它调成一次工具。装得下 ≠ 调成过。 */
  acts_verified: boolean
  gate_mb: number
  resident_mb: number
  cpu_mb: number
  tiers: { context: number; needs_free_mb: number; resident_mb: number }[]
}

/** 「按我这台电脑给一套建议」的返回值。source 说清是谁给的，界面不许含糊。 */
/** 门槛预检：要是用这个模型 / 这一档，这台机器接不接得住。数字都是当场读的。 */
export interface GateCheck {
  ok: boolean
  reason: string
  free_mb: number
  total_mb: number
  need_mb: number
  file_mb: number
  vram_mb: number
  gpu: string
  disk_free_mb: number
  model_label: string
  /** true = 这一条连「照样加载」都不放行，界面上就不该给那个按钮。 */
  hard: boolean
}

export interface LocalAdvice {
  model: string
  context: number
  idle_minutes: number
  eager: boolean
  reason: string
  source: 'ai' | 'rule'
  note?: string
  error?: string
}

export interface ModelServerStatus {
  phase: 'off' | 'loading' | 'running' | 'failed'
  detail: string
  can_run: boolean
  reason: string
  model: string
  model_label: string
  /** 拨盘上选的那个：'' = 自动（最大的），'none' = 不用离线模型，否则是文件名。 */
  model_selected: string
  models: BundledModel[]
  port: number
  /** The window the running child was given -- not necessarily the one dialed now. */
  context: number
  dialed_context: number
  /** False when the window cannot hold the tool table: she answers, does not act. */
  acts: boolean
  needed_mb: number
  eager: boolean
  idle_seconds: number
  load_ms: number
  free_mb: number
  ok?: boolean
  error?: string
}

/** One phone this machine will answer. */
export interface MobileDevice {
  device_id: string
  name: string
  added_at: number
  last_seen: number
}

/** 一台**手机**一行。``adb`` 按通道列（插线 + 两个 tcpip 端口是三行），服务端把它们并成一行。 */
export interface MirrorDevice {
  serial: string
  state: string
  label: string
  model: string
  wireless: boolean
  ready: boolean
  /** What to do about this row, in Chinese; empty when nothing is wrong. */
  hint: string
  /** 这台手机的全部通道（第一个就是 ``serial`` 那个）。只有一行时它是唯一的通道。 */
  channels: string[]
}

export interface MirrorTier {
  id: string
  label: string
  /** What this tier costs, so the choice is not a guess. */
  note: string
}

/** One way the scrcpy window should sit on the screen. */
export interface MirrorWindow {
  id: string
  label: string
  note: string
}

/** One way the phone can reach this computer, with the tradeoff written out. */
export interface MirrorMode {
  id: string
  label: string
  pros: string
  cons: string
}

/** Where the mirror is. The panel polls this, and nothing in it costs a subprocess. */
export interface MirrorState {
  phase: string
  detail: string
  serial: string
  available: boolean
  tier: string
  tiers: MirrorTier[]
  mode: string
  modes: MirrorMode[]
  window: string
  windows: MirrorWindow[]
  stretch: boolean
  keys: MirrorAction[]
  swipes: MirrorAction[]
  audio: boolean
  keep_screen: boolean
  seconds: number
  tail: string[]
  error?: string
}

/** 读手机那一屏的回答：文本 + 每条来源说了什么。 */
export interface MirrorScreenAnswer {
  text: string
  error: string
  sources: string[]
  allow_vision: boolean
}

/** 面板上一个按钮：它叫什么、那一下在手机上是什么。后端发什么就画什么，不在前端写死。 */
export interface MirrorAction {
  id: string
  label: string
  note: string
}


/** 生图：本机 ComfyUI 或云端 API 的一次答复（服务端 ImageAnswer 的镜像）。 */
export interface VideoPlan {
  size: string
  width: number
  height: number
  fps: number
  seconds: number
  frames: number
  steps: number
  explicit: boolean
  vram_total_mb: number
  max_frames: number
  capped: boolean
  note: string
}

export interface VideoCloudConfig {
  submit_url: string
  key_env: string
  model: string
  body: string
  headers: string
  task_path: string
  poll_url: string
  video_path: string
  status_path: string
  succeeded: string
  running: string
  failed: string
  key_set?: boolean
}

export interface VideoState {
  phase: string
  detail: string
  error: string
  last_path: string
  last_seconds: number
  model?: string
  plan?: VideoPlan
  choice?: { size: string; fps: number; seconds: number; steps: number }
  options?: {
    sizes: string[]
    fpss: number[]
    seconds: number[]
    engines?: string[]
    auto_profile: { size: string; fps: number; seconds: number; vram_total_mb: number }
  }
  engine?: string
  cloud?: VideoCloudConfig
  cloud_defaults?: VideoCloudConfig
}
export interface VideoAnswer {
  ok: boolean
  path: string
  error: string
  prompt_input: string
  prompt: string
  prompt_note: string
  seconds: number
  duration: number
  engine?: string
  frames?: number
  fps?: number
  size?: string
  steps?: number
  capped?: boolean
  note?: string
}

export interface ImageAnswer {
  ok: boolean
  /** 第一张的路径。**不是"就这一张"** —— 一次画几张看 `ImageState.count`。 */
  path: string
  /** 这一次落盘的全部图。一次出好几张时，面板要一张一张都摆出来。 */
  paths: string[]
  /** 实际走了哪条：``local`` | ``cloud``。空串是没画成。 */
  engine: string
  prompt: string
  /**
   * 用户原始描述（任意语言）。服务端整句翻译为英文；prompt 为实际送入引擎的译文。
   */
  prompt_input: string
  /** 自动识别的源语言与翻译服务信息。 */
  prompt_note: string
  size: string
  steps: number
  seed: number
  seconds: number
  /** 缩过的预览（data URL）。原图按原样落盘，这一条只是为了让弹窗立刻看得见。 */
  preview: string
  error: string
}

export interface ImageEngineOption {
  id: string
  label: string
  note: string
}

export interface ImageSizeOption {
  size: string
  note: string
}

/** 一次画几张的那一档。**只对本地引擎生效** —— 云端那条是 `n: 1` 写死的。 */
export interface ImageCountOption {
  count: number
  note: string
}

/** 本机引擎探到什么。``ok`` 是"有人答话"，不是"刚才画成功过"。 */
export interface ImageProbe {
  kind: string
  url: string
  ok: boolean
  detail: string
  version: string
  checkpoints: string[]
  probed_at: number
  vram_total_mb: number
  vram_free_mb: number
}

export interface ImageState {
  engine: string
  engines: ImageEngineOption[]
  sizes: ImageSizeOption[]
  size: string
  steps: number
  /** 一次画几张（只对本地引擎生效）。 */
  count: number
  counts: ImageCountOption[]
  comfy_url: string
  comfy_dir: string
  cloud_base: string
  cloud_model: string
  cloud_key_set: boolean
  can_start_local: boolean
  /** 这个包里到底带没带本地引擎：带了我才敢说「替你起一个」。 */
  vendored_engine: boolean
  local: ImageProbe | null
  phase: string
  detail: string
  last_path: string
  last_seconds: number
  tail: string[]
  error: string
}

export interface MirrorDevices {
  devices: MirrorDevice[]
  error: string
}

const MIRROR_TIERS: MirrorTier[] = [
  {
    id: 'smooth',
    label: '流畅优先',
    note: '画面缩到长边 800、30 帧、4Mbps。弱 WiFi / 老手机选这个，最不容易卡。',
  },
  {
    id: 'balanced',
    label: '均衡（默认）',
    note: '长边 1024、60 帧、6Mbps，H.264 各家手机都支持，延迟和清晰度的折中。',
  },
  {
    id: 'sharp',
    label: '清晰优先',
    note: '长边 1440、H.265。同带宽下更清楚，但手机要支持 H.265 编码，老机器上延迟反而更高。',
  },
  {
    id: 'native',
    label: '手机原分辨率',
    note: '不缩放。只适合 5GHz/6GHz WiFi 或 USB 线，2.4GHz 上基本一定会卡。',
  },
]

const MIRROR_WINDOWS: MirrorWindow[] = [
  {
    id: 'window',
    label: '窗口',
    note: '普通窗口，自己拖大小。竖屏手机在横屏显示器上左右会有黑边，这是比例问题不是坏了。',
  },
  {
    id: 'maximized',
    label: '铺满（最大化窗口）',
    note: '把窗口撑到整个屏幕。画面仍按手机比例居中，所以竖屏手机左右依旧有黑边 —— 要连黑边一起填上，勾下面的「拉伸填满」（会变形）。',
  },
  {
    id: 'fullscreen',
    label: '全屏',
    note: 'scrcpy 自己进全屏（等同窗口里按 F11）。这一档要重开一次投屏才生效，因为全屏状态是 SDL 持有的，从外面改不可靠。',
  },
]

const MIRROR_KEYS: MirrorAction[] = [
  { id: 'back', label: '返回', note: '等于手机上那下返回键。' },
  { id: 'home', label: '主页', note: '回桌面。投屏不会断，画面跟着变。' },
  { id: 'recents', label: '多任务', note: '最近任务那个键。' },
  { id: 'vol_up', label: '音量 +', note: '媒体音量大一格。投屏放视频时不用伸手去按手机侧键。' },
  { id: 'vol_down', label: '音量 −', note: '小一格。手机躺在一边够不着的时候用它。' },
]

const MIRROR_SWIPES: MirrorAction[] = [
  { id: 'next', label: '下一个（往上滑）', note: '就是手指从下往上划那么一下 —— 抖音、快手翻到下一条视频用它。' },
  { id: 'prev', label: '上一个（往下滑）', note: '反着划那么一下，回到上一条。' },
]

const MIRROR_MODES: MirrorMode[] = [
  {
    id: 'usb',
    label: 'USB 线（开 USB 调试）',
    pros: '最稳的一档：不占 WiFi、延迟最低、不受路由器和邻居信道影响，插上就能投，反手用鼠标操作手机也最跟手。',
    cons: '要一根能传数据的线（有些线只能充电）；人离电脑就得拔线；手机被线占着，部分老手机换个 USB 口还要重新点一次允许调试。',
  },
  {
    id: 'wifi',
    label: '同一个 WiFi（无线调试）',
    pros: '不用线，手机拿在手里也能投；配过一次之后，重连只要点一下「连接」。笔记本在沙发上、主机在桌上的时候只有这一档能用。',
    cons: '吃路由器：2.4GHz 或隔两堵墙就会卡，画质档要往下退；手机重启或换网络后端口会变，要重新连一次；Android 11 以上要先用「无线调试」的配对码配一次，10 及以下必须先插一次线点「开无线端口」；公司/公共 WiFi 开了 AP 隔离的连不上。',
  },
  {
    id: 'scan',
    label: '同一个 WiFi（不开无线调试，自动找）',
    pros:
      '不用碰「无线调试」那个页面，也不用填 IP 和 6 位配对码：手机插一次线点「开无线端口」之后，端口就固定在 5555，以后每次点「自动找」由电脑把本网段的手机找出来并连上。老手机（Android 10 及以下）只有这一条无线路可走。',
    cons:
      '第一次那根线省不掉：不开 USB 调试也不开无线调试，电脑上没有任何软件能让手机把屏幕交出来，这是安卓的安全模型。5555 端口在同一网段里对所有人开放，公司/公共 WiFi 别用这一档；开了 AP 隔离的网段扫不到；手机重启后 tcpip 模式会失效，要再插一次线。',
  },
]

/**
 * A browser tab has no ``adb`` and no phone, so the preview mirrors nothing.
 *
 * What it does keep is the bookkeeping: the phase moves off → running → off, the tier
 * and the two switches persist across calls, and the fps line shows up in 说明 the way
 * scrcpy's stdout does. A mock that only returns a fixed string cannot be used to test
 * the panel, and this panel has a state machine worth testing.
 */
let mockScreenVision = false

const mockMirror: MirrorState = {
  phase: 'off',
  detail: '',
  serial: '',
  available: true,
  tier: 'balanced',
  tiers: MIRROR_TIERS,
  mode: 'usb',
  modes: MIRROR_MODES,
  window: 'window',
  windows: MIRROR_WINDOWS,
  stretch: false,
  keys: MIRROR_KEYS,
  swipes: MIRROR_SWIPES,
  audio: false,
  keep_screen: true,
  seconds: 0,
  tail: [],
}

function mirrorTune(
  tier: string,
  audio: boolean,
  keepScreen: boolean,
  mode: string,
): MirrorState {
  if (MIRROR_TIERS.some((row) => row.id === tier)) mockMirror.tier = tier
  if (MIRROR_MODES.some((row) => row.id === mode)) mockMirror.mode = mode
  mockMirror.audio = audio
  mockMirror.keep_screen = keepScreen
  return { ...mockMirror }
}

function mirrorStart(serial: string): MirrorState {
  if (mockMirror.phase === 'running' || mockMirror.phase === 'starting') {
    mockMirror.detail = `已经在了（${mockMirror.serial}）。要先关掉再换设备`
    return { ...mockMirror }
  }
  mockMirror.phase = 'running'
  mockMirror.serial = serial || 'MOCK-PHONE-1'
  mockMirror.seconds = 0
  mockMirror.detail = 'current fps: 60.0'
  mockMirror.tail = ['scrcpy 5.0.1', 'Device ready', 'current fps: 60.0']
  return { ...mockMirror }
}

function mirrorStop(): MirrorState {
  mockMirror.phase = 'off'
  mockMirror.serial = ''
  mockMirror.seconds = 0
  mockMirror.tail = []
  mockMirror.detail = '手动停止'
  return { ...mockMirror }
}

/** The LAN endpoint's whole truth, as the desktop sees it. */
export interface MobileState {
  running: boolean
  url: string
  port: number
  /** SHA-256 of the self-signed certificate; the phone pins this. */
  fingerprint: string
  devices: MobileDevice[]
  pairing: { active: boolean; expires_in: number; attempts_left: number }
  error: string
  firewall: string
}

export interface MobileCode {
  code: string
  error: string
}

/** What the shell is doing right now: window up, icon in the tray, pet visible. */
export interface ShellState {
  visible: boolean
  tray: boolean
  /** `off` | `waiting` | `hearing` | `working` -- what the tray icon claims. */
  status: string
  pet: boolean
  error?: string
}

export type JunkKind = 'exact' | 'loose_files'

export interface JunkItem {
  path: string
  size_bytes: number
  category: string
  is_directory: boolean
  modified_at: number
  /** `loose_files` means "this folder's scattered files", not the folder itself. */
  kind: JunkKind
  /** How many files the row stands for; 1 unless it is an aggregate. */
  member_count: number
  /** A few member names, so a collapsed row is still recognisable. */
  sample: string[]
  /** Which scan produced this row; a delete is only honoured against the current one. */
  scanned_at: number
}

export interface JunkGroup {
  category: string
  total_bytes: number
  items: JunkItem[]
}

export interface DiskPlan {
  groups: JunkGroup[]
  total_bytes: number
  skipped_protected: number
  truncated: boolean
  error: string
}

/** Voice availability: "can I talk to it". Mirrors jarvis.core.events.VoicePhase. */
export type VoicePhase = 'off' | 'loading' | 'running' | 'failed' | 'muted'

/** Turn state: "is it listening or thinking". Mirrors UiVoiceState. */
export type TurnPhase = 'idle' | 'listening' | 'processing'

export interface VoiceStatus {
  phase: VoicePhase
  detail: string
  keyword: string
}

export interface ChatReply {
  question: string
  answer: string
  error: string
  /** What the model reported thinking. Empty unless the setting is on. */
  reasoning: string
  /** Which model answered, and which conversation the turn belonged to. */
  model?: string
  provider?: string
  conversation?: string
  task_id?: string
  cancelled?: boolean
  tools?: ToolReport
  /**
   * Files this turn put on disk: a drawn PNG, a generated .xlsx / .docx / .pptx / .pdf.
   *
   * Absolute paths, never bytes -- the page asks for the one picture it is about to show
   * (``artifact_read``) and hands the rest to the shell (``copy_image`` / ``open_artifact``).
   * This is the evidence half of the bubble: 「画好了」 with a path behind it is a claim the
   * operator can pick up, and one without is just a sentence a model wrote.
   */
  artifacts: string[]
}

/**
 * What one answer's request actually carried, as the backend saw it.
 *
 * The panel has a 带工具 checkbox and a tool count in the registry, and neither tells you
 * what went out with *this* request. A 4K local model gets a narrowed pack, or nothing at
 * all when the window refused the table; without this line those look identical to the
 * model declining to act, which is how a whole round of "she's useless offline" got
 * diagnosed wrong until it was written down.
 */
export interface ToolReport {
  /** Tools that went out with the request. */
  count: number
  /** Which capability pack was selected, e.g. `runtime+system`. Empty = the whole table. */
  pack: string
  /** Tools the model actually invoked, in call order. */
  used: string[]
  /** Why there are fewer hands than the checkbox implies: `overflow` | `model` | ''. */
  degraded: string
}

/**
 * What ``artifact_read`` hands back for one picture this turn produced.
 *
 * A shrunken ``data:image/...`` URL rather than a file URL: the page is not allowed to
 * read the disk, and the shell only ever answers for paths inside the directories *it*
 * wrote. Anything else comes back ``ok: false`` with the reason in ``error`` -- and that
 * reason is shown on screen, not logged.
 */
export interface ArtifactPicture {
  ok: boolean
  data_url: string
  error: string
}

/** ``artifact_stat`` 那一格：附件卡片第二行"多大 · 什么时候写的"。 */
export interface ArtifactStat {
  ok: boolean
  size: number
  /** epoch 秒；0 = 没读到。 */
  mtime: number
  error: string
}

/**
 * The answer the three "do something on this machine" doors share.
 *
 * ``copy_text`` / ``copy_image`` / ``open_artifact`` all either did it or say why they
 * did not. There is no third state: a button that neither acts nor explains is the bug
 * this project has been billed for repeatedly.
 */
export interface ActionResult {
  ok: boolean
  error: string
}

/** ``open_artifact`` 的两档：交给系统默认程序，或在资源管理器里把它选中。 */
export type ArtifactHow = 'open' | 'folder'

/**
 * Whether this machine has a network, as the shell's probe last saw it.
 *
 * ``online === null`` is "还没测过", which is a different claim from "没网" and has to stay
 * drawable as one. ``seconds_ago`` says how old the reading is, so a probe thread that
 * stopped moving cannot keep the bar saying 有网 forever. The door only ever reads a cache
 * -- it never blocks, which is what makes it safe on a 10 秒 interval.
 */
export interface NetworkState {
  online: boolean | null
  detail: string
  seconds_ago: number | null
}


export interface ChatTurn {
  role: 'user' | 'assistant'
  text: string
  /** Kept beside the text, never inside it: the answer is read aloud and replayed. */
  reasoning?: string
  /** Empty on a user turn and on a single-model answer. */
  model?: string
  /**
   * Everything said around a round table, for the fold under the answer.
   *
   * Display-only like `reasoning`: nothing stores it, so switching tabs and back drops it.
   * The answer alone is what the next question replays -- three models' opinions in the
   * history would be the largest thing in every request after it.
   */
  record?: string
  /** Absent on a user turn and on anything reloaded from the transcript. */
  tools?: ToolReport
  /**
   * Files this turn produced, in the same shape ``ChatReply`` carries.
   *
   * Always sent now (``jarvis.ui.state_bridge.ChatTurn.to_dict`` writes the key), so the
   * panel can draw a picture or a document row for a turn it did not see arrive. A user
   * turn has an empty list; so does an answer that only wrote a sentence.
   */
  artifacts: string[]
}

/**
 * One conversation tab, as the shell reports it.
 *
 * Status is per tab rather than global because two of them can be answering at the same
 * moment, and a strip that shows one light for all of them cannot tell you which tab is
 * still working.
 */
export interface ConversationCard {
  id: string
  title: string
  status: 'idle' | 'running' | 'done' | 'failed'
  phase: string
  provider: string
  model: string
  turns: number
  active: boolean
  task_id: string
  error: string
  /**
   * What kind of failure ``error`` is, so the panel can name the next step: ``auth`` /
   * ``forbidden`` / ``quota`` / ``timeout`` / ``network`` / ``server`` / ``request`` /
   * ``malformed`` / ``empty`` / ``crash`` / ``pictures``. Optional because it is only ever
   * sent with a failure, and a page that guessed advice from the wording of ``error``
   * would go quiet the day that wording changed.
   */
  error_kind?: string
  /**
   * Something this turn did that the answer itself does not show. ``no_tools`` when the
   * request had to leave the tool table out to fit the model's context window.
   *
   * Separate from ``error_kind`` because it is not a failure — the answer came. Drawing it
   * as one would teach everyone to ignore the card that reports the real ones.
   */
  note?: string
  /**
   * What this conversation still has to ask, in the order it was typed.
   *
   * Optional because it is a control strip, not a record: a conversation with nothing
   * waiting sends nothing. The questions are also already in the transcript — this list
   * exists so each one can be taken back out before she gets to it.
   */
  queued?: QueuedQuestion[]
}

/** One question waiting its turn behind the answer currently on screen. */
export interface QueuedQuestion {
  task_id: string
  question: string
}

/** The answer still arriving, carrying the whole partial text rather than a diff. */
export interface StreamingTurn {
  conversation_id: string
  task_id: string
  text: string
  phase: string
  model: string
}

/** One running or recently finished turn. */
export interface TaskInfo {
  task_id: string
  conversation_id: string
  kind: string
  question: string
  state: string
  phase: string
  error: string
  participants: string[]
  started_at: string
  finished_at: string
  answer_chars: number
  tools_used: string[]
}

/** The live state Python pushes into the page; also pullable on mount. */
export interface UiSnapshot {
  voice_state: TurnPhase
  voice: VoicePhase
  voice_detail: string
  history: ChatTurn[]
  last_event: string
  interrupted: boolean
  conversations?: ConversationCard[]
  streaming?: StreamingTurn | null
  /** Which animation the 「思考中」 surfaces draw. Absent means the old shell. */
  thinking_loader?: string
  /** Whether typed answers get read aloud. Same reason, same snapshot. */
  speaks_typed?: boolean
}

/** What ``chat_send`` answers with: the turn exists, its text has not. */
export interface ChatSendResult {
  ok: boolean
  task_id: string
  conversation: string
  question: string
  answer: string
  error: string
}

export interface DiskOutcome {
  deleted: string[]
  freed_bytes: number
  failed: [string, string][]
  log_path: string
  error: string
}

/** What ``GET <base_url>/models`` came back with, or why it could not. */
export interface EndpointModels {
  ok: boolean
  models: string[]
  error: string
}

export interface ModelRow {
  name: string
  base_url: string
  model: string
  models: ModelSpec[]
  default_model: string
  key_env: string
  key_set: boolean
  /** True = 这个端点不需要钥匙（本地模型 / 内网服务）。缺省等于需要。 */
  key_optional?: boolean
  /** 这一家自己的超时秒数；null/缺省 = 跟全局 llm.timeout_seconds。 */
  timeout_seconds?: number | null
  source: string
  edited: boolean
  current: boolean
  /** 自带模型那一行：用哪个模型由「选哪个离线模型」决定，这里加不加都会被盖掉。
   *  后端给这句话，是为了让界面少一个要抄的 'local' 字面量（抄漏就是又一处各说一套）。 */
  models_locked?: boolean
}

/** A row the operator hid: absent from the menu, untouched in the file. */
export interface HiddenRow {
  name: string
  source: string
  models: number
  current: boolean
}

export interface SettingsSnapshot {
  error: string
  providers: string[]
  models: ModelRow[]
  hidden_models?: HiddenRow[]
  provider: string
  base_url: string
  model: string
  /** The *name* of the environment variable. The value never crosses the bridge. */
  api_key_variable: string
  api_key_set: boolean
  overrides_active: string[]
  voice_auto_arm: boolean
  auto_speak_typed: boolean
  telemetry_interval_ms: number
  /** Ask the model for its reasoning and show it under the answer. */
  thinking_enabled: boolean
  /** Tokens of reasoning one answer may spend -- a real budget, not a 高/中/低 label. */
  thinking_budget: number
  thinking_budget_bounds: number[]
  /** False for the bundled llama.cpp, which ignores a thinking budget entirely
   *  (measured r90: 32/128/512/2048 all thought ~850 characters). The row says so
   *  instead of promising a number that moves nothing. */
  thinking_budget_honored?: boolean
  /** Past exchanges replayed into each request. */
  history_turns: number
  history_turns_bounds: number[]
  /** 上面这三格说的是**哪一对模型**（「alpha · alpha-1」），面板照着它写提示。 */
  tuning_pair?: string
  /** 这一对单独设过没有。false = 显示的是默认值，别的模型也照这个来。 */
  tuning_pinned?: boolean
  /** The sentence spoken on the wake word. Empty means she stays quiet. */
  wake_greeting: string
  wake_greeting_default: string
  wake_greeting_max: number
  /** Absent when the shell has no wake-word layer wired: then the box is not drawn. */
  wake_keywords?: string[]
  wake_keywords_stored?: string[]
  wake_keywords_default?: string[]
  wake_keywords_max?: number
  wake_keyword_min_chars?: number
  wake_keyword_max_chars?: number
  /** The animation shown to the left of 「思考中」: dots / matrix / ring / bars. */
  thinking_loader: string
  thinking_loader_choices: string[]
  /** {id,label} pairs straight from the shell, so a fifth loader cannot render as a raw id. */
  thinking_loader_options?: { id: string; label: string }[]
  /** What the interface calls her. Leaving the box empty hands back the shipped name. */
  display_name?: string
  display_name_default?: string
  display_name_max?: number
  /** The bundled offline model's dials. Each tier's note is a measured number. */
  local_model?: string
  local_model_label?: string
  local_models?: BundledModel[]
  local_context?: number
  local_context_options?: { id: number; label: string; note: string; needs_free_mb: number }[]
  local_idle_minutes?: number
  local_idle_options?: { id: number; label: string }[]
  local_eager?: boolean
  local_needs_free_mb?: number
  local_free_mb?: number
  /** What the tool table costs, measured: a window under this cannot host it. */
  local_tool_table_tokens?: number
  /** The section-wide request timeout the rows fall back to. */
  llm_timeout_seconds?: number
  /** The env var the cloud voice-cloning key lives in, and whether it is set. */
  cloud_voice_key_variable?: string
  cloud_voice_key_set?: boolean
  /** The save gate did not run a connectivity test; the row is stored untested. */
  warning?: string
  ok?: boolean
  /** Absent from an older shell -- then the 告警 fieldset is not drawn at all. */
  alerts?: AlertSettings
  /** Settings the assistant changed itself and no person has re-saved since. */
  ai_edited: string[]
  applied?: Record<string, unknown>
  problems?: Record<string, string>
}

export interface UsageTotals {
  days: number
  since: string
  until: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_tokens: number
  /** ``null`` means the provider never reported cached tokens: 「无读数」, not 0%. */
  cache_hit_percent: number | null
  cache_data_reported: boolean
  calls_without_cache_data: number
  avg_latency_ms: number
  /** True when this is the whole ledger (`days: 0`), not a window into it. */
  all_time: boolean
}

/**
 * One model's share of the ledger.
 *
 * `share_percent` is left to the page: the denominator is the same total the header
 * prints, and computing it twice in two places is how a pie stops adding up to 100%.
 */
export interface UsageModelRow {
  provider: string
  model: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_tokens: number
  cache_hit_percent: number | null
  avg_latency_ms: number
}

/** Where the ledger actually begins and ends. Empty strings when it is empty. */
export interface UsageSpan {
  first_at: string
  last_at: string
  rows: number
  days: number
}

export interface UsageDay {
  day: string
  calls: number
  prompt_tokens: number
  completion_tokens: number
  cached_tokens: number
  cache_hit_percent: number | null
}

export interface UsageReport {
  error: string
  summary: UsageTotals | null
  /** Empty for the all-time reading, on purpose: 400 bars is not a picture. */
  daily: UsageDay[]
  /** The same window as `summary`, sliced by model. Adds up to the totals above it. */
  models: UsageModelRow[]
  /** The ledger's own extent, so an all-time label can quote a date instead of guessing. */
  span: UsageSpan
}

/** One model inside a provider, as the pickers show it. */
export interface ModelSpec {
  id: string
  /** What to *show*; falls back to ``id`` when no friendlier name was given. */
  label: string
}

/**
 * One configured provider with the models it offers.
 *
 * Two levels rather than one flat list, because that is the shape of the data: a
 * key belongs to a provider, and a provider offers several models. A flat list
 * would repeat the provider on every row and still leave the page to group them.
 */
export interface ProviderChoice {
  name: string
  base_url: string
  models: ModelSpec[]
  /** The model the picker starts on when this provider is chosen without one. */
  default_model: string
  /** False means the environment variable named by ``key_variable`` is empty. */
  key_set: boolean
  key_variable: string
  /** True = this endpoint answers without a credential (local server). Absent = needs one. */
  key_optional?: boolean
  timeout_seconds?: number | null
  current: boolean
}

/** Is a credential missing *and* required for this row? Asked in four places, and one of
 * them forgot the "optional" half -- it told the operator the bundled offline model would
 * fail to answer, because no key was typed into a box that does not need one. */
export function needsKey(row: Pick<ProviderChoice, 'key_set' | 'key_optional'>): boolean {
  return !row.key_set && !row.key_optional
}

/** How hard the model should think, as the four levels the header offers. */
export type ThinkingLevel = 'off' | 'low' | 'medium' | 'high'

export interface ModelList {
  error: string
  providers: ProviderChoice[]
  provider: string
  model: string
  /** The thinking level remembered for the current provider/model pair. */
  thinking: string
  /** 面板里填过精确预算时才有值：档位是 custom 时用它画「自定义 8000」。 */
  thinking_custom_budget?: number | null
  /** Past exchanges replayed for the current pair. */
  turns: number
  /** Whether the tool table goes out with this pair's next request. */
  tools: boolean
  thinking_levels: string[]
  turns_bounds: number[]
  /** Per model, what 测一下 found. Absent means nobody has looked at any of them. */
  caps?: ModelCap[]
}

/**
 * One measured model, as the shell reports it.
 *
 * A measurement rather than a capability flag from a list, because the only thing worth
 * trusting here is what this endpoint answered when it was actually asked. `vision` is
 * `null` for "the picture half never ran" -- which is not the same claim as `false`.
 */
export interface ModelCap {
  provider: string
  model: string
  chat: boolean
  vision: boolean | null
  detail: string
  at: string
}

/** The verdict of pressing 测一下 on one model row. */
export interface ProbeVerdict {
  ok: boolean
  detail: string
  vision: boolean | null
  latency_ms: number
  provider: string
  model: string
  error?: string
}

/** One chair at the table. Provider may be blank when the model name is unique. */
export interface SeatChoice {
  provider: string
  model: string
}

/**
 * 协作形态。`single` 是"别协作，就现在这个模型"，也是默认。
 *
 * 名字和后端 `jarvis/app/collaboration.py` 里的 `MODE_*` 一一对应，测试钉住不许漂。
 */
export type CollaborationMode = 'single' | 'table' | 'boss' | 'vote'

/** 形态的人话名字，标题和下拉框都用它。 */
export const MODE_LABELS: Record<CollaborationMode, string> = {
  single: '单个模型',
  table: '圆桌轮流',
  boss: '主管分发',
  vote: '独立作答·匿名互评',
}

/**
 * 这一单会花几次请求。和后端 `requests_for()` 同一条算式，两份各写一次，
 * 因为页面上那个数字是用户点下去之前唯一能看到的价格。
 */
export function requestsFor(mode: CollaborationMode, seats: number, rounds = 2): number {
  if (seats <= 0) return 0
  if (mode === 'boss') return seats + 1
  if (mode === 'vote') return seats === 1 ? 1 : seats * 2
  if (mode === 'table') return seats * Math.min(3, Math.max(1, rounds)) + 1
  return seats
}

/** The verdict of changing one of the per-model knobs. */
export interface TuningResult {
  ok: boolean
  error: string
  thinking: string
  turns: number
  tools: boolean
}

/** The verdict of adding or removing a model from a provider's list. */
export interface ModelEditResult {
  ok: boolean
  error: string
  models: ModelSpec[]
  default_model: string
}

/** Which device is playing the assistant, and why if it is not this page. */
export interface AudioOutputState {
  output: 'browser' | 'speaker'
  reason: string
  bytes_sent?: number
}

/** What the model made of the current junk scan. */
export interface JunkAdvice {
  text: string
  error: string
  /** Tool names the analysis called on its way to an answer, newest last. */
  tools_used: string[]
}

/**
 * One finished tool call, as the registry remembered it.
 *
 * Refusals come through here too, and they are the entries most worth reading: a
 * safety gate that fired is otherwise invisible from the window.
 */
export interface ActivityEntry {
  at: number
  tool: string
  ok: boolean
  milliseconds: number
  detail: string
}

export interface ActivityLog {
  entries: ActivityEntry[]
  error: string
}

/** One speakable voice, as the picker lists it. */
export interface VoiceChoice {
  id: string
  label: string
  current: boolean
  /** ``builtin`` ships with the engine; ``clone`` was recorded by the operator. */
  kind?: string
  /** How long the clip behind a clone is, so the list can say what it is showing. */
  duration_ms?: number
  /** A clone whose clip was uploaded to the vendor. Only true when asked for. */
  cloud?: boolean
}

/** The rules the recording UI must state *before* somebody speaks into a microphone. */
export interface CloningState {
  available: boolean
  reason: string
  min_ms: number
  comfortable_ms: number
  max_ms: number
  max_voices: number
}

/** Where a desktop recording stands. The page polls this while the timer runs. */
export interface SampleStatus {
  phase: 'idle' | 'recording' | 'captured' | string
  ms: number
  peak: number
  silent?: boolean
  error: string
  ok?: boolean
  target_ms?: number
  min_ms?: number
  max_ms?: number
  can_save?: boolean
  /** Whether the clip was handed to the vendor for a cloud clone (only when asked). */
  uploaded?: boolean
  cloud_error?: string
  voice?: VoiceChoice
  voices?: VoiceChoice[]
}

export interface VoiceList {
  error: string
  engine: string
  current: string
  choices: VoiceChoice[]
  /** Rate multiplier and volume, the window's current values. */
  speed: number
  volume: number
  /** What the sliders may offer -- sent by the backend so the range lives in one place. */
  speed_min: number
  speed_max: number
  volume_min: number
  volume_max: number
  /** The recording rules, and whether a voice can be made here at all. */
  cloning?: CloningState
}

/** What the shell did with a skin change: applied to the other window, or not. */
export interface SkinApplyAnswer {
  ok: boolean
  skin?: string
  /** True when the pet window was open and got the new palette. */
  pet?: boolean
  error: string
}

/**
 * What the shell did with the skin's three inks.
 *
 * The bubbles on the desktop are painted by Python, so this is the page telling the
 * shell what colour it is wearing — see ``pet_palette`` in ``jarvis/ui/desktop.py``.
 */
export interface PaletteAnswer {
  ok: boolean
  /** True when a figure was up to be recoloured. ``false`` is not an error. */
  pet?: boolean
  error: string
}

export interface VoicePreview {
  ok: boolean
  error: string
  voice?: string
  chunks?: number
}

/** How much of the machine the assistant may touch right now. */
export interface ComputerAccessState {
  tier: number
  label: string
  describe: string
  enabled: boolean
  dry_run: boolean
  allow_mouse: boolean
  allow_keyboard: boolean
  /** 能不能把字写进输入框。独立于四个档位，默认 false。 */
  allow_typing: boolean
}

export interface ComputerLevel {
  tier: number
  label: string
  describe: string
  current: boolean
}

export interface ComputerLevels {
  error: string
  current: ComputerAccessState
  levels: ComputerLevel[]
}

/** The shell window's own state — the only thing on this surface that is about the window. */
export interface WindowState {
  error: string
  maximized: boolean
}

/** One command-line level: 关闭 / 演练 / 当前用户 / 管理员. */
export interface ShellLevel {
  tier: number
  mode: string
  label: string
  describe: string
  current: boolean
}

/** One line of the command audit — what ran, and whether it came back. */
export interface ShellCommand {
  at: number
  mode: string
  label: string
  script: string
  exit_code: number | null
  seconds: number | null
  note: string
}

export interface ShellLevels {
  error: string
  current: { tier: number; mode: string; label: string; describe: string }
  levels: ShellLevel[]
  commands: ShellCommand[]
}

/** A row the operator ticked: the number *and* the name it was shown under. */
export interface ProcessTarget {
  pid: number
  name: string
}

/**
 * Something the assistant asked to end, waiting on a person. It is never executed
 * without the operator pressing 确认, which goes through ``process_kill``.
 */
export interface ProcessProposal {
  pid: number
  name: string
  reason: string
  asked_at: number
  age_seconds: number
}

/**
 * What happened to one of them. ``note`` carries the refusals too — a system process
 * left alone, a PID that had already been recycled, a process owned by someone else.
 */
export interface KillOutcome {
  pid: number
  name: string
  ok: boolean
  note: string
}

export interface KillReport {
  results: KillOutcome[]
  ended: number
  error: string
}

/** One stored conversation. */
export interface SessionInfo {
  id: string
  title: string
  updated_at: string
  turns: number
}

/** What a batch delete did: which ids went, which were not there to begin with. */
export interface BatchSessionDelete extends SessionList {
  deleted?: string[]
  missing?: string[]
}

export interface VoiceBatchRemove {
  error: string
  voices: VoiceChoice[]
  removed?: string[]
}

export interface SessionList {
  error: string
  current: string
  sessions: SessionInfo[]
}

export interface StoredTurn {
  role: string
  content: string
  at: string
  attachments: Attachment[]
}

/**
 * A file handed in with a question.
 *
 * Images carry a downscaled thumbnail the page made itself; everything else is
 * name and size only, because this assistant reads neither video nor documents
 * and pretending otherwise would be the lie this project avoids.
 */
export interface Attachment {
  name: string
  kind: 'image' | 'video' | 'file'
  mime: string
  size: number
  data?: string
}

export interface SessionMessages {
  error: string
  current: string
  messages: StoredTurn[]
}

/**
 * One message on the audio channel: a slice of s16le PCM, or a command to stop.
 *
 * The shape is Python's (`jarvis.ui.audio_bridge`), not the page's invention, so a
 * field rename has to happen there first.
 */
export interface SpeechMessage {
  seq?: number
  sample_rate?: number
  channels?: number
  format?: string
  final?: boolean
  pcm?: string
  flush?: boolean
}

interface PywebviewApi {
  snapshot(): Promise<Report>
  alerts(): Promise<{ active: AlertRow[]; history: AlertRow[]; error: string }>
  alerts_ack(code: string): Promise<{ ok: boolean; error: string }>
  alerts_ack_all(): Promise<{ ok: boolean; count: number; error: string }>
  app_info(): Promise<AppInfo>
  disk_scan(): Promise<DiskPlan>
  disk_delete(items: JunkItem[], confirmed: boolean): Promise<DiskOutcome>
  process_kill(items: ProcessTarget[], confirmed: boolean): Promise<KillReport>
  process_proposals(): Promise<{ entries: ProcessProposal[]; error: string }>
  process_dismiss(pid: number): Promise<{ ok: boolean; error: string }>
  disk_advice(): Promise<JunkAdvice>
  activity_log(): Promise<ActivityLog>
  tts_voices(): Promise<VoiceList>
  tts_pick(voice: string): Promise<VoiceList>
  tts_preview(voice: string): Promise<VoicePreview>
  tts_set_style(speed?: number, volume?: number): Promise<VoiceList>
  /** Recording a sample of *this* machine's operator. Audio never crosses the bridge. */
  voice_sample_start(): Promise<SampleStatus>
  voice_sample_status(): Promise<SampleStatus>
  voice_sample_stop(): Promise<SampleStatus>
  voice_sample_discard(): Promise<SampleStatus>
  voice_sample_save(name: string, prompt_text: string, upload: boolean): Promise<SampleStatus>
  /** Forget a recorded voice: deletes one of the operator's own files. */
  voice_clone_remove(voice_id: string): Promise<{ error: string; voices: VoiceChoice[] }>
  voice_clone_remove_many(voiceIds: string[]): Promise<VoiceBatchRemove>
  computer_levels(): Promise<ComputerLevels>
  computer_set_tier(tier: number): Promise<ComputerLevels>
  computer_set_typing(allowed: boolean): Promise<ComputerLevels>
  skin_apply(skin: string): Promise<SkinApplyAnswer>
  pet_palette(fill: number, line: number, glow: number): Promise<PaletteAnswer>
  window_state(): Promise<WindowState>
  window_toggle_max(): Promise<WindowState>
  shell_state(): Promise<ShellState>
  window_hide(): Promise<ShellState>
  reminders(): Promise<ReminderBoard>
  reminder_add(text: string, when: string): Promise<ReminderWrite>
  reminder_cancel(key: string): Promise<{ ok: boolean; removed: string; error: string }>
  reminder_toggle(jobId: string, enabled: boolean): Promise<{ ok: boolean; error: string }>
  memory_list(): Promise<MemoryBoard>
  memory_forget(id: number): Promise<{ ok: boolean; error: string }>
  memory_forget_all(confirmed: boolean): Promise<{ removed: number; error: string }>
  knowledge_state(): Promise<KnowledgeBoard>
  knowledge_ingest(path: string): Promise<{ ok: boolean; error: string; document?: Record<string, unknown> }>
  knowledge_forget(docId: string): Promise<{ ok: boolean; error: string }>
  knowledge_probe(query: string): Promise<{ hits: Record<string, unknown>[]; error: string }>
  // 她自学那一层。形参名照投屏那条禁令避开 window/promise/arguments，`on` 与 Python 一致。
  growth_state(): Promise<GrowthState>
  growth_set(on: boolean): Promise<GrowthState>
  growth_forget(): Promise<GrowthForget>
  automation_overview(): Promise<AutomationOverview>
  scheduled_jobs(): Promise<ScheduledJobsBoard>
  job_toggle(jobId: string, enabled: boolean): Promise<{ ok: boolean; error: string }>
  job_run(jobId: string): Promise<JobRunAnswer>
  job_remove(jobId: string): Promise<{ ok: boolean; removed: boolean; error: string }>
  workflow_definitions(): Promise<WorkflowBoard>
  workflow_run(name: string): Promise<WorkflowRunAnswer>
  workflow_reload(): Promise<{ ok: boolean; count: number; error: string }>
  plan_goal(goal: string): Promise<PlanAnswer>
  pet_state(): Promise<PetState>
  pet_toggle(): Promise<PetState>
  pet_set_scale(scale: string): Promise<PetState>
  pet_set_idle(on: boolean): Promise<PetState>
  pet_grip(rect: GripRect): Promise<{ ok: boolean }>
  pet_drag(dragging: boolean): Promise<{ ok: boolean }>
  pet_frame(png: string): Promise<{ ok: boolean }>
  pet_arrived(): Promise<{ ok: boolean; error?: string }>
  pet_assets(): Promise<PetAssets>
  pet_generate(photo: string, name?: string, style?: string): Promise<PetGenerated>
  pet_pick(petId: string): Promise<PetAssets>
  pet_delete(petId: string): Promise<PetAssets>
  pet_model(): Promise<SilhouetteModel>
  model_server_status(): Promise<ModelServerStatus>
  model_server_start(): Promise<ModelServerStatus>
  model_server_stop(): Promise<ModelServerStatus>
  local_model_check(model: string, context: number): Promise<GateCheck>
  local_advice(): Promise<LocalAdvice>
  local_advice_apply(advice: LocalAdvice): Promise<{ error: string; snapshot: SettingsSnapshot }>
  shell_levels(): Promise<ShellLevels>
  shell_set_tier(tier: number): Promise<ShellLevels>
  mobile_state(): Promise<MobileState>
  mobile_toggle(on: boolean): Promise<MobileState>
  mobile_pair_code(): Promise<MobileCode>
  mobile_revoke(deviceId: string): Promise<{ revoked: boolean; devices: MobileDevice[] }>
  mobile_revoke_all(): Promise<{ revoked: number; devices: MobileDevice[] }>
  mobile_selftest(): Promise<{ plaintext_refused: boolean; note: string }>
  mirror_state(): Promise<MirrorState>
  // 生图。参数名照投屏那条禁令一样避开 window/promise/arguments 这些 stub 内部标识符。
  image_state(): Promise<ImageState>
  video_state(): Promise<VideoState>
  video_generate(
    prompt: string,
    frames: number | null,
    steps: number | null,
    size: string | null,
    fps: number | null,
    seconds: number | null,
  ): Promise<VideoAnswer>
  video_tune(
    size: string | null,
    fps: number | null,
    seconds: number | null,
    steps: number | null,
  ): Promise<VideoState>
  video_probe(): Promise<VideoState>
  // 视频引擎三档 + 云端多家可自填配置。参数名同样避开 stub 内部标识符。
  video_set_engine(engine: string): Promise<VideoState>
  video_cloud_save(fields: Record<string, string>): Promise<VideoState>
  video_cloud_reset(): Promise<VideoState>
  artifact_video(path: string): Promise<{ ok: boolean; data_url: string; error: string }>
  image_probe(): Promise<ImageState>
  image_generate(prompt: string, size: string): Promise<ImageAnswer>
  image_tune(engine: string, size: string, steps: number, count: number): Promise<ImageState>
  image_setup(
    comfy_url: string,
    comfy_dir: string,
    cloud_base: string,
    cloud_model: string,
  ): Promise<ImageState>
  mirror_devices(): Promise<MirrorDevices>
  mirror_start(serial: string): Promise<MirrorState>
  mirror_stop(): Promise<MirrorState>
  mirror_connect(address: string): Promise<MirrorState>
  mirror_pair(address: string, code: string): Promise<MirrorState>
  mirror_tune(
    tier: string,
    audio: boolean,
    keep_screen: boolean,
    mode: string,
  ): Promise<MirrorState>
  mirror_tcpip(serial: string): Promise<MirrorState>
  // 形参不许叫 window：pywebview 用 Python 方法的参数名生成 new Function，那个 body 里写的是
  // window.pywebview._jsApiCallback —— 参数叫 window 就把全局遮蔽掉，这一扇门在打包版里根本调不动。
  mirror_display(window_mode: string, stretch: boolean): Promise<MirrorState>
  mirror_key(key: string): Promise<MirrorState>
  mirror_screen(question: string): Promise<MirrorScreenAnswer>
  mirror_screen_vision(allow: boolean): Promise<MirrorScreenAnswer>
  mirror_swipe(direction: string): Promise<MirrorState>
  mirror_discover(): Promise<MirrorState>
  chat_sessions(): Promise<SessionList>
  chat_new_session(): Promise<SessionList>
  chat_switch(sessionId: string): Promise<SessionList>
  chat_rename(sessionId: string, title: string): Promise<SessionList>
  chat_delete(sessionId: string): Promise<SessionList>
  chat_delete_many(sessionIds: string[]): Promise<BatchSessionDelete>
  chat_messages(sessionId: string): Promise<SessionMessages>
  voice_status(): Promise<VoiceStatus>
  voice_enable(): Promise<VoiceStatus>
  voice_mute(): Promise<VoiceStatus>
  voice_talk(): Promise<VoiceStatus>
  chat_ask(text: string, attachments: Attachment[]): Promise<ChatReply>
  chat_send(
    text: string,
    attachments: Attachment[],
    conversation?: string,
    provider?: string,
    model?: string,
  ): Promise<ChatSendResult>
  chat_cancel(taskId: string): Promise<{ ok: boolean; error: string }>
  chat_collaborate(
    text: string,
    seats: SeatChoice[],
    rounds?: number,
    conversation?: string,
    mode?: CollaborationMode,
  ): Promise<ChatSendResult>
  llm_test(provider: string, model?: string): Promise<ProbeVerdict>
  chat_tasks(): Promise<{ tasks: TaskInfo[]; error: string }>
  chat_conversations(): Promise<{ conversations: ConversationCard[]; error: string }>
  chat_open(conversationId: string): Promise<{ conversation: string; conversations: ConversationCard[] }>
  chat_new(): Promise<{ conversation: string; conversations: ConversationCard[] }>
  chat_tab_model(
    conversationId: string,
    provider: string,
    model: string,
  ): Promise<{ ok: boolean; error: string; provider: string; model: string }>
  chat_clear(): Promise<{ ok: boolean }>
  state_snapshot(): Promise<UiSnapshot>
  settings_get(): Promise<SettingsSnapshot>
  settings_apply(patch: Record<string, unknown>): Promise<SettingsSnapshot>
  list_endpoint_models(
    base_url: string,
    provider?: string,
    api_key?: string,
  ): Promise<EndpointModels>
  usage_summary(days: number): Promise<UsageReport>
  chat_models(): Promise<ModelList>
  chat_pick(provider: string, model?: string): Promise<ModelList>
  chat_tuning(thinking?: string, turns?: number, tools?: boolean): Promise<TuningResult>
  llm_add_model(provider: string, modelId?: string, label?: string): Promise<ModelEditResult>
  llm_remove_model(provider: string, modelId?: string): Promise<ModelEditResult>
  audio_ready(ok: boolean, reason: string, role: string): Promise<AudioOutputState>
  audio_output(): Promise<AudioOutputState>
  speech_stop(): Promise<{ stopped: boolean }>
  // 本轮产出的文件：读图、进剪贴板、交给系统，以及"到底有没有网"。
  // 形参名照投屏那条禁令一样避开 window/promise/arguments —— 这些名字是 pywebview 生成
  // new Function 时会被遮蔽掉的内部标识符，占了名字这一扇门在打包版里根本调不动。
  artifact_read(path: string): Promise<ArtifactPicture>
  artifact_stat(path: string): Promise<ArtifactStat>
  copy_text(text: string): Promise<ActionResult>
  copy_image(path: string): Promise<ActionResult>
  open_artifact(path: string, how: string): Promise<ActionResult>
  network_state(): Promise<NetworkState>
  // ------------------------------------------------------------------ 扩展市场（插件页）
  // 门名一律写成 Python 方法名（下划线）。pywebview 的 ``js/api.js`` 是拿 ``element.func``
  // **原样**建函数的，不做下划线→驼峰转换；这里写 `extensions_addHttp` 的话，打包版
  // ``window.pywebview.api.extensions_addHttp`` 就是 undefined —— 又是一扇「注册了但永远
  // 调不动」的门（r95 那次的同一个形状）。驼峰只留给下面导出的包装函数名。
  // 形参名照投屏那条禁令避开 window/promise/arguments 这些 stub 内部标识符。
  extensions_state(): Promise<ExtensionState>
  extensions_search(query: string, kinds: string[]): Promise<ExtensionSearch>
  extensions_install(item_id: string): Promise<ExtensionState>
  extensions_recommended(): Promise<ExtensionRecommended>
  extensions_detail(item_id: string): Promise<ExtensionDetail>
  extensions_update(item_id: string): Promise<ExtensionState>
  extensions_remove_item(item_id: string, confirmed: boolean): Promise<ExtensionState>
  extensions_add_http(name: string, url: string): Promise<ExtensionState>
  extensions_layer_set(on: boolean): Promise<ExtensionState>
  extensions_server_set(name: string, on: boolean): Promise<ExtensionState>
  extensions_server_remove(name: string, confirmed: boolean): Promise<ExtensionState>
  extensions_reconnect(): Promise<ExtensionState>
  skill_set(slug: string, on: boolean): Promise<ExtensionState>
  skill_remove(slug: string, confirmed: boolean): Promise<ExtensionState>
}

declare global {
  interface Window {
    pywebview?: { api: PywebviewApi }
    /** Installed by the page; Python calls it through ``evaluate_js``. */
    __jarvisState?: (snapshot: UiSnapshot) => void
    /** The audio channel's entry point. See ``onSpeech`` below. */
    __jarvisAudio?: (message: SpeechMessage) => void
    /**
     * Whether the window is on screen. A hidden WebView2 window still reports
     * `document.hidden === false`, so the shell has to say this out loud -- see
     * ``jarvis.ui.lifecycle.BACKGROUND_SCRIPT``.
     */
    __jarvisBackground?: (foreground: boolean) => void
    /**
     * The pet window's cue channel: `emerge` replays the wormhole arrival, and
     * `wake`/`sleep` start and stop its draw loop -- a hidden WebView2 page still
     * thinks it is visible, so the shell has to say so.
     */
    __jarvisPet?: (command: string) => void
  }
}

/** The browser preview has no real window, so it pretends to have one. */
let maximizedMock = false

/** The preview also has no command line; the levels shown here are the real labels. */
let shellTierMock = 0

/** The preview's reminder list, so the tab can show what it looks like with rows in it. */
let reminderMock: ReminderRow[] = []

/**
 * The preview's automation tab. One scheduled job and one definition, so the empty
 * states are not the only thing a browser preview can ever show.
 *
 * Deliberately *not* the reminder namespace: the real bridge filters those out, and
 * a preview that showed them would be teaching the wrong shape.
 */
let automationJobsMock: ScheduledJobRow[] = [
  {
    job_id: 'workflow:每日早报',
    name: '每日早报',
    action: 'workflow:每日早报',
    trigger: 'cron',
    expression: '0 9 * * *',
    enabled: true,
    next_run: '明天 09:00（预览）',
    last: { ok: true, detail: '两步都成功', error: '', started_at: '', finished_at: '' },
  },
]

let workflowMock: WorkflowRow[] = [
  {
    name: '每日早报',
    description: '早上汇总磁盘与系统状态',
    trigger: 'cron',
    schedule: '0 9 * * *',
    steps: [{ name: '检查磁盘' }, { name: '磁盘紧张时清理' }],
    path: 'workflows/daily.yaml',
  },
]

/** A browser tab has no desktop to put a figure on, so the switch only remembers itself. */
let petShownMock = false

/** A browser tab holds no port, so the preview says so instead of inventing one. */
function mockMobile(): MobileState {
  return {
    running: false,
    url: '',
    port: 0,
    fingerprint: '',
    devices: [],
    pairing: { active: false, expires_in: 0, attempts_left: 0 },
    error: '浏览器预览里没有局域网服务，只有电脑版会监听',
    firewall: '',
  }
}

function mockShell(): ShellLevels {
  const labels: Record<number, [string, string, string]> = {
    0: ['off', '关闭', '模型问不到这条能力，命令行一次都不会碰。'],
    1: ['rehearsal', '演练', '模型可以要一条命令，这里回它「本来会执行什么」，一条都不真跑。'],
    2: ['user', '当前用户', '真的执行，权限就是你打开小夜时的那个身份——能读能写你能碰的东西。'],
    3: ['admin', '管理员', '每条命令都弹一次 UAC，你在弹窗上点「是」它才动。'],
  }
  const levels = Object.entries(labels).map(([tier, [mode, label, describe]]) => ({
    tier: Number(tier),
    mode,
    label,
    describe,
    current: Number(tier) === shellTierMock,
  }))
  const active = labels[shellTierMock] ?? labels[0]
  return {
    error: '',
    current: { tier: shellTierMock, mode: active[0], label: active[1], describe: active[2] },
    levels,
    commands: [],
  }
}

// The dev-bridge's own copy of the two sliders, so the mock remembers a drag
// the same way the real backend does.
let mockSpeed = 1
let mockVolume = 1
/** The picked 音色, remembered like the two sliders. Not ``mockVoice``, which is the fake voice service. */
let mockTtsVoice = 'zh-CN-XiaoxiaoNeural'

const mockVoiceChoices: VoiceChoice[] = [
  { id: 'zh-CN-XiaoxiaoNeural', label: '晓晓 · 女声 · 温和（Edge 默认）', current: false, kind: 'builtin' },
  { id: 'zh-CN-YunxiNeural', label: '云希 · 男声 · 清亮', current: false, kind: 'builtin' },
  { id: 'zh-CN-XiaoyiNeural', label: '晓伊 · 女声 · 活泼', current: false, kind: 'builtin' },
]

/**
 * Preview-only recorder. It counts up and hands back a stand-in clip so the whole flow
 * (录 -> 停 -> 写句子 -> 存 -> 列表里多一行「我的」) can be walked in a browser. App.vue
 * prints 「数据为模拟生成」 whenever this mock is what is answering -- and a browser page
 * has no microphone here anyway, since the real recording happens in Python.
 */
const mockSample = { phase: 'idle' as string, startedAt: 0, ms: 0 }
/** A session list that behaves like the real one: deletes delete, renames rename. */
const mockSessionStore: { rows: SessionInfo[]; current: string; seq: number } = {
  rows: [
    { id: 'mock-a', title: '现在几点了', turns: 1, updated_at: '2026-10-05T13:34:10' },
    { id: 'mock-b', title: '今天星期几', turns: 1, updated_at: '2026-10-02T13:30:39' },
    { id: 'mock-c', title: '打开记事本', turns: 1, updated_at: '2026-10-02T13:15:39' },
  ],
  current: 'mock-a',
  seq: 0,
}
/** Same three numbers the Python sampler reports, so a browser walk sees the same rules. */
const MOCK_SAMPLE_TARGET_MS = 15000
const MOCK_SAMPLE_MIN_MS = 2000
const MOCK_SAMPLE_MAX_MS = 30000

/**
 * Preview-only alert rows, so the box and its 「知道了」 can be exercised in a browser.
 *
 * Acknowledging clears them from the list without touching the simulated readings, which
 * is the real engine's behaviour: the disk did not get bigger because you said so.
 * App.vue prints 「数据为模拟生成」 whenever this mock is what is answering.
 */
let mockAlerts: AlertRow[] = [
  {
    code: 'disk:C:\\',
    rule: 'disk',
    label: '磁盘剩余空间 C:\\',
    severity: 'critical',
    message: 'C:\\ 可用只剩 8.0 GB，低于告警线 20 GB',
    value: 8,
    threshold: 20,
    unit: 'GB',
    since: Date.now() / 1000 - 420,
    last_seen: Date.now() / 1000,
    count: 8,
    acknowledged: false,
  },
  {
    code: 'cpu',
    rule: 'cpu',
    label: 'CPU 占用',
    severity: 'warn',
    message: 'CPU 已经连着占用 93%，超过告警线 90%',
    value: 93,
    threshold: 90,
    unit: '%',
    since: Date.now() / 1000 - 130,
    last_seen: Date.now() / 1000,
    count: 93,
    acknowledged: false,
  },
]

/**
 * The four rows the shell returns, with the vertex counts measured on a real photo
 * (a lying white tiger: 211 / 142 / 79 / 42 after the subject is cropped and cut a
 * second time). Kept as data rather than a formula because the direction is the
 * point: a looser tolerance has to come back with *fewer* lines, and a mock that got
 * that backwards is how a picker gets built showing four identical cards.
 */
const mockCandidates: [number, number, number][] = [
  [0.002, 0.1, 211],
  [0.004, 0.18, 142],
  [0.008, 0.26, 79],
  [0.016, 0.36, 42],
]
const mockLabels = ['细线', '常规', '粗线', '厚块']
let mockPets: PetFigureRow[] = []
let mockCurrentPet = ''
let mockPetSerial = 0

/** A polygon that gets fewer corners and more thickness as the candidate index grows. */
const mockTemplates: [string, string][] = [
  ['坐猫', 'cat'],
  ['团鸡', 'bird'],
  ['垂耳狗', 'dog'],
  ['团子', 'bun'],
]
const mockFur = '#c8a04a'

/** Mirrors what ``pet_gallery.py`` reports, for browser checks without a shell. */
const mockBundled: BundledModel[] = [
  {
    file: 'Qwen2.5-7B-Instruct.Q4_K_M.gguf',
    label: 'Qwen2.5-7B',
    size_mb: 4466,
    present: true,
    selected: true,
    license: 'Apache-2.0',
    measured: true,
    thinking: false,
    acts_verified: true,
    gate_mb: 5200,
    resident_mb: 5061,
    cpu_mb: 4758,
    tiers: [
      { context: 2048, needs_free_mb: 5072, resident_mb: 5034 },
      { context: 4096, needs_free_mb: 5200, resident_mb: 5061 },
      { context: 8192, needs_free_mb: 5456, resident_mb: 5182 },
      { context: 16384, needs_free_mb: 5968, resident_mb: 5226 },
    ],
  },
  {
    file: 'Qwen3-4B.Q4_K_M.gguf',
    label: 'Qwen3-4B',
    size_mb: 2590,
    present: true,
    selected: false,
    license: 'Apache-2.0',
    measured: true,
    thinking: true,
    acts_verified: false,
    gate_mb: 3400,
    resident_mb: 2828,
    cpu_mb: 2700,
    tiers: [
      { context: 2048, needs_free_mb: 3300, resident_mb: 2854 },
      { context: 4096, needs_free_mb: 3400, resident_mb: 2828 },
      { context: 8192, needs_free_mb: 3600, resident_mb: 2959 },
      { context: 16384, needs_free_mb: 4100, resident_mb: 3000 },
    ],
  },
  {
    file: 'Qwen3-1.7B-Q8_0.gguf',
    label: 'Qwen3-1.7B',
    size_mb: 1749,
    present: true,
    selected: false,
    license: 'Apache-2.0',
    measured: true,
    thinking: true,
    acts_verified: false,
    gate_mb: 2300,
    resident_mb: 2201,
    cpu_mb: 2121,
    tiers: [
      { context: 2048, needs_free_mb: 2200, resident_mb: 2224 },
      { context: 4096, needs_free_mb: 2300, resident_mb: 2201 },
      { context: 8192, needs_free_mb: 2450, resident_mb: 2205 },
      { context: 16384, needs_free_mb: 2700, resident_mb: 2260 },
    ],
  },
  {
    file: 'Qwen3-0.6B-Q8_0.gguf',
    label: 'Qwen3-0.6B',
    size_mb: 610,
    present: true,
    selected: false,
    license: 'Apache-2.0',
    measured: true,
    thinking: true,
    acts_verified: false,
    gate_mb: 1000,
    resident_mb: 895,
    cpu_mb: 974,
    tiers: [
      { context: 2048, needs_free_mb: 950, resident_mb: 919 },
      { context: 4096, needs_free_mb: 1000, resident_mb: 895 },
      { context: 8192, needs_free_mb: 1100, resident_mb: 900 },
      { context: 16384, needs_free_mb: 1300, resident_mb: 950 },
    ],
  },
]

let mockModelServer: ModelServerStatus = {
  phase: 'off',
  detail: '',
  can_run: true,
  reason: '',
  model: 'Qwen2.5-7B-Instruct.Q4_K_M.gguf',
  model_label: 'Qwen2.5-7B',
  model_selected: '',
  models: mockBundled,
  port: 0,
  context: 4096,
  dialed_context: 4096,
  acts: false,
  needed_mb: 5200,
  eager: false,
  idle_seconds: 600,
  load_ms: 0,
  free_mb: 9000,
}

const mockGallery: GalleryModel[] = [
  { id: 'builtin:ShibaInu', key: 'ShibaInu', name: '柴犬', kind: '动物', note: '坐着的那只', bytes: 1752268, clips: 12, url: 'models/ShibaInu.glb' },
  { id: 'builtin:Fox', key: 'Fox', name: '狐狸', kind: '动物', note: '会回头张望', bytes: 1909976, clips: 12, url: 'models/Fox.glb' },
  { id: 'builtin:YellowDragon', key: 'YellowDragon', name: '小龙', kind: '幻想', note: '会扑翅膀', bytes: 421472, clips: 4, url: 'models/YellowDragon.glb' },
  { id: 'builtin:Wizard', key: 'Wizard', name: '小法师', kind: '人物', note: '带法杖', bytes: 2108476, clips: 15, url: 'models/Wizard.glb' },
]

function mockFigure(
  name: string,
  lines: number,
  depth: number,
  style = 'outline',
  template = '',
): PetFigureRow {
  const points: string[] = []
  for (let index = 0; index < lines; index += 1) {
    const angle = (index / lines) * Math.PI * 2
    const wobble = 1 + (((index * 7) % 3) - 1) * 0.06
    points.push(
      (60 + Math.cos(angle) * 30 * wobble).toFixed(1) + ',' + (84 + Math.sin(angle) * 54 * wobble).toFixed(1),
    )
  }
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="168">' +
    '<polygon points="' + points.join(' ') + '" fill="none" stroke="#96e2ff" stroke-width="2"/></svg>'
  mockPetSerial += 1
  return {
    id: 'pet:mock' + mockPetSerial.toString().padStart(8, '0'),
    name,
    lines,
    aspect: 0.9,
    coverage: 0.31,
    depth,
    style,
    colour: style === 'cartoon' ? mockFur : '',
    template,
    has_texture: style === 'photo',
    created_at: new Date().toISOString().slice(0, 19),
    cost_ms: 700 + mockPetSerial,
    preview: 'data:image/svg+xml;base64,' + btoa(svg),
  }
}

/** 浏览器预览用的假引擎：本机 ComfyUI 在浏览器里根本不存在，所以本地那一档必须
 * 老实说"这里没有"；云端那一档假装画了一张（存一张真 PNG 的 base64，让预览那条路
 * 在开发模式下也真的被走一遍），并像后端一样记账：last_path、秒数、上一次的种子。 */
const MOCK_IMAGE_PNG = 'iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAIAAABt+uBvAAABRElEQVR4nO3ZMUpEURBE0dvDLMHYdbp80c4UXyCILziFaMfFFOfPd15e3x71+TOOx7cSnh+/PjL7x/GljS1IO/1UwrOHdjp8REys84BMbA7tmNic2zGxjYn1e4gotjGxfvkhoticazKxjYllYv3JdyYT25hYJpaJ9Q9vZrzuyINiFMvE+q8X576LzbkmD4obimVieVDMg2LX/ReTYnOuiWIbimViUSyKdd1BsTnXRLENxTKxKBbFuu6g2JxrotiGYplYFItiXXdQbM41UWxDsUwsikWxrjsoNueaKLahWCYWxaJY1x0Um3NNFNtQLBOLYlGs6w6Kzbkmim0ololFsSjWdQfF5lwTxTYUy8SiWBTruoNic66JYhuKZWJRLIp13UGxOddEsQ3FMrEoFsW67qDYnGui2IZimVgUi2Jdd1BszjW9A3WGD6M/xSE4AAAAAElFTkSuQmCC'

const MOCK_VIDEO_CLOUD: VideoCloudConfig = {
  submit_url:
    'https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis',
  key_env: 'DASHSCOPE_API_KEY',
  model: 'wanx2.1-t2v-turbo',
  body: '{"model": {{model}}, "input": {"prompt": {{prompt}}}, "parameters": {"size": "{{width}}*{{height}}", "duration": {{seconds}}, "fps": {{fps}}}}',
  headers: '{"X-DashScope-Async": "enable"}',
  task_path: 'output.task_id',
  poll_url: 'https://dashscope.aliyuncs.com/api/v1/tasks/{{task_id}}',
  video_path: 'output.video_url',
  status_path: 'output.task_status',
  succeeded: 'SUCCEEDED',
  running: 'PENDING,RUNNING',
  failed: 'FAILED,UNKNOWN',
  key_set: false,
}

function mockVideoState(): VideoState {
  return {
    phase: 'off',
    detail: '浏览器预览没有本地视频引擎',
    error: '',
    last_path: '',
    last_seconds: 0,
    plan: {
      size: '256x256', width: 256, height: 256, fps: 8, seconds: 2, frames: 16, steps: 16,
      explicit: false, vram_total_mb: 0, max_frames: 32, capped: false,
      note: '还没检测过本机引擎，按 4 GB 保守处理。',
    },
    choice: { size: '', fps: 0, seconds: 0, steps: 0 },
    options: {
      sizes: ['256x256', '384x384', '512x512'],
      fpss: [8, 12, 16, 24],
      seconds: [2, 3, 4, 8, 16, 32],
      engines: ['auto', 'local', 'cloud'],
      auto_profile: { size: '256x256', fps: 8, seconds: 2, vram_total_mb: 0 },
    },
    engine: 'local',
    cloud: { ...MOCK_VIDEO_CLOUD },
    cloud_defaults: { ...MOCK_VIDEO_CLOUD },
  }
}

const mockImage: ImageState = {
  engine: 'auto',
  engines: [
    { id: 'auto', label: '自动（本地优先）', note: '浏览器预览里没有本机引擎，所以这一档实际会走云端。' },
    { id: 'local', label: '只用本地 ComfyUI', note: '浏览器预览里不可能成 —— 这一格就是给你看拒绝长什么样的。' },
    { id: 'cloud', label: '只用云端 API', note: '预览里按成功处理，一次网都不出。' },
  ],
  sizes: [
    { size: '512x512', note: '最快的一档。作者这台 GTX 1650 实测 22 秒一张。' },
    { size: '768x768', note: '比 512 慢约两倍多，细节多一截。' },
    { size: '1024x1024', note: '4 GB 显存上是勉强的：慢，且和别的引擎抢显存时容易崩。' },
  ],
  size: '512x512',
  steps: 20,
  count: 4,
  counts: [
    { count: 1, note: '最快：512x512 二十步实测 22.4 秒。代价是——模型换个种子就会翻面，一次只给一张，等于把那张错的直接交出去。' },
    { count: 2, note: '这一档没有单独量过：两头是 1 张 22.4 秒、4 张一炉 83.6 秒，2 张按这两头推在 40~45 秒之间。比 1 张稳，比 4 张快。' },
    { count: 4, note: '实测 83.6 秒（每张 20.9 秒，一炉出的比跑四遍省），4 GB 卡上装得下。一次给四张让你挑。' },
  ],
  comfy_url: '',
  comfy_dir: '',
  cloud_base: '',
  cloud_model: '',
  cloud_key_set: false,
  can_start_local: false,
  vendored_engine: false,
  local: null,
  phase: 'off',
  detail: '',
  last_path: '',
  last_seconds: 0,
  tail: [],
  error: '',
}

let mockImageSeed = 4242

function mockImageFail(message: string): ImageAnswer {
  mockImage.phase = 'failed'
  mockImage.error = message
  return {
    ok: false,
    path: '',
    paths: [],
    engine: '',
    prompt: '',
    prompt_input: '',
    prompt_note: '',
    size: '',
    steps: 0,
    seed: 0,
    seconds: 0,
    preview: '',
    error: message,
  }
}

const mockImageAnswer = (prompt: string): ImageAnswer => {
  const size = mockImage.size
  const seconds = Math.round(size === '512x512' ? 22 : size === '768x768' ? 55 : 120)
  mockImageSeed += 1
  mockImage.phase = 'idle'
  mockImage.detail = '画好了（浏览器预览，没有真的引擎）'
  mockImage.error = ''
  mockImage.last_path = `C:\\Users\\你\\生图\\xiaoye_mock_${mockImageSeed}.png`
  mockImage.last_seconds = seconds
  // 一次画几张也照一遍：预览里那张卡本来就该是"四张并排长什么样"。
  const paths = Array.from({ length: mockImage.count }, (_, index) =>
    index === 0
      ? mockImage.last_path
      : `C:\\Users\\你\\生图\\xiaoye_mock_${mockImageSeed}_0000${index + 1}_.png`,
  )
  return {
    ok: true,
    path: mockImage.last_path,
    paths,
    engine: 'cloud',
    prompt,
    prompt_input: prompt,
    prompt_note: '',
    size,
    steps: mockImage.steps,
    seed: mockImageSeed,
    seconds,
    preview: `data:image/png;base64,${MOCK_IMAGE_PNG}`,
    error: '',
  }
}


/** 本轮产出的文件：浏览器预览里没有盘，也没有 Windows 剪贴板，所以这三扇门只能照实说
 *  「这里做不到」，同时把形状摆全 —— 图片那一行给一张真 PNG，文件那一行给一个路径。
 *  和 mockImage 一样，这些常量必须声明在 ``mock`` 那个对象字面量**外面**：写进字面量里
 *  这个文件就不长了（这个坑已经踩过一次）。 */
const MOCK_ARTIFACT_PIC = 'C:\\Users\\你\\Pictures\\小夜生图\\xiaoye_mock_橘猫.png'
const MOCK_ARTIFACT_DOC = 'C:\\Users\\你\\Documents\\小夜导出\\xiaoye_mock_季度汇总.xlsx'

/** 有没有网：预览里探测不了任何东西，所以给一个恒真的读数并在句子里写明是模拟。 */
const mockNetwork: NetworkState = { online: true, detail: '模拟', seconds_ago: 0 }

/** 预览里让「画」和「表」两句各带一件产出，气泡里那两行才有东西可排版。 */
const mockArtifacts = (text: string): string[] => {
  if (/画|图/.test(text)) return [MOCK_ARTIFACT_PIC]
  if (/表|文档|导出/.test(text)) return [MOCK_ARTIFACT_DOC]
  return []
}

/**
 * 她自学在预览里的那一格。
 *
 * 和 ``mockNetwork`` / 上面那三个 artifact 常量一样，**必须声明在 ``mock`` 那个对象字面量
 * 外面**：写进字面量里的话 ``growth_set`` 改的是一份，``growth_state`` 读回来的是另一份，
 * 预览里那颗开关就成了"按了没反应" —— 这个坑 r105 已经踩过一次了。
 */
const mockGrowth: GrowthState = {
  enabled: true,
  today: 0,
  limit_per_day: 20,
  total_seen: 0,
  folder: '_自学',
  has_knowledge: true,
  error: '',
}

/**
 * 扩展市场在浏览器预览里的这一层。
 *
 * 和 ``mockGrowth`` 一样，**必须声明在 ``mock`` 那个对象字面量外面**：写进字面量里，
 * 「按下去改的那一份」和「读回来的那一份」就不是同一个对象，开关看着没反应（这坑踩过）。
 *
 * 预览还得多担一条后端才有的规矩：``extensions_install`` 只认**刚才搜出来的 id**。
 * 所以这里也得有 ``mockExtById`` 那张表 —— 不记这一笔，开发模式下装那一下永远被拒，
 * 人就验不到真流程；记了，「不在搜索结果里」那一句在预览里也照样说得出口（故意拿一个
 * 没搜过的 id 去装，看到的应该就是生产那句拒绝）。
 */
const MOCK_EXT_SOURCE_LABELS: Record<string, string> = {
  curated: '精选（作者逐条验过）',
  npm: 'npm · MCP server',
  github: 'GitHub · 技能仓库',
}

const mockExtCatalogRaw = [
  {
    kind: 'mcp',
    id: 'curated:server-filesystem',
    name: 'filesystem',
    summary: '把指定文件夹读给她看的 MCP server。装之前那条命令行摊开在这张卡上。',
    source: 'curated',
    install: { transport: 'stdio', command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem'] },
    homepage: 'https://github.com/modelcontextprotocol/servers',
    stars: 0,
    downloads: 0,
    needs: ['npx'],
    pinned: '2026-09-30',
    icon: '🗂️',
    description:
      '把指定文件夹读给她看：只在她要用的时候开，白名单外的路径一律拒。装之前那条命令行摊在这张卡上 —— 它是跑在小夜外面的独立进程，用你当前账户的权限。',
    version: '2026-09-30',
  },
  {
    kind: 'skill',
    id: 'curated:pdf-forms',
    name: 'pdf-forms',
    summary: '怎么把一份填好的表单读回来并写回同一路径的说明书。',
    source: 'curated',
    install: { repo: 'example/author-skills', ref: 'a1b2c3d', path: 'skills/pdf-forms' },
    homepage: 'https://github.com/example/author-skills',
    stars: 0,
    downloads: 0,
    needs: [],
    pinned: 'a1b2c3d',
    icon: '📄',
    description: '怎么把一份填好的表单读回来、并把答案写回同一个路径的说明书。只有文本，不会被执行；钉在 commit 上，上游改版不会漂走。',
    version: '1.2.0',
  },
  {
    kind: 'mcp',
    id: 'npm:@modelcontextprotocol/server-filesystem',
    name: '@modelcontextprotocol/server-filesystem',
    summary: 'npm 那一路的同一条：限白名单目录，读写走独立进程。',
    source: 'npm',
    install: { transport: 'stdio', command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem'] },
    homepage: 'https://www.npmjs.com/package/@modelcontextprotocol/server-filesystem',
    stars: 0,
    downloads: 41_208,
    needs: ['npx'],
    pinned: '',
  },
  {
    kind: 'mcp',
    id: 'npm:mcp-server-postgres',
    name: 'mcp-server-postgres',
    summary: '连一个本地数据库。要环境变量，密钥不落进扩展文件。',
    source: 'npm',
    install: { transport: 'stdio', command: 'npx', args: ['-y', 'mcp-server-postgres'] },
    homepage: 'https://www.npmjs.com/package/mcp-server-postgres',
    stars: 0,
    downloads: 3_140,
    needs: ['npx'],
    pinned: '',
  },
  {
    kind: 'skill',
    id: 'github:anthropics/skills:document-skills',
    name: 'anthropics/skills · document-skills',
    summary: '一个仓库里翻出来的技能目录。正文只在被用到时才取进上下文。',
    source: 'github',
    install: { repo: 'anthropics/skills', ref: 'main', path: 'skills/document-skills' },
    homepage: 'https://github.com/anthropics/skills',
    stars: 44_000,
    downloads: 0,
    needs: [],
    pinned: 'main',
    icon: 'https://avatars.githubusercontent.com/u/100976983?s=400',
    description: '一个仓库里翻出来的技能目录（这一路的图标就是 GitHub 给的头像）。正文只在被用到的时候才取进上下文，不常驻。',
    version: '',
  },
  {
    kind: 'mcp',
    id: 'github:py-mcp-weather',
    name: 'py-mcp-weather',
    summary: 'Python 写的那类 stdio 服务，用 uvx 免安装跑 —— 本机没有 uvx 就装不了。',
    source: 'github',
    install: { transport: 'stdio', command: 'uvx', args: ['mcp-server-fetch'] },
    homepage: 'https://github.com/example/py-mcp-weather',
    stars: 210,
    downloads: 0,
    needs: ['uvx'],
    pinned: 'main',
  },
]

/**
 * 预览里也照同一条规矩补默认值：``icon`` / ``description`` / ``version`` 三栏后端**永远给**，
 * 少一栏界面就少画一块，而那块本来该说的话就没人说了。
 */
const mockExtCatalog: ExtensionCatalog[] = mockExtCatalogRaw.map((row) => ({
  icon: '',
  description: row.summary,
  version: '',
  ...row,
}))

/** 后端那张「只认搜出来的 id」的缓存表，预览照抄一份（空表＝什么都没搜过）。 */
const mockExtById = new Map<string, ExtensionCatalog>()

/**
 * 预览里「这一份装的时候记下来历了」的那几个 slug。
 *
 * 预置的那两份故意不在里面 —— 它们演的是「更早那版装的、判断不了更新」那条真实路径，
 * 那句话说错了，用户在真机上就会等一个永远不会来的更新。
 */
const mockExtProvenance = new Set<string>()

/** 预览里也先留一行审计，好让第三格知道自己在说什么（``at`` 是 epoch 秒）。 */
const mockExtSeedLog: ExtensionLogRow[] = [
  { at: Date.now() / 1000 - 4_200, event: '扩展运行已禁止（这一层总开关）' },
]

const mockExtensions: ExtensionState = {
  directory: 'E:\\小夜数据\\扩展',
  servers_file: 'E:\\小夜数据\\扩展\\servers.json',
  layer_enabled: false,
  servers: [
    {
      name: 'filesystem',
      transport: 'stdio',
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-filesystem', 'E:\\文档'],
      url: '',
      enabled: true,
      source: 'curated',
      homepage: 'https://github.com/modelcontextprotocol/servers',
      needs: ['npx'],
      env_from: [],
      note: '预览里预先摆好的一条，好让这一格不是空壳',
    },
  ],
  skills: [
    {
      slug: 'pdf-forms',
      name: 'PDF 表单',
      description: '怎么把一份填好的表单读回来并写回同一路径。',
      path: 'E:\\小夜数据\\扩展\\skills\\pdf-forms',
      body_chars: 4_812,
      version: '1.0.0',
      license: 'MIT',
      homepage: '',
      error: '',
      enabled: true,
    },
    {
      slug: 'broken-one',
      name: '读不了的那一份',
      description: '',
      path: 'E:\\小夜数据\\扩展\\skills\\broken-one',
      body_chars: 0,
      version: '',
      license: '',
      homepage: '',
      error: "SKILL.md 的前言不是一个映射，所以这一份读不过来",
      enabled: false,
    },
  ],
  mcp: {
    // 合同里 ``servers`` 是**条数**（后端 ``McpService.stats()`` 就是数字），逐台的在
    // ``statuses`` 里。预览按真实形状写，不写成一个更好看的数组。
    enabled: false,
    running: false,
    servers: 1,
    connected: 0,
    tools: 0,
    statuses: [{ name: 'filesystem', transport: 'stdio', connected: false, tool_count: 0, error: 'FileNotFoundError: 这台机器上找不到 npx' }],
    errors: { filesystem: 'FileNotFoundError: 这台机器上找不到 npx' },
    error: '',
  },
  // 预览里没有本机：给一个「有 node/npx/python、缺 uvx/deno」的读数，
  // 这样「缺运行时所以那条装不了、并写明为什么」这条真实路径在开发模式也看得见。
  runtime: {
    present: ['node', 'npx', 'python', 'this-runtime'],
    paths: {
      node: 'C:\\Program Files\\nodejs\\node.exe（模拟）',
      npx: 'C:\\Program Files\\nodejs\\npx.cmd（模拟）',
      python: '（模拟）随包解释器',
      'this-runtime': '（模拟）小夜自己这个 exe',
    },
    labels: {
      node: 'Node.js（npm 那类 MCP server 要用）',
      npx: 'npx（随 Node 一起来）',
      python: 'Python（自研的 stdio 服务用当前解释器）',
      uvx: 'uvx（Python 那类 MCP server 的免安装跑法）',
      deno: 'Deno（少数 server 用 deno run）',
    },
    missing: ['uvx', 'deno'],
    error: '',
  },
  search: { query: '', items: [], sources: [], failures: [] },
  store_error: '',
  log: mockExtSeedLog,
  verdicts: {},
  error: '',
}

/** 预览也按同一条规矩算判词：装了没 / 能不能装 / 能不能更新 / 能不能删。 */
function mockVerdicts(): Record<string, ExtensionRowVerdicts> {
  const out: Record<string, ExtensionRowVerdicts> = {}
  for (const [id, item] of mockExtById) {
    const installed = mockInstalledOf(item)
    out[id] = {
      installed,
      install: mockInstallVerdict(item, installed),
      update: mockUpdateVerdict(item, installed),
      remove: mockRemoveVerdict(installed),
    }
  }
  return out
}

function mockExtSnapshot(): ExtensionState {
  return {
    ...mockExtensions,
    verdicts: mockVerdicts(),
    servers: mockExtensions.servers.map((row) => ({ ...row, args: [...row.args], needs: [...row.needs], env_from: [...row.env_from] })),
    skills: mockExtensions.skills.map((row) => ({ ...row })),
    log: mockExtensions.log.map((row) => ({ ...row })),
    mcp: { ...mockExtensions.mcp, errors: { ...mockExtensions.mcp.errors } },
    runtime: {
      ...mockExtensions.runtime,
      present: [...mockExtensions.runtime.present],
      missing: [...mockExtensions.runtime.missing],
      paths: { ...mockExtensions.runtime.paths },
      labels: { ...mockExtensions.runtime.labels },
    },
    search: {
      ...mockExtensions.search,
      items: mockExtensions.search.items.map((row) => ({ ...row, needs: [...row.needs], install: { ...row.install } })),
      sources: mockExtensions.search.sources.map((row) => ({ ...row })),
      failures: [...mockExtensions.search.failures],
    },
  }
}

/** 两行制审计在预览里的下半：结果也要记一行，装挂掉那一次才留得下来。 */
function mockExtNote(text: string): void {
  mockExtensions.log = [{ at: Date.now() / 1000, event: text }, ...mockExtensions.log].slice(0, 12)
}

function mockExtensionsSearch(query: string, kinds: string[]): ExtensionSearch {
  const clean = String(query || '').trim()
  const wanted = kinds.length ? kinds : ['skill', 'mcp']
  const rows = mockExtCatalog.filter((row) => wanted.includes(row.kind))
  // 三路各自交代（同 ``sources.search_all``）：没给关键词时 npm/github 是「这一路没通」，
  // 不是「市场里没有」—— 这两句在屏上必须长得不一样，否则用户去改的那根线是错的。
  const sources: ExtensionSource[] = ['curated', 'npm', 'github'].map((name) => {
    const hits = rows.filter((row) => row.source === name)
    const stalled = name !== 'curated' && !clean
    return {
      source: name,
      label: MOCK_EXT_SOURCE_LABELS[name] ?? name,
      hits: stalled ? 0 : hits.length,
      error: stalled ? 'ValueError: 这一路要给关键词才有结果' : '',
      milliseconds: stalled ? 1 : 140 + name.length * 53,
    }
  })
  const alive = new Set(sources.filter((row) => !row.error).map((row) => row.source))
  const items = rows.filter((row) => alive.has(row.source))
  for (const row of items) mockExtById.set(row.id, row)
  const failures = sources.filter((row) => row.error).map((row) => `${row.label}：${row.error}`)
  mockExtensions.search = { query: clean, items, sources, failures }
  return { query: clean, items, sources, failures, error: '' }
}

function mockExtensionsInstall(itemId: string): ExtensionState {
  const wanted = String(itemId || '').trim()
  const item = mockExtById.get(wanted)
  if (!item) {
    const snapshot = mockExtSnapshot()
    snapshot.error =
      `这条不在刚才的搜索结果里（id=${wanted || '空'}）。请先搜一次再装 —— 安装不接受页面传来的命令行，否则这一页就等于一个命令输入框。`
    return snapshot
  }
  const gaps = item.needs.filter((need) => !mockExtensions.runtime.present.includes(need))
  if (gaps.length) {
    const snapshot = mockExtSnapshot()
    snapshot.error = `这台机器上缺 ${gaps.join('、')}，装上了也起不来（${item.needs.join('、')} 是这一条要的东西）`
    return snapshot
  }
  let result: ExtensionState['install']
  if (item.kind === 'mcp') {
    const name = String(item.install['name'] || item.name).trim() || item.name
    const row: ExtensionServer = {
      name,
      transport: String(item.install['transport'] || 'stdio'),
      command: String(item.install['command'] || ''),
      args: Array.isArray(item.install['args']) ? item.install['args'].map(String) : [],
      url: String(item.install['url'] || ''),
      enabled: true,
      source: item.source,
      homepage: item.homepage,
      needs: [...item.needs],
      env_from: Array.isArray(item.install['env_from']) ? item.install['env_from'].map(String) : [],
      note: item.summary.slice(0, 200),
    }
    mockExtensions.servers = [...mockExtensions.servers.filter((one) => one.name !== name), row]
    mockExtNote(`MCP server 已登记 ${name}（${row.command || row.url}）← ${item.source}`)
    result = {
      ok: true,
      kind: 'mcp',
      name,
      path: mockExtensions.servers_file,
      reason: '',
      detail: '已登记；连接状态在下面那格，要生效点「重连」',
      connect: '预览里不真连：能不能连上要看装完那一次',
    }
  } else {
    const raw = String(item.install['path'] || item.name)
    const slug = raw.replace(/\\/g, '/').split('/').filter(Boolean).pop() || 'skill'
    const place = `${mockExtensions.directory}/skills/${slug}`
    mockExtensions.skills = [
      ...mockExtensions.skills.filter((one) => one.slug !== slug),
      {
        slug,
        name: item.name,
        description: item.summary.slice(0, 200),
        path: place,
        body_chars: 3_040,
        version: '',
        license: '',
        homepage: item.homepage,
        error: '',
        enabled: true,
      },
    ]
    mockExtProvenance.add(slug)
    mockExtNote(`技能已装 ${slug} ← ${item.source}:${item.homepage || item.name}（预览只是记账，盘上没写文件）`)
    result = {
      ok: true,
      kind: 'skill',
      name: item.name,
      path: place,
      reason: '',
      detail: '预览不写盘，真版本会把 SKILL.md 落到上面那个目录',
      connect: '',
    }
  }
  // 改完之后才取快照：先取再改的话，预览里那一次「装上了」会在下一次读状态时被擦回去。
  const snapshot = mockExtSnapshot()
  snapshot.install = result
  snapshot.error = ''
  return snapshot
}

/**
 * 这一条在预览的账本里装了没有。
 *
 * 语义**镜像后端 `_installed_of`**：两边各写一套的话，预览里看着对的那三颗按钮
 * 到了真版本上就是另一回事 —— 而用户只在真版本上点。
 */
function mockInstalledOf(item: ExtensionCatalog): ExtensionInstalled {
  const blank: ExtensionInstalled = {
    yes: false,
    kind: item.kind,
    name: '',
    slug: '',
    path: '',
    at: 0,
    files: [],
    digest: '',
    enabled: false,
    provenance: false,
    changed: false,
    connected: false,
    words: '还没装',
  }
  if (item.kind === 'mcp') {
    const name = String(item.install['name'] || item.name).trim() || item.name
    const found = mockExtensions.servers.find((one) => one.name === name)
    if (!found) return blank
    const live = mockExtensions.mcp.statuses.find((one) => String(one['name'] ?? '') === name)
    const words = (key: string): string[] =>
      Array.isArray(item.install[key]) ? (item.install[key] as unknown[]).map(String) : []
    const changed =
      String(item.install['command'] ?? '') !== found.command ||
      String(item.install['url'] ?? '') !== found.url ||
      words('args').join(' ') !== found.args.join(' ')
    return {
      ...blank,
      yes: true,
      changed,
      name: found.name,
      slug: found.name,
      path: mockExtensions.servers_file,
      enabled: found.enabled,
      provenance: true,
      connected: Boolean(live?.['connected']),
      words: `已登记：${found.name}` + (found.enabled ? '' : '（停用）'),
    }
  }
  const raw = String(item.install['path'] || item.name)
  const slug = raw.replace(/\\/g, '/').split('/').filter(Boolean).pop() || 'skill'
  const found = mockExtensions.skills.find((one) => one.slug === slug)
  if (!found) return blank
  const hasProv = mockExtProvenance.has(slug)
  return {
    ...blank,
    yes: true,
    name: found.slug,
    slug: found.slug,
    path: found.path,
    enabled: found.enabled,
    provenance: hasProv,
    words:
      `已装：${found.slug}` +
      (found.enabled ? '' : '（关着）') +
      (hasProv ? '' : '（没留下来历记录）'),
  }
}

function mockInstallVerdict(item: ExtensionCatalog, installed: ExtensionInstalled): ExtensionVerdict {
  if (installed.yes) return { can: false, words: '这一条已经装了，不用重复装' }
  const gaps = item.needs.filter((need) => !mockExtensions.runtime.present.includes(need))
  if (gaps.length) return { can: false, words: `这台机器上缺 ${gaps.join('、')}，装上了也起不来。` }
  return { can: true, words: '可以装（装之前还会把要跑的那一条摊给你看）' }
}

/** 能不能更新、为什么不能。**这一句必须说实话**，不给一颗点了什么也不会变的按钮。 */
function mockUpdateVerdict(item: ExtensionCatalog, installed: ExtensionInstalled): ExtensionVerdict {
  if (!installed.yes) return { can: false, words: '还没装，谈不上更新' }
  if (item.kind !== 'mcp') {
    if (!installed.provenance)
      return {
        can: false,
        words:
          '这一份没留下来历记录（多半是更早那版装的，那时候还没记）—— 所以判断不了上游有没有出新内容。' +
          '要换新内容：先删掉这一份，再装一次。',
      }
    return { can: true, words: '按盘上那份来历重新取一次，比内容决定要不要动（比的是文件，不是版本号）' }
  }
  const launcher = String(item.install['command'] || '').trim().toLowerCase()
  if (['npx', 'uvx', 'pipx'].includes(launcher))
    return {
      can: false,
      words: `这一条每次起进程都是 \`${launcher} -y\` 现拉最新版，没有「更新」这一步。`,
    }
  return { can: true, words: '按清单上这一条重写一次登记（保留它现在的启停），然后当场连一次' }
}

function mockRemoveVerdict(installed: ExtensionInstalled): ExtensionVerdict {
  if (!installed.yes) return { can: false, words: '这条本来就没装，没什么可删' }
  return { can: true, words: '删之前会再问一次，且只动它自己那一个目录 / 那一条登记' }
}

function mockEmptyDetail(itemId: string, reason: string): ExtensionDetail {
  const blankItem: ExtensionCatalog = {
    kind: '',
    id: itemId,
    name: '',
    summary: '',
    source: '',
    install: {},
    homepage: '',
    stars: 0,
    downloads: 0,
    needs: [],
    pinned: '',
    icon: '',
    description: '',
    version: '',
  }
  const no = { can: false, words: reason }
  return {
    found: false,
    id: itemId,
    item: blankItem,
    installed: { ...mockInstalledOf(blankItem), kind: '', words: '' },
    install: no,
    update: no,
    remove: no,
    runtime: { ...mockExtensions.runtime, present: [...mockExtensions.runtime.present] },
    log: [],
    directory: '',
    servers_file: '',
    error: reason,
  }
}

function mockRecommended(): ExtensionRecommended {
  const rows = mockExtCatalog.filter((row) => row.source === 'curated')
  for (const row of rows) mockExtById.set(row.id, row)
  return {
    items: rows.map((row) => ({ ...row, needs: [...row.needs], install: { ...row.install } })),
    error: '',
    at: Date.now() / 1000,
    where: '作者逐条验过的那份精选清单（预览不联网）',
  }
}

function mockDetail(itemId: string): ExtensionDetail {
  const wanted = String(itemId || '').trim()
  const item = mockExtById.get(wanted)
  if (!item)
    return mockEmptyDetail(
      wanted,
      `这条不在我们给出去的那批里（id=${wanted || '空'}）。请先搜一次，或者从推荐那一格点进来。`,
    )
  const installed = mockInstalledOf(item)
  return {
    found: true,
    id: item.id,
    item: { ...item, needs: [...item.needs], install: { ...item.install } },
    installed,
    install: mockInstallVerdict(item, installed),
    update: mockUpdateVerdict(item, installed),
    remove: mockRemoveVerdict(installed),
    runtime: { ...mockExtensions.runtime, present: [...mockExtensions.runtime.present] },
    log: mockExtensions.log.slice(0, 5).map((row) => ({ ...row })),
    directory: mockExtensions.directory,
    servers_file: mockExtensions.servers_file,
    error: '',
  }
}

function mockUpdate(itemId: string): ExtensionState {
  const wanted = String(itemId || '').trim()
  const item = mockExtById.get(wanted)
  const snapshot = mockExtSnapshot()
  if (!item) {
    snapshot.error = '这条不在我们给出去的那批里，更新不接受页面传来的形状。'
    return snapshot
  }
  const installed = mockInstalledOf(item)
  const verdict = mockUpdateVerdict(item, installed)
  if (!verdict.can) {
    snapshot.error = verdict.words
    return snapshot
  }
  if (item.kind === 'mcp') {
    const name = installed.name
    mockExtensions.servers = mockExtensions.servers.map((one) =>
      one.name === name ? { ...one, note: item.summary.slice(0, 200), needs: [...item.needs] } : one,
    )
    mockExtNote(`更新 MCP 登记 ${name}（保住启停=${installed.enabled}）`)
    snapshot.update = {
      ok: true,
      kind: 'mcp',
      name,
      path: mockExtensions.servers_file,
      reason: '',
      detail: '预览只重写账本；真版本会按清单重写这条登记并当场连一次',
    }
  } else {
    mockExtNote(`技能检查更新 ${installed.slug}：预览不取网络`)
    snapshot.update = {
      ok: true,
      kind: 'skill',
      name: installed.name,
      path: installed.path,
      reason: '',
      detail: '预览不取网络：真版本会重新取一次，比的是取回的全部文件内容，一模一样就不写盘',
    }
  }
  snapshot.error = ''
  return snapshot
}

function mockRemoveItem(itemId: string, confirmed: boolean): ExtensionState {
  const wanted = String(itemId || '').trim()
  const item = mockExtById.get(wanted)
  const snapshot = mockExtSnapshot()
  if (!item) {
    snapshot.error = '这条不在我们给出去的那批里，删除不接受页面传来的形状。'
    return snapshot
  }
  const installed = mockInstalledOf(item)
  if (!installed.yes) {
    snapshot.error = '这条本来就没装，什么都没动。'
    return snapshot
  }
  if (!confirmed) {
    snapshot.error = '删除需要明确的确认（confirmed），这一步不会替你决定'
    return snapshot
  }
  if (item.kind === 'mcp') {
    mockExtensions.servers = mockExtensions.servers.filter((one) => one.name !== installed.name)
    mockExtNote(`从扩展页删掉 mcp ${installed.name}（按条目 id 解析出来的那一个）`)
  } else {
    mockExtensions.skills = mockExtensions.skills.filter((one) => one.slug !== installed.slug)
    mockExtProvenance.delete(installed.slug)
    mockExtNote(`从扩展页删掉 skill ${installed.slug}（按条目 id 解析出来的那一个）`)
  }
  const next = mockExtSnapshot()
  next.removed = {
    ok: true,
    kind: item.kind,
    name: installed.name,
    path: installed.path,
    reason: '',
    detail: item.kind === 'mcp' ? `删掉了这条登记：${installed.name}` : `删掉了这一份：${installed.slug}`,
  }
  return next
}

const mock: PywebviewApi = {
  async snapshot() {
    const jitter = (base: number, spread: number) =>
      Math.max(0, Math.min(100, base + (Math.random() - 0.5) * spread))
    const total = 16 * 1024 ** 3
    const used = (total * jitter(62, 10)) / 100
    return {
      metrics: {
        taken_at: Date.now() / 1000,
        uptime_seconds: 262_300,
        cpu: {
          percent: jitter(34, 26),
          cores: 12,
          per_core: Array.from({ length: 12 }, () => jitter(34, 50)),
        },
        memory: {
          percent: (used / total) * 100,
          total_bytes: total,
          used_bytes: used,
          available_bytes: total - used,
          swap_percent: jitter(12, 8),
        },
        net: {
          sent_bytes: 4_200_000_000,
          recv_bytes: 38_400_000_000,
          send_bps: jitter(200_000, 300_000),
          receive_bps: jitter(2_400_000, 4_000_000),
        },
        disks: [
          { mount: 'C:\\', fstype: 'NTFS', percent: 78, total_bytes: 512 * 1024 ** 3, used_bytes: 399 * 1024 ** 3, free_bytes: 113 * 1024 ** 3 },
          { mount: 'D:\\', fstype: 'NTFS', percent: 41, total_bytes: 1024 * 1024 ** 3, used_bytes: 420 * 1024 ** 3, free_bytes: 604 * 1024 ** 3 },
        ],
        top_processes: [
          { pid: 1, name: 'MockProcessA', cpu_percent: 12.5, memory_bytes: 1.4 * 1024 ** 3 },
          { pid: 2, name: 'MockProcessB', cpu_percent: 4.1, memory_bytes: 0.5 * 1024 ** 3 },
        ],
        warnings: [],
      },
      warnings: [],
      error: '',
      alerts: mockAlerts,
    }
  },
  async alerts() {
    return { active: mockAlerts, history: [], error: '' }
  },
  async alerts_ack(code) {
    const before = mockAlerts.length
    mockAlerts = mockAlerts.filter((row) => row.code !== code)
    return {
      ok: mockAlerts.length < before,
      error: mockAlerts.length < before ? '' : `没有这条告警：${code}`,
    }
  },
  async alerts_ack_all() {
    const count = mockAlerts.length
    mockAlerts = []
    return { ok: true, count, error: '' }
  },
  async app_info() {
    // The preview has to answer the same way the desktop does: whatever name the
    // operator saved is the name the top bar wears.
    return {
      name: mockSettings.display_name || '小夜',
      version: 'dev',
      engine: 'browser-mock',
      built: 'source',
    }
  },
  async disk_scan() {
    const now = Date.now() / 1000
    const exact = (path: string, size_bytes: number, is_directory: boolean): JunkItem => ({
      path,
      size_bytes,
      category: '临时文件',
      is_directory,
      modified_at: now - 3600,
      kind: 'exact',
      member_count: 1,
      sample: [],
      scanned_at: now,
    })
    return {
      groups: [
        {
          category: '临时文件',
          total_bytes: 3.2 * 1024 ** 3,
          items: [
            exact('C:\\mock\\temp\\buildcache', 1024 ** 3, true),
            exact('C:\\mock\\temp\\loose.log', 200 * 1024 ** 2, false),
            {
              // The shape %TEMP% really produces: one row for a thousand leftovers.
              path: 'C:\\Users\\me\\AppData\\Local\\Temp',
              size_bytes: 2 * 1024 ** 3,
              category: '临时文件',
              is_directory: false,
              modified_at: now - 7200,
              kind: 'loose_files',
              member_count: 1646,
              sample: ['tmp8a3f.tmp', 'JET6E21.tmp', 'wct1A2B.tmp', '0013f4ab.log', 'notes.~tmp'],
              scanned_at: now,
            },
          ],
        },
      ],
      total_bytes: 3.2 * 1024 ** 3,
      skipped_protected: 1,
      truncated: false,
      error: '',
    }
  },
  async voice_status() {
    return { ...mockVoice.status() }
  },
  async voice_enable() {
    return mockVoice.enable()
  },
  async voice_mute() {
    return mockVoice.mute()
  },
  async voice_talk() {
    return mockVoice.talk()
  },
  async chat_ask(text, attachments = []) {
    const row = mockProviders.find((entry) => entry.name === mockPicked.provider)
    if (row && needsKey(row)) {
      // 同步那扇门也一样：桌面版此时根本发不出请求，返回空答案才是撒谎。
      mockChat.status = 'failed'
      mockChat.error = `API key environment variable '${row.key_variable}' is not set (provider='${row.name}', variable='${row.key_variable}')`
      mockChat.error_kind = 'auth'
      return {
        question: text,
        answer: '',
        error: mockChat.error,
        reasoning: '',
        artifacts: [],
      }
    }
    const answer = '（浏览器预览模式的样例回答）我是小夜。桌面版里这句话来自真实模型。'
    // The desktop transcript is pushed from Python; the mock has to keep its own,
    // or the panel is empty in preview and nobody can tell that from a bug.
    const made = mockArtifacts(text)
    mockHistory.push(
      { role: 'user', text, artifacts: [] },
      { role: 'assistant', text: answer, artifacts: made },
    )
    return {
      question: text,
      answer: attachments.length ? `${answer}（收到 ${attachments.length} 个附件）` : answer,
      error: '',
      reasoning: '',
      artifacts: made,
    }
  },
  async chat_send(text, attachments = [], conversation = '', provider = '', model = '') {
    // The preview browser has no Python behind it, so the mock answers on a timer: the
    // question lands at once and the answer arrives a beat later, which is the least
    // this can do without pretending a fake can stream.
    const row = mockProviders.find((entry) => entry.name === (provider || mockPicked.provider))
    const keyless = Boolean(row) && needsKey(row!)
    const made = mockArtifacts(text)
    const answer = attachments.length
      ? `（浏览器预览模式的样例回答）我是小夜。带 ${attachments.length} 个附件，桌面版里这句话来自真实模型。`
      : '（浏览器预览模式的样例回答）我是小夜。桌面版里这句话来自真实模型。'
    mockHistory.push({ role: 'user', text, artifacts: [] })
    mockChat.status = 'running'
    mockChat.phase = '思考中'
    mockChat.error = ''
    mockChat.error_kind = ''
    mockChat.note = ''
    window.setTimeout(() => {
      if (keyless && row) {
        // 和桌面版同一句、同一个 kind：钥匙没填，请求在出门前就被自己拦下了。
        mockChat.status = 'failed'
        mockChat.phase = ''
        mockChat.error = `API key environment variable '${row.key_variable}' is not set (provider='${row.name}', variable='${row.key_variable}')`
        mockChat.error_kind = 'auth'
      } else {
        mockHistory.push({
          role: 'assistant',
          text: made.length ? `${answer}（预览样例：这一轮带出一件产出）` : answer,
          model: model || 'mock',
          artifacts: made,
        })
        mockChat.status = 'done'
        mockChat.phase = ''
        // 这一对勾了「不带工具」，所以这一轮的答案确实没有手：真版本在必须扔掉工具表才
        // 答得上来时也是这么记的，不是失败，但不能不说。
        const pair = mockTuning(`${(provider || mockPicked.provider)}\u0000${mockPicked.model}`)
        mockChat.note = pair.tools ? '' : 'no_tools'
      }
      pushMockSnapshot()
    }, 400)
    return {
      ok: true,
      task_id: 'mock',
      conversation: conversation || 'hud',
      question: text,
      answer: '',
      error: '',
    }
  },
  async chat_clear() {
    mockHistory.length = 0
    mockChat.status = 'idle'
    mockChat.phase = ''
    mockChat.error = ''
    mockChat.error_kind = ''
    mockChat.note = ''
    return { ok: true }
  },
  async chat_cancel() {
    return { ok: false, error: '预览模式的样例回答不是一轮真任务，没有可停的东西' }
  },
  async chat_collaborate(text, seats, _rounds = 2, conversation = '', mode = 'table') {
    // One seat's worth of sample text, and no claim that a table ran: the preview has no
    // model behind it, and a mock that fakes three opinions reads as the feature working.
    const answer = `（浏览器预览模式）${MODE_LABELS[mode]} 要真的模型，这里一个也没有。`
    mockHistory.push({ role: 'user', text, artifacts: [] })
    window.setTimeout(() => {
      mockHistory.push({ role: 'assistant', text: answer, record: '', artifacts: [] })
    }, 400)
    return {
      ok: true,
      task_id: 'mock',
      conversation: conversation || 'hud',
      question: text,
      answer: '',
      error: seats.length ? '' : '先挑至少一个模型',
    }
  },
  async llm_test(provider, model) {
    return {
      ok: false,
      detail: '预览模式发不出请求，连不通也测不出看图；桌面版里这两个都是真问一句',
      vision: null,
      latency_ms: 0,
      provider: provider || 'mock',
      model: model || 'mock',
    }
  },
  async chat_tasks() {
    return { tasks: [], error: '预览模式没有任务表' }
  },
  async chat_conversations() {
    return { conversations: [mockCard()], error: '预览模式只有一个样例对话' }
  },
  async chat_open() {
    return { conversation: 'hud', conversations: [] }
  },
  async chat_new() {
    mockHistory.length = 0
    return { conversation: 'hud', conversations: [] }
  },
  async chat_tab_model(_conversationId, provider, model) {
    return { ok: false, error: '预览模式不能分对话选模型', provider, model }
  },
  async state_snapshot() {
    return mockSnapshot()
  },
  async settings_get() {
    return { ...mockSettings }
  },
  async settings_apply(patch) {
    return applyMockSettings(patch)
  },
  async list_endpoint_models(base_url, provider, api_key) {
    // Same verdicts the real shell gives: a dead address says 连不上, a gateway that
    // wants a credential says so, and only a talking endpoint hands back ids.
    const url = String(base_url ?? '').trim()
    const typed = String(api_key ?? '').trim()
    if (!/^https?:\/\//.test(url)) return { ok: false, models: [], error: '地址必须以 http:// 或 https:// 开头' }
    if (url.includes('11434')) {
      return { ok: true, models: ['qwen2.5:7b-instruct', 'qwen2.5vl:7b', 'llava:7b'], error: '' }
    }
    if (url.includes('1234')) return { ok: true, models: ['loaded-model-q4'], error: '' }
    if (url.includes('8080')) return { ok: true, models: ['gguf-model'], error: '' }
    if (typed && url.includes('wkapi')) {
      // 填了钥匙就答 —— 桌面版现在把这一格里的 Key 一起发出去，预览得跟着一样。
      return { ok: true, models: ['gpt-5.6-sol'], error: '' }
    }
    if (provider && mockProviders.some((row) => row.name === provider && row.key_set)) {
      return { ok: true, models: mockProviders.find((row) => row.name === provider)!.models.map((m) => m.id), error: '' }
    }
    if (url.includes('wkapi')) return { ok: false, models: [], error: '这一家要钥匙才肯列模型；先把 Key 填上再拉' }
    return { ok: false, models: [], error: `连不上 ${url}：拒绝连接` }
  },
  async usage_summary(days) {
    // 0 is the whole ledger and is not clamped; anything else keeps the one-month ceiling.
    return mockUsage(days === 0 ? 0 : Math.min(Math.max(days, 1), 31))
  },
  async chat_models() {
    return mockModels()
  },
  async chat_pick(provider, model) {
    const row = mockProviders.find((entry) => entry.name === provider)
    if (!row) return { ...mockModels(), error: `未配置的服务商：${provider}` }
    const wanted = row.models.find((entry) => entry.id === model)
    mockPicked.provider = provider
    mockPicked.model = wanted ? wanted.id : row.default_model
    return mockModels()
  },
  async chat_tuning(thinking, turns, tools) {
    const list = mockModels()
    const current = { thinking: list.thinking, turns: list.turns, tools: list.tools }
    if (thinking !== undefined && !mockThinkingLevels.includes(thinking)) {
      return { ok: false, error: `未知的思考强度：${thinking}`, ...current }
    }
    const bounds = list.turns_bounds
    if (turns !== undefined && (turns < bounds[0] || turns > bounds[1])) {
      return { ok: false, error: `上下文轮数需在 ${bounds[0]}–${bounds[1]} 之间`, ...current }
    }
    const key = `${mockPicked.provider}\u0000${mockPicked.model}`
    const stored = mockTuning(key)
    const next = {
      thinking: thinking === undefined ? stored.thinking : thinking,
      turns: turns === undefined ? stored.turns : turns,
      tools: tools === undefined ? stored.tools : tools,
    }
    mockTunings[key] = next
    return { ok: true, error: '', ...next }
  },
  async llm_add_model(provider, modelId, label) {
    const row = mockProviders.find((entry) => entry.name === provider)
    const wanted = String(modelId ?? '').trim()
    if (!row) return { ok: false, error: `没有这个服务商：${provider}`, models: [], default_model: '' }
    if (!wanted || /\s/.test(wanted)) {
      return { ok: false, error: '模型名不能为空、不能带空格，也不能太长', models: [], default_model: '' }
    }
    if (row.models.some((entry) => entry.id === wanted)) {
      return { ok: false, error: `这个服务商里已经有 ${wanted} 了`, models: [], default_model: '' }
    }
    row.models = [...row.models, { id: wanted, label: String(label ?? '').trim() || wanted }]
    return { ok: true, error: '', models: row.models, default_model: row.default_model }
  },
  async llm_remove_model(provider, modelId) {
    const row = mockProviders.find((entry) => entry.name === provider)
    if (!row) return { ok: false, error: `没有这个服务商：${provider}`, models: [], default_model: '' }
    const kept = row.models.filter((entry) => entry.id !== modelId)
    if (kept.length === row.models.length) {
      return { ok: false, error: `这个服务商里没有 ${modelId}`, models: [], default_model: '' }
    }
    if (!kept.length) {
      return { ok: false, error: '至少要留一个模型', models: row.models, default_model: row.default_model }
    }
    row.models = kept
    if (!kept.some((entry) => entry.id === row.default_model)) row.default_model = kept[0].id
    return { ok: true, error: '', models: kept, default_model: row.default_model }
  },
  async audio_ready(ok, reason) {
    // In a plain browser there is no Python to hand the audio back to, so the honest
    // answer is the page's own verdict, unchanged -- not a fake "speaker" that would
    // make the preview look like a failure it did not have.
    return { output: ok ? 'browser' : 'speaker', reason, bytes_sent: 0 }
  },
  async audio_output() {
    return { output: 'browser', reason: '', bytes_sent: 0 }
  },
  async speech_stop() {
    return { stopped: false }
  },
  async artifact_read(path) {
    // 预览里没有盘可读，所以只认"看起来是图片"的那一个，并照实说这是样例像素。
    // 非图片那一句是给界面排版"门答了 ok:false + 一句原因"这个形状的。
    if (/\.(png|jpe?g|webp)$/i.test(String(path || ''))) {
      return {
        ok: true,
        data_url: `data:image/png;base64,${MOCK_IMAGE_PNG}`,
        error: '',
      }
    }
    return { ok: false, data_url: '', error: '预览里没有文件可读（桌面版只认软件自己产出的那几个目录）' }
  },
  async artifact_stat(path) {
    // 预览这一侧没有盘可 stat。返回 ok:false + 一句原因，界面就退回显示扩展名 ——
    // 不许编一个"12 KB · 刚才"出来，那正是这个项目反复为它付账的那种假。
    if (!String(path || '').trim()) return { ok: false, size: 0, mtime: 0, error: '没有路径' }
    return { ok: false, size: 0, mtime: 0, error: '预览里没有文件可问' }
  },
  async copy_text(text) {
    const body = String(text || '')
    if (!body.trim()) return { ok: false, error: '没有要复制的内容' }
    try {
      // 预览走浏览器自己的剪贴板，成了就说成了；做不到就把异常那句原样交出去，
      // 这一扇门在预览里失败是常态（非安全上下文里 navigator.clipboard 就没有）。
      await navigator.clipboard.writeText(body)
      return { ok: true, error: '' }
    } catch (err) {
      return {
        ok: false,
        error: `浏览器不让页面写剪贴板：${err instanceof Error ? err.message : String(err)}`,
      }
    }
  },
  async copy_image(path) {
    return {
      ok: false,
      error: `预览放不进图片（${String(path || '')} 也没有真的文件）—— 桌面版走 Windows 剪贴板`,
    }
  },
  async open_artifact(path, how) {
    return {
      ok: false,
      error: `预览打不开文件${how === 'folder' ? '，也没有资源管理器可选中它' : ''}：${String(path || '')}`,
    }
  },
  async network_state() {
    return { ...mockNetwork }
  },
  async process_kill(items) {
    // The preview refuses the same three ways the real controller does, so the
    // result strip can be laid out without a machine to practise on.
    return {
      results: items.map((item) => ({
        pid: item.pid,
        name: item.name,
        ok: false,
        note: '浏览器预览模式：不会真的结束任何进程',
      })),
      ended: 0,
      error: '',
    }
  },
  async process_proposals() {
    return { entries: [], error: '浏览器预览模式：小夜不能在这里提议' }
  },
  async process_dismiss() {
    return { ok: false, error: '浏览器预览模式' }
  },
  async disk_delete(items) {
    const files = items.reduce((sum, item) => sum + Math.max(1, item.member_count), 0)
    return {
      deleted: items.map((item) => item.path),
      freed_bytes: items.reduce((sum, item) => sum + item.size_bytes, 0),
      failed: [],
      log_path: `C:\\mock\\deletions.jsonl (${files} files)`,
      error: '',
    }
  },
  async disk_advice() {
    return {
      text:
        '「临时文件」是程序运行留下的中间产物，删掉通常只会让下次运行慢一点；' +
        '「浏览器缓存」删掉后网页要重新下载素材。第二行那个 1.0 GB 的 buildcache ' +
        '名字像是构建系统的缓存，如果它正被一个跑着的任务使用，删它会让那次构建作废——' +
        '建议先确认没有构建在进行。',
      error: '',
      tools_used: ['list_directory'],
    }
  },
  async tts_voices() {
    // Remembered like 语速/音量: a picker that will not show what it just chose teaches
    // nothing about the row that re-reads it after the dialog closes.
    return {
      error: '',
      engine: 'edge_tts',
      current: mockTtsVoice,
      choices: mockVoiceChoices.map((row) => ({ ...row, current: row.id === mockTtsVoice })),
      cloning: {
        available: true,
        reason: '',
        min_ms: MOCK_SAMPLE_MIN_MS,
        comfortable_ms: 15000,
        max_ms: MOCK_SAMPLE_MAX_MS,
        max_voices: 20,
      },
      speed: mockSpeed,
      volume: mockVolume,
      speed_min: 0.5,
      speed_max: 1.5,
      volume_min: 0,
      volume_max: 1,
    }
  },
  async tts_pick(voice: string) {
    mockTtsVoice = voice
    return this.tts_voices()
  },
  async tts_preview() {
    return { ok: true, error: '', voice: 'zh-CN-XiaoxiaoNeural', chunks: 4 }
  },
  async tts_set_style(speed?: number, volume?: number) {
    if (speed !== undefined) mockSpeed = speed
    if (volume !== undefined) mockVolume = volume
    return this.tts_voices()
  },
  async voice_sample_start() {
    mockSample.phase = 'recording'
    mockSample.startedAt = Date.now()
    mockSample.ms = 0
    return this.voice_sample_status()
  },
  async voice_sample_status() {
    if (mockSample.phase === 'recording') {
      mockSample.ms = Math.min(Date.now() - mockSample.startedAt, MOCK_SAMPLE_MAX_MS)
    }
    const rolling = mockSample.phase === 'recording'
    return {
      phase: mockSample.phase,
      ms: mockSample.ms,
      peak: rolling ? 5200 : mockSample.phase === 'captured' ? 6000 : 0,
      silent: false,
      error: '',
      target_ms: MOCK_SAMPLE_TARGET_MS,
      min_ms: MOCK_SAMPLE_MIN_MS,
      max_ms: MOCK_SAMPLE_MAX_MS,
      can_save: mockSample.phase === 'captured' && mockSample.ms >= MOCK_SAMPLE_MIN_MS,
    }
  },
  async voice_sample_stop() {
    if (mockSample.phase !== 'recording') {
      return { ...(await this.voice_sample_status()), ok: false, error: '现在没在录' }
    }
    mockSample.phase = 'captured'
    mockSample.ms = Math.max(mockSample.ms, 8000)
    return { ...((await this.voice_sample_status()) as SampleStatus), ok: true, error: '' }
  },
  async voice_clone_remove(voice_id: string) {
    return this.voice_clone_remove_many([String(voice_id || '')])
  },
  async voice_clone_remove_many(voiceIds: string[]) {
    const clones = () => mockVoiceChoices.filter((row) => row.kind === 'clone')
    const removed: string[] = []
    const failed: string[] = []
    for (const raw of voiceIds || []) {
      const id = String(raw || '')
      const at = mockVoiceChoices.findIndex((row) => row.id === id && row.kind === 'clone')
      if (at < 0) {
        failed.push(id)
        continue
      }
      mockVoiceChoices.splice(at, 1)
      removed.push(id)
    }
    const error = failed.length ? failed.map((id) => id + '：没有这个音色').join('；') : ''
    return { error, voices: clones(), removed }
  },
  async voice_sample_discard() {
    mockSample.phase = 'idle'
    mockSample.ms = 0
    return this.voice_sample_status()
  },
  async voice_sample_save(name: string, prompt_text: string, upload = false) {
    if (mockSample.phase !== 'captured') {
      return { ...(await this.voice_sample_status()), ok: false, error: '先录一段再存' }
    }
    if (!String(prompt_text || '').trim()) {
      return { ...(await this.voice_sample_status()), ok: false, error: '要写下你刚才念的那句话' }
    }
    const id = 'clone:' + Math.random().toString(16).slice(2, 14)
    const label = (String(name || '').trim() || '我的声音') + ' · 我录的'
    mockVoiceChoices.unshift({ id, label, current: false, kind: 'clone', duration_ms: mockSample.ms })
    mockSample.phase = 'idle'
    const ms = mockSample.ms
    mockSample.ms = 0
    return {
      ...((await this.voice_sample_status()) as SampleStatus),
      ok: true,
      error: '',
      ms,
      uploaded: false,
      // The mock has no vendor, so the honest simulation is the failure it would hit on a
      // machine without a key: the recording stays local and the reason comes back.
      cloud_error: upload ? '（模拟）没设 DASHSCOPE_API_KEY，真机上这里回的是厂商那句原因' : '',
      voice: { id, label, current: false, kind: 'clone' },
      voices: mockVoiceChoices.filter((row) => row.kind === 'clone'),
    }
  },
  async chat_sessions() {
    return { error: '', current: mockSessionStore.current, sessions: [...mockSessionStore.rows] }
  },
  async chat_new_session() {
    const id = 'mock-new-' + (mockSessionStore.seq += 1)
    mockSessionStore.rows.unshift({ id, title: '新对话', turns: 0, updated_at: '刚刚' })
    mockSessionStore.current = id
    return { error: '', current: id, sessions: [...mockSessionStore.rows] }
  },
  async chat_switch(sessionId: string) {
    const id = String(sessionId || '')
    if (!mockSessionStore.rows.some((row) => row.id === id)) {
      return { error: '没有这个对话', current: mockSessionStore.current, sessions: [...mockSessionStore.rows] }
    }
    mockSessionStore.current = id
    return { error: '', current: id, sessions: [...mockSessionStore.rows] }
  },
  async chat_rename(sessionId: string, title: string) {
    const row = mockSessionStore.rows.find((item) => item.id === String(sessionId || ''))
    if (!row) return { error: '没有这个对话', current: mockSessionStore.current, sessions: [...mockSessionStore.rows] }
    row.title = String(title || '').trim() || row.title
    return { error: '', current: mockSessionStore.current, sessions: [...mockSessionStore.rows] }
  },
  async chat_delete(sessionId: string) {
    return this.chat_delete_many([String(sessionId || '')])
  },
  async chat_delete_many(sessionIds: string[]) {
    const wanted = (sessionIds || []).map((id) => String(id || '')).filter(Boolean)
    const deleted: string[] = []
    const missing: string[] = []
    for (const id of wanted) {
      const at = mockSessionStore.rows.findIndex((row) => row.id === id)
      if (at < 0) {
        missing.push(id)
        continue
      }
      mockSessionStore.rows.splice(at, 1)
      deleted.push(id)
    }
    if (deleted.includes(mockSessionStore.current)) {
      const id = 'mock-new-' + (mockSessionStore.seq += 1)
      mockSessionStore.rows.unshift({ id, title: '新对话', turns: 0, updated_at: '刚刚' })
      mockSessionStore.current = id
    }
    const base = { error: '', current: mockSessionStore.current, sessions: [...mockSessionStore.rows] }
    if (!wanted.length) return { ...base, error: '没说删哪几个对话' }
    return { ...base, deleted, missing }
  },
  async chat_messages() {
    return { error: '', current: 'mock', messages: [] }
  },
  async computer_levels() {
    return {
      error: '',
      current: {
        tier: 1,
        label: '演练',
        describe: '模型能规划动作并看到结果，但指针和键盘一下都不动。',
        enabled: true,
        dry_run: true,
        allow_mouse: false,
        allow_keyboard: false,
        allow_typing: false,
      },
      levels: [
        { tier: 0, label: '关闭', describe: '工具一条都不注册。', current: false },
        { tier: 1, label: '演练', describe: '只报告，不动。', current: true },
        { tier: 2, label: '键盘', describe: '真的按键；指针不动。', current: false },
        { tier: 3, label: '键鼠全开', describe: '指针和键盘都真的动。', current: false },
      ],
    }
  },
  async computer_set_tier(tier: number) {
    const list = await this.computer_levels()
    return {
      ...list,
      current: { ...list.current, tier },
      levels: list.levels.map((l) => ({ ...l, current: l.tier === tier })),
    }
  },
  async computer_set_typing(allowed: boolean) {
    const list = await this.computer_levels()
    return { ...list, current: { ...list.current, allow_typing: allowed } }
  },
  async skin_apply(skin: string) {
    return { ok: true, skin, pet: false, error: '' }
  },
  async pet_palette() {
    return { ok: false, pet: false, error: '浏览器预览模式没有宠物窗口' }
  },
  async window_state() {
    return { error: '', maximized: false }
  },
  async shell_state() {
    return { visible: true, tray: false, status: 'off', pet: false }
  },
  async window_hide() {
    return { visible: true, tray: false, status: 'off', pet: false, error: '浏览器预览没有托盘' }
  },
  async reminders() {
    return { rows: reminderMock, channels: ['voice', 'tray'], error: '' }
  },
  async reminder_add(text: string, when: string) {
    if (!text.trim()) return { ok: false, row: null, error: '提醒内容不能为空' }
    if (!/后|点|明天|今天|后天|\d{4}/.test(when)) {
      return { ok: false, row: null, error: '听不懂这个时间。试试「十分钟后」或「明天 9 点半」' }
    }
    const row: ReminderRow = {
      job_id: 'reminder:preview' + (reminderMock.length + 1),
      text,
      when: when + '（预览不计时）',
      enabled: true,
      next_run: '',
      created_at: '',
    }
    reminderMock = [...reminderMock, row]
    return { ok: true, row, error: '' }
  },
  async reminder_cancel(key: string) {
    const hit = reminderMock.find((row) => row.job_id === key || row.text.includes(key))
    if (!hit) return { ok: false, removed: '', error: '没找到那条提醒' }
    reminderMock = reminderMock.filter((row) => row !== hit)
    return { ok: true, removed: hit.text, error: '' }
  },
  async reminder_toggle(jobId: string, enabled: boolean) {
    reminderMock = reminderMock.map((row) => (row.job_id === jobId ? { ...row, enabled } : row))
    return { ok: true, error: '' }
  },
  async automation_overview() {
    return {
      scheduler: {
        name: 'scheduler',
        running: true,
        jobs: automationJobsMock.length,
        enabled_jobs: automationJobsMock.filter((row) => row.enabled).length,
        runs: 12,
        failures: 1,
      },
      workflow: { name: 'workflow', running: true, definitions: 1, scheduled: 1, runs: 3, failures: 0 },
      planner: { name: 'planner', running: true },
      planner_enabled: true,
      error: '',
    }
  },
  async scheduled_jobs() {
    return { rows: automationJobsMock, error: '' }
  },
  async job_toggle(jobId: string, enabled: boolean) {
    automationJobsMock = automationJobsMock.map((row) =>
      row.job_id === jobId ? { ...row, enabled } : row,
    )
    return { ok: true, error: '' }
  },
  async job_run(jobId: string) {
    if (!automationJobsMock.some((row) => row.job_id === jobId)) {
      return { ok: false, run: null, error: '没有这个任务' }
    }
    return {
      ok: true,
      run: { ok: true, detail: '（预览里的试跑不会真的执行）', error: '', started_at: '', finished_at: '' },
      error: '',
    }
  },
  async job_remove(jobId: string) {
    const before = automationJobsMock.length
    automationJobsMock = automationJobsMock.filter((row) => row.job_id !== jobId)
    return { ok: true, removed: automationJobsMock.length < before, error: '' }
  },
  async workflow_definitions() {
    return { rows: workflowMock, runs: [], error: '' }
  },
  async workflow_run(name: string) {
    return {
      ok: true,
      run: {
        run_id: 1,
        workflow: name,
        started_at: '',
        finished_at: '',
        ok: true,
        steps: [],
        error: '（预览里没有真的跑）',
      },
      error: '',
    }
  },
  async workflow_reload() {
    return { ok: true, count: workflowMock.length, error: '' }
  },
  async plan_goal(goal: string) {
    return {
      ok: true,
      plan: {
        goal,
        status: 'draft',
        rationale: '（浏览器预览里的计划是示例，不会调用模型）',
        revision: 1,
        steps: [
          { step_id: 's1', title: '先看一眼磁盘', action: 'disk_scan', status: 'pending', notes: '', depends_on: [] },
          { step_id: 's2', title: '确认之后再清理', action: 'disk_delete', status: 'pending', notes: '', depends_on: ['s1'] },
        ],
      },
      error: '',
    }
  },
  async memory_list() {
    return {
      rows: [
        { memory_id: 1, kind: 'fact', scope: 'user', content: '（浏览器预览里的记忆是示例文本）', source: 'demo' },
      ],
      error: '',
    }
  },
  async memory_forget() {
    return { ok: true, error: '' }
  },
  async memory_forget_all() {
    return { removed: 0, error: '浏览器预览没有真记忆库' }
  },
  async knowledge_state() {
    return { documents: [], stats: { records: 0 }, error: '' }
  },
  async knowledge_ingest() {
    return { ok: false, error: '浏览器预览不入库' }
  },
  async knowledge_forget() {
    return { ok: false, error: '浏览器预览不入库' }
  },
  async knowledge_probe() {
    return { hits: [], error: '' }
  },
  async growth_state() {
    return { ...mockGrowth }
  },
  async growth_set(on) {
    // 改的是字面量外面那一份，所以下一次 growth_state 读得到 —— 回读，不原样吐回参数。
    mockGrowth.enabled = Boolean(on)
    mockGrowth.error = ''
    return { ...mockGrowth }
  },
  async growth_forget() {
    // 预览里没有盘：数字归零是真的，"删掉了文件"这一句不能编，所以老实写进 error 里，
    // 让界面把它画出来而不是咽进 console。
    const removed = mockGrowth.total_seen
    mockGrowth.today = 0
    mockGrowth.total_seen = 0
    return { ok: true, removed, error: '预览只是把数字归零，没有真的文件可删' }
  },
  // ---------------------------------------------------------------- 扩展市场（插件页）
  async extensions_state() {
    return mockExtSnapshot()
  },
  async extensions_search(query, kinds) {
    return mockExtensionsSearch(query, kinds)
  },
  async extensions_install(item_id) {
    return mockExtensionsInstall(item_id)
  },
  async extensions_recommended() {
    return mockRecommended()
  },
  async extensions_detail(item_id) {
    return mockDetail(item_id)
  },
  async extensions_update(item_id) {
    return mockUpdate(item_id)
  },
  async extensions_remove_item(item_id, confirmed) {
    return mockRemoveItem(item_id, confirmed)
  },
  async extensions_add_http(name, url) {
    const clean = String(name || '').trim()
    const link = String(url || '').trim()
    const snapshot = mockExtSnapshot()
    if (!clean || !/^https?:\/\//i.test(link)) {
      snapshot.error = '名字要给一个，地址必须是 http 或 https 开头的完整一条。'
      return snapshot
    }
    // 预览不校验内网/回环（那是 Python 里 ``validate_url`` 那道闸在做），但同名覆盖这条是真的。
    mockExtensions.servers = [
      ...mockExtensions.servers.filter((one) => one.name !== clean),
      {
        name: clean,
        transport: 'http',
        command: '',
        args: [],
        url: link,
        enabled: true,
        source: 'manual',
        homepage: '',
        needs: [],
        env_from: [],
        note: '页面上手工登记的一条 http 服务',
      },
    ]
    mockExtNote(`手工登记 http MCP：${clean} → ${link}`)
    return mockExtSnapshot()
  },
  async extensions_layer_set(on) {
    mockExtensions.layer_enabled = Boolean(on)
    mockExtNote(`扩展运行${on ? '已允许' : '已禁止'}（这一层总开关）`)
    return mockExtSnapshot()
  },
  async extensions_server_set(name, on) {
    const wanted = String(name || '').trim()
    if (!mockExtensions.servers.some((one) => one.name === wanted)) {
      const snapshot = mockExtSnapshot()
      snapshot.error = `没有这条 MCP server：${wanted}`
      return snapshot
    }
    mockExtensions.servers = mockExtensions.servers.map((one) =>
      one.name === wanted ? { ...one, enabled: Boolean(on) } : one,
    )
    mockExtNote(`${on ? '启用' : '停用'} MCP server ${wanted}`)
    return mockExtSnapshot()
  },
  async extensions_server_remove(name, confirmed) {
    if (confirmed !== true) {
      const snapshot = mockExtSnapshot()
      snapshot.error = '删除 MCP server 需要明确的确认（confirmed）'
      return snapshot
    }
    const wanted = String(name || '').trim()
    if (!mockExtensions.servers.some((one) => one.name === wanted)) {
      const snapshot = mockExtSnapshot()
      snapshot.error = `没有这条 MCP server：${wanted}`
      return snapshot
    }
    mockExtensions.servers = mockExtensions.servers.filter((one) => one.name !== wanted)
    mockExtNote(`删掉 MCP server 登记 ${wanted}（配置记录，不动盘上任何别的东西）`)
    return mockExtSnapshot()
  },
  async extensions_reconnect() {
    // 预览里没有一个真进程可连：所以「重连完了」这句不能编，只把 errors 原样留着让人看见。
    const snapshot = mockExtSnapshot()
    snapshot.error = '预览里没有真 MCP 进程，重连这一下什么都没连'
    return snapshot
  },
  async skill_set(slug, on) {
    const wanted = String(slug || '').trim()
    if (!mockExtensions.skills.some((one) => one.slug === wanted)) {
      const snapshot = mockExtSnapshot()
      snapshot.error = `没有这份技能：${wanted}`
      return snapshot
    }
    mockExtensions.skills = mockExtensions.skills.map((one) =>
      one.slug === wanted ? { ...one, enabled: Boolean(on) } : one,
    )
    mockExtNote(`${on ? '启用' : '停用'}技能 ${wanted}`)
    return mockExtSnapshot()
  },
  async skill_remove(slug, confirmed) {
    if (confirmed !== true) {
      const snapshot = mockExtSnapshot()
      snapshot.error = '删除技能需要明确的确认（confirmed）'
      return snapshot
    }
    const wanted = String(slug || '').trim()
    const row = mockExtensions.skills.find((one) => one.slug === wanted)
    if (!row) {
      const snapshot = mockExtSnapshot()
      snapshot.error = `没有这份技能：${wanted}`
      return snapshot
    }
    mockExtensions.skills = mockExtensions.skills.filter((one) => one.slug !== wanted)
    // 真后端删的是盘上那个目录；预览没有盘，这一句就必须留在纸上而不是咽下去。
    mockExtNote(`技能已删 ${wanted}（预览只从清单里去掉，${row.path} 没动）`)
    return mockExtSnapshot()
  },
  async pet_state() {
    return { shown: petShownMock }
  },
  async pet_toggle() {
    petShownMock = !petShownMock
    return { shown: petShownMock }
  },
  async pet_set_scale(scale: string) {
    return { shown: petShownMock, scale, error: '' }
  },
  async pet_set_idle(on: boolean) {
    return { shown: petShownMock, idle_motion: on, error: '' }
  },
  async pet_grip() {
    return { ok: false }
  },
  async pet_drag() {
    return { ok: false }
  },
  async pet_frame() {
    return { ok: false }
  },
  async pet_arrived() {
    return { ok: false }
  },
  async pet_assets() {
    return {
      ok: true,
      pets: mockPets,
      models: mockGallery,
      current: mockCurrentPet,
      can_generate: true,
      reason: '',
      error: '',
    }
  },
  async pet_generate(photo: string, name?: string, style?: string) {
    const base = (name || '照片').slice(0, 16)
    if (!photo) {
      return {
        ok: false,
        pets: mockPets,
        current: mockCurrentPet,
        made: [],
        cost_ms: 0,
        segment_ms: 0,
        error: '没收到照片',
      }
    }
    if (mockPets.length + mockCandidates.length > 12) {
      return {
        ok: false,
        pets: mockPets,
        current: mockCurrentPet,
        made: [],
        cost_ms: 0,
        segment_ms: 0,
        error: '最多存 12 个，先删一个再生成',
      }
    }
    const wanted = ['outline', 'photo', 'cartoon'].includes(style || '') ? (style as string) : 'outline'
    const made =
      wanted === 'cartoon'
        ? mockTemplates.map(([label, kind], index) =>
            mockFigure(base + '·' + label, 30 + index, 0.22, 'cartoon', kind),
          )
        : mockCandidates.map(([, depth, lines], index) =>
            mockFigure(base + '·' + mockLabels[index], lines, depth, wanted),
          )
    mockPets = [...mockPets, ...made]
    return {
      ok: true,
      pets: mockPets,
      current: mockCurrentPet,
      can_generate: true,
      reason: '',
      made: made.map((row) => row.id),
      cost_ms: 742,
      segment_ms: 698,
      error: '',
    }
  },
  async pet_pick(petId: string) {
    if (petId && !mockPets.some((row) => row.id === petId)) {
      return {
        ok: false,
        pets: mockPets,
        current: mockCurrentPet,
        can_generate: true,
        reason: '',
        error: '这只宠物已经不在了',
      }
    }
    mockCurrentPet = petId
    return { ok: true, pets: mockPets, current: mockCurrentPet, can_generate: true, reason: '', error: '' }
  },
  async pet_delete(petId: string) {
    if (!mockPets.some((row) => row.id === petId)) {
      return {
        ok: false,
        pets: mockPets,
        current: mockCurrentPet,
        can_generate: true,
        reason: '',
        error: '没有这只宠物，或者已经删掉了',
      }
    }
    mockPets = mockPets.filter((row) => row.id !== petId)
    if (mockCurrentPet === petId) mockCurrentPet = ''
    return { ok: true, pets: mockPets, current: mockCurrentPet, can_generate: true, reason: '', error: '' }
  },
  async model_server_status() {
    return mockModelServer
  },
  async model_server_start() {
    mockModelServer = { ...mockModelServer, phase: 'running', detail: '', port: 28181, load_ms: 12000 }
    return mockModelServer
  },
  async model_server_stop() {
    mockModelServer = { ...mockModelServer, phase: 'off', detail: '界面上停掉了', port: 0 }
    return mockModelServer
  },
  async local_model_check(model: string, context: number) {
    // 和后端 verdict_for 同一套判据：文件比总内存大就是硬拦，「照样加载」也越不过。
    const row = mockBundled.find((item) => item.file === model)
    const fileMb = row?.size_mb ?? 0
    const need = row ? Math.round((row.gate_mb / 4096) * context) : 0
    const total = Math.round(Math.max(mockModelServer.free_mb, 16384) * 1.6)
    const hard = fileMb > 0 && fileMb > total
    return {
      ok: !hard,
      reason: hard
        ? `这个模型文件有 ${(fileMb / 1024).toFixed(1)} GB，比这台机器的总内存 ${(total / 1024).toFixed(0)} GB 还大`
        : '',
      free_mb: mockModelServer.free_mb,
      total_mb: total,
      need_mb: need,
      file_mb: fileMb,
      vram_mb: 4096,
      gpu: 'Mock GPU',
      disk_free_mb: 200000,
      model_label: row?.label ?? '这个模型',
      hard,
    }
  },

  async local_advice() {
    // 和后端规则引擎同一套算法：空闲除以 1.25 当预算，从大到小挑装得下的那个。
    const budget = Math.floor(Math.min(mockModelServer.free_mb, 16384 * 0.6) / 1.25)
    const fit = mockBundled.filter((row) => row.present).find((row) => row.gate_mb <= budget)
    const canAct = fit !== undefined && fit.tiers.some((tier) => tier.needs_free_mb <= budget && tier.context >= 8192)
    return {
      model: fit ? fit.file : 'none',
      context: canAct ? 8192 : 4096,
      idle_minutes: budget - (fit?.gate_mb ?? 0) >= 4096 ? 30 : 10,
      eager: false,
      reason: fit
        ? `空闲 ${Math.round(mockModelServer.free_mb / 1024)} GB，留了余量之后 ${fit.label} 装得下（要约 ${fit.gate_mb} MB）`
        : `空闲只有 ${Math.round(mockModelServer.free_mb / 1024)} GB，最小的都放不下，这一台建议直接用云端模型`,
      source: 'rule',
      note: 'mock：这条是本地规则算的',
    }
  },
  async local_advice_apply(advice) {
    const known = mockBundled.find((row) => row.file === advice.model)
    if (advice.model && advice.model !== 'none' && !known?.present) {
      return { error: `「${advice.model}」的文件不在模型目录里，选不了`, snapshot: mockSettings }
    }
    if (!known && advice.model !== 'none') {
      return { error: '这个离线模型这台机器上没有', snapshot: mockSettings }
    }
    Object.assign(mockSettings, {
      local_model: advice.model,
      local_model_label: known?.label ?? '不用离线模型',
      local_context: advice.context,
      local_idle_minutes: advice.idle_minutes,
      local_eager: advice.eager,
      local_needs_free_mb: known?.gate_mb ?? 0,
    })
    mockModelServer = {
      ...mockModelServer,
      model_selected: advice.model,
      model: advice.model === 'none' ? '' : advice.model,
      model_label: known?.label ?? '',
      needed_mb: known?.gate_mb ?? 0,
      phase: 'off',
      port: 0,
    }
    return { error: '', snapshot: mockSettings }
  },
  async pet_model() {
    const builtin = mockGallery.find((row) => row.id === mockCurrentPet)
    if (builtin) {
      return {
        kind: 'builtin',
        model: builtin,
        id: builtin.id,
        name: builtin.name,
        depth: 0,
        style: '',
        colour: '',
        template: '',
        aspect: 1,
        texture: '',
        points: [],
      }
    }
    const chosen = mockPets.find((row) => row.id === mockCurrentPet)
    if (!chosen) {
      return {
        kind: '',
        id: '',
        name: '',
        depth: 0,
        style: '',
        colour: '',
        template: '',
        aspect: 1,
        texture: '',
        points: [],
      }
    }
    const points: [number, number][] = []
    for (let index = 0; index < chosen.lines; index += 1) {
      const angle = (index / chosen.lines) * Math.PI * 2
      points.push([Math.cos(angle) * 0.45, Math.sin(angle) * 0.5 + 0.5])
    }
    return {
      kind: 'silhouette',
      id: chosen.id,
      name: chosen.name,
      depth: chosen.depth,
      style: chosen.style,
      colour: chosen.colour,
      template: chosen.template,
      aspect: chosen.aspect,
      texture: chosen.has_texture ? chosen.preview : '',
      points,
    }
  },
  async window_toggle_max() {
    maximizedMock = !maximizedMock
    return { error: '', maximized: maximizedMock }
  },
  async shell_levels() {
    return mockShell()
  },
  async shell_set_tier(tier: number) {
    shellTierMock = tier
    return mockShell()
  },
  async mobile_state() {
    return mockMobile()
  },
  async mobile_toggle() {
    return mockMobile()
  },
  async mobile_pair_code() {
    return { code: '', error: '浏览器预览里没有能配对的端口' }
  },
  async mobile_revoke() {
    return { revoked: false, devices: [] }
  },
  async mobile_revoke_all() {
    return { revoked: 0, devices: [] }
  },
  async mobile_selftest() {
    return { plaintext_refused: true, note: '浏览器预览里没有端口可查' }
  },
  async mirror_state() {
    return { ...mockMirror }
  },

  async image_state() {
    return { ...mockImage }
  },
  async video_state() {
    return mockVideoState()
  },
  async video_tune() {
    return mockVideoState()
  },
  async video_probe() {
    return mockVideoState()
  },
  async video_set_engine(engine) {
    return { ...mockVideoState(), engine }
  },
  async video_cloud_save() {
    return mockVideoState()
  },
  async video_cloud_reset() {
    return mockVideoState()
  },
  async video_generate(prompt) {
    return { ok: false, path: '', error: '浏览器预览没有本地视频引擎',
      prompt_input: prompt, prompt: '', prompt_note: '', seconds: 0, duration: 0 }
  },
  async artifact_video() {
    return { ok: false, data_url: '', error: '浏览器预览无法读取本地视频' }
  },
  async image_probe() {
    // 浏览器里没有 8188，探测要照实说没找到 —— 面板上那句拒绝就是给用户看这个形状的。
    mockImage.local = {
      kind: 'comfyui',
      url: 'http://127.0.0.1:8188',
      ok: false,
      detail: '浏览器预览里没有本机引擎（真的软件会去问 127.0.0.1:8188）',
      version: '',
      checkpoints: [],
      probed_at: 0,
      vram_total_mb: 0,
      vram_free_mb: 0,
    }
    mockImage.error = ''
    return { ...mockImage }
  },
  async image_generate(prompt, size) {
    const wanted = String(size || '').trim()
    if (wanted) mockImage.size = wanted
    if (mockImage.engine === 'local') {
      return mockImageFail('这一档只用本机 ComfyUI，而它没在跑。要么把引擎切到「自动」，要么把 ComfyUI 开着。')
    }
    if (!String(prompt || '').trim()) return mockImageFail('要一句画什么的描述，不能空着。')
    await new Promise((resolve) => setTimeout(resolve, 400))
    return mockImageAnswer(String(prompt))
  },
  async image_tune(engine, size, steps, count) {
    mockImage.engine = String(engine || mockImage.engine)
    mockImage.size = String(size || mockImage.size)
    mockImage.steps = Number(steps) || mockImage.steps
    const wanted = Number(count)
    if (mockImage.counts.some((row) => row.count === wanted)) mockImage.count = wanted
    return { ...mockImage }
  },
  async image_setup(comfyUrl, comfyDir, cloudBase, cloudModel) {
    mockImage.comfy_url = String(comfyUrl || '')
    mockImage.comfy_dir = String(comfyDir || '')
    mockImage.cloud_base = String(cloudBase || '')
    mockImage.cloud_model = String(cloudModel || '')
    mockImage.cloud_key_set = !!mockImage.cloud_base
    return { ...mockImage }
  },
  async mirror_devices() {
    return {
      devices: [
        {
          serial: 'MOCK-PHONE-1',
          state: 'device',
          label: 'Mock Phone',
          model: 'Mock Phone',
          wireless: false,
          ready: true,
          hint: '',
          channels: ['MOCK-PHONE-1'],
        },
      ],
      error: '',
    }
  },
  async mirror_start(serial) {
    return mirrorStart(String(serial || ''))
  },
  async mirror_stop() {
    return mirrorStop()
  },
  async mirror_connect(address) {
    mockMirror.detail = `已连上 ${address}（浏览器预览，没有真的 adb）`
    return { ...mockMirror }
  },
  async mirror_pair(address, code) {
    mockMirror.detail = `配对 ${address} / ${code}（浏览器预览，没有真的 adb）`
    return { ...mockMirror }
  },
  async mirror_display(window_mode, stretch) {
    if (MIRROR_WINDOWS.some((row) => row.id === window_mode)) mockMirror.window = window_mode
    mockMirror.stretch = !!stretch
    if (window_mode === 'fullscreen' && mockMirror.phase === 'running') {
      mockMirror.error = '全屏要重开一次投屏（或者现在这个窗口里按 F11）'
    } else {
      mockMirror.error = ''
    }
    return { ...mockMirror }
  },
  async mirror_screen(question) {
    const allowVision = mockScreenVision
    const lines: string[] = []
    if (mockMirror.phase !== 'running') {
      return {
        text: ['这一屏我没读出来：没在投屏，adb 里也没剩下唯一那台。', '来源：读屏幕：没连着能读的手机'].join('\n'),
        error: '没在投屏，不知道读哪台',
        sources: ['读屏幕：没连着能读的手机'],
        allow_vision: allowVision,
      }
    }
    lines.push('手机上现在是：com.android.settings/.SubSettings（浏览器预览，手机自己没报，这是假的）')
    lines.push('屏幕上的文字（手机自己报的，按出现顺序）：')
    lines.push('关于平板｜型号 V2309A｜Android 版本 15')
    if (allowVision) lines.push('看图模型说的：这是一台手机的“关于本机”页（浏览器预览，没有真的发出去）')
    else lines.push('截图：没拿（没勾「允许把这块屏发给能看图的模型」）')
    if (question) lines.push(`你问的是：${question}`)
    lines.push('来源：手机自报 App；无障碍树：读到 3 条文字')
    return { text: lines.join('\n'), error: '', sources: ['手机自报 App', '无障碍树：读到 3 条文字'], allow_vision: allowVision }
  },
  async mirror_screen_vision(allow) {
    // 与后端同形：这扇门不重新读屏，只回这一格现在的样子。
    if (typeof allow === 'boolean') mockScreenVision = allow
    return {
      text: mockScreenVision ? '能看图的那一档可以收这张屏了' : '不发给模型了：只用手机自己报上来的字和本地 OCR',
      error: '',
      sources: [],
      allow_vision: mockScreenVision,
    }
  },
  async mirror_key(key) {
    // mock 也要按真后端的规矩记账：没在投屏就拒，并且说清是"不知道发给哪台"，不是"按了没反应"。
    if (mockMirror.phase !== 'running') {
      mockMirror.error = '现在没在投屏，不知道这一下要发给哪台手机；先点「开始投屏」'
      return { ...mockMirror }
    }
    const row = MIRROR_KEYS.find((item) => item.id === key)
    if (!row) {
      mockMirror.error = `没有「${key}」这个按键`
      return { ...mockMirror }
    }
    mockMirror.error = ''
    mockMirror.detail = `已发给 ${mockMirror.serial}：${row.label}（浏览器预览，没有真的 adb）`
    return { ...mockMirror }
  },
  async mirror_swipe(direction) {
    if (mockMirror.phase !== 'running') {
      mockMirror.error = '现在没在投屏，不知道这一下要发给哪台手机；先点「开始投屏」'
      return { ...mockMirror }
    }
    const row = MIRROR_SWIPES.find((item) => item.id === direction)
    if (!row) {
      mockMirror.error = `没有「${direction}」这个滑动方向`
      return { ...mockMirror }
    }
    mockMirror.error = ''
    mockMirror.detail = `已发给 ${mockMirror.serial}：${row.label}（浏览器预览，没有真的 adb）`
    return { ...mockMirror }
  },
  async mirror_discover() {
    mockMirror.phase = 'off'
    mockMirror.serial = ''
    mockMirror.detail = '扫了本网段的 5555 端口，连上 1 台：192.168.1.23:5555（浏览器预览，没有真的网络）'
    return { ...mockMirror }
  },
  async mirror_tune(tier, audio, keep_screen, mode) {
    return mirrorTune(String(tier || ''), !!audio, !!keep_screen, String(mode || ''))
  },
  async mirror_tcpip(serial) {
    const target = String(serial || 'MOCK-PHONE-1')
    mockMirror.phase = 'off'
    mockMirror.serial = ''
    mockMirror.tail = []
    mockMirror.detail = `已让 ${target} 改听 5555 端口，并连上 192.168.1.23:5555（浏览器预览，没有真的 adb）`
    return { ...mockMirror }
  },
  async activity_log() {
    const now = Date.now() / 1000
    return {
      error: '',
      entries: [
        {
          at: now - 12,
          tool: 'mouse_click',
          ok: false,
          milliseconds: 1,
          detail: '被安全策略拒绝：computer.allow_mouse 为 false',
        },
        {
          at: now - 40,
          tool: 'system_report',
          ok: true,
          milliseconds: 240,
          detail: 'CPU 12% · 内存 63% · 3 个分区',
        },
      ],
    }
  },
}

/** The preview's model list, one of the entries deliberately missing its key, so
 *  the picker's "不可用" state is something the browser pass can actually show. */
const mockThinkingLevels = ['off', 'low', 'medium', 'high']

/** Preview-only memory for the two per-model knobs, keyed the way Python keys it. */
const mockTunings: Record<string, { thinking: string; turns: number; tools: boolean }> = {}

/** Preview-only selection, so a pick in the browser pass sticks across panels. */
/** What the preview pretends is picked. 本机那个免 Key 的行能答，两家没填 Key 的会拒 ——
 *  这两条路都要能在预览里点出来，否则「没答上时屏上写什么」只能靠桌面版撞运气。 */
const mockPicked = { provider: 'ollama', model: 'qwen2.5:7b-instruct' }

/** The preview's providers. Mutated by the add/remove mocks so the panel can be
 *  exercised with a real edit rather than only read. */
const mockProviders: ProviderChoice[] = [
  {
    name: 'qwenai',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    models: [
      { id: 'qwen3.8-flash', label: 'Qwen3.8 Flash' },
      { id: 'qwen3.8-max', label: 'Qwen3.8 Max' },
    ],
    default_model: 'qwen3.8-flash',
    key_set: false,
    key_variable: 'QWENAI_API_KEY',
    current: true,
  },
  {
    name: 'wkapi',
    base_url: 'https://wkapi.example.com/v1',
    models: [{ id: 'gpt-5.6-sol', label: 'GPT-5.6 Sol' }],
    default_model: 'gpt-5.6-sol',
    key_set: false,
    key_variable: 'WKAPI_API_KEY',
    current: false,
  },
  {
    name: 'ollama',
    base_url: 'http://localhost:11434/v1',
    models: [{ id: 'qwen2.5:7b-instruct', label: '本机 Qwen' }],
    default_model: 'qwen2.5:7b-instruct',
    key_set: false,
    key_variable: 'OLLAMA_API_KEY',
    key_optional: true,
    current: false,
  },
]

/**
 * What the preview's one conversation is doing, kept like the desktop keeps it.
 *
 * The card is the page's only window onto a turn, so a mock that always said ``idle``
 * could not show the one state that matters when nothing comes back. The failure mirrors
 * the real one: a provider whose key variable is unset is refused *before* the request
 * goes out, with the same sentence and the same ``auth`` kind.
 */
const mockChat = {
  status: 'idle' as ConversationCard['status'],
  phase: '',
  error: '',
  error_kind: '',
  note: '',
}

function mockCard(): ConversationCard {
  return {
    id: 'hud',
    title: '当前对话',
    status: mockChat.status,
    phase: mockChat.phase,
    provider: mockPicked.provider,
    model: mockPicked.model,
    turns: mockHistory.length,
    active: true,
    task_id: '',
    error: mockChat.error,
    error_kind: mockChat.error_kind,
    note: mockChat.note,
  }
}

function mockSnapshot(): UiSnapshot {
  return {
    // 样例那一轮在跑就说「在想」，跑完交还给麦克风那侧记的状态。
    voice_state: mockChat.status === 'running' ? 'processing' : mockTurn,
    voice: mockVoice.status().phase,
    voice_detail: mockVoice.status().detail,
    history: [...mockHistory],
    last_event: 'mock',
    interrupted: false,
    conversations: [mockCard()],
    streaming: null,
  }
}

/**
 * 预览里没有 Python 的推送通道，所以样例自己敲这同一个门：``window.__jarvisState``。
 *
 * 不这么做的话，预览就只能在点发送的那一瞬间拉一次 —— 那之后无论答案来了还是这一轮
 * 被拒了，屏上都看不见，等于把这次要验的那张卡片永远藏起来。
 */
function pushMockSnapshot(): void {
  window.__jarvisState?.(mockSnapshot())
}

function mockTuning(key: string): { thinking: string; turns: number; tools: boolean } {
  return mockTunings[key] ?? { thinking: 'medium', turns: 10, tools: true }
}

function mockModels(): ModelList {
  return {
    error: '',
    providers: mockProviders.map((row) => ({ ...row, models: [...row.models] })),
    provider: mockPicked.provider,
    model: mockPicked.model,
    thinking: mockTuning(`${mockPicked.provider}\u0000${mockPicked.model}`).thinking,
    turns: mockTuning(`${mockPicked.provider}\u0000${mockPicked.model}`).turns,
    tools: mockTuning(`${mockPicked.provider}\u0000${mockPicked.model}`).tools,
    thinking_levels: mockThinkingLevels,
    turns_bounds: [0, 50],
  }
}

/** Preview-only settings object so the dialog can be exercised in a browser.
 *  Mirrors the real shape, including the rule that no key value is ever present. */
const mockSettings: SettingsSnapshot = {
  error: '',
  providers: ['qwenai', 'wkapi'],
  models: [
    {
      name: 'qwenai',
      base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      model: 'qwen3.8-flash',
      models: [
        { id: 'qwen3.8-flash', label: 'Qwen3.8 Flash' },
        { id: 'qwen3.8-max', label: 'Qwen3.8 Max' },
      ],
      default_model: 'qwen3.8-flash',
      key_env: 'QWENAI_API_KEY',
      key_set: false,
      source: '配置文件',
      edited: false,
      current: true,
    },
    {
      name: 'wkapi',
      base_url: 'https://wkapi.example.com/v1',
      model: 'claude-sonnet',
      models: [{ id: 'claude-sonnet', label: 'claude-sonnet' }],
      default_model: 'claude-sonnet',
      key_env: 'WKAPI_API_KEY',
      key_set: true,
      source: '界面添加',
      edited: false,
      current: false,
    },
    {
      // A third row from the file, so the preview has something that can be hidden:
      // the row in use never offers it, and a window-added row offers 删除 instead.
      name: 'deepseek',
      base_url: 'https://api.deepseek.example/v1',
      model: 'deepseek-chat',
      models: [{ id: 'deepseek-chat', label: 'deepseek-chat' }],
      default_model: 'deepseek-chat',
      key_env: 'DEEPSEEK_API_KEY',
      key_set: false,
      source: '配置文件',
      edited: false,
      current: false,
    },
  ],
  provider: 'qwenai',
  hidden_models: [],
  base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  model: 'qwen3.8-flash',
  api_key_variable: 'QWENAI_API_KEY',
  api_key_set: false,
  cloud_voice_key_variable: 'DASHSCOPE_API_KEY',
  cloud_voice_key_set: false,
  overrides_active: [],
  voice_auto_arm: true,
  auto_speak_typed: true,
  telemetry_interval_ms: 1500,
  thinking_enabled: false,
  thinking_budget: 2048,
  thinking_budget_bounds: [64, 16000],
  thinking_budget_honored: true,
  history_turns: 10,
  history_turns_bounds: [0, 50],
  ai_edited: [],
  wake_greeting: '您好，主人，我是智能语音助手，小夜',
  wake_greeting_default: '您好，主人，我是智能语音助手，小夜',
  wake_greeting_max: 200,
  wake_keywords: ['你好小夜', '你好小智', '你好晓夜', '你好小业'],
  wake_keywords_stored: [],
  wake_keywords_default: ['你好小夜', '你好小智', '你好晓夜', '你好小业'],
  wake_keywords_max: 6,
  wake_keyword_min_chars: 2,
  wake_keyword_max_chars: 12,
  thinking_loader: 'dots',
  thinking_loader_choices: ['dots', 'matrix', 'ring', 'bars'],
  display_name: '小夜',
  display_name_default: '小夜',
  display_name_max: 12,
  local_model: '',
  local_model_label: 'Qwen2.5-7B',
  local_models: mockBundled,
  local_context: 4096,
  local_context_options: [
    { id: 2048, label: '2K', note: '本机常驻约 4.9 GB（没显卡的机器约 4.5 GB）；装不下 4702 token 的工具清单，这一档她只回答、不动手', needs_free_mb: 5072 },
    { id: 4096, label: '4K', note: '本机常驻约 4.9 GB（没显卡的机器约 4.6 GB）；装不下 4702 token 的工具清单，这一档她只回答、不动手', needs_free_mb: 5200 },
    { id: 8192, label: '8K', note: '本机常驻约 5.1 GB（没显卡的机器约 4.9 GB）；装得下那份 4702 token 的工具清单，她能一边答一边动手', needs_free_mb: 5456 },
    { id: 16384, label: '16K', note: '本机常驻约 5.1 GB（没显卡的机器约 5.3 GB）；装得下那份 4702 token 的工具清单，她能一边答一边动手', needs_free_mb: 5968 },
  ],
  local_idle_minutes: 10,
  local_idle_options: [
    { id: 0, label: '不自动关（一直占内存）' },
    { id: 2, label: '2 分钟' },
    { id: 5, label: '5 分钟' },
    { id: 10, label: '10 分钟' },
    { id: 30, label: '30 分钟' },
  ],
  local_eager: false,
  local_needs_free_mb: 5200,
  local_free_mb: 9000,
  local_tool_table_tokens: 4702,
  thinking_loader_options: [
    { id: 'dots', label: '三个点（默认）' },
    { id: 'matrix', label: '矩阵雨' },
    { id: 'ring', label: '圆环' },
    { id: 'bars', label: '竖条' },
  ],
  llm_timeout_seconds: 60,
  alerts: {
    rules: [
      {
        code: 'cpu',
        label: 'CPU 占用',
        unit: '%',
        help: '按所有核心的平均算，100% 是每个核心都排满。',
        low: 50,
        high: 100,
        direction: 'above',
        enabled: true,
        threshold: 90,
        default_threshold: 90,
      },
      {
        code: 'memory',
        label: '内存占用',
        unit: '%',
        help: '到线之后系统开始往交换分区搬，机器会明显变卡。',
        low: 50,
        high: 99,
        direction: 'above',
        enabled: true,
        threshold: 90,
        default_threshold: 90,
      },
      {
        code: 'swap',
        label: '交换分区占用',
        unit: '%',
        help: '交换分区吃满，下一步就是进程被系统杀掉。',
        low: 10,
        high: 99,
        direction: 'above',
        enabled: true,
        threshold: 80,
        default_threshold: 80,
      },
      {
        code: 'disk',
        label: '磁盘剩余空间',
        unit: 'GB',
        help: '每一块盘各算一条。删东西要先扫描、勾选、确认，本工具不会自动删。',
        low: 1,
        high: 500,
        direction: 'below',
        enabled: true,
        threshold: 20,
        default_threshold: 20,
      },
    ],
    cooldown_minutes: 10,
    cooldown_bounds: [1, 180],
    speak_critical: true,
    sustain_seconds: 60,
  },
}

function applyMockSettings(patch: Record<string, unknown>): SettingsSnapshot {
  const problems: Record<string, string> = {}
  const applied: Record<string, unknown> = {}
  const next = { ...mockSettings, problems, applied }
  for (const [key, value] of Object.entries(patch)) {
    if (key === 'target' || key === 'key_for') {
      // Addressing fields, not settings -- same rule as ``SettingsService.apply``.
      continue
    }
    if (key === 'api_key') {
      const text = String(value ?? '').trim()
      if (!text) {
        problems[key] = '密钥为空'
        continue
      }
      next.api_key_set = true
      applied[key] = 'set'
      continue
    }
    if (key === 'add_model') {
      const reason = mockAddProviders(next, value)
      if (reason) {
        problems[key] = reason
        continue
      }
      applied[key] = value
      continue
    }
    if (key === 'timeout_seconds') {
      const target = String(patch.target ?? next.provider)
      const row = next.models.find((item) => item.name === target)
      if (!row) {
        problems[key] = `未配置的模型：${target}`
        continue
      }
      const raw = String(value ?? '').trim()
      if (raw) {
        const seconds = Number(raw)
        if (!Number.isFinite(seconds) || seconds < 5 || seconds > 600) {
          problems[key] = '超时秒数要在 5–600 之间'
          continue
        }
        row.timeout_seconds = seconds
      } else {
        row.timeout_seconds = null
      }
      const booked = mockProviders.find((item) => item.name === target)
      if (booked) booked.timeout_seconds = row.timeout_seconds ?? null
      applied[key] = row.timeout_seconds
      continue
    }
    if (key === 'key_optional') {
      // Row-scoped, like 调用地址: it edits the row named by ``target`` and nothing else,
      // and an unknown row is refused the way ``SettingsService`` refuses it.
      const target = String(patch.target ?? next.provider)
      const row = next.models.find((item) => item.name === target)
      if (!row) {
        problems[key] = `未配置的模型：${target}`
        continue
      }
      const flag = Boolean(value)
      row.key_optional = flag
      const booked = mockProviders.find((item) => item.name === target)
      if (booked) booked.key_optional = flag
      applied[key] = flag
      continue
    }
    if (key === 'base_url' && !/^https?:\/\/.+/.test(String(value))) {
      problems[key] = '地址必须以 http:// 或 https:// 开头'
      continue
    }
    if (key === 'model' && /\s/.test(String(value))) {
      problems[key] = '模型名不能含空格'
      continue
    }
    if (key === 'wake_keywords') {
      const reason = mockWakeKeywordPatch(next, value)
      if (reason) {
        problems[key] = reason
        continue
      }
      applied[key] = value
      continue
    }
    if (key === 'provider') {
      // The row that says 在用 has to follow the pick, or the preview shows a 藏起来 button
      // on the row that is in use -- the real shell recomputes this from its own section.
      const chosen = String(value ?? '')
      next.provider = chosen
      next.models = next.models.map((row) => ({ ...row, current: row.name === chosen }))
      applied[key] = value
      continue
    }
    if (key === 'hide_provider' || key === 'show_provider') {
      const name = String(value ?? '').trim()
      const hidden = new Set((next.hidden_models ?? []).map((row) => row.name))
      const visible = next.models.find((row) => row.name === name)
      if (key === 'hide_provider') {
        if (!visible) {
          problems[key] = `没有这一行服务商：${name}`
          continue
        }
        if (visible.current) {
          problems[key] = `「${name}」正在用，先切到别的服务商再藏`
          continue
        }
        hidden.add(name)
        next.models = next.models.filter((row) => row.name !== name)
      } else {
        if (!hidden.has(name)) {
          problems[key] = `「${name}」没有被藏起来`
          continue
        }
        hidden.delete(name)
        const row = (next.hidden_models ?? []).find((item) => item.name === name)
        if (row) {
          next.models = [
            ...next.models,
            {
              name: row.name,
              base_url: '',
              model: '',
              models: [],
              default_model: '',
              key_env: '',
              key_set: false,
              source: row.source,
              edited: false,
              current: false,
            },
          ].sort((a, b) => a.name.localeCompare(b.name))
        }
      }
      const rows = next.models.map((row) => row.name)
      next.hidden_models = (next.hidden_models ?? [])
        .filter((row) => hidden.has(row.name) && !rows.includes(row.name))
        .concat(
          key === 'hide_provider' && visible
            ? [
                {
                  name,
                  source: visible.source,
                  models: visible.models.length,
                  current: false,
                },
              ]
            : [],
        )
        .sort((a, b) => a.name.localeCompare(b.name))
      applied[key] = value
      continue
    }
    if (key.startsWith('alerts_')) {
      const reason = mockAlertPatch(next, key, value)
      if (reason) {
        problems[key] = reason
        continue
      }
      applied[key] = value
      continue
    }
    if (key in next) {
      ;(next as unknown as Record<string, unknown>)[key] = value
      applied[key] = value
    } else {
      problems[key] = '未知设置项'
    }
  }
  Object.assign(mockSettings, next)
  return { ...mockSettings }
}

/**
 * The add-provider queue, applied the way ``SettingsService._add_model`` applies it:
 * one batch, all or nothing, and a name refused against both the rows on screen and the
 * rest of the batch. A mock that accepted what the real shell refuses would let a browser
 * walk "prove" a save that never reaches disk -- which is precisely how the
 * ModelSpec bug stayed invisible for so long. Returns a refusal, or '' when applied.
 */
function mockAddProviders(next: SettingsSnapshot, value: unknown): string {
  const rows = Array.isArray(value) ? value : [value]
  if (!rows.length) return '没有要添加的服务商'
  const known = new Set(next.models.map((row) => row.name))
  const fresh: ModelRow[] = []
  for (const item of rows) {
    const row = (item ?? {}) as Record<string, unknown>
    const name = String(row.name ?? '').trim()
    const url = String(row.base_url ?? '').trim()
    // The same folding the shell does: a list of ids (or {id,label}), with the older
    // single ``model`` still accepted. A mock that quietly kept only the first would
    // make the multi-model row look broken here and fine on the desktop.
    const listed: { id: string; label: string }[] = []
    const seen = new Set<string>()
    const push = (id: string, label: string): void => {
      const wanted = id.trim()
      if (wanted && !seen.has(wanted)) {
        seen.add(wanted)
        listed.push({ id: wanted, label: label.trim() || wanted })
      }
    }
    if (Array.isArray(row.models)) {
      for (const entry of row.models) {
        if (entry && typeof entry === 'object') {
          push(String((entry as { id?: unknown }).id ?? ''), String((entry as { label?: unknown }).label ?? ''))
        } else {
          push(String(entry ?? ''), '')
        }
      }
    }
    push(String(row.model ?? ''), '')
    if (!/^[a-z0-9_-]{1,32}$/.test(name)) return '模型名字只能是小写字母、数字、- 和 _，1-32 位'
    if (!/^https?:\/\//.test(url)) return `${name}：地址必须以 http:// 或 https:// 开头`
    if (!listed.length) return `${name}：至少写一个模型名`
    const bad = listed.find((spec) => /\s/.test(spec.id) || spec.id.length > 150)
    if (bad) return `${name}：模型名 ${bad.id} 不能含空格且不超过 150 字符`
    if (known.has(name)) return `已经有叫 ${name} 的模型；要改它就在那一行上改`
    known.add(name)
    const model = listed[0].id
    const noKey = Boolean(row.key_optional)
    fresh.push({
      name,
      base_url: url,
      model,
      models: listed,
      default_model: model,
      key_env: `${name.toUpperCase().replace(/-/g, '_')}_API_KEY`,
      key_set: !noKey && Boolean(String(row.api_key ?? '').trim()),
      key_optional: noKey,
      timeout_seconds: typeof row.timeout_seconds === 'number' ? row.timeout_seconds : null,
      source: '界面添加',
      edited: false,
      current: false,
    })
  }
  next.models = [...next.models, ...fresh]
  next.providers = [...next.providers, ...fresh.map((row) => row.name)]
  for (const row of fresh) {
    mockProviders.push({
      name: row.name,
      base_url: row.base_url,
      models: row.models,
      default_model: row.default_model,
      key_set: row.key_set,
      key_variable: row.key_env,
      key_optional: row.key_optional,
      current: false,
    })
  }
  return ''
}

/** Same verdicts as ``jarvis/app/wake_keywords.py``, so a browser preview cannot accept a
 *  list the real shell would refuse. Returns a refusal message, or '' when applied. */
function mockWakeKeywordPatch(next: SettingsSnapshot, value: unknown): string {
  const max = next.wake_keywords_max ?? 6
  const minChars = next.wake_keyword_min_chars ?? 2
  const maxChars = next.wake_keyword_max_chars ?? 12
  const pieces = String(value ?? '')
    .split(/[、,，;；/\n]+/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0)
  const kept: string[] = []
  const seen = new Set<string>()
  for (const piece of pieces) {
    if (piece.length < minChars) return `「${piece}」太短了：唤醒词至少 ${minChars} 个字`
    if (piece.length > maxChars) return `「${piece}」太长了：唤醒词最多 ${maxChars} 个字`
    const folded = piece.toLocaleLowerCase()
    if (seen.has(folded)) continue
    seen.add(folded)
    kept.push(piece)
  }
  if (kept.length > max) return `最多 ${max} 个唤醒词，现在 ${kept.length} 个`
  const defaults = next.wake_keywords_default ?? []
  next.wake_keywords_stored = kept
  next.wake_keywords = kept.length ? kept : defaults
  return ''
}

/** Same verdicts the Python engine gives, so a browser preview cannot accept a patch
 *  the real shell would refuse. Returns a refusal message, or '' when applied. */
function mockAlertPatch(
  next: SettingsSnapshot,
  key: string,
  value: unknown,
): string {
  const section = next.alerts
  if (!section) return '预览里没有告警配置'
  if (key === 'alerts_cooldown_minutes') {
    const raw = Number(value)
    const [low, high] = section.cooldown_bounds
    if (!Number.isFinite(raw) || raw < low || raw > high) {
      return `重复提醒间隔只能在 ${low}~${high} 分钟之间`
    }
    next.alerts = { ...section, cooldown_minutes: raw }
    return ''
  }
  if (key === 'alerts_speak_critical') {
    next.alerts = { ...section, speak_critical: Boolean(value) }
    return ''
  }
  if (key !== 'alerts_rules') return '未知设置项'
  if (typeof value !== 'object' || value === null) {
    return '告警设置必须是 {编码: {enabled, threshold}} 的形状'
  }
  const rows = value as Record<string, Partial<AlertRuleSetting>>
  for (const code of Object.keys(rows)) {
    const rule = section.rules.find((item) => item.code === code)
    if (!rule) return `没有这条告警：${code}`
    const change = rows[code] ?? {}
    if (change.threshold !== undefined) {
      const raw = Number(change.threshold)
      if (!Number.isFinite(raw) || raw < rule.low || raw > rule.high) {
        return `${rule.label} 的阈值只能在 ${rule.low}~${rule.high} 之间`
      }
    }
  }
  next.alerts = {
    ...section,
    rules: section.rules.map((rule) => ({ ...rule, ...(rows[rule.code] ?? {}) })),
  }
  return ''
}

/** Preview-only usage numbers. Labelled as samples wherever it is rendered. */
function mockUsage(days: number): UsageReport {
  const perDay: UsageDay[] = []
  const now = new Date()
  for (let i = Math.max(days, 0) - 1; i >= 0; i -= 1) {
    const stamp = new Date(now)
    stamp.setDate(now.getDate() - i)
    perDay.push({
      day: stamp.toISOString().slice(0, 10),
      calls: 6,
      prompt_tokens: 1200,
      completion_tokens: 380,
      cached_tokens: 0,
      cache_hit_percent: null,
    })
  }
  const prompt = perDay.reduce((sum, row) => sum + row.prompt_tokens, 0)
  const completion = perDay.reduce((sum, row) => sum + row.completion_tokens, 0)
  const calls = days === 0 ? 2_580 : perDay.length * 6
  const ledgerRows = days === 0 ? 12_480 : perDay.length
  const totals = days === 0 ? { prompt: 4_182_940, completion: 613_220 } : { prompt, completion }
  const sampleModels: UsageModelRow[] = [
    {
      provider: 'deepseek',
      model: 'deepseek-chat',
      calls: Math.round(calls * 0.52),
      prompt_tokens: Math.round(totals.prompt * 0.46),
      completion_tokens: Math.round(totals.completion * 0.51),
      total_tokens: 0,
      cached_tokens: 0,
      cache_hit_percent: null,
      avg_latency_ms: 780,
    },
    {
      provider: 'qwen',
      model: 'qwen-max',
      calls: Math.round(calls * 0.31),
      prompt_tokens: Math.round(totals.prompt * 0.38),
      completion_tokens: Math.round(totals.completion * 0.29),
      total_tokens: 0,
      cached_tokens: 0,
      cache_hit_percent: null,
      avg_latency_ms: 1_240,
    },
    {
      provider: 'openai',
      model: 'gpt-4o',
      calls: Math.round(calls * 0.17),
      prompt_tokens: Math.round(totals.prompt * 0.16),
      completion_tokens: Math.round(totals.completion * 0.2),
      total_tokens: 0,
      cached_tokens: 0,
      cache_hit_percent: null,
      avg_latency_ms: 2_050,
    },
  ].map((row) => ({ ...row, total_tokens: row.prompt_tokens + row.completion_tokens }))
  return {
    error: '',
    summary: {
      days,
      since: perDay[0]?.day ?? '',
      until: perDay[perDay.length - 1]?.day ?? '',
      calls,
      prompt_tokens: totals.prompt,
      completion_tokens: totals.completion,
      total_tokens: totals.prompt + totals.completion,
      cached_tokens: 0,
      cache_hit_percent: null,
      cache_data_reported: false,
      calls_without_cache_data: calls,
      avg_latency_ms: 900,
      all_time: days === 0,
    },
    daily: days === 0 ? [] : perDay,
    models: sampleModels,
    span: {
      first_at: days === 0 ? '2026-07-08T09:12:04+00:00' : perDay[0]?.day ?? '',
      last_at: days === 0 ? '2026-10-04T02:31:55+00:00' : perDay[perDay.length - 1]?.day ?? '',
      rows: ledgerRows,
      days: days === 0 ? 89 : days,
    },
  }
}

/** Stands in for the real microphone so ``npm run dev`` can exercise the panel. */
const mockVoice = {
  phase: 'off' as VoicePhase,
  detail: '',
  keyword: '你好小夜',
  timer: undefined as ReturnType<typeof setTimeout> | undefined,
  status(): VoiceStatus {
    return { phase: this.phase, detail: this.detail, keyword: this.phase === 'running' ? this.keyword : '' }
  },
  enable(): VoiceStatus {
    if (this.phase === 'running' || this.phase === 'loading') return this.status()
    this.phase = 'loading'
    this.detail = '正在加载语音模型（浏览器预览为模拟）'
    clearTimeout(this.timer)
    this.timer = setTimeout(() => {
      this.phase = 'running'
      this.detail = ''
    }, 1800)
    return this.status()
  },
  mute(): VoiceStatus {
    clearTimeout(this.timer)
    this.phase = 'muted'
    this.detail = '麦克风已释放'
    return this.status()
  },
  talk(): VoiceStatus {
    if (this.phase !== 'running') {
      return { ...this.status(), detail: MOCK_REFUSALS[this.phase] ?? '先点「启用语音」' }
    }
    mockTurn = 'listening'
    return { ...this.status(), detail: '在听，说完停一下即可' }
  },
}

const MOCK_REFUSALS: Record<VoicePhase, string> = {
  off: '先点「启用语音」，加载约 30 秒',
  loading: '语音模型还在加载，等它变成「待唤醒」再按',
  running: '',
  failed: '语音不可用，状态条上有原因',
  muted: '麦克风已释放，先点「启用语音」',
}

let mockTurn: TurnPhase = 'idle'
const mockHistory: ChatTurn[] = []

const api = (): PywebviewApi | null => window.pywebview?.api ?? null

/**
 * Whether the current readings come from the synthetic browser feed.
 *
 * pywebview injects `window.pywebview` *after* the page script has run, so this
 * cannot be decided at import time — doing so labels real desktop readings as
 * mock data, which is worse than no badge at all.
 */
export const runningMocked = ref(true)

let resolved: Promise<PywebviewApi> | null = null

function waitForBridge(timeoutMs: number): Promise<PywebviewApi | null> {
  const existing = api()
  if (existing) return Promise.resolve(existing)
  return new Promise((resolve) => {
    const done = (value: PywebviewApi | null) => {
      window.removeEventListener('pywebviewready', onReady)
      resolve(value)
    }
    const onReady = () => done(api())
    window.addEventListener('pywebviewready', onReady, { once: true })
    setTimeout(() => done(api()), timeoutMs)
  })
}

/**
 * Keep watching for the bridge after the initial wait gave up.
 *
 * The 2 s window above is a *latency* budget, not a verdict: pywebview injects
 * `window.pywebview` from a thread that starts after navigation completes, and
 * on a cold start — a frozen build, a busy machine — that lands later than 2 s.
 * Without this, the page decides "no bridge" once and shows synthetic readings
 * for the rest of the session while a perfectly good bridge sits unused.
 *
 * Upgrading in place is strictly better than a longer timeout: a longer wait
 * would just move the threshold, and a page that recovers is correct at every
 * threshold.
 */
function upgradeWhenBridgeArrives(): void {
  const adopt = () => {
    const found = api()
    if (!found) return
    resolved = Promise.resolve(found)
    runningMocked.value = false
  }
  if (api()) {
    adopt()
    return
  }
  // Not `{ once: true }`: pywebview fires this per navigation, and a reload
  // should re-adopt rather than leave the page stuck on synthetic data.
  window.addEventListener('pywebviewready', adopt)
}

function bridge(): Promise<PywebviewApi> {
  resolved ??= waitForBridge(2000).then((found) => {
    runningMocked.value = found === null
    if (found === null) upgradeWhenBridgeArrives()
    return found ?? mock
  })
  return resolved
}

export function fetchSnapshot(): Promise<Report> {
  return bridge().then((target) => target.snapshot())
}

/**
 * 「知道了」 on one alert. The reading that caused it is unchanged -- this buys silence
 * on screen, and the alert comes back if the disk fills up again after the cooldown.
 */
export function acknowledgeAlert(code: string): Promise<{ ok: boolean; error: string }> {
  return bridge().then((target) => target.alerts_ack(code))
}

/** 顶栏那颗「告警 N」点开的这一格要的数：在着的 + 最近恢复的。 */
export function fetchAlerts(): Promise<{ active: AlertRow[]; history: AlertRow[]; error: string }> {
  return bridge().then((target) => target.alerts())
}

export function acknowledgeAllAlerts(): Promise<{ ok: boolean; count: number; error: string }> {
  return bridge().then((target) => target.alerts_ack_all())
}

export function fetchAppInfo(): Promise<AppInfo> {
  return bridge().then((target) => target.app_info())
}

export function fetchDiskPlan(): Promise<DiskPlan> {
  return bridge().then((target) => target.disk_scan())
}

/**
 * Delete the entries a human ticked. `confirmed` is hardcoded true here on
 * purpose: this function is only called from the panel's second, explicit
 * confirmation step. Entries travel as whole objects rather than path strings
 * because one entry can stand for a thousand files, and Python has to be able to
 * tell that apart from "delete this folder".
 */
export function deleteJunk(items: JunkItem[]): Promise<DiskOutcome> {
  return bridge().then((target) => target.disk_delete(items, true))
}

/**
 * End the processes a human ticked and then confirmed.
 *
 * ``confirmed`` is sent as ``true`` for the same reason the delete button sends it:
 * the page only calls this from the second click. It is not a trust signal -- the
 * controller re-checks the protection list and the PID's current name regardless.
 */
export function killProcesses(items: ProcessTarget[]): Promise<KillReport> {
  return bridge().then((target) => target.process_kill(items, true))
}

/**
 * The proposals the assistant has made and nobody has answered.
 *
 * Read when the panel opens rather than pushed: like the memory and knowledge lists,
 * this is something a person looks at on purpose, and a poll for it would be a poll
 * that is usually empty.
 */
export function fetchProcessProposals(): Promise<ProcessProposal[]> {
  return bridge().then((target) =>
    target.process_proposals().then((report) => {
      if (report.error) throw new Error(report.error)
      return report.entries
    }),
  )
}

/** Answer a proposal with 「不用了」. Ends nothing. */
export function dismissProcessProposal(pid: number): Promise<{ ok: boolean; error: string }> {
  return bridge().then((target) => target.process_dismiss(pid))
}

/**
 * Ask the model what it makes of the scan already on screen.
 *
 * It analyses the *current* scan and cannot name anything to delete — the tick
 * boxes and the second confirmation are still the only way anything goes.
 */
export function junkAdvice(): Promise<JunkAdvice> {
  return bridge().then((target) => target.disk_advice())
}

/**
 * The assistant's last actions on this machine, newest first.
 *
 * Read on demand rather than polled: the page only asks when somebody opens the
 * panel, so an idle window pays nothing for a list nobody is looking at.
 */
export function fetchActivity(): Promise<ActivityLog> {
  return bridge().then((target) => target.activity_log())
}

export function fetchVoices(): Promise<VoiceList> {
  return bridge().then((target) => target.tts_voices())
}

export function pickVoice(voice: string): Promise<VoiceList> {
  return bridge().then((target) => target.tts_pick(voice))
}

/**
 * Hear one voice without committing to it. The audio arrives on the same channel
 * real answers use, so the voice core reacts to a preview like to speech.
 */
export function previewVoice(voice: string): Promise<VoicePreview> {
  return bridge().then((target) => target.tts_preview(voice))
}

/**
 * Move the rate / volume sliders. Applies to the next sentence, previews included.
 */
export function setVoiceStyle(speed?: number, volume?: number): Promise<VoiceList> {
  return bridge().then((target) => target.tts_set_style(speed, volume))
}

/**
 * The five desktop-recording verbs. The clip stays in Python's memory between 录 and 存,
 * so nothing here is a payload: only the state crosses, plus the two short strings the
 * operator types when they decide to keep it.
 */
export function startVoiceSample(): Promise<SampleStatus> {
  return bridge().then((target) => target.voice_sample_start())
}

export function voiceSampleStatus(): Promise<SampleStatus> {
  return bridge().then((target) => target.voice_sample_status())
}

export function stopVoiceSample(): Promise<SampleStatus> {
  return bridge().then((target) => target.voice_sample_stop())
}

/** Removing a recorded voice deletes the operator's own clip. Nothing here is automatic. */
export function removeVoiceClone(voiceId: string): Promise<{ error: string; voices: VoiceChoice[] }> {
  return bridge().then((target) => target.voice_clone_remove(voiceId))
}

export function removeVoiceClones(voiceIds: string[]): Promise<VoiceBatchRemove> {
  return bridge().then((target) => target.voice_clone_remove_many(voiceIds))
}

export function discardVoiceSample(): Promise<SampleStatus> {
  return bridge().then((target) => target.voice_sample_discard())
}

/** Storing the take. `upload` is the operator's one-time yes to sending their voice to
 *  the vendor; it defaults to off here as well as in Python, so forgetting to pass it
 *  can never turn into a disclosure. */
export function saveVoiceSample(
  name: string,
  promptText: string,
  upload = false
): Promise<SampleStatus> {
  return bridge().then((target) => target.voice_sample_save(name, promptText, upload))
}

export function fetchComputerLevels(): Promise<ComputerLevels> {
  return bridge().then((target) => target.computer_levels())
}

/**
 * 把皮肤送到桌面上那扇宠物窗。
 *
 * 本页自己换色是即时可见的（``setSkin``），但宠物是**另一页**：它有自己的一份图和材质，
 * 不告诉它就只剩仪表盘换了色，看起来像 bug。所以换皮肤时同时推一次。
 */
export function skinApply(skin: string): Promise<SkinApplyAnswer> {
  return bridge().then((target) => target.skin_apply(skin))
}

/**
 * 把这身颜色的三个墨色报给壳子。
 *
 * 桌上的两张卡（她说的话、「思考中」）是 **Python 画的**，页面换肤换不到它们 —— 只告诉
 * 壳子 id 不够，壳子那边没有这张配色表（那是会过期的一份副本）。所以由页面报数，一次挂载
 * 一次、每次换肤一次；id 那条路（``skinApply``）继续管页面自己的重着色。
 */
export function reportPetPalette(
  inks: Pick<Skin, 'fill' | 'line' | 'glow'>,
): Promise<PaletteAnswer> {
  return bridge().then((target) => target.pet_palette(inks.fill, inks.line, inks.glow))
}

/**
 * 打开/关掉"允许打字"。它和四个档位是两把锁：档位管"能碰哪儿"，这把管"能不能把字写进
 * 别人的输入框"。立即生效。
 */
export function setComputerTyping(allowed: boolean): Promise<ComputerLevels> {
  return bridge().then((target) => target.computer_set_typing(allowed))
}

export function setComputerTier(tier: number): Promise<ComputerLevels> {
  return bridge().then((target) => target.computer_set_tier(tier))
}

export function fetchWindowState(): Promise<WindowState> {
  return bridge().then((target) => target.window_state())
}

export function toggleWindowMax(): Promise<WindowState> {
  return bridge().then((target) => target.window_toggle_max())
}

export function fetchShellState(): Promise<ShellState> {
  return bridge().then((target) => target.shell_state())
}

/** Put the window in the tray. The only exit is the icon's own menu. */
export function hideWindow(): Promise<ShellState> {
  return bridge().then((target) => target.window_hide())
}

export function fetchReminders(): Promise<ReminderBoard> {
  return bridge().then((target) => target.reminders())
}

export function addReminder(text: string, when: string): Promise<ReminderWrite> {
  return bridge().then((target) => target.reminder_add(text, when))
}

export function cancelReminder(key: string): Promise<{ ok: boolean; removed: string; error: string }> {
  return bridge().then((target) => target.reminder_cancel(key))
}

export function toggleReminder(
  jobId: string,
  enabled: boolean,
): Promise<{ ok: boolean; error: string }> {
  return bridge().then((target) => target.reminder_toggle(jobId, enabled))
}

export function fetchAutomationOverview(): Promise<AutomationOverview> {
  return bridge().then((target) => target.automation_overview())
}

export function fetchScheduledJobs(): Promise<ScheduledJobsBoard> {
  return bridge().then((target) => target.scheduled_jobs())
}

export function toggleScheduledJob(
  jobId: string,
  enabled: boolean,
): Promise<{ ok: boolean; error: string }> {
  return bridge().then((target) => target.job_toggle(jobId, enabled))
}

/** Run one scheduled job once, the same path the clock takes. */
export function runScheduledJob(jobId: string): Promise<JobRunAnswer> {
  return bridge().then((target) => target.job_run(jobId))
}

export function removeScheduledJob(
  jobId: string,
): Promise<{ ok: boolean; removed: boolean; error: string }> {
  return bridge().then((target) => target.job_remove(jobId))
}

export function fetchWorkflows(): Promise<WorkflowBoard> {
  return bridge().then((target) => target.workflow_definitions())
}

export function runWorkflow(name: string): Promise<WorkflowRunAnswer> {
  return bridge().then((target) => target.workflow_run(name))
}

/** Re-read the definition folder. A new YAML should not need a restart. */
export function reloadWorkflows(): Promise<{ ok: boolean; count: number; error: string }> {
  return bridge().then((target) => target.workflow_reload())
}

/**
 * Ask for a plan. The answer is a plan -- nothing here executes a step, and the
 * panel says so, because "plan" in a UI is easy to read as "do it".
 */
export function planGoal(goal: string): Promise<PlanAnswer> {
  return bridge().then((target) => target.plan_goal(goal))
}

export function fetchMemories(): Promise<MemoryBoard> {
  return bridge().then((target) => target.memory_list())
}

export function forgetMemory(id: number): Promise<{ ok: boolean; error: string }> {
  return bridge().then((target) => target.memory_forget(id))
}

export function forgetAllMemories(confirmed: boolean): Promise<{ removed: number; error: string }> {
  return bridge().then((target) => target.memory_forget_all(confirmed))
}

export function fetchKnowledge(): Promise<KnowledgeBoard> {
  return bridge().then((target) => target.knowledge_state())
}

export function ingestKnowledge(path: string): Promise<{ ok: boolean; error: string }> {
  return bridge().then((target) => target.knowledge_ingest(path))
}

export function forgetKnowledge(docId: string): Promise<{ ok: boolean; error: string }> {
  return bridge().then((target) => target.knowledge_forget(docId))
}

export function probeKnowledge(query: string): Promise<{ hits: Record<string, unknown>[]; error: string }> {
  return bridge().then((target) => target.knowledge_probe(query))
}

/**
 * 她自学那一格。永远是一整份，所以调用方不需要"这一格存在吗"这一问 —— 存在，
 * 只不过可能写着"这个进程没有接自学这一层"。
 *
 * 面板只在打开时读它一次：这一层没有轮询，轮询会把"你刚按下去的那一下"擦掉。
 */
export function growthState(): Promise<GrowthState> {
  return bridge().then((target) => target.growth_state())
}

/** 开或关她自学。回的是后端**读回来**的那一份，界面照着它画，不猜。 */
export function growthSet(on: boolean): Promise<GrowthState> {
  return bridge().then((target) => target.growth_set(on))
}

/** 只删她自学记下的那些条，人喂的资料一条不动。删了几条要回给人看。 */
export function growthForget(): Promise<GrowthForget> {
  return bridge().then((target) => target.growth_forget())
}

/* --------------------------------------------------------------------------- 扩展市场
 * 这十扇门的返回值**全是同一份 ``ExtensionState``**（搜索那一扇除外）：后端每改一次就回读
 * 一次整份状态，界面照着它画，不拿"我刚才点了什么"去猜。这一层没有轮询 —— 只在弹窗打开、
 * 或者切到那一格时读一次，轮询会把"你刚按下去的那一下"擦掉。
 *
 * 函数名是驼峰（页面的写法），桥面门名是 Python 的下划线原名（见 ``interface PywebviewApi``
 * 那一段的说明）：pywebview 不做名字转换，写错一个字母那扇门在打包版里就是 undefined。
 */

/** 这一层现在什么样。永远是一整份，所以不需要问「这一格存在吗」。 */
export function extensionsState(): Promise<ExtensionState> {
  return bridge().then((target) => target.extensions_state())
}

/**
 * 三路并发搜。每一路的成败单独带回来：``failures`` 非空时屏上写的不是「没有结果」，
 * 而是「这一路没通，原文是这句」。
 */
export function extensionsSearch(query: string, kinds: string[]): Promise<ExtensionSearch> {
  return bridge().then((target) => target.extensions_search(query, kinds))
}

/**
 * 按 id 装。**后端只认刚才搜出来的 id**，页面传不进命令行 —— 这不是洁癖：接受那个形状
 * 就等于这一页变成一个命令输入框。所以「没搜过」时这颗按钮必须是灰的并写明为什么灰。
 */
export function extensionsInstall(itemId: string): Promise<ExtensionState> {
  return bridge().then((target) => target.extensions_install(itemId))
}

/**
 * 打开页面就能看到的那一格。读不到时 ``error`` 非空 —— 「这份清单没读到」和
 * 「没有推荐」在屏上是两句话，混成一句用户就会去换关键词。
 */
export function extensionsRecommended(): Promise<ExtensionRecommended> {
  return bridge().then((target) => target.extensions_recommended())
}

/** 点图标弹出来的那张卡。只认我们给出去的 id，和装同一条规矩。 */
export function extensionsDetail(itemId: string): Promise<ExtensionDetail> {
  return bridge().then((target) => target.extensions_detail(itemId))
}

/** 更新这一条。判断不了的（旧版装的技能、每次现拉的那类）后端会拒绝并说明原因。 */
export function extensionsUpdate(itemId: string): Promise<ExtensionState> {
  return bridge().then((target) => target.extensions_update(itemId))
}

/** 从搜索结果或详情卡删掉这一条。名字由后端解析，页面不拼。 */
export function extensionsRemoveItem(itemId: string, confirmed: boolean): Promise<ExtensionState> {
  return bridge().then((target) => target.extensions_remove_item(itemId, confirmed))
}

/** 手工加一条 **http** 的 MCP server（不起本机进程）。stdio 只能从搜索结果装。 */
export function extensionsAddHttp(name: string, url: string): Promise<ExtensionState> {
  return bridge().then((target) => target.extensions_add_http(name, url))
}

/** 那一大颗「允许她运行外部扩展程序」。关 = 一条 server 都不连。 */
export function extensionsLayerSet(on: boolean): Promise<ExtensionState> {
  return bridge().then((target) => target.extensions_layer_set(on))
}

/** 单独启停一条登记的 server。回的是读回来的整份，不是把 ``on`` 原样吐回去。 */
export function extensionsServerSet(name: string, on: boolean): Promise<ExtensionState> {
  return bridge().then((target) => target.extensions_server_set(name, on))
}

/** 删一条登记。``confirmed`` 必须是真 —— 删配置也是删除动作，不是「关一下」。 */
export function extensionsServerRemove(name: string, confirmed: boolean): Promise<ExtensionState> {
  return bridge().then((target) => target.extensions_server_remove(name, confirmed))
}

/** 重连一遍 MCP：装完不必重启小夜，要重启用户会以为没装上。 */
export function extensionsReconnect(): Promise<ExtensionState> {
  return bridge().then((target) => target.extensions_reconnect())
}

/** 启停一份技能（只是说明书，不执行）。 */
export function skillSet(slug: string, on: boolean): Promise<ExtensionState> {
  return bridge().then((target) => target.skill_set(slug, on))
}

/** 删一份技能：只删 ``扩展/skills/<slug>`` 那一个目录，不动别的东西。 */
export function skillRemove(slug: string, confirmed: boolean): Promise<ExtensionState> {
  return bridge().then((target) => target.skill_remove(slug, confirmed))
}

export function fetchPetState(): Promise<PetState> {
  return bridge().then((target) => target.pet_state())
}

/** Show or hide the desktop pet. Python remembers the choice across launches. */
/** 换一档大小：小 / 中 / 大 / 特大。改的是她那个窗口，不是把她画在更大的框里。 */
export function setPetScale(scale: string): Promise<PetState> {
  return bridge().then((target) => target.pet_set_scale(scale))
}

/** 「她会自己动一动」。 */
export function setPetIdle(on: boolean): Promise<PetState> {
  return bridge().then((target) => target.pet_set_idle(on))
}

export function togglePet(): Promise<PetState> {
  return bridge().then((target) => target.pet_toggle())
}

/**
 * Tell the shell where the drag handle is, in fractions of this viewport.
 *
 * The window is click-through everywhere else, so Python cannot know where the one
 * grabbable patch is unless the page that drew it says so.
 */
export function reportPetGrip(rect: GripRect): Promise<{ ok: boolean }> {
  return bridge().then((target) => target.pet_grip(rect))
}

export function setPetDragging(dragging: boolean): Promise<{ ok: boolean }> {
  return bridge().then((target) => target.pet_drag(dragging))
}

/**
 * One composited frame for the alpha pet window: a PNG data URL whose transparent
 * pixels are the desktop showing through.
 *
 * Only sent while the shell has asked for it (see ``PetStage``), because it is the
 * only way this window can be anything other than a rectangle -- colour-key
 * transparency was measured dead twice.
 */
/** The generated figures, the one showing, and whether a photo can be used now. */
export function petAssets(): Promise<PetAssets> {
  return bridge().then((target) => target.pet_assets())
}

/** One photo in, four figures out. ``photo`` is a data URL the page resized itself. */
export function petGenerate(
  photo: string,
  name?: string,
  style?: string,
): Promise<PetGenerated> {
  return bridge().then((target) => target.pet_generate(photo, name, style))
}

/** Put one figure on the desktop. Empty string gives her the built-in one back. */
export function petPick(petId: string): Promise<PetAssets> {
  return bridge().then((target) => target.pet_pick(petId))
}

export function petDelete(petId: string): Promise<PetAssets> {
  return bridge().then((target) => target.pet_delete(petId))
}

/** The polygon the pet page extrudes. Called on load and whenever the shell says the figure changed. */
export function modelServerStatus(): Promise<ModelServerStatus> {
  return bridge().then((target) => target.model_server_status())
}

export function modelServerStart(): Promise<ModelServerStatus> {
  return bridge().then((target) => target.model_server_start())
}

export function modelServerStop(): Promise<ModelServerStatus> {
  return bridge().then((target) => target.model_server_stop())
}

/** 让后端看一眼这台机器，回一套该用的离线模型与旋钮。可能要等模型先加载。 */
/** 选中那一刻先问一句"这台接得住吗"。读的是后端同一份判定，不改任何设置。 */
export function localModelCheck(model: string, context: number): Promise<GateCheck> {
  return bridge().then((target) => target.local_model_check(model, context))
}

export function localAdvice(): Promise<LocalAdvice> {
  return bridge().then((target) => target.local_advice())
}

/** 把建议卡按下去。后端会再校验一遍，不在档位里的值一律拒。 */
export function localAdviceApply(advice: LocalAdvice): Promise<{ error: string; snapshot: SettingsSnapshot }> {
  return bridge().then((target) => target.local_advice_apply(advice))
}

export function petModel(): Promise<SilhouetteModel> {
  return bridge().then((target) => target.pet_model())
}

export function pushPetFrame(png: string): Promise<{ ok: boolean }> {
  return bridge().then((target) => target.pet_frame(png))
}

/**
 * The figure finished appearing.
 *
 * The shell holds the wake greeting until this arrives, because the operator asked to
 * be spoken to only once she is fully on screen. The page is the only party that knows
 * when the animation is over -- its length lives here, not in Python -- so the report
 * goes out from here rather than the shell guessing a duration and drifting the moment
 * somebody retimes it.
 */
export function reportPetArrived(): Promise<{ ok: boolean }> {
  return bridge().then((target) => target.pet_arrived())
}

export function fetchShellLevels(): Promise<ShellLevels> {
  return bridge().then((target) => target.shell_levels())
}

export function setShellTier(tier: number): Promise<ShellLevels> {
  return bridge().then((target) => target.shell_set_tier(tier))
}

export function fetchSessions(): Promise<SessionList> {
  return bridge().then((target) => target.chat_sessions())
}

export function newSession(): Promise<SessionList> {
  return bridge().then((target) => target.chat_new_session())
}

export function switchSession(sessionId: string): Promise<SessionList> {
  return bridge().then((target) => target.chat_switch(sessionId))
}

export function renameSession(sessionId: string, title: string): Promise<SessionList> {
  return bridge().then((target) => target.chat_rename(sessionId, title))
}

export function deleteSession(sessionId: string): Promise<SessionList> {
  return bridge().then((target) => target.chat_delete(sessionId))
}

/** Several conversations at once; the answer names the ids that were not there. */
export function deleteSessions(sessionIds: string[]): Promise<BatchSessionDelete> {
  return bridge().then((target) => target.chat_delete_many(sessionIds))
}

/** Every stored turn of one conversation, oldest first. */
export function fetchSessionMessages(sessionId: string): Promise<SessionMessages> {
  return bridge().then((target) => target.chat_messages(sessionId))
}

/**
 * Ask a typed question. Blocks on the model, so callers own a busy flag.
 *
 * Errors arrive as ``{ error }`` rather than as rejections: the service turns
 * provider failures into that field, and the page should show the same shape for
 * "no API key" as for "the model said nothing".
 */
export function chatAsk(text: string, attachments: Attachment[] = []): Promise<ChatReply> {
  return bridge().then((target) => target.chat_ask(text, attachments))
}

/**
 * Start one turn without waiting for it.
 *
 * ``chatAsk`` stays for the callers that genuinely want the answer in the same call;
 * the panel does not, because "wait twenty seconds, then show everything at once" is
 * the experience being replaced.
 */
export function chatSend(
  text: string,
  attachments: Attachment[] = [],
  conversation = '',
  provider = '',
  model = '',
): Promise<ChatSendResult> {
  return bridge().then((target) =>
    target.chat_send(text, attachments, conversation, provider, model),
  )
}

export function chatCancel(taskId: string): Promise<{ ok: boolean; error: string }> {
  return bridge().then((target) => target.chat_cancel(taskId))
}

/**
 * Ask one question with several models. Starts a turn and returns at once.
 *
 * One door for three shapes -- they differ only in what each seat is allowed to see, and
 * the transcript, the memory and the stop button must not grow three slightly different
 * copies of "what gets stored". It holds the line for up to `requestsFor()` calls, so the
 * one thing it may not be is synchronous.
 */
export function chatCollaborate(
  text: string,
  seats: SeatChoice[],
  mode: CollaborationMode = 'table',
  rounds = 2,
  conversation = '',
): Promise<ChatSendResult> {
  return bridge().then((target) =>
    target.chat_collaborate(text, seats, rounds, conversation, mode),
  )
}

/** Ask one model whether it answers, and whether it can see a picture. */
export function llmTest(provider: string, model = ''): Promise<ProbeVerdict> {
  return bridge().then((target) => target.llm_test(provider, model))
}

export function fetchTasks(): Promise<{ tasks: TaskInfo[]; error: string }> {
  return bridge().then((target) => target.chat_tasks())
}

export function fetchConversations(): Promise<{
  conversations: ConversationCard[]
  error: string
}> {
  return bridge().then((target) => target.chat_conversations())
}

export function openConversation(conversationId: string): Promise<{
  conversation: string
  conversations: ConversationCard[]
}> {
  return bridge().then((target) => target.chat_open(conversationId))
}

export function newConversation(): Promise<{
  conversation: string
  conversations: ConversationCard[]
}> {
  return bridge().then((target) => target.chat_new())
}

/** Point one conversation at one model, leaving the others alone. */
export function pickConversationModel(
  conversationId: string,
  provider: string,
  model: string,
): Promise<{ ok: boolean; error: string; provider: string; model: string }> {
  return bridge().then((target) => target.chat_tab_model(conversationId, provider, model))
}

export function fetchVoiceStatus(): Promise<VoiceStatus> {
  return bridge().then((target) => target.voice_status())
}

export function enableVoice(): Promise<VoiceStatus> {
  return bridge().then((target) => target.voice_enable())
}

export function muteVoice(): Promise<VoiceStatus> {
  return bridge().then((target) => target.voice_mute())
}

/**
 * Open one spoken turn without the wake word.
 *
 * The answer is a status, not a boolean: when the press is refused, the `detail`
 * is the only thing the operator gets, so it has to say why.
 */
export function talkNow(): Promise<VoiceStatus> {
  return bridge().then((target) => target.voice_talk())
}

/** Forget the conversation and empty the transcript. */
export function clearChat(): Promise<{ ok: boolean }> {
  return bridge().then((target) => target.chat_clear())
}

/** Pull the current state once, so a page that mounted after the events still agrees. */
export function fetchUiSnapshot(): Promise<UiSnapshot> {
  return bridge().then((target) => target.state_snapshot())
}

/**
 * Receive pushed state. Returns the detach function.
 *
 * Python calls ``window.__jarvisState`` from its own thread; the page owns when it
 * starts listening, so registration and teardown live with the store that renders
 * it.
 */
export function onUiState(handler: (snapshot: UiSnapshot) => void): () => void {
  const previous = window.__jarvisState
  window.__jarvisState = (snapshot: UiSnapshot) => {
    if (snapshot.voice === 'running') {
      mockTurn = snapshot.voice_state
    }
    handler(snapshot)
  }
  return () => {
    window.__jarvisState = previous
  }
}

/**
 * Listen for the shell telling us it is no longer on screen.
 *
 * The window can be hidden by the X, by the tray, or by the operating system, and
 * only the first two come through here. That is enough: the point is to stop
 * walking the process table every 1.5 seconds for a dashboard nobody is looking at,
 * and ``document.hidden`` cannot be trusted to say when that is true.
 */
export function onBackground(handler: (foreground: boolean) => void): () => void {
  const previous = window.__jarvisBackground
  window.__jarvisBackground = (foreground: boolean) => handler(foreground)
  return () => {
    window.__jarvisBackground = previous
  }
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const exponent = Math.min(Math.floor(Math.log2(value) / 10), units.length - 1)
  const scaled = value / 2 ** (10 * exponent)
  return `${scaled.toFixed(scaled >= 100 || exponent === 0 ? 0 : 1)} ${units[exponent]}`
}

export function formatUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '--'
  const days = Math.floor(seconds / 86_400)
  const hours = Math.floor((seconds % 86_400) / 3600)
  return days > 0 ? `${days}天 ${hours}时` : `${hours}时 ${Math.floor((seconds % 3600) / 60)}分`
}

/** The settings dialog's two calls. Both answer with a fresh snapshot so the
 *  screen can show what was accepted and what was refused. */
export function fetchSettings(): Promise<SettingsSnapshot> {
  return bridge().then((target) => target.settings_get())
}

export function saveSettings(patch: Record<string, unknown>): Promise<SettingsSnapshot> {
  return bridge().then((target) => target.settings_apply(patch))
}

/**
 * Ask an endpoint which models it serves, so the model list is a pick instead of a copy job.
 *
 * ``api_key`` is the key typed into the add form for a row that does not exist yet -- the
 * only credential there is for a gateway that guards ``/models``. It goes out as an
 * argument to one request and is kept nowhere: not in preferences, not in a log line.
 */
export function listEndpointModels(
  base_url: string,
  provider = '',
  api_key = '',
): Promise<EndpointModels> {
  return bridge().then((target) => target.list_endpoint_models(base_url, provider, api_key))
}

export function fetchUsage(days: number): Promise<UsageReport> {
  return bridge().then((target) => target.usage_summary(days))
}

/**
 * Receive synthesized speech from the desktop shell.
 *
 * Installed before the page reports that it can play audio, so there is no window
 * in which Python could push samples into `undefined`. The handler is not allowed to
 * throw: this runs inside a `evaluate_js` call from a voice thread, and an exception
 * here is how a half-spoken answer becomes a stuck pipeline.
 */
export function onSpeech(handler: (message: SpeechMessage) => void): () => void {
  const previous = window.__jarvisAudio
  window.__jarvisAudio = (message: SpeechMessage) => {
    try {
      handler(message ?? {})
    } catch {
      // Dropping one slice costs a hiccup. Letting it escape costs the microphone.
    }
  }
  return () => {
    window.__jarvisAudio = previous
  }
}

/** The models the assistant can answer with, and the one it is using. */
export function fetchModels(): Promise<ModelList> {
  return bridge().then((target) => target.chat_models())
}

export function pickModel(provider: string, model = ''): Promise<ModelList> {
  return bridge().then((target) => target.chat_pick(provider, model))
}

/** Save the thinking level and/or context size remembered for the current model. */
export function saveTuning(
  thinking?: string,
  turns?: number,
  tools?: boolean,
): Promise<TuningResult> {
  return bridge().then((target) => target.chat_tuning(thinking, turns, tools))
}

/** Add one model to a provider's list. A window edit, not a config.yaml write. */
export function addProviderModel(
  provider: string,
  modelId: string,
  label = '',
): Promise<ModelEditResult> {
  return bridge().then((target) => target.llm_add_model(provider, modelId, label))
}

/** Remove one model from a provider's list. The last row cannot be removed. */
export function removeProviderModel(provider: string, modelId: string): Promise<ModelEditResult> {
  return bridge().then((target) => target.llm_remove_model(provider, modelId))
}

/**
 * Tell Python whether this page is the output device -- and *which page* is asking.
 *
 * The role matters: the desktop figure also opens an audio channel, because her mouth
 * reads the same samples, and her "I can play" must not answer for the dashboard's "I
 * cannot". That is how an assistant goes silent while every indicator says it spoke.
 */
export function reportAudioReadiness(ok: boolean, reason: string): Promise<AudioOutputState> {
  return bridge().then((target) => target.audio_ready(ok, reason, pageMode))
}

/** Where the audio is going right now -- read after a reload, when the page's own
 *  memory of what it claimed is gone. */
export function fetchAudioOutput(): Promise<AudioOutputState> {
  return bridge().then((target) => target.audio_output())
}

/** Stop the assistant mid-sentence. */
export function requestSpeechStop(): Promise<{ stopped: boolean }> {
  return bridge().then((target) => target.speech_stop())
}

/** What the phone endpoint is doing: open or shut, where, and who is paired. */
export function fetchMobileState(): Promise<MobileState> {
  return bridge().then((target) => target.mobile_state())
}

/**
 * Open or close the listening port.
 *
 * The answer is the *state after* the attempt, not the request: a bind that failed
 * has to show as shut, or the panel would be claiming a network listener that is
 * not there.
 */
export function setMobileOpen(on: boolean): Promise<MobileState> {
  return bridge().then((target) => target.mobile_toggle(on))
}

/** Ask for a six-digit code for a phone to type. */
export function requestPairCode(): Promise<MobileCode> {
  return bridge().then((target) => target.mobile_pair_code())
}

export function revokeDevice(deviceId: string): Promise<{ revoked: boolean; devices: MobileDevice[] }> {
  return bridge().then((target) => target.mobile_revoke(deviceId))
}

export function revokeAllDevices(): Promise<{ revoked: number; devices: MobileDevice[] }> {
  return bridge().then((target) => target.mobile_revoke_all())
}

/** Measure whether a plaintext request is really refused, instead of asserting it. */
export function runMobileSelftest(): Promise<{ plaintext_refused: boolean; note: string }> {
  return bridge().then((target) => target.mobile_selftest())
}

/**
 * Where the phone mirror is. Cheap and side-effect free: the panel polls it every
 * second and a half while open, so nothing in here may shell out to ``adb``.
 */
export function fetchMirrorState(): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_state())
}

/** The phones ``adb`` can see. Slower (it runs adb), so only on open and on 刷新. */

/** 生图那一格的快照。轮询安全：服务端那一侧一个网络请求都不发。 */
export function fetchImageState(): Promise<ImageState> {
  return bridge().then((target) => target.image_state())
}

export function fetchVideoState(): Promise<VideoState> {
  return bridge().then((target) => target.video_state())
}
export function generateVideo(
  prompt: string,
  opts: { size?: string | null; fps?: number | null; seconds?: number | null; steps?: number | null } = {},
): Promise<VideoAnswer> {
  return bridge().then((target) =>
    target.video_generate(
      prompt,
      null,
      opts.steps ?? null,
      opts.size ?? null,
      opts.fps ?? null,
      opts.seconds ?? null,
    ),
  )
}
export function tuneVideo(
  size: string | null,
  fps: number | null,
  seconds: number | null,
  steps: number | null,
): Promise<VideoState> {
  return bridge().then((target) => target.video_tune(size, fps, seconds, steps))
}
export function probeVideoEngine(): Promise<VideoState> {
  return bridge().then((target) => target.video_probe())
}
export function setVideoEngine(engine: string): Promise<VideoState> {
  return bridge().then((target) => target.video_set_engine(engine))
}
export function saveVideoCloud(fields: Record<string, string>): Promise<VideoState> {
  return bridge().then((target) => target.video_cloud_save(fields))
}
export function resetVideoCloud(): Promise<VideoState> {
  return bridge().then((target) => target.video_cloud_reset())
}
export function artifactVideo(path: string): Promise<{ ok: boolean; data_url: string; error: string }> {
  return bridge().then((target) => target.artifact_video(path))
}

/** 真去问一次本机有没有 ComfyUI 在跑。只在按键时发，不进轮询。 */
export function probeImageEngine(): Promise<ImageState> {
  return bridge().then((target) => target.image_probe())
}

/** 画一张。慢是常态，所以调用方自己转圈，别挂在 1.5 秒的轮询上。 */
export function generateImage(prompt: string, size: string): Promise<ImageAnswer> {
  return bridge().then((target) => target.image_generate(prompt, size))
}

/** 档位、尺寸、步数、一次几张一次发齐（分开发会互相盖掉，投屏那两格第 95 轮就是这么坏的）。 */
export function tuneImage(
  engine: string,
  size: string,
  steps: number,
  count: number,
): Promise<ImageState> {
  return bridge().then((target) => target.image_tune(engine, size, steps, count))
}

/** 两个地址、一个目录、一个模型名。钥匙不在这儿 —— 它走设置那扇门，只进环境变量。 */
export function setupImage(
  comfyUrl: string,
  comfyDir: string,
  cloudBase: string,
  cloudModel: string,
): Promise<ImageState> {
  return bridge().then((target) => target.image_setup(comfyUrl, comfyDir, cloudBase, cloudModel))
}

export function fetchMirrorDevices(): Promise<MirrorDevices> {
  return bridge().then((target) => target.mirror_devices())
}

/**
 * 把这一轮画出的一张图读成 data URL。
 *
 * 页面不许自己碰盘：这一扇门只认软件产出的那几个目录，别的什么都回 `ok: false` 加一句
 * 原因 —— 而那句原因要画在屏上，不许进 console。调用方自己按路径缓存：一张图问一次。
 */
export function artifactRead(path: string): Promise<ArtifactPicture> {
  return bridge().then((target) => target.artifact_read(path))
}

/**
 * 后手那扇门：把一段字交给 Windows 剪贴板。
 *
 * 顺序是**先试** `api/clipboard.ts` 那条浏览器自己的路，它报了 `failed` 才走到这里；
 * 两边都说没成，屏上就得写着没成，而不是让按钮按下去什么都没发生。
 */
export function artifactStat(path: string): Promise<ArtifactStat> {
  return bridge().then((target) => target.artifact_stat(path))
}

export function copyTextViaBridge(text: string): Promise<ActionResult> {
  return bridge().then((target) => target.copy_text(text))
}

/** 把图片本身放进剪贴板（CF_DIB）。浏览器做不到这件事，所以这是唯一的一扇门。 */
export function copyImage(path: string): Promise<ActionResult> {
  return bridge().then((target) => target.copy_image(path))
}

/** 交给系统默认程序，或在资源管理器里选中它。桌面版没有"下载"这个动作 —— 文件本来就在盘上。 */
export function openArtifact(path: string, how: ArtifactHow = 'open'): Promise<ActionResult> {
  return bridge().then((target) => target.open_artifact(path, how))
}

/**
 * 有没有网。**只读那一侧的缓存**，所以可以按 10 秒问一次；它不是探测，探测在那条守护线程上。
 *
 * `online === null` 是"还没测过"，界面必须能把它和"没网"分开画。
 */
export function fetchNetworkState(): Promise<NetworkState> {
  return bridge().then((target) => target.network_state())
}

/** Open the scrcpy window. Empty serial means "the one phone that is connected". */
export function startMirror(serial: string): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_start(serial))
}

export function stopMirror(): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_stop())
}

/** ``adb connect`` to the address the phone's 无线调试 page is showing. */
export function connectMirrorWireless(address: string): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_connect(address))
}

/** ``adb pair`` with the six-digit code. Both are printed on the phone's dialog. */
export function pairMirrorWireless(address: string, code: string): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_pair(address, code))
}

/**
 * Remember the picture settings. One call carries all three because the panel always
 * knows the whole triple, and sending ``undefined`` for "leave that alone" is a shape
 * pywebview has no good answer for.
 */
export function tuneMirror(
  tier: string,
  audio: boolean,
  keepScreen: boolean,
  mode: string,
): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_tune(tier, audio, keepScreen, mode))
}

/**
 * ``adb tcpip 5555`` on a plugged-in phone, then connect to the address it reports.
 *
 * This is the only way onto WiFi for Android 10 and older, which have no 无线调试
 * pairing dialog at all.
 */
export function openMirrorWirelessPort(serial: string): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_tcpip(serial))
}

/**
 * Choose how big the scrcpy window is, and whether the picture stretches to fill it.
 *
 * 铺满 works on the open window; 全屏 is a launch flag, so the answer says when a
 * restart is what is actually left.
 */
export function displayMirrorWindow(windowMode: string, stretch: boolean): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_display(windowMode, stretch))
}

/**
 * Press one of the phone's own three buttons: 返回 / 主页 / 多任务.
 *
 * It changes the phone, so the answer says which phone it went to -- or why it could
 * not say. Never fire this when nothing is mirrored: the backend refuses, and the panel
 * shows that refusal instead of a silent nothing.
 */
export function pressMirrorKey(key: string): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_key(key))
}

/**
 * 读手机那一屏。回答里的 ``sources`` 是重点：哪条路走了、哪条没走、为什么没走，
 * 界面要能说出来，不然"看不到"和"没权限看"就成了一回事。
 */
export function readMirrorScreen(question = ''): Promise<MirrorScreenAnswer> {
  return bridge().then((target) => target.mirror_screen(question))
}

/** 那格「允许把这块屏发给能看图的模型」。传 undefined 是只读。 */
export function setMirrorScreenVision(allow: boolean): Promise<MirrorScreenAnswer> {
  return bridge().then((target) => target.mirror_screen_vision(allow))
}

/** Fling the mirrored phone: 'next' 往上滑（抖音下一条）、'prev' 往下滑。 */
export function swipeMirrorPhone(direction: string): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_swipe(direction))
}

/**
 * Sweep this machine's /24 for phones listening on the tcpip port and connect them.
 *
 * Takes a couple of seconds, so the panel shows 正在扫本网段… while it runs. It is a
 * button, never a timer.
 */
export function discoverMirrorPhones(): Promise<MirrorState> {
  return bridge().then((target) => target.mirror_discover())
}
