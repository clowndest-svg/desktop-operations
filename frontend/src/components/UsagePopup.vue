<template>
  <div v-if="open" class="usage" role="dialog" aria-modal="true" aria-label="Token 用量">
    <div class="usage__scrim" @click="emit('close')"></div>

    <section class="usage__box">
      <header class="usage__head">
        <h2 class="hud-title">用量 · TOKEN LEDGER</h2>
        <div class="usage__ranges">
          <button
            v-for="option in RANGES"
            :key="option.days"
            class="hud-btn"
            :class="{ 'hud-btn--primary': option.days === days }"
            type="button"
            @click="select(option.days)"
          >
            {{ option.label }}
          </button>
          <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
        </div>
      </header>

      <p v-if="loadError" class="usage__error">{{ loadError }}</p>
      <p v-if="loading && !totals" class="usage__loading">读取中…</p>

      <template v-if="totals">
        <div class="usage__grid">
          <div class="usage__cell">
            <span class="hud-label">总 token</span>
            <span class="usage__big hud-num">{{ totals.total_tokens.toLocaleString() }}</span>
          </div>
          <div class="usage__cell">
            <span class="hud-label">输入 / 输出</span>
            <span class="usage__mid hud-num">
              {{ totals.prompt_tokens.toLocaleString() }} / {{ totals.completion_tokens.toLocaleString() }}
            </span>
          </div>
          <div class="usage__cell">
            <span class="hud-label">调用次数</span>
            <span class="usage__mid hud-num">{{ totals.calls.toLocaleString() }}</span>
          </div>
          <div class="usage__cell">
            <span class="hud-label">命中缓存</span>
            <!--
              Three states, and the middle one is the reason this file exists:
              a provider that never reports cached tokens is not a provider with a
              0% hit rate. Showing 0% there would be a number nobody can falsify.
            -->
            <span v-if="!totals.cache_data_reported" class="usage__mid usage__mid--none">
              无读数
            </span>
            <span v-else class="usage__mid hud-num">{{ cacheLine }}</span>
          </div>
        </div>

        <p class="usage__note">
          <template v-if="!totals.cache_data_reported">
            本窗口 {{ totals.calls_without_cache_data }} 次调用没有一个返回缓存字段。
            这是「没有读数」，不是「没命中」——两者不一样，前者要换 provider 或等它开始上报。
          </template>
          <template v-else>
            缓存 {{ totals.cached_tokens.toLocaleString() }} / 输入
            {{ totals.prompt_tokens.toLocaleString() }} tokens
          </template>
          · 平均延迟 {{ Math.round(totals.avg_latency_ms) }} ms ·
          窗口 {{ windowLabel }}<template v-if="!days && span.rows">
            · 账本共 {{ span.rows.toLocaleString() }} 条记录</template>
        </p>

        <!--
          各模型这张扇形图：扇区是"谁花了多少"，右边那列是同一个问题的精确读数。
          两边都从同一次查询出来（`models` 和 `summary` 共用一组窗口边界），所以扇区加起来
          必然等于顶上那个总数 —— 对不上就说明两半用了两个窗口，那是这个面板唯一不能犯的错。
        -->
        <div v-if="modelRows.length" class="usage__split">
          <div class="usage__pie">
            <svg :viewBox="`0 0 ${DONUT} ${DONUT}`" class="usage__donut" role="img" :aria-label="donutLabel">
              <circle :cx="RING_C" :cy="RING_C" :r="RING_R" class="usage__track" :stroke-width="RING_W" />
              <circle
                v-for="slice in slices"
                :key="slice.key"
                :cx="RING_C"
                :cy="RING_C"
                :r="RING_R"
                :stroke="slice.color"
                :stroke-width="RING_W"
                :stroke-dasharray="`${slice.len} ${CIRCUMFERENCE - slice.len}`"
                :stroke-dashoffset="`${-slice.offset}`"
                :transform="`rotate(-90 ${RING_C} ${RING_C})`"
                class="usage__slice"
              >
                <title>{{ slice.label }}</title>
              </circle>
              <text :x="RING_C" :y="RING_C - 2" class="usage__donut-num">{{ compactGrand }}</text>
              <text :x="RING_C" :y="RING_C + 15" class="usage__donut-cap">TOTAL TOKENS</text>
            </svg>
            <ul class="usage__swatches">
              <li v-for="slice in slices" :key="`k-${slice.key}`">
                <i :style="{ background: slice.color }"></i>
                <span class="hud-num">{{ slice.label }}</span>
                <b class="hud-num">{{ slice.percent.toFixed(2) }}%</b>
              </li>
            </ul>
          </div>

          <!--
            The scroller is the safety net, not the design: seven right-aligned columns of
            tabular figures need more width than a 680px dialog had, and the last two
            (缓存 / 均延迟) simply fell off the panel edge. The box is wider now, and on a
            narrow window or a big DPI scale the table scrolls instead of being cut.
          -->
          <div class="usage__table-wrap">
            <table class="usage__mt">
              <thead>
                <tr>
                  <th>模型</th>
                  <th class="usage__num">token</th>
                  <th class="usage__num">占比</th>
                  <th class="usage__num">输入 / 输出</th>
                  <th class="usage__num">次数</th>
                  <th class="usage__num">缓存</th>
                  <th class="usage__num">均延迟</th>
                </tr>
              </thead>
              <tbody>
              <tr v-for="row in modelRows" :key="row.key" :class="{ 'usage__mt--rest': row.rest }">
                <td>
                  <i class="usage__chip" :style="{ background: row.color }"></i>
                  <span>{{ row.label }}</span>
                </td>
                <td class="usage__num hud-num">{{ row.total.toLocaleString() }}</td>
                <td class="usage__num hud-num">{{ row.percent.toFixed(2) }}%</td>
                <td class="usage__num hud-num">
                  {{ row.prompt.toLocaleString() }} / {{ row.completion.toLocaleString() }}
                </td>
                <td class="usage__num hud-num">{{ row.calls.toLocaleString() }}</td>
                <td class="usage__num hud-num">{{ row.cache }}</td>
                <td class="usage__num hud-num">{{ Math.round(row.latency) }} ms</td>
              </tr>
              <tr class="usage__mt--sum">
                <td>合计</td>
                <td class="usage__num hud-num">{{ totals.total_tokens.toLocaleString() }}</td>
                <td class="usage__num hud-num">100.00%</td>
                <td class="usage__num hud-num">
                  {{ totals.prompt_tokens.toLocaleString() }} /
                  {{ totals.completion_tokens.toLocaleString() }}
                </td>
                <td class="usage__num hud-num">{{ totals.calls.toLocaleString() }}</td>
                <td class="usage__num hud-num">{{ cacheLine }}</td>
                <td class="usage__num hud-num">{{ Math.round(totals.avg_latency_ms) }} ms</td>
              </tr>
            </tbody>
            </table>
          </div>
        </div>
        <p v-else-if="!loading" class="usage__loading">还没有按模型分开的记录。</p>

        <p v-if="!days && totals" class="usage__note">
          从始至终这一段不画每日柱：一天一根、跑过一年就是几百根，那不是图。总数、输入/输出、
          调用次数、缓存命中和各模型占比这里都是全量的。
        </p>
        <div v-else-if="daily.length" class="usage__chart" role="img" :aria-label="chartLabel">
          <div v-for="row in daily" :key="row.day" class="usage__col" :title="tipFor(row)">
            <div class="usage__stack" :style="{ height: scaled(row) + 'px' }">
              <div class="usage__bar usage__bar--out" :style="{ height: share(row, row.completion_tokens) + '%' }"></div>
              <div class="usage__bar usage__bar--in" :style="{ height: share(row, row.prompt_tokens) + '%' }"></div>
            </div>
            <span class="usage__day">{{ row.day.slice(5) }}</span>
          </div>
        </div>
        <p v-else class="usage__loading">这个窗口里还没有记账。用一次对话再回来看。</p>

        <div class="usage__legend">
          <span><i class="usage__swatch usage__swatch--in"></i>输入</span>
          <span><i class="usage__swatch usage__swatch--out"></i>输出</span>
          <span class="hud-label">缺口 = 那天没有任何记录（不是 0）</span>
        </div>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  fetchUsage,
  type UsageDay,
  type UsageModelRow,
  type UsageSpan,
  type UsageTotals,
} from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

/** The longest range the backend will answer with, mirrored here as a choice. */
/** `days: 0` is the whole ledger -- the one value the month ceiling does not apply to. */
const RANGES = [
  { days: 1, label: '今天' },
  { days: 7, label: '7 天' },
  { days: 31, label: '一个月' },
  { days: 0, label: '从始至终' },
]

const CHART_HEIGHT = 120

/** Ring geometry for the donut. Numbers, not percentages, so the arcs are checkable. */
const DONUT = 168
const RING_W = 20
const RING_R = (DONUT - RING_W) / 2 - 6
const RING_C = DONUT / 2
const CIRCUMFERENCE = 2 * Math.PI * RING_R

/** The palette is the HUD's own tokens; the eighth slot is "其余 N 家". */
const SLICE_COLORS = ['#4dd8ff', '#9d7cff', '#46e6a8', '#ffb547', '#ff5d5d', '#59a7ff', '#7fd3e8']
const REST_COLOR = '#42627a'

/** More than this and the ring turns into a barber pole; the table still shows every row. */
const MAX_SLICES = 7

const days = ref(7)
const totals = ref<UsageTotals | null>(null)
const daily = ref<UsageDay[]>([])
const models = ref<UsageModelRow[]>([])
const span = ref<UsageSpan>({ first_at: '', last_at: '', rows: 0, days: 0 })
const loading = ref(false)
const loadError = ref('')

const cacheLine = computed(() =>
  totals.value?.cache_hit_percent === null || totals.value?.cache_hit_percent === undefined
    ? '无读数'
    : `${totals.value.cache_hit_percent.toFixed(1)}%`,
)

const windowLabel = computed(() => {
  if (!totals.value) return ''
  if (!days.value) {
    // Quote the ledger's own first row rather than an install date: a database carried
    // over from an older build starts earlier than the app did on this machine.
    const first = (span.value.first_at || totals.value.since).slice(0, 10)
    const last = (span.value.last_at || totals.value.until).slice(0, 10)
    return `从始至终 ${first} → ${last}（${span.value.days} 天）`
  }
  const { since, until } = totals.value
  return `${since.slice(0, 10)} → ${until.slice(0, 10)}`
})

/** Rows big enough to matter, then one collapsed remainder so the ring sums exactly. */
const modelRows = computed(() => {
  const grand = totals.value?.total_tokens ?? 0
  if (!grand) return []
  const sorted = [...models.value].sort((a, b) => b.total_tokens - a.total_tokens)
  const head = sorted.slice(0, MAX_SLICES)
  const tail = sorted.slice(head.length)
  const rows = head.map((row, index) => ({
    key: `${row.provider}/${row.model}`,
    label: `${row.provider} / ${row.model}`,
    color: SLICE_COLORS[index % SLICE_COLORS.length],
    total: row.total_tokens,
    prompt: row.prompt_tokens,
    completion: row.completion_tokens,
    calls: row.calls,
    latency: row.avg_latency_ms,
    cache: row.cache_hit_percent === null ? '无读数' : `${row.cache_hit_percent.toFixed(1)}%`,
    percent: (row.total_tokens / grand) * 100,
    rest: false,
  }))
  if (tail.length) {
    const sum = (pick: (row: UsageModelRow) => number) => tail.reduce((acc, row) => acc + pick(row), 0)
    const cached = sum((row) => row.cached_tokens)
    const prompt = sum((row) => row.prompt_tokens)
    rows.push({
      key: '__rest__',
      label: `其余 ${tail.length} 家合计`,
      color: REST_COLOR,
      total: sum((row) => row.total_tokens),
      prompt,
      completion: sum((row) => row.completion_tokens),
      calls: sum((row) => row.calls),
      latency: sum((row) => row.avg_latency_ms * row.calls) / Math.max(1, sum((row) => row.calls)),
      cache: prompt > 0 && cached > 0 ? `${((100 * cached) / prompt).toFixed(1)}%` : '无读数',
      percent: (sum((row) => row.total_tokens) / grand) * 100,
      rest: true,
    })
  }
  return rows
})

const slices = computed(() => {
  let offset = 0
  // A hairline between slices so two adjacent equal-sized ones are still two slices.
  const gap = modelRows.value.length > 1 ? 2 : 0
  return modelRows.value.map((row) => {
    const len = Math.max(0, (row.percent / 100) * CIRCUMFERENCE - gap)
    const slice = { ...row, offset, len }
    offset += (row.percent / 100) * CIRCUMFERENCE
    return slice
  })
})

const donutLabel = computed(() => {
  const grand = totals.value?.total_tokens ?? 0
  return modelRows.value.length
    ? `${modelRows.value.length} 段，共 ${grand.toLocaleString()} tokens，${
        days.value ? `最近 ${days.value} 天` : '从始至终'
      }`
    : '没有记录'
})

/** 1.23M / 456k -- the ring's centre has ~7 characters of room and no need for more. */
const compactGrand = computed(() => {
  const value = totals.value?.total_tokens ?? 0
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(value)
})

const chartLabel = computed(() =>
  daily.value.length
    ? `${days.value} 天内 ${daily.value.length} 天有记录，共 ${totals.value?.calls ?? 0} 次调用`
    : '没有记录',
)

/** Tallest day sets the scale, so the chart reads as a comparison and not as bars
 *  that all touch the ceiling once usage grows. */
const peak = computed(() =>
  Math.max(1, ...daily.value.map((row) => row.prompt_tokens + row.completion_tokens)),
)

function scaled(row: UsageDay): number {
  const value = row.prompt_tokens + row.completion_tokens
  return Math.max(2, Math.round((value / peak.value) * CHART_HEIGHT))
}

/** A segment's share of its own day, as a percentage of the stacked column. */
function share(row: UsageDay, part: number): string {
  const total = row.prompt_tokens + row.completion_tokens
  if (total <= 0) return '0'
  return `${((part / total) * 100).toFixed(2)}`
}

function tipFor(row: UsageDay): string {
  const cache = row.cache_hit_percent === null ? '无读数' : `${row.cache_hit_percent.toFixed(1)}%`
  return `${row.day} · ${row.calls} 次 · 输入 ${row.prompt_tokens} · 输出 ${row.completion_tokens} · 缓存 ${cache}`
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const report = await fetchUsage(days.value)
    totals.value = report.summary
    daily.value = report.daily
    models.value = report.models ?? []
    span.value = report.span ?? { first_at: '', last_at: '', rows: 0, days: 0 }
    loadError.value = report.error ?? ''
  } catch (err) {
    loadError.value = `读取用量失败：${err instanceof Error ? err.message : String(err)}`
  } finally {
    loading.value = false
  }
}

function select(value: number) {
  days.value = value
  void load()
}

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) void load()
  },
)
</script>

<style scoped>
.usage {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.usage__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.usage__box {
  position: relative;
  /* 680px could not hold the donut column *and* seven columns of figures: the last two
     fell off the panel. 900px fits both without scrolling at normal scaling; below that
     the table scrolls inside its own wrapper rather than being cut. */
  width: min(900px, 94vw);
  max-height: 88vh;
  overflow: auto;
  padding: 16px 20px 20px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.usage__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.usage__ranges {
  display: flex;
  gap: 6px;
}

.usage__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
}

.usage__cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 12px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  background: rgba(3, 9, 16, 0.5);
}

.usage__big {
  color: var(--hud-cyan);
  font-size: 24px;
  text-shadow: 0 0 14px var(--hud-glow);
}

.usage__mid {
  color: var(--hud-text);
  font-size: 15px;
}

.usage__mid--none {
  color: var(--hud-amber);
}

.usage__note {
  margin: 10px 0 12px;
  color: var(--hud-dim);
  font-size: 11px;
  line-height: 1.6;
}

.usage__chart {
  display: flex;
  align-items: flex-end;
  gap: 3px;
  height: 150px;
  padding-top: 8px;
  border-top: 1px solid var(--hud-line);
}

.usage__col {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: flex-end;
  gap: 4px;
  min-width: 0;
}

.usage__stack {
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  width: 100%;
  max-width: 18px;
}

.usage__bar {
  width: 100%;
}

.usage__bar--in {
  background: linear-gradient(180deg, rgba(77, 216, 255, 0.85), rgba(77, 216, 255, 0.35));
}

.usage__bar--out {
  background: linear-gradient(180deg, rgba(157, 124, 255, 0.8), rgba(157, 124, 255, 0.3));
}

.usage__day {
  color: var(--hud-dim);
  font-size: 9px;
  white-space: nowrap;
}

.usage__legend {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 8px;
  color: var(--hud-text);
  font-size: 11px;
}

.usage__swatch {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 4px;
}

.usage__swatch--in {
  background: var(--hud-cyan);
}

.usage__swatch--out {
  background: var(--hud-violet);
}

.usage__error {
  color: var(--hud-red);
  font-size: 12px;
}

.usage__loading {
  color: var(--hud-amber);
  font-size: 12px;
}

/* ---- 各模型：扇形图 + 精确读数，同一块地方上下对齐 ------------------ */

.usage__split {
  display: grid;
  grid-template-columns: 176px minmax(0, 1fr);
  gap: 14px;
  align-items: start;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--hud-line);
}

.usage__pie {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.usage__donut {
  width: 168px;
  height: 168px;
}

/* The unfilled track: without it a 3-slice ring reads as a broken circle. */
.usage__track {
  fill: none;
  stroke: var(--hud-line);
  opacity: 0.55;
}

.usage__slice {
  fill: none;
  stroke-linecap: butt;
  filter: drop-shadow(0 0 5px var(--hud-glow));
}

.usage__donut-num {
  fill: var(--hud-cyan);
  font-family: var(--hud-mono);
  font-size: 23px;
  font-weight: 600;
  letter-spacing: 0.5px;
  text-anchor: middle;
  text-shadow: 0 0 14px var(--hud-glow);
}

.usage__donut-cap {
  fill: var(--hud-dim);
  font-family: var(--hud-mono);
  font-size: 8px;
  letter-spacing: 2px;
  text-anchor: middle;
}

.usage__swatches {
  display: grid;
  gap: 3px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 10px;
}

.usage__swatches li {
  display: grid;
  grid-template-columns: 9px minmax(0, 1fr) auto;
  gap: 5px;
  align-items: center;
  color: var(--hud-dim);
}

.usage__swatches i {
  width: 9px;
  height: 3px;
  border-radius: 2px;
}

.usage__swatches span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--hud-text);
}

.usage__swatches b {
  font-weight: 500;
  color: var(--hud-cyan);
}

.usage__table-wrap {
  /* The scroller has to be the grid item that is allowed to shrink; without min-width:0 a
     grid track refuses to go below its content and the overflow never happens -- the
     table just keeps walking out of the dialog. */
  min-width: 0;
  overflow-x: auto;
}

.usage__mt {
  width: 100%;
  min-width: 620px;
  border-collapse: collapse;
  font-size: 11px;
}

.usage__mt th {
  padding: 0 6px 5px 0;
  border-bottom: 1px solid var(--hud-line);
  color: var(--hud-dim);
  font-family: var(--hud-mono);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 1.2px;
  text-align: left;
  text-transform: uppercase;
  white-space: nowrap;
}

.usage__mt td {
  padding: 4px 6px 4px 0;
  border-bottom: 1px dashed rgba(77, 216, 255, 0.1);
  color: var(--hud-text);
  white-space: nowrap;
}

/* Tabular figures so three rows of six-digit numbers line up in their decimals. */
.usage__num,
.usage__mt .hud-num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.usage__num {
  padding-left: 6px;
}

.usage__mt th.usage__num {
  padding-left: 6px;
}

.usage__chip {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 5px;
  border-radius: 2px;
  vertical-align: -1px;
}

.usage__mt--rest td {
  color: var(--hud-dim);
  font-style: italic;
}

.usage__mt--sum td {
  border-bottom: none;
  border-top: 1px solid var(--hud-line);
  color: var(--hud-cyan);
}
</style>
