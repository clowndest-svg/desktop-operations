<template>
  <div v-if="open" class="assistant" role="dialog" aria-modal="true" aria-label="助手能力">
    <div class="assistant__scrim" @click="emit('close')"></div>

    <section class="assistant__box">
      <header class="assistant__head">
        <h2 class="hud-title">助手 · ASSISTANT</h2>
        <div class="assistant__tabs">
          <button
            v-for="entry in TABS"
            :key="entry.id"
            class="hud-btn"
            :class="{ 'hud-btn--primary': entry.id === tab }"
            type="button"
            @click="select(entry.id)"
          >
            {{ entry.label }}
          </button>
          <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
        </div>
      </header>

      <!-- ======================================================= 提醒 -->
      <template v-if="tab === 'reminders'">
        <p v-if="notice" class="assistant__note" :class="{ 'assistant__note--bad': noticeBad }">{{ notice }}</p>
        <!--
          A reminder with nothing to announce with is a note in a drawer. The
          channels are reported by Python rather than guessed from the page, which
          cannot see whether the microphone loaded or the tray came up.
        -->
        <p v-if="channels.length !== 2" class="assistant__warn">
          播报通道：{{ channels.length ? channels.join(' · ') : '无（到点不会出声，也不会弹托盘）' }}
        </p>

        <form class="assistant__form" @submit.prevent="submitReminder">
          <input v-model="newText" class="hud-field" placeholder="提醒我什么" />
          <input v-model="newWhen" class="hud-field assistant__when" placeholder="十分钟后 / 明天 9 点半" />
          <button class="hud-btn hud-btn--primary" type="submit" :disabled="adding">
            {{ adding ? '写入中…' : '添加' }}
          </button>
        </form>

        <p v-if="!rows.length" class="assistant__empty">还没有提醒。说一句「十分钟后提醒我喝水」也一样。</p>
        <ul v-else class="assistant__list">
          <li v-for="row in rows" :key="row.job_id" class="assistant__row" :class="{ 'assistant__row--off': !row.enabled }">
            <span class="assistant__time hud-num">{{ row.when }}</span>
            <span class="assistant__text">{{ row.text }}</span>
            <span v-if="row.enabled && row.next_run" class="hud-chip assistant__next">下次 {{ row.next_run }}</span>
            <span v-else-if="!row.enabled" class="hud-chip">已停/已响</span>
            <button class="hud-btn" type="button" @click="flip(row)">
              {{ row.enabled ? '暂停' : '恢复' }}
            </button>
            <button class="hud-btn" type="button" @click="remove(row.job_id)">取消</button>
          </li>
        </ul>
      </template>

      <!-- ======================================================== 记忆 -->
      <template v-else-if="tab === 'memory'">
        <p v-if="notice" class="assistant__note" :class="{ 'assistant__note--bad': noticeBad }">{{ notice }}</p>
        <div class="assistant__headrow">
          <span class="hud-label">它记住的关于你的条目 · {{ memories.length }}</span>
          <button v-if="!confirmWipe" class="hud-btn" type="button" :disabled="!memories.length" @click="confirmWipe = true">
            全部忘掉
          </button>
          <template v-else>
            <span class="assistant__warn">这会删掉 {{ memories.length }} 条记忆，磁盘上的对话历史不受影响</span>
            <button class="hud-btn hud-btn--primary" type="button" @click="wipe">确认忘掉</button>
            <button class="hud-btn" type="button" @click="confirmWipe = false">算了</button>
          </template>
        </div>
        <p v-if="!memories.length" class="assistant__empty">
          还没有记忆。对话里出现"记住…"这类话时它会自己写，也可以在对话里直接说。
        </p>
        <ul v-else class="assistant__list">
          <li v-for="row in memories" :key="row.memory_id" class="assistant__row">
            <span class="hud-chip">{{ KINDS[row.kind] ?? row.kind }}</span>
            <span class="assistant__text">{{ row.content }}</span>
            <span class="hud-label assistant__meta">来自 {{ row.source || '未知' }}</span>
            <button class="hud-btn" type="button" @click="forget(row.memory_id)">忘掉</button>
          </li>
        </ul>
      </template>

      <!-- ===================================================== 知识库 -->
      <template v-else-if="tab === 'knowledge'">
        <p v-if="notice" class="assistant__note" :class="{ 'assistant__note--bad': noticeBad }">{{ notice }}</p>
        <div class="assistant__headrow">
          <span class="hud-label">
            文档 {{ docs.length }} · 分块 {{ String(stats.chunks ?? 0) }} · 记录 {{ String(stats.records ?? 0) }}
          </span>
        </div>
        <form class="assistant__form" @submit.prevent="addDoc">
          <input v-model="newPath" class="hud-field" placeholder="文件或文件夹的完整路径" />
          <button class="hud-btn hud-btn--primary" type="submit" :disabled="ingesting">
            {{ ingesting ? '读取中…' : '加入知识库' }}
          </button>
        </form>
        <p class="assistant__hint">
          只读取内容建索引，<b>不会移动或删除磁盘上的文件</b>；下面的"忘掉"也只删索引里的记录。
        </p>

        <p v-if="!docs.length" class="assistant__empty">知识库是空的。</p>
        <ul v-else class="assistant__list">
          <li v-for="doc in docs" :key="String(doc.doc_id)" class="assistant__row">
            <span class="assistant__text">{{ String(doc.title || doc.source || doc.doc_id) }}</span>
            <span class="hud-label assistant__meta">{{ String(doc.chunks ?? '') }} 块</span>
            <button class="hud-btn" type="button" @click="forgetDoc(String(doc.doc_id))">忘掉</button>
          </li>
        </ul>

        <form class="assistant__form" @submit.prevent="probe">
          <input v-model="probeQuery" class="hud-field" placeholder="试试它会检索到什么" />
          <button class="hud-btn" type="submit">命中测试</button>
        </form>
        <ul v-if="hits.length" class="assistant__list">
          <li v-for="(hit, index) in hits" :key="index" class="assistant__row assistant__row--hit">
            <span class="hud-chip">{{ Number(hit.score ?? 0).toFixed(2) }}</span>
            <span class="assistant__text">{{ String(hit.text ?? '').slice(0, 160) }}</span>
          </li>
        </ul>
        <p v-else-if="probed" class="assistant__empty">没有命中任何片段。</p>
      </template>

      <!-- ===================================================== 自动化 -->
      <template v-else>
        <p v-if="notice" class="assistant__note" :class="{ 'assistant__note--bad': noticeBad }">{{ notice }}</p>

        <div class="assistant__headrow">
          <span class="hud-label">
            定时任务 {{ num(overview.scheduler, 'jobs') }}（启用 {{ num(overview.scheduler, 'enabled_jobs') }}）
            · 工作流 {{ num(overview.workflow, 'definitions') }}
            · 执行 {{ num(overview.scheduler, 'runs') }} 次 · 失败 {{ num(overview.scheduler, 'failures') }}
          </span>
          <button class="hud-btn" type="button" @click="load('automation')">刷新</button>
        </div>

        <!-- ------------------------------------------------ 定时任务 -->
        <h3 class="hud-label assistant__section">定时任务</h3>
        <p class="assistant__hint">
          提醒归「提醒」页签管，这里只管其余的定时任务（带 <code>trigger: cron</code> 的工作流会出现在这里）。
        </p>
        <p v-if="!jobs.length" class="assistant__empty">还没有别的定时任务。</p>
        <ul v-else class="assistant__list">
          <li
            v-for="row in jobs"
            :key="row.job_id"
            class="assistant__row"
            :class="{ 'assistant__row--off': !row.enabled }"
          >
            <span class="hud-chip">{{ row.trigger }}</span>
            <span class="assistant__text">{{ row.name }}</span>
            <span v-if="row.enabled && row.next_run" class="hud-label assistant__meta">下次 {{ row.next_run }}</span>
            <span v-else-if="!row.enabled" class="hud-chip">已停</span>
            <!--
              The last outcome is on the row rather than behind a click: "it has been
              failing every night" is the one thing a schedule list exists to say.
            -->
            <span
              v-if="row.last"
              class="hud-chip"
              :class="{ 'assistant__chip--bad': !row.last.ok }"
              :title="row.last.detail || row.last.error"
            >
              {{ row.last.ok ? '上次成功' : '上次失败' }}
            </span>
            <button class="hud-btn" type="button" @click="flipJob(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </button>
            <button class="hud-btn" type="button" @click="tryJob(row.job_id)">试跑</button>
            <button class="hud-btn" type="button" @click="dropJob(row.job_id)">删除</button>
          </li>
        </ul>

        <!-- -------------------------------------------------- 工作流 -->
        <h3 class="hud-label assistant__section">工作流</h3>
        <div class="assistant__headrow">
          <span class="assistant__hint">定义放在数据目录的 workflows 文件夹里。</span>
          <button class="hud-btn" type="button" @click="reloadWf">重新读取</button>
        </div>
        <p v-if="!workflows.length" class="assistant__empty">还没有工作流定义。</p>
        <ul v-else class="assistant__list">
          <li v-for="row in workflows" :key="row.name" class="assistant__row">
            <span class="hud-chip">{{ row.trigger }}</span>
            <span class="assistant__text">{{ row.name }}</span>
            <span class="hud-label assistant__meta">{{ row.steps.length }} 步</span>
            <button class="hud-btn" type="button" @click="tryWorkflow(row.name)">跑一次</button>
          </li>
        </ul>
        <p class="assistant__hint">
          手动跑<b>不会绕过安全开关</b>：里面的每一步照旧吃各自的档位 —— 命令行停在「关闭」时，
          需要跑命令的那一步一样会被拒。
        </p>

        <!-- ---------------------------------------------------- 计划 -->
        <h3 class="hud-label assistant__section">计划</h3>
        <form class="assistant__form" @submit.prevent="makePlan">
          <input v-model="goal" class="hud-field" placeholder="要达成什么，例如「把 C 盘清出 10G」" />
          <button class="hud-btn hud-btn--primary" type="submit" :disabled="planning">
            {{ planning ? '拆解中…' : '拆成步骤' }}
          </button>
        </form>
        <p class="assistant__hint">
          这里<b>只出计划、不执行</b>：没有哪一步会被真的跑起来。要花一次模型调用。
          <span v-if="!overview.planner_enabled">当前配置里 planner 是关闭的。</span>
        </p>
        <div v-if="plan" class="assistant__plan">
          <p class="assistant__plan-goal">{{ plan.goal }}</p>
          <p v-if="plan.rationale" class="assistant__hint">{{ plan.rationale }}</p>
          <ol class="assistant__steps">
            <li v-for="step in plan.steps" :key="step.step_id" class="assistant__step">
              <span class="hud-chip">{{ step.status }}</span>
              <span class="assistant__text">{{ step.title }}</span>
              <span v-if="step.action" class="hud-label assistant__meta">{{ step.action }}</span>
            </li>
          </ol>
        </div>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * The capabilities that were already built and had no door.
 *
 * One popup with tabs rather than four buttons on the top bar: the bar had seven
 * controls and the row is what the operator scans for machine state -- an
 * "open the drawer of everything else" entry belongs at the end of it, not spread
 * across it.
 *
 * Nothing polls. Each tab is read when it is opened, because all four of these are
 * things a person looks at deliberately, and a timer that reads the memory table
 * every second would be pure cost for a panel that is closed.
 *
 * 「自动化」 is the newest and the one that was missing for two rounds: the scheduler
 * and the workflow engine have had ``stats()`` methods whose docstrings say "for the
 * HUD's automation panel" since they were written, and the panel did not exist.
 */
import { onMounted, ref, watch } from 'vue'
import {
  addReminder,
  cancelReminder,
  fetchAutomationOverview,
  fetchMemories,
  fetchKnowledge,
  fetchReminders,
  fetchScheduledJobs,
  fetchWorkflows,
  forgetAllMemories,
  forgetKnowledge,
  forgetMemory,
  ingestKnowledge,
  planGoal,
  probeKnowledge,
  reloadWorkflows,
  removeScheduledJob,
  runScheduledJob,
  runWorkflow,
  toggleReminder,
  toggleScheduledJob,
  type AutomationOverview,
  type KnowledgeBoard,
  type MemoryRow,
  type PlanRow,
  type ReminderRow,
  type ScheduledJobRow,
  type WorkflowRow,
} from '@/api/bridge'

type TabId = 'reminders' | 'memory' | 'knowledge' | 'automation'

const TABS: readonly { id: TabId; label: string }[] = [
  { id: 'reminders', label: '提醒' },
  { id: 'memory', label: '记忆' },
  { id: 'knowledge', label: '知识库' },
  { id: 'automation', label: '自动化' },
]

const KINDS: Record<string, string> = {
  fact: '事实',
  episode: '片段',
  task: '任务',
  preference: '偏好',
}

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const tab = ref<TabId>('reminders')
const notice = ref('')
const noticeBad = ref(false)

const rows = ref<ReminderRow[]>([])
const channels = ref<string[]>([])
const newText = ref('')
const newWhen = ref('')
const adding = ref(false)

const memories = ref<MemoryRow[]>([])
const confirmWipe = ref(false)

const docs = ref<Record<string, unknown>[]>([])
const stats = ref<Record<string, unknown>>({})
const newPath = ref('')
const ingesting = ref(false)
const probeQuery = ref('')
const hits = ref<Record<string, unknown>[]>([])
const probed = ref(false)

const overview = ref<AutomationOverview>({
  scheduler: {},
  workflow: {},
  planner: {},
  planner_enabled: false,
  error: '',
})
const jobs = ref<ScheduledJobRow[]>([])
const workflows = ref<WorkflowRow[]>([])
const goal = ref('')
const planning = ref(false)
const plan = ref<PlanRow | null>(null)

/**
 * One counter out of a block that is ``{}`` when this build has no such engine.
 *
 * ``--`` rather than 0 would be the honest reading, but a count line with dashes in
 * it reads as broken; the empty block is already reported by the tab's own empty
 * states, which say "this process has no scheduler" in words.
 */
function num(block: Record<string, unknown>, key: string): number {
  const value = block[key]
  return typeof value === 'number' ? value : 0
}

function say(message: string, bad = false): void {
  notice.value = message
  noticeBad.value = bad
}

function select(next: TabId): void {
  tab.value = next
  confirmWipe.value = false
  void load(next)
}

async function load(which: TabId): Promise<void> {
  try {
    if (which === 'reminders') {
      const board = await fetchReminders()
      rows.value = board.rows
      channels.value = board.channels
      if (board.error) say(board.error, true)
    } else if (which === 'memory') {
      const board = await fetchMemories()
      memories.value = board.rows
      if (board.error) say(board.error, true)
    } else if (which === 'knowledge') {
      const board: KnowledgeBoard = await fetchKnowledge()
      docs.value = board.documents
      stats.value = board.stats
      if (board.error) say(board.error, true)
    } else {
      // Three calls in parallel: the tab draws one screen, and doing them in series
      // would make the counters and the rows arrive at visibly different times.
      const [board, workflow, counters] = await Promise.all([
        fetchScheduledJobs(),
        fetchWorkflows(),
        fetchAutomationOverview(),
      ])
      jobs.value = board.rows
      workflows.value = workflow.rows
      overview.value = counters
      const problem = board.error || workflow.error || counters.error
      if (problem) say(problem, true)
    }
  } catch (err) {
    say(err instanceof Error ? err.message : String(err), true)
  }
}

async function submitReminder(): Promise<void> {
  if (adding.value) return
  adding.value = true
  notice.value = ''
  try {
    const answer = await addReminder(newText.value, newWhen.value)
    if (!answer.ok) {
      say(answer.error || '没能写入', true)
    } else {
      notice.value = `已记下：${answer.row?.text ?? ''} ${answer.row?.when ?? ''}`
      noticeBad.value = false
      newText.value = ''
      newWhen.value = ''
      await load('reminders')
    }
  } finally {
    adding.value = false
  }
}

async function remove(key: string): Promise<void> {
  const answer = await cancelReminder(key)
  if (!answer.ok) say(answer.error, true)
  else notice.value = `已取消：${answer.removed}`
  await load('reminders')
}

async function flip(row: ReminderRow): Promise<void> {
  const answer = await toggleReminder(row.job_id, !row.enabled)
  if (!answer.ok) say(answer.error, true)
  await load('reminders')
}

async function forget(id: number): Promise<void> {
  const answer = await forgetMemory(id)
  if (!answer.ok) say(answer.error || '那条没删掉', true)
  await load('memory')
}

async function wipe(): Promise<void> {
  const answer = await forgetAllMemories(true)
  confirmWipe.value = false
  if (answer.error) say(answer.error, true)
  else notice.value = `已忘掉 ${answer.removed} 条`
  await load('memory')
}

async function addDoc(): Promise<void> {
  if (ingesting.value) return
  ingesting.value = true
  notice.value = ''
  try {
    const answer = await ingestKnowledge(newPath.value)
    if (!answer.ok) say(answer.error || '没能加入', true)
    else notice.value = '已加入知识库并建好索引'
    newPath.value = ''
    await load('knowledge')
  } finally {
    ingesting.value = false
  }
}

async function forgetDoc(docId: string): Promise<void> {
  const answer = await forgetKnowledge(docId)
  if (!answer.ok) say(answer.error || '没删掉', true)
  else notice.value = '已从知识库忘掉（磁盘上的文件没动）'
  await load('knowledge')
}

async function probe(): Promise<void> {
  probed.value = true
  const answer = await probeKnowledge(probeQuery.value)
  hits.value = answer.hits
  if (answer.error) say(answer.error, true)
}

async function flipJob(row: ScheduledJobRow): Promise<void> {
  const answer = await toggleScheduledJob(row.job_id, !row.enabled)
  if (!answer.ok) say(answer.error, true)
  await load('automation')
}

/**
 * "试一下" is the whole reason this tab is worth having.
 *
 * A schedule that has never fired is a guess; running it once by hand, from the same
 * path the clock takes, is how an operator finds out whether it works *before*
 * trusting it with a nightly job.
 */
async function tryJob(jobId: string): Promise<void> {
  const answer = await runScheduledJob(jobId)
  if (!answer.ok) say(answer.error || '试跑失败', true)
  else if (answer.run) say(answer.run.detail || '试跑完成')
  await load('automation')
}

async function dropJob(jobId: string): Promise<void> {
  const answer = await removeScheduledJob(jobId)
  if (!answer.ok) say(answer.error, true)
  else say(answer.removed ? '已删除' : '那个任务已经不在了')
  await load('automation')
}

async function tryWorkflow(name: string): Promise<void> {
  const answer = await runWorkflow(name)
  if (!answer.ok && answer.error) say(answer.error, true)
  else if (answer.run) say(`${name}：${answer.run.ok ? '跑完了' : '失败了'}`)
  await load('automation')
}

async function reloadWf(): Promise<void> {
  const answer = await reloadWorkflows()
  if (!answer.ok) say(answer.error, true)
  else say(`重新读取：${answer.count} 个工作流`)
  await load('automation')
}

async function makePlan(): Promise<void> {
  if (planning.value) return
  planning.value = true
  notice.value = ''
  try {
    const answer = await planGoal(goal.value)
    if (!answer.ok) {
      plan.value = null
      say(answer.error || '没能拆解这个目标', true)
    } else {
      plan.value = answer.plan
    }
  } finally {
    planning.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) void load(tab.value)
  },
)

onMounted(() => {
  if (props.open) void load(tab.value)
})
</script>

<style scoped>
.assistant {
  position: fixed;
  inset: 0;
  z-index: 44;
  display: grid;
  place-items: center;
}

.assistant__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.assistant__box {
  position: relative;
  width: min(880px, 92vw);
  max-height: 82vh;
  overflow: auto;
  padding: 12px 14px 16px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  background: linear-gradient(rgba(9, 22, 38, 0.985), rgba(3, 7, 13, 0.99));
  box-shadow: var(--hud-elev-2);
}

.assistant__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.assistant__tabs,
.assistant__headrow,
.assistant__form {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.assistant__headrow {
  justify-content: space-between;
  margin: 6px 0;
}

.assistant__form {
  margin: 8px 0;
}

.assistant__when {
  width: 210px;
}

.assistant__list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.assistant__row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border: 1px solid rgba(77, 216, 255, 0.12);
  border-radius: var(--hud-pill);
  background: rgba(77, 216, 255, 0.05);
  color: var(--hud-text);
  font-size: 12px;
}

.assistant__row--off {
  opacity: 0.5;
}

.assistant__row--hit {
  align-items: flex-start;
  border-radius: 10px;
}

.assistant__time {
  min-width: 116px;
  color: var(--hud-cyan);
}

.assistant__text {
  flex: 1;
  min-width: 0;
}

.assistant__next,
.assistant__meta {
  color: var(--hud-dim);
}

.assistant__note {
  margin: 6px 0;
  color: var(--hud-cyan);
  font-size: 12px;
}

.assistant__note--bad {
  color: #ff8f8f;
}

.assistant__warn {
  margin: 6px 0;
  color: var(--hud-amber, #ffb547);
  font-size: 12px;
}

.assistant__hint {
  margin: 4px 0 8px;
  color: var(--hud-dim);
  font-size: 11px;
}

.assistant__empty,
.assistant__loading {
  margin: 10px 0;
  color: var(--hud-dim);
  font-size: 12px;
}

/* The four blocks of the automation tab need a divider: without one the tab reads as
   a single long list and the operator has to guess where the workflows start. */
.assistant__section {
  margin: 14px 0 4px;
  padding-bottom: 3px;
  border-bottom: 1px solid rgba(77, 216, 255, 0.14);
  color: var(--hud-cyan);
}

.assistant__chip--bad {
  border-color: rgba(255, 93, 93, 0.45);
  color: #ff9b9b;
}

.assistant__plan {
  margin: 10px 0 0;
  padding: 8px 10px;
  border: 1px solid rgba(77, 216, 255, 0.16);
  border-radius: var(--hud-radius);
  background: rgba(77, 216, 255, 0.04);
}

.assistant__plan-goal {
  margin: 0 0 6px;
  color: var(--hud-text);
  font-size: 13px;
}

.assistant__steps {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.assistant__step {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--hud-text);
  font-size: 12px;
}

.assistant__step .hud-chip {
  min-width: 56px;
  text-align: center;
}
</style>
