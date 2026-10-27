<template>
  <Teleport to="body">
  <div v-if="open" class="ss" role="dialog" aria-modal="true" aria-label="对话历史">
    <div class="ss__scrim" @click="emit('close')"></div>

    <section class="ss__box">
      <header class="ss__head">
        <h2 class="hud-title">对话历史 · SESSIONS</h2>
        <div class="ss__actions">
          <button
            class="hud-btn"
            type="button"
            :disabled="working || loading || sessions.length === 0"
            :title="allSelected ? '取消已加载对话的勾选' : '只选择当前已加载的结果'"
            @click="toggleAll"
          >
            {{ allSelected ? '取消全选' : '全选已加载' }}
          </button>
          <button
            class="hud-btn ss__del"
            type="button"
            :disabled="working || loading || selected.length === 0"
            @click="askRemoveMany"
          >
            删除所选{{ selected.length ? '（' + selected.length + '）' : '' }}
          </button>
          <button class="hud-btn" type="button" :disabled="working" @click="create">新对话</button>
          <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
        </div>
      </header>

      <form class="ss__search" @submit.prevent="resetSearch">
        <input v-model="query" type="search" maxlength="200" :disabled="working" aria-label="搜索对话标题或内容" placeholder="搜索所有历史的标题或内容…" />
        <label><input v-model="pinnedOnly" type="checkbox" :disabled="working" />只看置顶</label>
        <button class="hud-btn" type="submit" :disabled="working">刷新</button>
      </form>
      <p v-if="error" class="ss__error" role="alert">{{ error }}</p>
      <p v-if="notice" class="ss__empty" role="status">{{ notice }}</p>
      <p v-if="loading && sessions.length === 0" class="ss__empty" role="status">正在查找…</p>
      <p v-else-if="!error && sessions.length === 0" class="ss__empty">
        {{ query.trim() || pinnedOnly ? '没有匹配的对话，试试其他关键词或取消置顶筛选。' : '还没有存下来的对话，开始聊天后会保存在这里。' }}
      </p>

      <ul v-if="sessions.length" class="ss__list" :aria-busy="loading">
        <li v-for="session in sessions" :key="session.id" class="ss__row" :class="{ 'ss__row--on': session.id === current }">
          <label class="ss__pick" :title="selected.includes(session.id) ? '取消勾选' : '勾上，一起删'">
            <input
              type="checkbox"
              :disabled="working || loading"
              :checked="selected.includes(session.id)"
              @change="toggle(session.id)"
            />
          </label>
          <div class="ss__body">
            <strong :title="session.title">{{ session.pinned ? '★ ' : '' }}{{ session.title }}</strong>
            <em>{{ session.turns }} 轮 · {{ stamp(session.updated_at) }}</em>
            <p v-if="session.excerpt" class="ss__excerpt">{{ session.excerpt }}</p>
          </div>
          <span class="ss__ops">
            <button v-if="session.id !== current" class="hud-btn" type="button" :disabled="working || loading" @click="openSession(session.id)">
              打开
            </button>
            <span v-else class="ss__now">正在聊</span>
            <button class="hud-btn" type="button" :disabled="working || loading" :aria-pressed="!!session.pinned" @click="pin(session)">{{ session.pinned ? '取消置顶' : '置顶' }}</button>
            <button class="hud-btn" type="button" :disabled="working || loading" @click="rename(session)">改名</button>
            <button class="hud-btn" type="button" :disabled="working || loading" @click="exportSession(session.id)">导出</button>
            <button class="hud-btn ss__del" type="button" :disabled="working || loading" @click="remove(session.id)">删除</button>
          </span>
        </li>
      </ul>
      <div class="ss__more">
        <span>已显示 {{ sessions.length }} 场{{ selected.length ? ` · 已选 ${selected.length} 场` : '' }}</span>
        <button v-if="hasMore" class="hud-btn" type="button" :disabled="working || loading" @click="load(true)">{{ loading ? '加载中…' : '加载更多' }}</button>
      </div>
      <form v-if="renaming" class="ss__search" @submit.prevent="saveName">
        <input v-model="renameTitle" maxlength="48" aria-label="新的对话名称" :disabled="working" />
        <button class="hud-btn" type="submit" :disabled="working || !renameTitle.trim()">保存名称</button>
        <button class="hud-btn" type="button" :disabled="working" @click="renaming = ''">取消</button>
      </form>

      <!--
        Deletion here is the only place a stored conversation can disappear, and it
        takes a second click on purpose -- the same shape as the junk panel's gate,
        because "my history vanished" and "my files vanished" feel identical from
        the operator's side of the glass.
      -->
      <p v-if="confirming" class="ss__confirm">
        删掉「{{ confirmingTitle }}」？里面的每一句都会没，不能恢复。
        <span class="ss__confirm-ops">
          <button class="hud-btn danger" type="button" :disabled="working" @click="remove(confirming, true)">确认删除</button>
          <button class="hud-btn" type="button" :disabled="working" @click="confirming = ''">取消</button>
        </span>
      </p>

      <p v-if="confirmMany" class="ss__confirm">
        删掉选中的 {{ selected.length }} 场对话？{{ confirmManyTitles }}每一句都会没，不能恢复。
        <span class="ss__confirm-ops">
          <button class="hud-btn danger" type="button" :disabled="working" @click="removeMany">
            确认删除
          </button>
          <button class="hud-btn" type="button" :disabled="working" @click="confirmMany = false">
            取消
          </button>
        </span>
      </p>

      <p class="ss__note">
        搜索覆盖全部本机历史，导出包含完整消息。全选只勾选已加载的结果；更改筛选会清除勾选。删除前请确认，重要对话可以先导出。
      </p>
    </section>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
/**
 * The stored conversations: open one, rename one, delete one, start a new one.
 *
 * Everything here talks to the transcript store through the bridge; nothing is
 * kept in the page, so what this list shows is what is on disk -- which is the
 * whole point of having moved history out of a JavaScript array.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  deleteSession,
  deleteSessions,
  fetchSessions,
  exportConversation,
  newSession,
  pinSession,
  renameSession,
  switchSession,
  type SessionInfo,
} from '@/api/bridge'
import { stamp } from '@/api/time'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'switched'): void }>()

const sessions = ref<SessionInfo[]>([])
const current = ref('')
const error = ref('')
const working = ref(false)
const loading = ref(false)
const query = ref('')
const pinnedOnly = ref(false)
const hasMore = ref(false)
const nextOffset = ref(0)
const notice = ref('')
const renaming = ref('')
const renameTitle = ref('')
let requestVersion = 0
let searchTimer: ReturnType<typeof setTimeout> | undefined
const confirming = ref('')
const confirmingTitle = ref('')
/** The checkbox column: what a batch delete will take. Empty almost always. */
const selected = ref<string[]>([])
const confirmMany = ref(false)

const allSelected = computed(
  () => sessions.value.length > 0 && selected.value.length === sessions.value.length,
)
const confirmManyTitles = computed(() => {
  const titles = selected.value
    .map((id) => sessions.value.find((entry) => entry.id === id)?.title ?? id)
    .slice(0, 3)
  const more = selected.value.length > titles.length ? ' 等' : ''
  return titles.length ? '「' + titles.join('」「') + '」' + more : ''
})

function toggle(id: string): void {
  confirmMany.value = false
  selected.value = selected.value.includes(id)
    ? selected.value.filter((entry) => entry !== id)
    : [...selected.value, id]
}

function toggleAll(): void {
  confirmMany.value = false
  selected.value = allSelected.value ? [] : sessions.value.map((entry) => entry.id)
}

function askRemoveMany(): void {
  if (selected.value.length === 0) return
  confirmMany.value = true
}

async function removeMany(): Promise<void> {
  confirmMany.value = false
  const ids = [...selected.value]
  if (ids.length === 0) return
  working.value = true
  try {
    const list = await deleteSessions(ids)
    // A batch that came back with ids it could not find says so here, not silently:
    // "I ticked five and three went" has to be readable on screen.
    error.value = list.missing?.length
      ? (list.error || '') + ' 没找到的：' + list.missing.join('、')
      : list.error
    selected.value = []
    const problem = error.value
    await load()
    error.value = problem || error.value
    emit('switched')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

async function load(append = false): Promise<void> {
  const version = ++requestVersion
  loading.value = true
  error.value = ''
  try {
    const list = await fetchSessions(query.value, append ? nextOffset.value : 0, 50, pinnedOnly.value)
    if (version !== requestVersion || !props.open) return
    if (list.error) { error.value = list.error; return }
    const existing = new Set(append ? sessions.value.map(row => row.id) : [])
    sessions.value = [...(append ? sessions.value : []), ...list.sessions.filter(row => !existing.has(row.id))]
    current.value = list.current
    hasMore.value = !!list.has_more
    nextOffset.value = list.next_offset ?? sessions.value.length
    selected.value = selected.value.filter(id => sessions.value.some(row => row.id === id))
  } catch (err) {
    if (version === requestVersion) error.value = err instanceof Error ? err.message : String(err)
  } finally {
    if (version === requestVersion) loading.value = false
  }
}

function resetSearch(): void {
  clearTimeout(searchTimer)
  requestVersion++
  selected.value = []
  confirming.value = ''
  confirmMany.value = false
  renaming.value = ''
  sessions.value = []
  hasMore.value = false
  notice.value = ''
  void load()
}

async function create(): Promise<void> {
  working.value = true
  try {
    const list = await newSession()
    if (list.error) { error.value = list.error; return }
    sessions.value = list.sessions
    current.value = list.current
    emit('switched')
    emit('close')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

async function openSession(id: string): Promise<void> {
  working.value = true
  try {
    const list = await switchSession(id)
    if (list.error) { error.value = list.error; return }
    sessions.value = list.sessions
    current.value = list.current
    emit('switched')
    emit('close')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

function rename(session: SessionInfo): void {
  renaming.value = session.id
  renameTitle.value = session.title
  confirming.value = ''
  confirmMany.value = false
}

async function saveName(): Promise<void> {
  if (!renaming.value || !renameTitle.value.trim()) return
  working.value = true
  try {
    const list = await renameSession(renaming.value, renameTitle.value.trim())
    error.value = list.error
    if (!list.error) { renaming.value = ''; await load(); emit('switched') }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

async function pin(session: SessionInfo): Promise<void> {
  working.value = true
  try {
    const list = await pinSession(session.id, !session.pinned)
    error.value = list.error
    if (!list.error) { selected.value = []; await load(); emit('switched') }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally { working.value = false }
}

async function exportSession(id: string): Promise<void> {
  working.value = true
  notice.value = ''
  error.value = ''
  try {
    const result = await exportConversation(id)
    if (!result.ok) error.value = result.error
    else notice.value = `已导出 ${result.count} 条消息：${result.path}`
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally { working.value = false }
}

async function remove(id: string, confirmed = false): Promise<void> {
  if (!confirmed) {
    confirmMany.value = false
    const target = sessions.value.find((entry) => entry.id === id)
    confirming.value = id
    confirmingTitle.value = target?.title ?? id
    return
  }
  confirming.value = ''
  working.value = true
  try {
    const list = await deleteSession(id)
    error.value = list.error
    if (!list.error) { selected.value = selected.value.filter(entry => entry !== id); await load() }
    emit('switched')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

watch(
  () => props.open,
  (shown) => {
    clearTimeout(searchTimer)
    requestVersion++
    loading.value = false
    if (shown) resetSearch()
  },
)
watch([query, pinnedOnly], () => {
  clearTimeout(searchTimer)
  requestVersion++
  selected.value = []
  sessions.value = []
  confirming.value = ''
  confirmMany.value = false
  renaming.value = ''
  hasMore.value = false
  if (props.open) {
    loading.value = true
    searchTimer = setTimeout(resetSearch, 250)
  }
})
onBeforeUnmount(() => { clearTimeout(searchTimer); requestVersion++ })
</script>

<style scoped>
.ss__pick {
  margin-right: 10px;
}

.ss {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.ss__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.ss__box {
  position: relative;
  width: min(900px, 94vw);
  box-sizing: border-box;
  max-height: 82vh;
  display: flex;
  flex-direction: column;
  padding: 16px 20px 14px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.ss__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.ss__actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.ss__error,
.ss__empty {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.7;
}

.ss__error {
  color: var(--hud-red);
}

.ss__empty {
  color: var(--hud-dim);
}

.ss__list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ss__row {
  display: grid;
  /* minmax(0, 1fr)：不给 0 下限，长标题会把整行撑开，右边那三枚按钮被挤出弹窗。 */
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  padding: 6px 9px;
  border: 1px solid transparent;
  border-radius: var(--hud-radius);
}

.ss__row--on {
  border-color: rgba(77, 216, 255, 0.4);
  background: rgba(77, 216, 255, 0.07);
}

/*
 * 一行摆完：标题、几轮、什么时候聊过，然后才是那三枚按钮。
 * 以前标题和"4 轮 · 时间"是上下两行，一条对话占两块高度，列表里翻三场就到底了。
 */
.ss__body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.ss__body strong {
  font-size: 12px;
  color: var(--hud-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 0 1 auto;
  min-width: 0;
}

.ss__body em {
  font-size: 10px;
  font-style: normal;
  color: var(--hud-dim);
  flex: none;
  /* 时间戳是这行里最不该被挤掉的一段：宁可省略标题。 */
  font-variant-numeric: tabular-nums;
}

.ss__ops {
  display: flex;
  gap: 5px;
  align-items: center;
}

.ss__now {
  font-size: 10px;
  color: var(--hud-cyan);
  padding: 2px 8px;
}

.ss__del {
  color: var(--hud-red);
}

.ss__confirm {
  border-radius: var(--hud-radius);
  margin: 10px 0 0;
  padding: 7px 9px;
  font-size: 11px;
  line-height: 1.7;
  color: var(--hud-red);
  border: 1px solid rgba(255, 93, 93, 0.45);
  background: rgba(255, 93, 93, 0.08);
}

.ss__confirm-ops {
  display: inline-flex;
  gap: 6px;
  margin-left: 8px;
}

.ss__note {
  margin: 10px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--hud-line);
  font-size: 10px;
  line-height: 1.6;
  color: var(--hud-dim);
}
.ss__search { display: flex; gap: 10px; align-items: center; margin: 0 0 12px; flex-wrap: wrap; }
.ss__search > input { flex: 1; min-width: 160px; padding: 10px; border: 1px solid var(--hud-line); border-radius: var(--hud-radius); color: var(--hud-text); background: rgba(0,0,0,.2); font: inherit; }
.ss__search label, .ss__more { font-size: 12px; color: var(--hud-dim); }
.ss__more { display: flex; align-items: center; justify-content: space-between; padding: 10px 0; }
.ss__excerpt { margin: 0; font-size: 12px; color: var(--hud-dim); overflow-wrap: anywhere; white-space: pre-wrap; }
.ss__empty { overflow-wrap: anywhere; }
@media (max-width: 720px) {
  .ss__row { grid-template-columns: auto minmax(0, 1fr); }
  .ss__ops { grid-column: 2; flex-wrap: wrap; }
  .ss__box { padding: 14px; }
}
</style>
