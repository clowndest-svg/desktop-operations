/**
 * 手机端读到的电脑数据，形状写在这里。
 *
 * 为什么不直接 import 桌面那份桥面类型：那个文件是桌面桥面的门面，
 * 里面每一个函数都要摸桌面壳注进来的那个 api 对象。手机上没有这个东西，一旦 import 进来，
 * 打包装进去的是"永远调不通的桌面代码"，而界面上看起来一切正常。
 * 所以这边只抄**形状**，并且由 `scripts/verify_phone_contract.py` 拿着真回答核对一次：
 * 手机读的键，电脑必须真的给。抄了不核，就是等着漂移。
 */

export interface PcMetrics {
  cpu: { percent: number; cores: number }
  memory: { percent: number; total_bytes: number; used_bytes: number }
  disks: { mount: string; percent: number; free_bytes: number }[]
  uptime_seconds: number
}

export interface PcSnapshot {
  metrics: PcMetrics
  warnings: string[]
  error: string
}

export interface PcReminder {
  job_id: string
  text: string
  when: string
}

export interface PcReminders {
  rows: PcReminder[]
  channels: string[]
  error: string
}

export interface PcActivityEntry {
  at: number
  tool: string
  ok: boolean
  detail: string
}

export interface PcActivityLog {
  entries: PcActivityEntry[]
  error: string
}

export interface PcMemoryRow {
  id: number
  kind: string
  text: string
}

export interface PcMemories {
  rows: PcMemoryRow[]
  error: string
}

/** 电脑上的历史会话。手机不存第二份真相，要看历史就是看电脑那一份。 */
export interface PcSessionRow {
  id: string
  title: string
  updated_at: string
  turns: number
}

export interface PcSessionList {
  sessions: PcSessionRow[]
  current: string
  error: string
}

/**
 * 电脑上存的一轮。字段名是 Python 那边定的（`transcript_service._turn`），不是手机这边起的：
 * 键是 `content` 不是 `text` —— 上一版这里写成 `text`，于是从电脑上翻开历史会话时
 * 每一条气泡都是空的，看着像"历史没了"，其实读错了字段。
 */
export interface PcTurn {
  role: string
  content: string
  at: string
}

export interface PcMessageList {
  messages: PcTurn[]
  current: string
  error: string
}

/** 电脑上的知识库。只读：入库和删除都要回电脑上做。 */
export interface PcKnowledgeDoc {
  doc_id: string
  source: string
  title: string
  media_type: string
  size_bytes: number
  chunk_count: number
  ingested_at: string
}

export interface PcKnowledgeStats {
  running: boolean
  enabled: boolean
  documents: number
  chunks: number
  vector_records: number
}

export interface PcKnowledge {
  documents: PcKnowledgeDoc[]
  stats: PcKnowledgeStats
  error: string
}

/**
 * 电脑上配好的服务商，以及它下面的模型。
 *
 * `key_set` 是"这个 provider 的环境变量里有没有东西"，**不是**那串东西本身 ——
 * 电脑端只回变量的名字，从不回值，手机这边也不该有地方能装下它。
 */
export interface PcModelSpec {
  id: string
  label: string
}

export interface PcProviderChoice {
  name: string
  base_url: string
  models: PcModelSpec[]
  default_model: string
  key_set: boolean
  key_variable: string
  /** True = answers without a credential (local server); the phone must not call it unconfigured. */
  key_optional?: boolean
  timeout_seconds?: number | null
  current: boolean
}

export interface PcModels {
  error: string
  providers: PcProviderChoice[]
  provider: string
  model: string
}

/**
 * 手机上做过的一笔动作（台账里的一行）。
 *
 * 形状放在 types.ts 而不是 agent.ts：`api.ts` 要存它，`agent.ts` 要写它，
 * 而 agent 本来就 import api —— 类型放在下游会绕成一个循环引用。
 */
export interface ActionRow {
  at: number
  name: string
  ok: boolean
  detail: string
  /** 这件事会不会离开这台手机（拨号、发短信、开链接）。 */
  outbound: boolean
}

/** 手机读电脑时真正用到的键。核对脚本以这份清单为准。 */
export const READ_KEYS: Record<string, string[]> = {
  snapshot: ['metrics', 'cpu', 'memory', 'disks', 'uptime_seconds', 'warnings', 'error'],
  reminders: ['rows', 'channels', 'error', 'job_id', 'text', 'when'],
  activity_log: ['entries', 'error', 'at', 'tool', 'ok', 'detail'],
  memory_list: ['rows', 'error', 'id', 'kind', 'text'],
  chat_sessions: ['sessions', 'current', 'error', 'id', 'title', 'turns'],
  chat_messages: ['messages', 'current', 'error', 'role', 'content'],
  knowledge_state: ['documents', 'stats', 'error', 'doc_id', 'source', 'title', 'chunk_count'],
  chat_models: ['error', 'providers', 'provider', 'model', 'models', 'key_set', 'key_optional', 'timeout_seconds'],
}
