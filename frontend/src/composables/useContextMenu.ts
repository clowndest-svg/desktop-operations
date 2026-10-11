/**
 * 一处右键菜单，全屏共用。
 *
 * 为什么要自己做：打包版那扇 WebView2 把原生右键菜单关了（那是有道理的 —— 原生菜单里
 * 「打印」「查看网页源代码」对这台助手没有意义），关掉之后**右键就变成了什么都没有**。
 * 用户的第一反应是"这窗口不接受鼠标"，而他要的复制图片 / 复制路径 / 打开所在文件夹
 * 恰好都在那儿。
 *
 * 状态放在模块级而不是组件里：气泡、附件卡片、生图弹窗都要叫它，prop 一路钻下去会
 * 把三层组件变成三层转发。
 */

import { reactive } from 'vue'

export interface MenuAction {
  label: string
  run: () => void | Promise<void>
  /** 不可点也要画出来 —— 消失了用户就不知道这件事存在过。 */
  disabled?: boolean
  /** 删除类的那一行：配色不同，且不许是默认项。 */
  danger?: boolean
}

export interface MenuSeparator {
  separator: true
}

export type MenuEntry = MenuAction | MenuSeparator

export function isSeparator(entry: MenuEntry): entry is MenuSeparator {
  return (entry as MenuSeparator).separator === true
}

interface MenuState {
  open: boolean
  x: number
  y: number
  entries: MenuEntry[]
}

const state = reactive<MenuState>({ open: false, x: 0, y: 0, entries: [] })

export function useContextMenu() {
  /** 在鼠标那儿打开。`event` 用来取坐标，调用方负责 `@contextmenu.prevent`。 */
  function openMenu(event: MouseEvent, entries: MenuEntry[]): void {
    if (!entries.some((entry) => !isSeparator(entry))) return
    state.entries = entries
    state.x = event.clientX
    state.y = event.clientY
    state.open = true
  }

  function closeMenu(): void {
    state.open = false
  }

  return { state, openMenu, closeMenu }
}
