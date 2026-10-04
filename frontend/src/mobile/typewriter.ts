/**
 * 逐字上屏。
 *
 * **这不是真流式**，这一点得写在最前面：答案在放第一个字之前就已经整段在手里了。
 * 真的那条要 SSE + 逐块读取，而电脑端的 RPC 是请求-响应、独立模式那条一次拿回整段，
 * 两条路都没有分块。把它当"流式"讲出去，就是在拿观感冒充能力。
 *
 * 那为什么还要做：一段六百字的答案"啪"地整块盖上来，和它一个字一个字蹦出来，
 * 读起来是两种东西 —— 前者像查询结果，后者像有人在说话。豆包那种手感有一半来自这里。
 *
 * 关键是**总时长固定**：按字数定速度的话，短答案一闪而过、长答案要等半分钟。
 * 这里固定大约一秒放完，多长都一样 —— 手感一致，而且不惩罚长答案。
 */
import { ref, type Ref } from 'vue'

export interface Reveal {
  /** 当前该显示出来的那一段。 */
  text: Ref<string>
  /** 还在放。界面据此画光标、给"显示全部"出口。 */
  active: Ref<boolean>
  start(full: string): Promise<void>
  /** 立刻放完。 */
  skip(): void
  /** 放弃这一段（比如用户把它删了）。 */
  cancel(): void
}

const TOTAL_MS = 1050
const TICK_MS = 28

export function useReveal(): Reveal {
  const text = ref('')
  const active = ref(false)
  let full = ''
  let timer: number | null = null
  let settle: (() => void) | null = null

  /** 收摊。**必须把等待中的 Promise 也放掉** —— 只清定时器的话，外面那个
   * `await start()` 会永远挂着，调用方后面的代码（写历史、清 busy、免提接着听）
   * 全都不会执行。这种"停在那里不报错"的 bug 在真机上最难看出来。 */
  function stop(): void {
    if (timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
    active.value = false
    const done = settle
    settle = null
    done?.()
  }

  function start(next: string): Promise<void> {
    stop()
    full = next
    text.value = ''
    if (!next) return Promise.resolve()
    return new Promise<void>((resolve) => {
      settle = resolve
      active.value = true
      const ticks = Math.max(1, Math.round(TOTAL_MS / TICK_MS))
      const step = Math.max(1, Math.ceil(next.length / ticks))
      timer = window.setInterval(() => {
        if (text.value.length + step >= full.length) {
          text.value = full
          stop()
          return
        }
        text.value = full.slice(0, text.value.length + step)
      }, TICK_MS)
    })
  }

  function skip(): void {
    if (!active.value) return
    text.value = full
    stop()
  }

  return { text, active, start, skip, cancel: stop }
}
