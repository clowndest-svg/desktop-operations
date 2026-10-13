import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { LineSeriesOption } from 'echarts/charts'
import type { GridComponentOption, TooltipComponentOption } from 'echarts/components'
import { onBeforeUnmount, onMounted, shallowRef, type Ref, type ShallowRef } from 'vue'

/*
 * `echarts/core` plus a register call, not `import * as echarts from 'echarts'`.
 *
 * The barrel entry pulls in every chart type ECharts has -- bar, pie, radar, map,
 * graph, tree, sankey, candlestick, gauge -- and both renderers, and the whole lot
 * landed in the one chunk the window loads: measured at 2,381,870 bytes, against
 * `echarts.min.js`'s own 1,096 KB on disk. This window draws exactly one picture,
 * the two lines in TrendPanel, so these four are the whole bill.
 *
 * Registering by hand has a failure mode worth knowing about: an unregistered piece
 * does not raise, it silently draws nothing. `ChartOption` below is the guard --
 * composing the option type from the same four modules makes a `type: 'bar'` series
 * a compile error rather than an empty panel, and `tests/test_ui_bundle.py` holds
 * the byte ceiling so nobody reintroduces the barrel by accident.
 */
echarts.use([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

/** The only option shape this window can render: see the note above. */
export type ChartOption = echarts.ComposeOption<
  LineSeriesOption | GridComponentOption | TooltipComponentOption
>

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
  build: () => ChartOption,
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
