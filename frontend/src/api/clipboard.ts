/**
 * 往剪贴板放一段字，并且**说清楚有没有放进去**。
 *
 * 面板上这些东西（地址、六位配对码、证书指纹、那条 PowerShell 命令）是要用手机照着敲的，
 * 六个字符的指纹打错一位就是"连不上，不知道为什么"。所以复制这个动作不能假装成功：
 * `execCommand('copy')` 是会返回 false 的，`navigator.clipboard` 在非安全上下文里干脆是
 * undefined —— 只 catch 不回报，按钮按下去什么都没发生，比没有这个按钮更坏。
 *
 * 手机那边 `mobile/MessageBubble.vue` 有一份自己的同名逻辑，这里没有复用：那个文件在
 * `mobile/` 下、顶层还引着 Capacitor 的东西，桌面包不该为了一段剪贴板代码去碰一个
 * 别人正在改的移动端文件。两份的差别是这份**回报结果**，改动往这份收。
 */

export type CopyOutcome = 'copied' | 'empty' | 'failed'

/** Copy a piece of text. Failures are returned, not thrown -- showing one is the caller's job. */
export async function copyText(value: string): Promise<CopyOutcome> {
  if (!value.trim()) return 'empty'
  try {
    await navigator.clipboard.writeText(value)
    return 'copied'
  } catch {
    // 没有剪贴板权限、页面不是安全上下文、或者就是没实现。下面走老路。
  }
  let box: HTMLTextAreaElement | null = null
  try {
    box = document.createElement('textarea')
    box.value = value
    box.setAttribute('readonly', '')
    box.style.position = 'fixed'
    box.style.top = '-1000px'
    box.style.opacity = '0'
    document.body.appendChild(box)
    box.select()
    // 返回值必须看：execCommand 失败时不抛异常，只给你个 false。
    return document.execCommand('copy') ? 'copied' : 'failed'
  } catch {
    return 'failed'
  } finally {
    // 每按一次留一个隐藏 textarea 在 DOM 里，按二十次就是二十个。选区也会把后面的点击带歪。
    if (box) {
      box.blur()
      box.remove()
    }
  }
}
