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

export interface PetState {
  shown: boolean
  error?: string
}

/** One phone this machine will answer. */
export interface MobileDevice {
  device_id: string
  name: string
  added_at: number
  last_seen: number
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

export interface ModelRow {
  name: string
  base_url: string
  model: string
  models: ModelSpec[]
  default_model: string
  key_env: string
  key_set: boolean
  source: string
  edited: boolean
  current: boolean
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
  /** Past exchanges replayed into each request. */
  history_turns: number
  history_turns_bounds: number[]
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
  current: boolean
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
  /** Past exchanges replayed for the current pair. */
  turns: number
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
  vote: '并行三答·互评投票',
}

/**
 * 这一单会花几次请求。和后端 `requests_for()` 同一条算式，两份各写一次，
 * 因为页面上那个数字是用户点下去之前唯一能看到的价格。
 */
export function requestsFor(mode: CollaborationMode, seats: number, rounds = 2): number {
  if (seats <= 0) return 0
  if (mode === 'boss') return seats + 1
  if (mode === 'vote') return seats * 2
  if (mode === 'table') return seats * (Math.max(1, rounds) + 1)
  return seats
}

/** The verdict of changing one of the two per-model knobs. */
export interface TuningResult {
  ok: boolean
  error: string
  thinking: string
  turns: number
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
  pet_grip(rect: GripRect): Promise<{ ok: boolean }>
  pet_drag(dragging: boolean): Promise<{ ok: boolean }>
  pet_frame(png: string): Promise<{ ok: boolean }>
  pet_arrived(): Promise<{ ok: boolean; error?: string }>
  shell_levels(): Promise<ShellLevels>
  shell_set_tier(tier: number): Promise<ShellLevels>
  mobile_state(): Promise<MobileState>
  mobile_toggle(on: boolean): Promise<MobileState>
  mobile_pair_code(): Promise<MobileCode>
  mobile_revoke(deviceId: string): Promise<{ revoked: boolean; devices: MobileDevice[] }>
  mobile_revoke_all(): Promise<{ revoked: number; devices: MobileDevice[] }>
  mobile_selftest(): Promise<{ plaintext_refused: boolean; note: string }>
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
  usage_summary(days: number): Promise<UsageReport>
  chat_models(): Promise<ModelList>
  chat_pick(provider: string, model?: string): Promise<ModelList>
  chat_tuning(thinking?: string, turns?: number): Promise<TuningResult>
  llm_add_model(provider: string, modelId?: string, label?: string): Promise<ModelEditResult>
  llm_remove_model(provider: string, modelId?: string): Promise<ModelEditResult>
  audio_ready(ok: boolean, reason: string, role: string): Promise<AudioOutputState>
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
      reasoning: '',
    }
  },
  async chat_send(text, attachments = [], conversation = '', _provider = '', model = '') {
    // The preview browser has no Python behind it, so the mock answers on a timer: the
    // question lands at once and the answer arrives a beat later, which is the least
    // this can do without pretending a fake can stream.
    const answer = attachments.length
      ? `（浏览器预览模式的样例回答）我是小夜。带 ${attachments.length} 个附件，桌面版里这句话来自真实模型。`
      : '（浏览器预览模式的样例回答）我是小夜。桌面版里这句话来自真实模型。'
    mockHistory.push({ role: 'user', text })
    window.setTimeout(() => {
      mockHistory.push({ role: 'assistant', text: answer, model: model || 'mock' })
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
    return { ok: true }
  },
  async chat_cancel() {
    return { ok: false, error: '预览模式的样例回答不是一轮真任务，没有可停的东西' }
  },
  async chat_collaborate(text, seats, _rounds = 2, conversation = '', mode = 'table') {
    // One seat's worth of sample text, and no claim that a table ran: the preview has no
    // model behind it, and a mock that fakes three opinions reads as the feature working.
    const answer = `（浏览器预览模式）${MODE_LABELS[mode]} 要真的模型，这里一个也没有。`
    mockHistory.push({ role: 'user', text })
    window.setTimeout(() => {
      mockHistory.push({ role: 'assistant', text: answer, record: '' })
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
    return {
      conversations: [
        {
          id: 'hud',
          title: '当前对话',
          status: 'idle',
          phase: '',
          provider: '',
          model: '',
          turns: mockHistory.length,
          active: true,
          task_id: '',
          error: '',
        },
      ],
      error: '预览模式只有一个样例对话',
    }
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
    return {
      voice_state: mockTurn,
      voice: mockVoice.status().phase,
      voice_detail: mockVoice.status().detail,
      history: [...mockHistory],
      last_event: 'mock',
      interrupted: false,
      conversations: [],
      streaming: null,
    }
  },
  async settings_get() {
    return { ...mockSettings }
  },
  async settings_apply(patch) {
    return applyMockSettings(patch)
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
  async chat_tuning(thinking, turns) {
    const list = mockModels()
    const current = { thinking: list.thinking, turns: list.turns }
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
    const id = String(voice_id || '')
    const at = mockVoiceChoices.findIndex((row) => row.id === id && row.kind === 'clone')
    if (at < 0) return { error: '没有这个音色：' + id, voices: mockVoiceChoices.filter((row) => row.kind === 'clone') }
    mockVoiceChoices.splice(at, 1)
    return { error: '', voices: mockVoiceChoices.filter((row) => row.kind === 'clone') }
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
  async pet_arrived() {
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
const mockTunings: Record<string, { thinking: string; turns: number }> = {}

/** Preview-only selection, so a pick in the browser pass sticks across panels. */
const mockPicked = { provider: 'qwenai', model: 'qwen3.8-flash' }

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
]

function mockTuning(key: string): { thinking: string; turns: number } {
  return mockTunings[key] ?? { thinking: 'medium', turns: 10 }
}

function mockModels(): ModelList {
  return {
    error: '',
    providers: mockProviders.map((row) => ({ ...row, models: [...row.models] })),
    provider: mockPicked.provider,
    model: mockPicked.model,
    thinking: mockTuning(`${mockPicked.provider}\u0000${mockPicked.model}`).thinking,
    turns: mockTuning(`${mockPicked.provider}\u0000${mockPicked.model}`).turns,
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
  overrides_active: [],
  voice_auto_arm: true,
  auto_speak_typed: true,
  telemetry_interval_ms: 1500,
  thinking_enabled: false,
  thinking_budget: 2048,
  thinking_budget_bounds: [64, 16000],
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
    const model = String(row.model ?? '').trim()
    if (!/^[a-z0-9_-]{1,32}$/.test(name)) return '模型名字只能是小写字母、数字、- 和 _，1-32 位'
    if (!/^https?:\/\//.test(url)) return `${name}：地址必须以 http:// 或 https:// 开头`
    if (!model || /\s/.test(model) || model.length > 150) {
      return `${name}：模型名不能含空格且不超过 150 字符`
    }
    if (known.has(name)) return `已经有叫 ${name} 的模型；要改它就在那一行上改`
    known.add(name)
    fresh.push({
      name,
      base_url: url,
      model,
      models: [{ id: model, label: model }],
      default_model: model,
      key_env: `${name.toUpperCase().replace(/-/g, '_')}_API_KEY`,
      key_set: Boolean(String(row.api_key ?? '').trim()),
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
export function saveTuning(thinking?: string, turns?: number): Promise<TuningResult> {
  return bridge().then((target) => target.chat_tuning(thinking, turns))
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
