<template>
  <div class="gauge">
    <div ref="canvas" class="gauge__canvas"></div>
    <div class="gauge__foot">
      <span class="hud-label">{{ label }}</span>
      <span class="hud-num gauge__value" :style="{ color: tone }">{{ display }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { EChartsOption } from 'echarts'
import { useChart } from '@/composables/useChart'

interface Props {
  label: string
  value: number
}

const props = defineProps<Props>()

const canvas = ref<HTMLElement>()

const display = computed(() => `${props.value.toFixed(1)}%`)

// Amber above 70, red above 90: the same thresholds the panel text uses, so a
// glance at the colour and a glance at the number never disagree.
const tone = computed(() => {
  if (props.value >= 90) return 'var(--hud-red)'
  if (props.value >= 70) return 'var(--hud-amber)'
  return 'var(--hud-cyan)'
})

function build(): EChartsOption {
  const color = tone.value
  return {
    animationDuration: 420,
    series: [
      {
        type: 'gauge',
        startAngle: 220,
        endAngle: -40,
        radius: '96%',
        center: ['50%', '58%'],
        progress: {
          show: true,
          width: 7,
          roundCap: true,
          itemStyle: { color, shadowBlur: 12, shadowColor: color },
        },
        axisLine: { lineStyle: { width: 7, color: [[1, 'rgba(77,216,255,0.14)']] } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        pointer: { show: false },
        anchor: { show: false },
        title: { show: false },
        detail: { show: false },
        data: [{ value: props.value }],
      },
    ],
  }
}

const { redraw } = useChart(canvas, build)

watch(() => props.value, redraw)

onMounted(redraw)
</script>

<style scoped>
.gauge {
  display: flex;
  flex-direction: column;
  align-items: center;
}

.gauge__canvas {
  width: 100%;
  height: 116px;
}

.gauge__foot {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  margin-top: -22px;
}

.gauge__value {
  font-size: 21px;
  text-shadow: 0 0 14px currentColor;
}
</style>
