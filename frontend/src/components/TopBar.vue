<template>
  <header class="bar">
    <div class="bar__brand" :title="`版本 ${info.version} · ${info.engine}${info.built ? ' · ' + info.built : ''}`">
      <img class="bar__mark" :src="brandLogo" alt="小夜 AI 语音助手" width="40" height="40" />
      <strong>{{ info.name }}</strong><span class="bar__tag">陪你聊聊，也帮你做事</span>
    </div>
    <nav class="bar__actions" aria-label="主要功能">
      <button v-if="alertCount" class="bar__alert" type="button" @click="emit('alerts')">{{ alertCount }} 条提醒</button>
      <button ref="trigger" class="bar__button" type="button" :aria-expanded="opened" aria-controls="feature-menu" @click="opened = !opened">功能 <span aria-hidden="true">⌄</span></button>
      <button class="bar__button" type="button" @click="choose('settings')">设置</button>
    </nav>
    <div v-if="opened" class="bar__scrim" @click.self="close" @keydown.esc.stop="close">
      <section id="feature-menu" ref="menu" class="feature-menu" role="dialog" aria-modal="true" aria-label="功能">
        <header><strong>想做点什么？</strong><button class="bar__button" type="button" aria-label="关闭功能菜单" @click="close">关闭</button></header>
        <input ref="search" v-model="query" class="feature-menu__search" placeholder="搜索功能，例如：画图、知识库、手机" aria-label="搜索功能" />
        <div class="feature-menu__columns">
          <div v-for="(column, index) in columns" :key="index" class="feature-menu__column">
            <section v-for="group in column" :key="group.name" class="feature-menu__group">
              <h2>{{ group.name }}</h2>
              <button v-for="item in group.items" :key="item.label" type="button" @click="activate(item)"><span>{{ item.label }}</span><small>{{ item.hint }}</small></button>
            </section>
          </div>
        </div>
        <p v-if="!visibleGroups.length" class="feature-menu__empty">没有匹配的功能，试试“语音”“知识库”或“文件”。</p>
        <footer><span>{{ voice.label }}</span><span>{{ info.built ? '构建 ' + info.built : 'v' + info.version }}</span></footer>
        <p v-if="problem" role="alert">{{ problem }}</p>
      </section>
    </div>
  </header>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import brandLogo from '@/assets/brand/logo.webp'
import { fetchAppInfo, hideWindow, togglePet, toggleWindowMax, type AppInfo } from '@/api/bridge'
import { useSystemStore } from '@/stores/system'
import { useVoiceStore } from '@/stores/voice'

type Feature = 'settings' | 'activity' | 'alerts' | 'access' | 'phone' | 'mirror'
  | 'images' | 'videos' | 'assistant' | 'extensions' | 'skin' | 'systems' | 'voiceControls' | 'portraits'
  | 'usage' | 'sessions'
/** `tab` 只给「提醒与助手」用：那一张弹窗里装着记忆、知识库和自动化三个页签。 */
type Entry = { label: string; hint: string; keys?: string; event?: Feature; tab?: string; action?: () => Promise<unknown> }
type Group = { name: string; items: Entry[] }
const emit = defineEmits<{ (event: Feature, tab?: string): void }>()
const store = useSystemStore()
const voice = useVoiceStore()
const info = ref<AppInfo>({ name: '小夜', version: '--', engine: '--' })
const opened = ref(false)
const query = ref('')
const problem = ref('')
const menu = ref<HTMLElement | null>(null)
const search = ref<HTMLInputElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const alertCount = computed(() => store.alerts.length + store.warnings.length)
const groups: Group[] = [
  { name: '创作与日常', items: [
    { label: '画图', hint: '描述画面，生成图片', event: 'images' },
    { label: '做视频', hint: '把想法变成一段视频', event: 'videos' },
    { label: '提醒与助手', hint: '日程、提醒和日常事务', keys: '待办 闹钟 计划', event: 'assistant', tab: 'reminders' },
  ] },
  { name: '知识与记忆', items: [
    { label: '长期记忆', hint: '她记住的事实、偏好和片段', keys: 'memory 回忆 忘记', event: 'assistant', tab: 'memory' },
    { label: '知识库', hint: '喂资料进去，让她按资料回答', keys: 'knowledge 文档 检索 向量', event: 'assistant', tab: 'knowledge' },
    { label: '自动化', hint: '定时任务与固定流程', keys: 'automation 工作流 定时', event: 'assistant', tab: 'automation' },
  ] },
  { name: '设备与文件', items: [
    { label: '连接手机', hint: '在手机继续和小夜聊天', event: 'phone' },
    { label: '手机投屏', hint: '把手机画面显示到电脑', event: 'mirror' },
    { label: '电脑操作权限', hint: '管理小夜能做哪些操作', event: 'access' },
    { label: '系统与工具', hint: '资源、进程、磁盘与清理', event: 'systems' },
  ] },
  { name: '个性与设置', items: [
    { label: '形象与声音', hint: '20 个写实形象，搭配喜欢的音色', event: 'portraits' },
    { label: '语音控制', hint: '麦克风、朗读与音频状态', keys: '唤醒词 音量 语速', event: 'voiceControls' },
    { label: '外观', hint: '选择界面配色', keys: '皮肤 主题', event: 'skin' },
    { label: '桌面宠物', hint: '显示或收起桌面小伙伴', action: togglePet },
    { label: '扩展', hint: '添加插件与技能', keys: '市场 MCP 技能', event: 'extensions' },
  ] },
  { name: '记录与窗口', items: [
    { label: '操作记录', hint: '查看执行结果与审计记录', event: 'activity' },
    { label: '对话历史', hint: '找回之前聊过的每一场', keys: '会话 搜索 钉选', event: 'sessions' },
    { label: '用量', hint: '这些回答花了多少 token 和钱', keys: 'token 花费 统计', event: 'usage' },
    { label: '提醒与告警', hint: '查看需要留意的状态', event: 'alerts' },
    { label: '最大化 / 还原', hint: '切换窗口大小', action: toggleWindowMax },
    { label: '收进托盘', hint: '在后台继续待命', action: hideWindow },
  ] },
]
const visibleGroups = computed(() => groups.map(group => ({ ...group,
  items: group.items.filter(item => `${item.label} ${item.hint} ${item.keys ?? ''} ${group.name}`.includes(query.value.trim())),
})).filter(group => group.items.length))
/**
 * 两列各自是一条纵向流，而不是四张卡片铺进一个行优先网格。
 *
 * 网格按行取最高项，右列比左列多一项时整行被抬高，短列尾部就是一大片空白 ——
 * 上一版「扩展」孤零零挂在左列末尾、下面空掉四分之一屏，就是这么来的。
 *
 * 分法是**只找一个切点**：前 k 组进左列、其余进右列，取两列高度差最小的那个 k。
 * 分组顺序因此原样保留（先读左列再读右列仍然对得上"创作→知识→设备→个性→记录"），
 * 而贪心做不到这一点 —— 上一版按"补进较矮的一列"分配，最后那组 6 项整块砸进已经
 * 有 9 行的左列，实测两列差出 291 px。
 */
const columns = computed<Group[][]>(() => {
  const list = visibleGroups.value
  if (list.length < 2) return [list, []]
  const cost = (group: Group): number => group.items.length + 1
  const total = list.reduce((sum, group) => sum + cost(group), 0)
  let best = 1
  let bestGap = Number.POSITIVE_INFINITY
  let walked = 0
  for (let cut = 1; cut < list.length; cut += 1) {
    walked += cost(list[cut - 1]!)
    const gap = Math.abs(total - walked * 2)
    if (gap < bestGap) { bestGap = gap; best = cut }
  }
  return [list.slice(0, best), list.slice(best)]
})

function close(): void { opened.value = false; void nextTick(() => trigger.value?.focus()) }
function choose(event: Feature, tab?: string): void { close(); emit(event, tab) }
async function activate(item: Entry): Promise<void> {
  if (item.event) { choose(item.event, item.tab); return }
  try { await item.action?.(); close() }
  catch { problem.value = '操作未完成，请稍后重试。' }
}
function trap(event: KeyboardEvent): void {
  if (!opened.value) return
  if (event.key === 'Escape') { event.preventDefault(); close(); return }
  if (event.key !== 'Tab') return
  const entries = Array.from(menu.value?.querySelectorAll<HTMLElement>('button,input') ?? [])
  const first = entries[0], last = entries.at(-1)
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
async function onSettingsSaved(): Promise<void> {
  try { info.value = await fetchAppInfo(); document.title = info.value.name } catch { /* Keep last known name. */ }
}
watch(opened, async value => {
  if (value) { query.value = ''; problem.value = ''; await nextTick(); search.value?.focus() }
})
onMounted(() => { void onSettingsSaved(); window.addEventListener('jarvis-settings-saved', onSettingsSaved); document.addEventListener('keydown', trap) })
onBeforeUnmount(() => { window.removeEventListener('jarvis-settings-saved', onSettingsSaved); document.removeEventListener('keydown', trap) })
</script>

<style scoped>
.bar { display:flex; align-items:center; justify-content:space-between; gap:12px; height:68px; flex:none; padding:10px 24px; background:#18151edb; border-bottom:1px solid #bca9c71c; }
.bar__brand,.bar__actions { display:flex; align-items:center; gap:12px; }
.bar__mark { border-radius:50%; flex:none; }
.bar__brand strong { font-size:18px; letter-spacing:.12em; color:var(--hud-text); }
.bar__tag { font-size:12px; color:var(--hud-dim); margin-left:8px; }
.bar__button { border:1px solid var(--hud-line); border-radius:10px; padding:8px 15px; min-height:36px; white-space:nowrap; flex:none; color:var(--hud-text); background:var(--hud-glow); font:inherit; font-size:12px; cursor:pointer; }
.bar__button:hover { border-color:var(--hud-rim); background:var(--hud-glow); }
.bar__alert { background:transparent; border:0; color:var(--hud-amber); cursor:pointer; white-space:nowrap; flex:none; }
.bar__scrim { position:fixed; z-index:45; inset:0; background:#09070b55; backdrop-filter:blur(3px); }
.feature-menu { position:absolute; top:76px; right:20px; width:min(680px,calc(100vw - 40px)); max-height:calc(100vh - 96px); overflow:auto; box-sizing:border-box; padding:18px 20px 14px; border-radius:18px; border:1px solid var(--hud-line); background:var(--hud-scrim); box-shadow:0 24px 100px #0008; }
.feature-menu>header,.feature-menu>footer { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.feature-menu>header strong { font-size:17px; color:var(--hud-text); }
.feature-menu__search { display:block; width:100%; box-sizing:border-box; min-height:38px; padding:0 12px; margin:14px 0 14px; border:1px solid var(--hud-line); background:var(--hud-bg-deep); border-radius:10px; font:inherit; font-size:13px; color:var(--hud-text); }
.feature-menu__search:focus-visible { outline:none; border-color:var(--hud-rim); }
.feature-menu__search::placeholder { color:var(--hud-dim); }
.feature-menu__columns { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:0 26px; align-items:start; }
.feature-menu__column { display:flex; flex-direction:column; gap:16px; min-width:0 }
.feature-menu__group { display:flex; flex-direction:column; gap:2px; min-width:0; }
.feature-menu__group h2 { font-size:11px; font-weight:500; letter-spacing:.1em; color:var(--hud-dim); margin:0 0 5px; }
.feature-menu__group button { display:flex; flex-direction:column; gap:1px; padding:7px 10px; width:100%; min-height:44px; text-align:left; color:var(--hud-text); border:0; border-radius:10px; background:transparent; font:inherit; cursor:pointer; }
.feature-menu__group button:hover,.feature-menu__group button:focus-visible { background:var(--hud-glow); }
.feature-menu__group span { font-size:13px; line-height:1.55; }
.feature-menu__group small { font-size:11px; line-height:1.45; color:var(--hud-dim); }
.feature-menu>footer { margin-top:16px; padding:10px 0 2px; border-top:1px solid var(--hud-line); color:var(--hud-dim); font-size:11px; }
.feature-menu>footer span { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.feature-menu__empty { margin:14px 0; font-size:13px; color:var(--hud-dim); }
.feature-menu p[role='alert'] { margin:8px 0 0; font-size:12px; color:var(--hud-red); }
@media(max-width:700px) { .bar__tag { display:none; } .bar { padding-inline:16px; } .feature-menu__columns { grid-template-columns:1fr; } .feature-menu__column { gap:14px; } }
</style>
