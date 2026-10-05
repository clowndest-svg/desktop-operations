<template>
  <div v-if="open" class="ss" role="dialog" aria-modal="true" aria-label="对话历史">
    <div class="ss__scrim" @click="emit('close')"></div>

    <section class="ss__box">
      <header class="ss__head">
        <h2 class="hud-title">对话历史 · SESSIONS</h2>
        <div class="ss__actions">
          <button
            class="hud-btn"
            type="button"
            :disabled="working || sessions.length === 0"
            :title="allSelected ? '把勾全部去掉' : '勾上每一场对话'"
            @click="toggleAll"
          >
            {{ allSelected ? '取消全选' : '全选' }}
          </button>
          <button
            class="hud-btn ss__del"
            type="button"
            :disabled="working || selected.length === 0"
            @click="askRemoveMany"
          >
            删除所选{{ selected.length ? '（' + selected.length + '）' : '' }}
          </button>
          <button class="hud-btn" type="button" :disabled="working" @click="create">新对话</button>
          <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
        </div>
      </header>

      <p v-if="error" class="ss__error">{{ error }}</p>
      <p v-else-if="sessions.length === 0" class="ss__empty">
        还没有存下来的对话。从这里开始说的每一句都会记进本机数据库，关掉软件也在。
      </p>

      <ul v-else class="ss__list">
        <li v-for="session in sessions" :key="session.id" class="ss__row" :class="{ 'ss__row--on': session.id === current }">
          <label class="ss__pick" :title="selected.includes(session.id) ? '取消勾选' : '勾上，一起删'">
            <input
              type="checkbox"
              :checked="selected.includes(session.id)"
              @change="toggle(session.id)"
            />
          </label>
          <div class="ss__body">
            <strong :title="session.title">{{ session.title }}</strong>
            <em>{{ session.turns }} 轮 · {{ session.updated_at }}</em>
          </div>
          <span class="ss__ops">
            <button v-if="session.id !== current" class="hud-btn" type="button" :disabled="working" @click="openSession(session.id)">
              打开
            </button>
            <span v-else class="ss__now">正在聊</span>
            <button class="hud-btn" type="button" :disabled="working" @click="rename(session)">改名</button>
            <button class="hud-btn ss__del" type="button" :disabled="working" @click="remove(session.id)">删除</button>
          </span>
        </li>
      </ul>

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
        对话存在本机 SQLite 里（与 token 账本同一个库），不上传。「清空」只是开一场新对话，旧的仍在这里。
        勾上几场一起删走的，是上面「删除所选」那一格 —— 和单条删除一样要再确认一次。
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * The stored conversations: open one, rename one, delete one, start a new one.
 *
 * Everything here talks to the transcript store through the bridge; nothing is
 * kept in the page, so what this list shows is what is on disk -- which is the
 * whole point of having moved history out of a JavaScript array.
 */
import { computed, ref, watch } from 'vue'
import {
  deleteSession,
  deleteSessions,
  fetchSessions,
  newSession,
  renameSession,
  switchSession,
  type SessionInfo,
} from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'switched'): void }>()

const sessions = ref<SessionInfo[]>([])
const current = ref('')
const error = ref('')
const working = ref(false)
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
  selected.value = selected.value.includes(id)
    ? selected.value.filter((entry) => entry !== id)
    : [...selected.value, id]
}

function toggleAll(): void {
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
    sessions.value = list.sessions
    current.value = list.current
    // A batch that came back with ids it could not find says so here, not silently:
    // "I ticked five and three went" has to be readable on screen.
    error.value = list.missing?.length
      ? (list.error || '') + ' 没找到的：' + list.missing.join('、')
      : list.error
    selected.value = selected.value.filter((id) => !ids.includes(id))
    emit('switched')
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

async function load(): Promise<void> {
  error.value = ''
  try {
    const list = await fetchSessions()
    sessions.value = list.sessions
    current.value = list.current
    error.value = list.error
    selected.value = selected.value.filter((id) => list.sessions.some((row) => row.id === id))
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function create(): Promise<void> {
  working.value = true
  try {
    const list = await newSession()
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

async function rename(session: SessionInfo): Promise<void> {
  const title = window.prompt('新的名字', session.title)
  if (title === null || title.trim() === '') return
  working.value = true
  try {
    const list = await renameSession(session.id, title)
    sessions.value = list.sessions
    error.value = list.error
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

async function remove(id: string, confirmed = false): Promise<void> {
  if (!confirmed) {
    const target = sessions.value.find((entry) => entry.id === id)
    confirming.value = id
    confirmingTitle.value = target?.title ?? id
    return
  }
  confirming.value = ''
  working.value = true
  try {
    const list = await deleteSession(id)
    sessions.value = list.sessions
    current.value = list.current
    error.value = list.error
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
    if (shown) void load()
  },
)
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
  width: min(620px, 94vw);
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
}

.ss__actions {
  display: flex;
  gap: 6px;
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
  grid-template-columns: 1fr auto;
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

.ss__body strong {
  display: block;
  font-size: 12px;
  color: var(--hud-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 320px;
}

.ss__body em {
  display: block;
  margin-top: 2px;
  font-size: 10px;
  font-style: normal;
  color: var(--hud-dim);
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
</style>
