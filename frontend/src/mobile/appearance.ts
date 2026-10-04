/**
 * 深浅两套外观。
 *
 * 为什么和 `src/theme.ts` 那六套皮肤分开：那六套换的是**色相**（蓝/青/紫/翠/琥珀/玫），
 * 底色一直是深色玻璃。这里换的是**明暗**，是另一根轴。两件事混成一个列表，用户就得在
 * 九个选项里挑一个"蓝的浅色"——那不是选择，是排列组合。
 *
 * 存在 `localStorage` 而不是 Python 的偏好里，和皮肤同一个理由：它是**这一块屏幕**的属性，
 * 必须在第一帧之前就生效（先闪一下深色再变白，读起来就是"App 启动不对"），
 * 不值得为它在每次启动时多跑一趟桥。
 */
import { ref } from 'vue'

export type Appearance = 'dark' | 'light'

const STORAGE_KEY = 'xiaoye.appearance'

function stored(): Appearance {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    return saved === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export const appearance = ref<Appearance>(stored())

/** 地址栏配色得跟着外观走，否则浅色页面顶着一圈深色状态栏。 */
function syncBrowserChrome(): void {
  const meta = document.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute('content', appearance.value === 'light' ? '#f1f4f8' : '#070d15')
}

export function applyAppearance(): void {
  document.documentElement.dataset.appearance = appearance.value
  syncBrowserChrome()
}

export function setAppearance(next: Appearance): void {
  appearance.value = next
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // 无痕模式或锁死的 WebView：这一轮照样生效，只是下次要重新选。
  }
  applyAppearance()
}

export function toggleAppearance(): void {
  setAppearance(appearance.value === 'light' ? 'dark' : 'light')
}
