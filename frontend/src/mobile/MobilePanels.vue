<template>
  <div class="pb">
    <p v-if="error" class="pb__error">{{ error }}</p>

    <section class="card">
      <h3>这台电脑</h3>
      <template v-if="snapshot">
        <div v-for="row in gauges" :key="row.label" class="gauge">
          <div class="gauge__top">
            <span class="gauge__label">{{ row.label }}</span>
            <span class="gauge__value">{{ row.value }}</span>
          </div>
          <div class="gauge__track">
            <div class="gauge__fill" :class="row.tone" :style="{ width: `${row.percent}%` }" />
          </div>
          <span class="gauge__note">{{ row.note }}</span>
        </div>
      </template>
      <p v-else-if="loading" class="pb__dim">正在读这台机器的状态…</p>
      <p v-else class="pb__dim">没读到遥测</p>
      <p v-for="warn in snapshot?.warnings ?? []" :key="warn" class="pb__warn">{{ warn }}</p>
    </section>

    <section class="card">
      <h3>提醒</h3>
      <ul v-if="reminders && reminders.rows.length" class="pb__list">
        <li v-for="row in reminders.rows" :key="row.job_id">
          <strong>{{ row.when }}</strong>
          <span>{{ row.text }}</span>
        </li>
      </ul>
      <p v-else-if="!loading" class="pb__dim">电脑上还没有排好的提醒</p>
      <label class="field">
        <span>提醒什么</span>
        <input v-model="draft.text" type="text" placeholder="喝水" />
      </label>
      <label class="field">
        <span>什么时候</span>
        <input v-model="draft.when" type="text" placeholder="10 分钟后" />
      </label>
      <button class="wide" type="button" :disabled="busy || !draft.text.trim()" @click="add">
        加一条
      </button>
      <p v-if="note" class="pb__note">{{ note }}</p>
      <p class="pb__dim">
        手机上只能<strong>新建</strong>提醒。取消和删除要回电脑上做 —— 这条红线两边一致。
      </p>
    </section>

    <section class="card">
      <h3>她记得的</h3>
      <ul v-if="memories && memories.rows.length" class="pb__list">
        <li v-for="row in memories.rows" :key="row.id">
          <strong>{{ row.kind }}</strong>
          <span>{{ row.text }}</span>
        </li>
      </ul>
      <p v-else-if="!loading" class="pb__dim">还没有记住什么</p>
      <p class="pb__dim">忘掉某一条要回电脑上点：手机上不给删除入口。</p>
    </section>

    <section class="card">
      <h3>知识库</h3>
      <p v-if="knowledge" class="pb__stats">
        <span>{{ knowledge.stats.documents ?? 0 }} 份文档</span>
        <span>{{ knowledge.stats.chunks ?? 0 }} 个片段</span>
        <span>{{ knowledge.stats.vector_records ?? 0 }} 条向量</span>
      </p>
      <ul v-if="knowledge && knowledge.documents.length" class="pb__list">
        <li v-for="doc in knowledge.documents.slice(0, 6)" :key="doc.doc_id">
          <strong>{{ doc.chunk_count }} 段</strong>
          <span>{{ doc.title || doc.source }}</span>
        </li>
      </ul>
      <p v-else-if="!loading" class="pb__dim">电脑上的知识库还是空的，入库要回电脑上做。</p>
    </section>

    <section class="card">
      <h3>当前模型</h3>
      <p v-if="models && models.model" class="pb__now">
        电脑上的她在用 {{ models.provider }} · {{ models.model }}
      </p>
      <ul v-if="models && models.providers.length" class="pb__list">
        <li v-for="row in models.providers" :key="row.name">
          <strong>{{ row.name }}</strong>
          <span>
            {{ row.models.length }} 个模型 · 起始 {{ row.default_model
            }}{{ row.key_set ? '' : row.key_optional ? '（免 Key）' : '（没配 Key，问不了）' }}
          </span>
        </li>
      </ul>
      <p v-else-if="!loading" class="pb__dim">没读到服务商列表</p>
      <p class="pb__dim">换模型要回电脑上点：手机上不给切。</p>
    </section>

    <section class="card">
      <h3>最近动作</h3>
      <ul v-if="activity && activity.entries.length" class="pb__list">
        <li v-for="(entry, index) in activity.entries.slice(0, 12)" :key="index">
          <strong>{{ entry.tool }}</strong>
          <span>{{ entry.ok ? '成功' : '没成' }} · {{ entry.detail }}</span>
        </li>
      </ul>
      <p v-else-if="!loading" class="pb__dim">她还没在这台电脑上动过手</p>
    </section>

    <button class="wide" type="button" :disabled="loading" @click="load">刷新</button>
  </div>
</template>

<script setup lang="ts">
/**
 * 手机上看到的电脑：遥测、提醒、记忆、知识库、模型、动作台账。
 *
 * 每一块都是电脑界面上早就有的东西，只是换个入口——手机这边**不新增能力面**。
 * 唯一的写操作是"新建提醒"；取消、删除、改档位、开麦克风、换模型都没有做入口，
 * 不是忘了做，是刻意不做（`lan_server.ALLOWED_METHODS` 是同一件事的另一半）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { pcCall, type PcLink } from './api'
import type {
  PcActivityLog,
  PcKnowledge,
  PcMemories,
  PcModels,
  PcReminders,
  PcSnapshot,
} from './types'

const props = defineProps<{ link: PcLink }>()

const snapshot = ref<PcSnapshot | null>(null)
const reminders = ref<PcReminders | null>(null)
const memories = ref<PcMemories | null>(null)
const activity = ref<PcActivityLog | null>(null)
const knowledge = ref<PcKnowledge | null>(null)
const models = ref<PcModels | null>(null)
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const note = ref('')
const draft = reactive({ text: '', when: '' })

interface Gauge {
  label: string
  value: string
  note: string
  percent: number
  tone: string
}

function round(value: number | undefined): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value) : 0
}

function gibs(bytes: number | undefined): string {
  return typeof bytes === 'number' ? (bytes / 1024 ** 3).toFixed(1) : '—'
}

/**
 * 三档色：正常、偏高、爆表。
 *
 * 90% 以上才变红是有意的 —— 把 60% 也涂成黄色，屏幕上就永远有黄色，
 * 那时候红色也不再意味着"去看一眼"。
 */
function tone(percent: number): string {
  if (percent >= 90) return 'is-bad'
  if (percent >= 75) return 'is-warm'
  return ''
}

const gauges = computed<Gauge[]>(() => {
  const metrics = snapshot.value?.metrics
  if (!metrics) return []
  const rows: Gauge[] = [
    {
      label: 'CPU',
      value: `${round(metrics.cpu.percent)}%`,
      note: `${metrics.cpu.cores} 核`,
      percent: round(metrics.cpu.percent),
      tone: tone(round(metrics.cpu.percent)),
    },
    {
      label: '内存',
      value: `${round(metrics.memory.percent)}%`,
      note: `共 ${gibs(metrics.memory.total_bytes)} GiB`,
      percent: round(metrics.memory.percent),
      tone: tone(round(metrics.memory.percent)),
    },
    {
      label: '已运行',
      value: `${(metrics.uptime_seconds / 86400).toFixed(2)} 天`,
      note: '开机到现在',
      percent: 0,
      tone: '',
    },
  ]
  for (const disk of metrics.disks) {
    rows.push({
      label: `磁盘 ${disk.mount}`,
      value: `${round(disk.percent)}%`,
      note: `剩 ${gibs(disk.free_bytes)} GiB`,
      percent: round(disk.percent),
      tone: tone(round(disk.percent)),
    })
  }
  return rows
})

async function grab<T>(method: string): Promise<T | null> {
  try {
    return await pcCall<T>(props.link, method)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    return null
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  const [telemetry, board, remembered, logged, docs, picked] = await Promise.all([
    grab<PcSnapshot>('snapshot'),
    grab<PcReminders>('reminders'),
    grab<PcMemories>('memory_list'),
    grab<PcActivityLog>('activity_log'),
    grab<PcKnowledge>('knowledge_state'),
    grab<PcModels>('chat_models'),
  ])
  snapshot.value = telemetry
  reminders.value = board
  memories.value = remembered
  activity.value = logged
  knowledge.value = docs
  models.value = picked
  loading.value = false
}

async function add(): Promise<void> {
  busy.value = true
  note.value = ''
  try {
    const written = await pcCall<{ ok?: boolean; error?: string; row?: { when?: string } }>(
      props.link,
      'reminder_add',
      { text: draft.text.trim(), when: draft.when.trim() || '5 分钟后' },
    )
    if (written.error) {
      note.value = written.error
    } else {
      note.value = `已排好：${written.row?.when ?? draft.when}`
      draft.text = ''
      draft.when = ''
      reminders.value = await pcCall<PcReminders>(props.link, 'reminders')
    }
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.pb {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-bottom: 8px;
}

.card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border-radius: var(--xy-r-lg);
  background: var(--xy-card);
  border: 1px solid var(--xy-line);
}

.card h3 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--xy-text-2);
}

.gauge {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.gauge__top {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.gauge__label {
  font-size: 13px;
}

.gauge__value {
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  color: var(--xy-text-2);
}

.gauge__track {
  height: 6px;
  border-radius: var(--xy-pill);
  background: var(--xy-card-2);
  overflow: hidden;
}

.gauge__fill {
  height: 100%;
  border-radius: var(--xy-pill);
  background: var(--xy-accent);
  transition: width 0.3s ease;
}

.gauge__fill.is-warm {
  background: var(--xy-warn);
}

.gauge__fill.is-bad {
  background: var(--xy-danger);
}

.gauge__note {
  font-size: 11px;
  color: var(--xy-text-3);
}

.pb__stats {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin: 0;
  font-size: 13px;
}

.pb__now {
  margin: 0;
  font-size: 14px;
}

.pb__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.pb__list li {
  display: grid;
  grid-template-columns: 84px 1fr;
  gap: 8px;
  font-size: 13px;
  line-height: 1.5;
}

.pb__list strong {
  color: var(--xy-text-2);
  font-weight: 500;
}

.pb__list span {
  min-width: 0;
  overflow-wrap: anywhere;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.field span {
  font-size: 12px;
  color: var(--xy-text-3);
}

.field input {
  padding: 12px 14px;
  border-radius: var(--xy-r-sm);
  border: 1px solid var(--xy-line);
  background: var(--xy-bg-soft);
  color: var(--xy-text);
}

.wide {
  width: 100%;
  padding: 12px 16px;
  border-radius: var(--xy-r-md);
  border: 1px solid var(--xy-line);
  background: var(--xy-card-2);
  color: var(--xy-text);
  font-size: 14px;
}

.wide:disabled {
  opacity: 0.45;
}

.pb__dim {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--xy-text-3);
}

.pb__warn {
  margin: 0;
  font-size: 12px;
  color: var(--xy-warn);
}

.pb__note {
  margin: 0;
  font-size: 12px;
  color: var(--xy-ok);
}

.pb__error {
  margin: 0;
  font-size: 12px;
  color: var(--xy-danger);
}
</style>
