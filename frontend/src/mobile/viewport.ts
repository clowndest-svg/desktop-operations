/**
 * 手机版和"键盘/视口"有关的两件小事。
 *
 * ## 这里**不再**自己算视口高度
 *
 * 上一版这里有一套 `visualViewport` 逻辑：把可视区高度写进 `--xy-view`，
 * 再把 `offsetTop` 写进 `--xy-pan`，靠 `translateY` 跟着浏览器平移。**那是个错误**，
 * 而且是在真机上才暴露的：
 *
 * 在设置面板里点一个靠底部的输入框时，浏览器会**平移可视区**去把光标露出来
 * （`offsetTop` 变成好几百像素）。那一套逻辑于是把整个界面按这个偏移往下推，
 * 屏幕上就只剩一片黑 —— 进程还活着、没有崩溃日志、前台还是这个应用。
 *
 * 根因是它想修的问题**已经由原生层修掉了**：`MainActivity.liftAboveKeyboard()`
 * 把键盘高度变成 WebView 的下外边距，WebView 自己就变矮了，网页拿到的
 * 布局视口本来就是对的。再叠一层"跟着可视区跑"的修正，只会和浏览器抢同一个自由度。
 *
 * 所以现在的分工是：
 *   - **尺寸**：原生层负责（Android 15+ 的 IME 外边距 / 更老版本的 `adjustResize`），
 *     网页这边只写 `height: 100%`，一个视口单位都不碰；
 *   - **这里**：只做两件网页层才做得到的事 —— 报告键盘开没开、把聚焦的输入框滚进视野。
 */
import { ref } from 'vue'

/** 键盘是否真的弹起来了。用它收起"打字时没必要看"的装饰。 */
export const keyboardOpen = ref(false)

/** 键盘动画大约 250–300ms。太早量到的是旧高度，滚了等于没滚。 */
const SETTLE_MS = 320

/** 见过的最高的可视区 —— 也就是"没有键盘时有多高"。 */
let tallest = 0

function measure(): void {
  const height = window.visualViewport?.height ?? window.innerHeight
  if (height > tallest) tallest = height
  // 80px 是下限：地址栏收放、状态栏变化都能让这两个数差几十像素，
  // 那些不该被当成"键盘弹了"，否则顶栏会跟着一闪一闪。
  keyboardOpen.value = tallest - height > 80
}

/**
 * 装上监听，返回卸载函数。
 *
 * `resize` 和 `scroll` 都要听：键盘弹出让可视区**变小**（resize），
 * 而浏览器为了让光标露出来还会**平移**可视区（scroll）。这里只是重新量一次、
 * 更新那个布尔值，**不写任何影响布局的样式** —— 上一版就是在那条路上把界面推没了。
 */
export function installViewport(): () => void {
  tallest = window.visualViewport?.height ?? window.innerHeight
  measure()
  const viewport = window.visualViewport
  viewport?.addEventListener('resize', measure)
  viewport?.addEventListener('scroll', measure)
  window.addEventListener('orientationchange', measure)

  // 表单里的输入框获得焦点后，把它滚进可视区。
  // 设置面板和"连电脑"是两个可滚动的抽屉，键盘一弹，正在填的那个框常常在键盘后面。
  const onFocus = (event: Event): void => {
    const target = event.target as HTMLElement | null
    if (!target || (target.tagName !== 'INPUT' && target.tagName !== 'TEXTAREA')) return
    window.setTimeout(() => revealInScroller(target), SETTLE_MS)
  }
  document.addEventListener('focusin', onFocus)

  return () => {
    viewport?.removeEventListener('resize', measure)
    viewport?.removeEventListener('scroll', measure)
    window.removeEventListener('orientationchange', measure)
    document.removeEventListener('focusin', onFocus)
  }
}

/**
 * 只滚**最近的那个可滚动祖先**，绝不碰文档。
 *
 * 为什么不用 `scrollIntoView`：它会把所有祖先滚动盒一起滚，**包括文档本身**。
 * 而 `html/body` 上的 `overflow: hidden` 只是不显示滚动条，**程序化滚动照样生效** ——
 * 于是它把整页往上滚了一大段，`.app` 跟着出了屏幕，屏幕上只剩窗口背景（看着像黑屏）。
 *
 * 真机上的表现很能说明问题：聊天页点输入框一切正常（那个框本来就在视野里，
 * `scrollIntoView` 滚了 0），而设置面板里点靠底部的输入框**整屏变黑**，
 * 松开键盘又自己好了。同一个函数，两种结果，差别只在"要不要滚"。
 */
function revealInScroller(target: HTMLElement): void {
  let box = target.parentElement
  while (box && box !== document.body && box !== document.documentElement) {
    const style = window.getComputedStyle(box)
    const scrollable = /auto|scroll/.test(style.overflowY) && box.scrollHeight > box.clientHeight + 4
    if (scrollable) {
      const outer = box.getBoundingClientRect()
      const inner = target.getBoundingClientRect()
      const delta = inner.top + inner.height / 2 - (outer.top + outer.height / 2)
      box.scrollTop += delta
      return
    }
    box = box.parentElement
  }
  // 一个可滚祖先都没有：那这个框本来就在视野里，什么都不用做。
}

/**
 * 主动收起键盘。
 *
 * `blur` 在 Android 上会连带收起输入法，这是唯一一个不依赖原生插件的收法。
 * 用在按下麦克风之前：一张还立着的键盘会盖住底部的"听着…"状态，
 * 用户按下去看到的是键盘，分不清麦克风到底开没开。
 */
export function dismissKeyboard(): void {
  const active = document.activeElement as HTMLElement | null
  if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) {
    active.blur()
  }
}
