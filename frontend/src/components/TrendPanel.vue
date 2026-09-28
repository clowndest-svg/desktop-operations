<template>
  <section class="hud-panel trend">
    <header class="hud-title">负载趋势 · LOAD TREND</header>
    <div ref="canvas" class="trend__canvas"></div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { EChartsOption } from 'echarts'
import { useChart } from '@/composables/useChart'
import { useSystemStore } from '@/stores/system'

const store = useSystemStore()
const canvas = ref<HTMLElement>()

const option = computed<EChartsOption>(() => ({
  animation: false,
  grid: { top: 18, right: 12, bottom: 20, left: 34 },
  tooltip: { show: false },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    axisLine: { lineStyle: { color: 'rgba(77,216,255,0.25)' } },
    axisLabel: { show: false },
    axisTick: { show: false },
    data: store.cpuHistory.map((p) => new Date(p.t).toLocaleTimeString('zh-CN', { hour12: false })),
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
      data: store.cpuHistory.map((p) => p.v),
      lineStyle: { color: '#4dd8ff', width: 1.6, shadowBlur: 10, shadowColor: 'rgba(77,216,255,0.6)' },
      areaStyle: { color: 'rgba(77,216,255,0.12)' },
    },
    {
      name: '内存',
      type: 'line',
      smooth: true,
      symbol: 'none',
      data: store.memoryHistory.map((p) => p.v),
      lineStyle: { color: '#ffb547', width: 1.6 },
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
  padding: 10px 12px 4px;
  min-height: 168px;
}

.trend__canvas {
  flex: 1;
  min-height: 130px;
}
</style>
