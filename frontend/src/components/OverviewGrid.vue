<template>
  <!--
    占用排行下面那一格：四件"抬眼就该知道"的事，每件都来自已经存在的接口。

    它们不是新能力，是**已经采过、却没地方看**的数：接下来要做什么（提醒）、今天花了多少
    （用量台账）、她刚才动了什么（工具台账）、机器有没有话要说（遥测告警）。面板每 60 秒读
    一次，窗口收进托盘就停 —— 这四样都不是每秒在变的东西，为它们常驻轮询是白白烧电。
  -->
  <section class="ov" aria-label="概览">
    <button class="ov__cell" type="button" title="打开助手里的提醒页" @click="emit('reminders')">
      <h4 class="hud-label ov__title">接下来 · UPCOMING</h4>
      <ul v-if="upcoming.length" class="ov__list">
        <li v-for="row in upcoming" :key="row.job_id" class="ov__line">
          <span class="ov__time hud-num">{{ row.next_run || row.when }}</span>
          <span class="ov__text">{{ row.text }}</span>
        </li>
      </ul>
      <p v-else class="ov__empty">还没有提醒</p>
    </button>

    <button class="ov__cell" type="button" title="打开用量面板" @click="emit('usage')">
      <h4 class="hud-label ov__title">今日用量 · TODAY</h4>
      <!--
        三行、每行都短：这格只有 120 来个像素宽，写"输入 + 输出"这种说明词会把数字本身
        挤出边框（第一版就是这样被截掉的）。说明放在 title 里，屏上只留数。
      -->
      <template v-if="today && today.calls > 0">
        <p class="ov__line" :title="`今天 ${today.calls} 次调用，输入 ${today.prompt_tokens} tokens，输出 ${today.completion_tokens}，平均 ${(today.avg_latency_ms / 1000).toFixed(1)} 秒`">
          <span class="ov__time hud-num">{{ today.calls }}</span>
          <span class="ov__text">次调用</span>
        </p>
        <p class="ov__line hud-num" title="输入 + 输出 tokens">
          <span class="ov__text">{{ tokens }}</span>
        </p>
        <p class="ov__line">
          <span class="ov__text ov__dim">{{ latency }}<template v-if="cache !== ''"> · 缓存 {{ cache }}</template></span>
        </p>
      </template>
      <p v-else class="ov__empty">今天还没问过</p>
    </button>

    <button class="ov__cell" type="button" title="打开活动日志" @click="emit('actions')">
      <h4 class="hud-label ov__title">最近动作 · ACTIONS</h4>
      <ul v-if="actions.length" class="ov__list">
        <li v-for="(entry, index) in actions" :key="`${entry.at}-${index}`" class="ov__line">
          <span class="ov__time hud-num" :class="entry.ok ? 'ov__ok' : 'ov__bad'">
            {{ entry.ok ? '✓' : '✗' }}
          </span>
          <span class="ov__text">{{ entry.tool }} · {{ formatMs(entry.milliseconds) }}</span>
        </li>
      </ul>
      <p v-else class="ov__empty">她还没动过手</p>
    </button>

    <!--
      An alert is a state, not a sentence: each one carries its own severity and a way to
      say 知道了. The sampling warnings still land in this cell, but in their own branch --
      "this machine is past a line you drew" and "I could not finish reading it" are
      different claims and a person reacts to them differently.
    -->
    <div class="ov__cell ov__cell--still">
      <h4 class="hud-label ov__title ov__title--row">
        告警 · ALERTS
        <button
          v-if="openAlerts.length > 1"
          class="ov__act"
          type="button"
          title="把这一批都记成已看过"
          @click="ackAll"
        >
          全部知道了
        </button>
      </h4>
      <ul v-if="openAlerts.length" class="ov__list">
        <li v-for="row in openAlerts" :key="row.code" class="ov__line">
          <span class="ov__text" :class="row.severity === 'critical' ? 'ov__bad' : 'ov__warn'">
            {{ row.message }}
          </span>
          <button class="ov__act" type="button" title="这条我认了，恢复了再提醒我" @click="ack(row.code)">
            知道了
          </button>
        </li>
      </ul>
      <ul v-else-if="system.warnings.length" class="ov__list">
        <li v-for="(line, index) in system.warnings.slice(0, 2)" :key="index" class="ov__line">
          <span class="ov__text ov__warn">{{ line }}</span>
        </li>
      </ul>
      <p v-else class="ov__empty">没有告警</p>
    </div>
  </section>
</template>

<script setup lang="ts">
/**
 * Four glanceable readouts, all of them from data the app already keeps.
 *
 * The point is not new information -- every one of these has a popup already -- it is
 * that a popup is a click. "What is coming up", "what has today cost", "what did she
 * just do", "is the machine complaining" are questions an operator asks with their
 * eyes, and the answer should be on screen while they are asking it.
 *
 * It polls on its own, slowly (60 s), and stops while the window is in the tray:
 * none of the four changes by the second, and an assistant that lives in the
 * background has no business waking up for a number nobody is looking at.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  acknowledgeAlert,
  acknowledgeAllAlerts,
  fetchActivity,
  fetchReminders,
  fetchUsage,
  onBackground,
  type ActivityEntry,
  type ReminderRow,
  type UsageTotals,
} from '@/api/bridge'
import { useSystemStore } from '@/stores/system'

const emit = defineEmits<{
  (e: 'reminders'): void
  (e: 'usage'): void
  (e: 'actions'): void
}>()

const system = useSystemStore()
const reminders = ref<ReminderRow[]>([])
const today = ref<UsageTotals | null>(null)
const actions = ref<ActivityEntry[]>([])

const VISIBLE_MS = 60_000
const HIDDEN_MS = 600_000
let timer: ReturnType<typeof setInterval> | undefined
let detach: (() => void) | undefined
let hidden = false

const upcoming = computed(() => reminders.value.filter((row) => row.enabled).slice(0, 3))
const openAlerts = computed(() => system.alerts.slice(0, 3))

/**
 * 「知道了」 is a claim about the operator, not about the machine: it hides this one and
 * promises to bring it back when the reading recovers and crosses again. The engine owns
 * that, so the page only asks and re-reads on the next poll.
 */
async function ack(code: string): Promise<void> {
  await acknowledgeAlert(code).catch(() => undefined)
}

async function ackAll(): Promise<void> {
  await acknowledgeAllAlerts().catch(() => undefined)
}

const cache = computed(() => {
  const report = today.value
  if (report === null) return ''
  return report.cache_hit_percent === null ? '' : `${report.cache_hit_percent.toFixed(0)}%`
})

const tokens = computed(() => {
  const report = today.value
  if (report === null) return '0'
  return `${short(report.prompt_tokens)} + ${short(report.completion_tokens)}`
})

const latency = computed(() => {
  const report = today.value
  if (report === null) return ''
  return `${(report.avg_latency_ms / 1000).toFixed(1)} s`
})

function short(value: number): string {
  if (value < 1000) return String(value)
  return `${(value / 1000).toFixed(1)}k`
}

function formatMs(value: number): string {
  if (value < 1000) return `${value} ms`
  return `${(value / 1000).toFixed(1)} s`
}

async function refresh(): Promise<void> {
  // Each read is independent and each is allowed to fail alone: a reminder store that
  // cannot be read must not blank out today's token count.
  const [board, usage, log] = await Promise.all([
    fetchReminders().catch(() => null),
    fetchUsage(1).catch(() => null),
    fetchActivity().catch(() => null),
  ])
  if (board !== null) reminders.value = board.rows
  if (usage !== null) today.value = usage.summary
  if (log !== null) actions.value = log.entries.slice(0, 3)
}

function reschedule(): void {
  if (timer !== undefined) clearInterval(timer)
  timer = setInterval(() => void refresh(), hidden ? HIDDEN_MS : VISIBLE_MS)
}

onMounted(() => {
  void refresh()
  reschedule()
  detach = onBackground((foreground) => {
    hidden = !foreground
    reschedule()
    if (foreground) void refresh()
  })
})

onBeforeUnmount(() => {
  if (timer !== undefined) clearInterval(timer)
  detach?.()
})
</script>

<style scoped>
.ov {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  /* 贴在卡片底部：上面留给进程表，它自己沉到下面那半截空位里。 */
  margin-top: auto;
  padding-top: 10px;
  border-top: 1px solid var(--hud-line);
  min-height: 0;
}

.ov__cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-height: 0;
  padding: 6px 8px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  background: rgba(6, 16, 28, 0.35);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  overflow: hidden;
}

.ov__cell:hover {
  border-color: rgba(77, 216, 255, 0.4);
}

.ov__cell--still {
  cursor: default;
}

.ov__cell--still:hover {
  border-color: var(--hud-line);
}

.ov__title {
  margin: 0 0 2px;
  color: var(--hud-dim);
}

/* Only this one cell has a control in its heading, so only it lays out as a row. */
.ov__title--row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 6px;
}

.ov__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: 0;
  overflow: hidden;
}

.ov__line {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--hud-text);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.ov__time {
  flex: 0 0 auto;
  color: var(--hud-cyan);
}

.ov__text {
  overflow: hidden;
  text-overflow: ellipsis;
}

.ov__dim {
  color: var(--hud-dim);
}

.ov__ok {
  color: var(--hud-green);
}

.ov__bad {
  color: var(--hud-red);
}

/* 未过线的告警是琥珀：它要说"看这里"，不要说"出事了"。 */
.ov__warn {
  color: var(--hud-amber);
}

.ov__act {
  border: 1px solid rgba(120, 160, 200, 0.28);
  background: transparent;
  color: var(--hud-dim);
  font: inherit;
  font-size: 10px;
  line-height: 1.4;
  padding: 1px 7px;
  border-radius: var(--hud-pill);
  cursor: pointer;
  white-space: nowrap;
}

.ov__act:hover {
  color: var(--hud-text);
  border-color: rgba(120, 160, 200, 0.55);
}

.ov__empty {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--hud-dim);
}
</style>
