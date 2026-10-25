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
        <input ref="search" v-model="query" class="feature-menu__search" placeholder="搜索功能，例如：画图、手机、提醒" aria-label="搜索功能" />
        <div class="feature-menu__groups">
          <section v-for="group in visibleGroups" :key="group.name">
            <h2>{{ group.name }}</h2>
            <button v-for="item in group.items" :key="item.label" type="button" @click="activate(item)"><span>{{ item.label }}</span><small>{{ item.hint }}</small></button>
          </section>
        </div>
        <p v-if="!visibleGroups.length" class="feature-menu__empty">没有匹配的功能，试试“语音”或“文件”。</p>
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
type Entry = { label: string; hint: string; event?: Feature; action?: () => Promise<unknown> }
const emit = defineEmits<{ (event: Feature): void }>()
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
const groups: { name: string; items: Entry[] }[] = [
  { name: '创作与日常', items: [
    { label: '画图', hint: '描述画面，生成图片', event: 'images' },
    { label: '做视频', hint: '把想法变成一段视频', event: 'videos' },
    { label: '提醒与助手', hint: '日程、提醒和日常事务', event: 'assistant' },
  ] },
  { name: '设备与文件', items: [
    { label: '连接手机', hint: '在手机继续和小夜聊天', event: 'phone' },
    { label: '手机投屏', hint: '把手机画面显示到电脑', event: 'mirror' },
    { label: '电脑操作权限', hint: '管理小夜能做哪些操作', event: 'access' },
    { label: '系统与工具', hint: '资源、进程、磁盘与清理', event: 'systems' },
  ] },
  { name: '个性与设置', items: [
    { label: '形象与声音', hint: '20 个写实形象，搭配喜欢的音色', event: 'portraits' },
    { label: '语音控制', hint: '麦克风、朗读与音频状态', event: 'voiceControls' },
    { label: '外观', hint: '选择界面配色', event: 'skin' },
    { label: '桌面宠物', hint: '显示或收起桌面小伙伴', action: togglePet },
    { label: '扩展', hint: '添加插件与技能', event: 'extensions' },
  ] },
  { name: '记录与窗口', items: [
    { label: '操作记录', hint: '查看执行结果与审计记录', event: 'activity' },
    { label: '提醒与告警', hint: '查看需要留意的状态', event: 'alerts' },
    { label: '最大化 / 还原', hint: '切换窗口大小', action: toggleWindowMax },
    { label: '收进托盘', hint: '在后台继续待命', action: hideWindow },
  ] },
]
const visibleGroups = computed(() => groups.map(group => ({ ...group,
  items: group.items.filter(item => `${item.label} ${item.hint} ${group.name}`.includes(query.value.trim())),
})).filter(group => group.items.length))

function close(): void { opened.value = false; void nextTick(() => trigger.value?.focus()) }
function choose(event: Feature): void { close(); emit(event) }
async function activate(item: Entry): Promise<void> {
  if (item.event) { choose(item.event); return }
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
.bar__tag { font-size:12px; color:#b6abbf; margin-left:8px; }
.bar__button { border:1px solid #bfadcc33; border-radius:10px; padding:8px 15px; min-height:36px; color:#e6dce9; background:#ffffff05; font:inherit; font-size:12px; cursor:pointer; }
.bar__button:hover { background:#c1adca16; border-color:#bfadcc66; }
.bar__alert { background:transparent; border:0; color:var(--hud-amber); cursor:pointer; }
.bar__scrim { position:fixed; z-index:45; inset:0; background:#09070b55; backdrop-filter:blur(3px); }
.feature-menu { position:absolute; top:76px; right:20px; width:min(610px,calc(100vw - 40px)); max-height:calc(100vh - 96px); overflow:auto; box-sizing:border-box; padding:20px; border-radius:18px; border:1px solid #bca9c733; background:#211c28fa; box-shadow:0 24px 100px #0008; }
.feature-menu>header,.feature-menu>footer { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.feature-menu>header strong { font-size:17px; }
.feature-menu__search { width:100%; box-sizing:border-box; padding:12px; margin:18px 0 10px; border:1px solid #bfadcc33; background:#17131c; border-radius:10px; font:inherit; color:var(--hud-text); }
.feature-menu__groups { display:grid; grid-template-columns:1fr 1fr; gap:12px 22px; }
.feature-menu h2 { font-size:11px; font-weight:500; letter-spacing:.1em; color:#b5a8c0; margin:14px 0 8px; }
.feature-menu__groups button { display:flex; flex-direction:column; gap:4px; padding:10px 12px; width:100%; min-height:56px; text-align:left; color:var(--hud-text); border:0; border-radius:10px; background:transparent; font:inherit; cursor:pointer; }
.feature-menu__groups button:hover,.feature-menu__groups button:focus-visible { background:#bc9ecd17; }
.feature-menu__groups span { font-size:13px; }
.feature-menu__groups small { font-size:11px; color:#b3a7bf; }
.feature-menu>footer { margin-top:20px; padding-top:12px; border-top:1px solid #bfadcc22; color:#a69bad; font-size:11px; }
.feature-menu__empty { font-size:13px; color:#b3a7bf; }
@media(max-width:700px) { .bar__tag { display:none; } .bar { padding-inline:16px; } .feature-menu__groups { grid-template-columns:1fr; } }
</style>
