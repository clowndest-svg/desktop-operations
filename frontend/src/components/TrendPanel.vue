<template>
  <section class="hud-panel trend">
    <header class="hud-title">
      负载趋势 · LOAD TREND
      <span class="trend__legend">
        <span class="trend__key" :title="cpuKeyHint">
          <i class="trend__swatch trend__swatch--cpu"></i>CPU
          <b class="hud-num">{{ cpuLabel }}</b>
        </span>
        <span class="trend__key" title="已用 / 物理内存。这条线长期偏高是正常的，不是变慢了。">
          <i class="trend__swatch trend__swatch--mem"></i>内存
          <b class="hud-num">{{ memoryLabel }}</b>
        </span>
      </span>
    </header>

    <div ref="canvas" class="trend__canvas"></div>

    <!--
      The complaint was "看不懂", and what was missing was not decoration but the
      three things a chart cannot say by itself: what each line is, over what span,
      and what a value at the top of the scale would mean. The span and the cadence
      are measured off the plotted points rather than written as a constant, because
      the poll interval is configurable and drops to 10 秒 while the window is
      hidden -- a printed "最近 90 秒" would be a guess about someone else's setting.
    -->
    <p class="trend__caption">
      <span class="hud-label">横轴 {{ spanLabel }} · {{ pointsLabel }}</span>
      <span class="trend__hint">悬停看某一时刻的读数</span>
    </p>
    <p class="trend__explain">
      青线 CPU＝所有核心的平均占用，100% 是每个核心都排满，一根尖峰就是有进程在干活（下面那排小竖条是逐核）。
      橙线 内存＝已用÷物理内存，长期偏高是正常的，空闲内存会被拿去做缓存，它不是速度。
    </p>
  </section>
</template>

<script setup lang="ts">
/**
 * The load chart, and the reading guide that makes it mean something.
 *
 * Two lines were drawn over an axis with no labels, no legend and tooltips switched
 * off -- the picture said "some history", and only the person who wrote it knew
 * which line was which or how long the window was. Everything added here is the
 * minimum that makes the shape readable: a named line with its current value, the
 * actual time span, and what the top of the scale means.
 */
import { computed, ref, watch } from 'vue'
import type { EChartsOption } from 'echarts'
import { useChart } from '@/composables/useChart'
import { useSystemStore } from '@/stores/system'

const store = useSystemStore()
const canvas = ref<HTMLElement>()

const CPU_COLOR = '#4dd8ff'
const MEMORY_COLOR = '#ffb547'

/** Every timestamp that produced a plotted point, oldest first. */
const stamps = computed<number[]>(() => {
  const all = new Set<number>()
  for (const point of store.cpuHistory) all.add(point.t)
  for (const point of store.memoryHistory) all.add(point.t)
  return [...all].sort((left, right) => left - right)
})

function duration(milliseconds: number): string {
  const seconds = Math.max(0, Math.round(milliseconds / 1000))
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

const spanLabel = computed(() => {
  const points = stamps.value
  if (points.length < 2) return '刚开始采样'
  return `最近 ${duration(points[points.length - 1] - points[0])}`
})

/**
 * The real cadence, taken from the middle of the gaps actually on screen.
 *
 * The configured interval is not what the chart shows: a hidden window polls every
 * 10 秒, and a slow read is skipped rather than queued. The median gap says what was
 * really sampled, which is the only number worth printing next to a curve.
 */
const pointsLabel = computed(() => {
  const points = stamps.value
  if (points.length < 2) return `${points.length} 点`
  const gaps = points
    .slice(1)
    .map((at, index) => at - points[index])
    .filter((gap) => gap > 0)
    .sort((left, right) => left - right)
  const median = gaps[Math.floor(gaps.length / 2)] ?? 0
  return `${points.length} 点 · 约每 ${duration(median)}一点`
})

const cpuLabel = computed(() => (store.cpuReady ? `${store.cpuPercent.toFixed(0)}%` : '--'))
const memoryLabel = computed(() =>
  store.metrics.memory ? `${store.memoryPercent.toFixed(0)}%` : '--',
)
const cpuKeyHint = computed(
  () =>
    `整机 CPU 平均占用（${store.cores || '?'} 个核心全部计入）。100% 表示每个核心都排满，` +
    '不是死机。第一次读数没有可比较的前一个样本，所以先显示 --。',
)

const option = computed<EChartsOption>(() => ({
  animation: false,
  grid: { top: 18, right: 12, bottom: 20, left: 34 },
  // Off was the original setting, and it is why the chart could not be read: with
  // no axis labels and no tooltip, a point on the line has no time and no value.
  tooltip: {
    trigger: 'axis',
    confine: true,
    backgroundColor: 'rgba(6, 16, 28, 0.94)',
    borderColor: 'rgba(77, 216, 255, 0.35)',
    borderWidth: 1,
    textStyle: { color: '#d7eefc', fontSize: 11 },
    axisPointer: { type: 'line', lineStyle: { color: 'rgba(77,216,255,0.35)' } },
    valueFormatter: (value) => (typeof value === 'number' ? `${value.toFixed(1)}%` : '--'),
  },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    axisLine: { lineStyle: { color: 'rgba(77,216,255,0.25)' } },
    axisLabel: { show: false },
    axisTick: { show: false },
    data: store.cpuHistory.map((point) => new Date(point.t).toLocaleTimeString('zh-CN', { hour12: false })),
  },
  yAxis: {
    type: 'value',
    min: 0,
    max: 100,
    splitLine: { lineStyle: { color: 'rgba(77,216,255,0.08)' } },
    axisLabel: { color: '#6b8ba4', fontSize: 10, formatter: '{value}%' },
  },
  series: [
    {
      name: 'CPU',
      type: 'line',
      smooth: true,
      symbol: 'none',
      data: store.cpuHistory.map((point) => point.v),
      lineStyle: { color: CPU_COLOR, width: 1.6, shadowBlur: 10, shadowColor: 'rgba(77,216,255,0.6)' },
      areaStyle: { color: 'rgba(77,216,255,0.12)' },
    },
    {
      name: '内存',
      type: 'line',
      smooth: true,
      symbol: 'none',
      // Its own timestamps: the CPU series skips the first read (there is nothing to
      // difference against yet), so the two are not always the same length, and
      // padding the memory line to match would invent a reading.
      data: store.memoryHistory.map((point) => point.v),
      lineStyle: { color: MEMORY_COLOR, width: 1.6 },
      areaStyle: { color: 'rgba(255,181,71,0.09)' },
    },
  ],
}))

const { chart } = useChart(canvas, () => option.value)

watch(option, (next) => chart.value?.setOption(next), { deep: true })
</script>

<style scoped>
.trend {
  display: flex;
  flex-direction: column;
  padding: 10px 12px 6px;
  /*
   * No floor here. The row can legitimately be shorter than a chart wants to be at
   * the window's minimum size, and a min-height larger than the track does not make
   * the panel taller -- it makes it spill over its neighbour, which is what the
   * "there is a black box / things overlap" complaint was about.
   */
  min-height: 0;
}

.trend__legend {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
}

.trend__key {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
  letter-spacing: 0.06em;
  color: var(--hud-dim);
  cursor: help;
}

.trend__key b {
  color: var(--hud-text);
  font-size: 11px;
  font-weight: 500;
}

.trend__swatch {
  width: 14px;
  height: 3px;
  border-radius: var(--hud-pill);
}

.trend__swatch--cpu {
  background: var(--hud-cyan);
  box-shadow: 0 0 8px var(--hud-glow);
}

.trend__swatch--mem {
  background: var(--hud-amber);
}

.trend__canvas {
  flex: 1;
  min-height: 88px;
}

.trend__caption {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
  margin: 2px 0 0;
  font-size: 10px;
}

.trend__hint {
  color: var(--hud-dim);
  font-size: 10px;
  letter-spacing: 0.04em;
}

/* The part that answers "看不懂": what a value on this chart actually claims. */
.trend__explain {
  margin: 2px 0 0;
  font-size: 10px;
  line-height: 1.6;
  color: var(--hud-dim);
  letter-spacing: 0.01em;
}
</style>
