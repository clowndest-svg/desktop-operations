import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { onBeforeUnmount, onMounted, shallowRef, type Ref, type ShallowRef } from 'vue'

/**
 * Mount an ECharts instance on an element, keep it sized to its container, and
 * tear it down on unmount. Without the dispose the chart leaks a canvas and a
 * resize observer every time a panel remounts.
 *
 * `shallowRef` rather than `ref`: a chart is a heavyweight foreign object, and
 * Vue's deep reactive proxy over it is both pointless and type-hostile.
 */
export function useChart(
  element: Ref<HTMLElement | undefined>,
  build: () => EChartsOption,
): { chart: ShallowRef<echarts.EChartsType | null>; redraw: () => void } {
  const chart = shallowRef<echarts.EChartsType | null>(null)
  let observer: ResizeObserver | undefined

  onMounted(() => {
    if (!element.value) return
    chart.value = echarts.init(element.value, undefined, { renderer: 'canvas' })
    chart.value.setOption(build())
    observer = new ResizeObserver(() => chart.value?.resize())
    observer.observe(element.value)
  })

  onBeforeUnmount(() => {
    observer?.disconnect()
    observer = undefined
    chart.value?.dispose()
    chart.value = null
  })

  function redraw(): void {
    chart.value?.setOption(build(), true)
  }

  return { chart, redraw }
}
