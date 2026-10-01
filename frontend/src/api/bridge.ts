/**
 * The only channel between the page and Python.
 *
 * Outside the desktop shell (plain browser during development) there is no
 * `pywebview`, so this falls back to a synthetic feed. That keeps `npm run dev`
 * usable without faking the whole backend — but the mock is clearly labelled so
 * nobody mistakes a browser screenshot for a real reading.
 */

import { ref } from 'vue'

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

export interface PetState {
  shown: boolean
  error?: string
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
}

export interface ChatTurn {
  role: 'user' | 'assistant'
  text: string
}

/** The live state Python pushes into the page; also pullable on mount. */
export interface UiSnapshot {
  voice_state: TurnPhase
  voice: VoicePhase
  voice_detail: string
  history: ChatTurn[]
  last_event: string
  interrupted: boolean
}

export interface DiskOutcome {
  deleted: string[]
  freed_bytes: number
  failed: [string, string][]
  log_path: string
  error: string
}

export interface ModelRow {
  name: string
  base_url: string
  model: string
  key_env: string
  key_set: boolean
  source: string
  edited: boolean
  current: boolean
}

export interface SettingsSnapshot {
  error: string
  providers: string[]
  models: ModelRow[]
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
  daily: UsageDay[]
}

/** One configured model the chat can be pointed at. */
export interface ModelChoice {
  provider: string
  model: string
  /** False means the environment variable named by ``key_variable`` is empty. */
  key_set: boolean
  key_variable: string
  current: boolean
}

export interface ModelList {
  error: string
  current: string
  choices: ModelChoice[]
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
}

export interface VoiceList {
  error: string
  engine: string
  current: string
  choices: VoiceChoice[]
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
  app_info(): Promise<AppInfo>
  disk_scan(): Promise<DiskPlan>
  disk_delete(items: JunkItem[], confirmed: boolean): Promise<DiskOutcome>
  process_kill(items: ProcessTarget[], confirmed: boolean): Promise<KillReport>
  disk_advice(): Promise<JunkAdvice>
  activity_log(): Promise<ActivityLog>
  tts_voices(): Promise<VoiceList>
  tts_pick(voice: string): Promise<VoiceList>
  tts_preview(voice: string): Promise<VoicePreview>
  computer_levels(): Promise<ComputerLevels>
  computer_set_tier(tier: number): Promise<ComputerLevels>
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
  pet_state(): Promise<PetState>
  pet_toggle(): Promise<PetState>
  pet_grip(rect: GripRect): Promise<{ ok: boolean }>
  pet_drag(dragging: boolean): Promise<{ ok: boolean }>
  pet_frame(png: string): Promise<{ ok: boolean }>
  shell_levels(): Promise<ShellLevels>
  shell_set_tier(tier: number): Promise<ShellLevels>
  chat_sessions(): Promise<SessionList>
  chat_new_session(): Promise<SessionList>
  chat_switch(sessionId: string): Promise<SessionList>
  chat_rename(sessionId: string, title: string): Promise<SessionList>
  chat_delete(sessionId: string): Promise<SessionList>
  chat_messages(sessionId: string): Promise<SessionMessages>
  voice_status(): Promise<VoiceStatus>
  voice_enable(): Promise<VoiceStatus>
  voice_mute(): Promise<VoiceStatus>
  voice_talk(): Promise<VoiceStatus>
  chat_ask(text: string, attachments: Attachment[]): Promise<ChatReply>
  chat_clear(): Promise<{ ok: boolean }>
  state_snapshot(): Promise<UiSnapshot>
  settings_get(): Promise<SettingsSnapshot>
  settings_apply(patch: Record<string, unknown>): Promise<SettingsSnapshot>
  usage_summary(days: number): Promise<UsageReport>
  chat_models(): Promise<ModelList>
  chat_pick(provider: string): Promise<ModelList>
  audio_ready(ok: boolean, reason: string): Promise<AudioOutputState>
  audio_output(): Promise<AudioOutputState>
  speech_stop(): Promise<{ stopped: boolean }>
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

/** A browser tab has no desktop to put a figure on, so the switch only remembers itself. */
let petShownMock = false

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
    }
  },
  async app_info() {
    return { name: '小夜', version: 'dev', engine: 'browser-mock', built: 'source' }
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
    const answer = '（浏览器预览模式的样例回答）我是小夜。桌面版里这句话来自真实模型。'
    // The desktop transcript is pushed from Python; the mock has to keep its own,
    // or the panel is empty in preview and nobody can tell that from a bug.
    mockHistory.push({ role: 'user', text }, { role: 'assistant', text: answer })
    return {
      question: text,
      answer: attachments.length ? `${answer}（收到 ${attachments.length} 个附件）` : answer,
      error: '',
    }
  },
  async chat_clear() {
    mockHistory.length = 0
    return { ok: true }
  },
  async state_snapshot() {
    return {
      voice_state: mockTurn,
      voice: mockVoice.status().phase,
      voice_detail: mockVoice.status().detail,
      history: [...mockHistory],
      last_event: 'mock',
      interrupted: false,
    }
  },
  async settings_get() {
    return { ...mockSettings }
  },
  async settings_apply(patch) {
    return applyMockSettings(patch)
  },
  async usage_summary(days) {
    return mockUsage(Math.min(Math.max(days, 1), 31))
  },
  async chat_models() {
    return mockModels()
  },
  async chat_pick(provider) {
    const list = mockModels()
    const picked = list.choices.find((entry) => entry.provider === provider)
    if (!picked) return { ...list, error: `未配置的模型：${provider}` }
    list.choices.forEach((entry) => {
      entry.current = entry.provider === provider
    })
    list.current = provider
    return list
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
    return {
      error: '',
      engine: 'edge_tts',
      current: 'zh-CN-XiaoxiaoNeural',
      choices: [
        { id: 'zh-CN-XiaoxiaoNeural', label: '晓晓 · 女声 · 温和（Edge 默认）', current: true },
        { id: 'zh-CN-YunxiNeural', label: '云希 · 男声 · 清亮', current: false },
        { id: 'zh-CN-XiaoyiNeural', label: '晓伊 · 女声 · 活泼', current: false },
      ],
    }
  },
  async tts_pick(voice: string) {
    const list = await this.tts_voices()
    return { ...list, current: voice, choices: list.choices.map((c) => ({ ...c, current: c.id === voice })) }
  },
  async tts_preview() {
    return { ok: true, error: '', voice: 'zh-CN-XiaoxiaoNeural', chunks: 4 }
  },
  async chat_sessions() {
    return { error: '', current: 'mock', sessions: [] }
  },
  async chat_new_session() {
    return { error: '', current: 'mock', sessions: [] }
  },
  async chat_switch() {
    return { error: '', current: 'mock', sessions: [] }
  },
  async chat_rename() {
    return { error: '', current: 'mock', sessions: [] }
  },
  async chat_delete() {
    return { error: '', current: 'mock', sessions: [] }
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
  async pet_state() {
    return { shown: petShownMock }
  },
  async pet_toggle() {
    petShownMock = !petShownMock
    return { shown: petShownMock }
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

/** Two preview models, one of them deliberately missing its key, so the picker's
 *  "不可用" state is something the browser pass can actually show. */
function mockModels(): ModelList {
  return {
    error: '',
    current: 'qwenai',
    choices: [
      { provider: 'qwenai', model: 'qwen3.8-flash', key_set: false, key_variable: 'QWENAI_API_KEY', current: true },
      { provider: 'wkapi', model: 'gpt-5.6-sol', key_set: false, key_variable: 'WKAPI_API_KEY', current: false },
    ],
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
      key_env: 'WKAPI_API_KEY',
      key_set: true,
      source: '界面添加',
      edited: false,
      current: false,
    },
  ],
  provider: 'qwenai',
  base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
  model: 'qwen3.8-flash',
  api_key_variable: 'QWENAI_API_KEY',
  api_key_set: false,
  overrides_active: [],
  voice_auto_arm: true,
  auto_speak_typed: true,
  telemetry_interval_ms: 1500,
}

function applyMockSettings(patch: Record<string, unknown>): SettingsSnapshot {
  const problems: Record<string, string> = {}
  const applied: Record<string, unknown> = {}
  const next = { ...mockSettings, problems, applied }
  for (const [key, value] of Object.entries(patch)) {
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
    if (key === 'base_url' && !/^https?:\/\/.+/.test(String(value))) {
      problems[key] = '地址必须以 http:// 或 https:// 开头'
      continue
    }
    if (key === 'model' && /\s/.test(String(value))) {
      problems[key] = '模型名不能含空格'
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

/** Preview-only usage numbers. Labelled as samples wherever it is rendered. */
function mockUsage(days: number): UsageReport {
  const perDay: UsageDay[] = []
  const now = new Date()
  for (let i = days - 1; i >= 0; i -= 1) {
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
  return {
    error: '',
    summary: {
      days,
      since: perDay[0]?.day ?? '',
      until: perDay[perDay.length - 1]?.day ?? '',
      calls: perDay.length * 6,
      prompt_tokens: prompt,
      completion_tokens: completion,
      total_tokens: prompt + completion,
      cached_tokens: 0,
      cache_hit_percent: null,
      cache_data_reported: false,
      calls_without_cache_data: perDay.length * 6,
      avg_latency_ms: 900,
    },
    daily: perDay,
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

export function fetchComputerLevels(): Promise<ComputerLevels> {
  return bridge().then((target) => target.computer_levels())
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

export function fetchPetState(): Promise<PetState> {
  return bridge().then((target) => target.pet_state())
}

/** Show or hide the desktop pet. Python remembers the choice across launches. */
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
export function pushPetFrame(png: string): Promise<{ ok: boolean }> {
  return bridge().then((target) => target.pet_frame(png))
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

export function pickModel(provider: string): Promise<ModelList> {
  return bridge().then((target) => target.chat_pick(provider))
}

/** Tell Python whether this page is the output device. */
export function reportAudioReadiness(ok: boolean, reason: string): Promise<AudioOutputState> {
  return bridge().then((target) => target.audio_ready(ok, reason))
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
