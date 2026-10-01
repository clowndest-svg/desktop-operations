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
          窗口 {{ windowLabel }}
        </p>

        <div v-if="daily.length" class="usage__chart" role="img" :aria-label="chartLabel">
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

import { fetchUsage, type UsageDay, type UsageTotals } from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

/** The longest range the backend will answer with, mirrored here as a choice. */
const RANGES = [
  { days: 1, label: '今天' },
  { days: 7, label: '7 天' },
  { days: 31, label: '一个月' },
]

const CHART_HEIGHT = 120

const days = ref(7)
const totals = ref<UsageTotals | null>(null)
const daily = ref<UsageDay[]>([])
const loading = ref(false)
const loadError = ref('')

const cacheLine = computed(() =>
  totals.value?.cache_hit_percent === null || totals.value?.cache_hit_percent === undefined
    ? '无读数'
    : `${totals.value.cache_hit_percent.toFixed(1)}%`,
)

const windowLabel = computed(() => {
  if (!totals.value) return ''
  const { since, until } = totals.value
  return `${since.slice(0, 10)} → ${until.slice(0, 10)}`
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
  width: min(680px, 94vw);
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
</style>
