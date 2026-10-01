<template>
  <section class="hud-panel chat" :class="{ 'chat--max': expanded }">
    <!--
      The rail only exists in the expanded view. Inline, the session list is a popup
      you have to open; enlarged, it is the left column of the same screen, because
      "which conversation am I in" is a question worth seeing while you read one.
    -->
    <aside v-if="expanded" class="chat__rail">
      <div class="chat__rail-head">
        <span class="hud-label">会话 · {{ sessions.length }}</span>
        <button class="hud-btn" type="button" :disabled="working" @click="createSession">＋新</button>
      </div>
      <ul class="chat__rail-list">
        <li v-for="session in sessions" :key="session.id">
          <button
            class="chat__session"
            :class="{ 'chat__session--on': session.id === activeSession }"
            type="button"
            :title="`${session.title} · ${session.turns} 轮`"
            @click="openSession(session.id)"
          >
            <span class="chat__session-title">{{ session.title }}</span>
            <span class="chat__session-meta">{{ session.turns }} 轮 · {{ dayOf(session.updated_at) }}</span>
          </button>
        </li>
      </ul>
      <p v-if="railError" class="chat__rail-error">{{ railError }}</p>
    </aside>

    <div class="chat__main">
      <header class="hud-title">
        对话 · CONSOLE
        <button
          class="hud-btn chat__talk"
          type="button"
          :disabled="!canTalk"
          :title="talkTitle"
          @click="talk"
        >
          {{ listening ? '聆听中…' : '按一下说' }}
        </button>
        <button class="hud-btn chat__clear" type="button" :disabled="busy || lines.length === 0" @click="clear">
          清空
        </button>
        <button class="hud-btn chat__sessions" type="button" @click="sessionsOpen = true">历史</button>
        <!-- 用量放在对话区而不是设置里：它是"这场对话花了多少"，不是"机器配置"。 -->
        <button class="hud-btn chat__usage" type="button" @click="emit('usage')">用量</button>
        <button class="hud-btn chat__voice" type="button" @click="voicePickOpen = true">音色</button>
        <!--
          放大 is the answer to "对话框太小了": the same conversation, the same state,
          filling the window. It re-positions this one element rather than opening a
          second copy, because two components reading one transcript is how the two
          views start disagreeing about what was said.
        -->
        <button
          class="hud-btn chat__expand"
          type="button"
          :title="expanded ? '收回对话区（Esc）' : '放大到整窗，在弹窗里对话'"
          @click="toggleExpanded"
        >
          {{ expanded ? '收回' : '放大' }}
        </button>
        <!--
          The model picker lives here rather than only in the settings dialog because
          that is where the answer appears: you decide "this reply was too dumb / too
          slow / too expensive" while looking at the reply. It writes the same
          preference the settings screen writes -- one source of truth, so the two can
          never disagree about which model is answering.

          A model whose key is missing is still listed, marked: hiding it would make
          "why is there no gpt option" the question, and the answer is a one-line
          environment variable the operator can set themselves.
        -->
        <select
          v-if="models.length > 1"
          class="hud-field chat__model"
          :value="current"
          :disabled="switching"
          @change="choose"
        >
          <option v-for="entry in models" :key="entry.provider" :value="entry.provider">
            {{ entry.model }}{{ entry.key_set ? '' : '（缺 key）' }}
          </option>
        </select>
      </header>

      <div ref="listRef" class="chat__log">
        <p v-if="lines.length === 0" class="hud-label chat__empty">
          打一句话、或点「按一下说」直接开口。文字问答只需要 API Key；
          语音要在上方点「启用语音」，加载约 30 秒。
        </p>
        <div v-for="(line, index) in lines" :key="index" class="chat__turn" :class="`chat__turn--${line.role}`">
          <div class="chat__bubble">
            <span class="chat__who">{{ line.role === 'user' ? '你' : '小夜' }}</span>
            <div v-if="line.attachments.length" class="chat__files">
              <template v-for="(file, slot) in line.attachments" :key="slot">
                <img v-if="file.kind === 'image' && file.data" class="chat__thumb" :src="file.data" :alt="file.name" />
                <span v-else class="chat__file">{{ file.name }} · {{ formatBytes(file.size) }}</span>
              </template>
            </div>
            <span class="chat__text">{{ line.text }}</span>
          </div>
        </div>
        <!--
          A question with no visible answer is indistinguishable from a hung app, and a
          model turn can take twenty seconds. The bubble appears the moment the request
          leaves, so the silence in between has a shape.
        -->
        <div v-if="busy" class="chat__turn chat__turn--assistant">
          <div class="chat__bubble chat__bubble--thinking">
            <span class="chat__who">小夜</span>
            <span class="chat__dots" role="status" aria-label="思考中"><i></i><i></i><i></i></span>
          </div>
        </div>
        <div v-if="error" class="chat__error">{{ error }}</div>
      </div>

      <!--
        Attachments are shrunk in the page before they go anywhere: a 4 MB photo
        becomes a ~30 KB thumbnail, which is what a chat bubble and a model context
        window both actually want. What cannot be shrunk (video, documents) travels
        as a name and a size, and the panel says so instead of implying the assistant
        watched the film.
      -->
      <div v-if="pending.length" class="chat__pending">
        <span v-for="(file, slot) in pending" :key="slot" class="chat__chip">
          <img v-if="file.kind === 'image' && file.data" :src="file.data" alt="" />
          {{ file.name }}
          <button class="chat__chip-x" type="button" title="去掉这个附件" @click="pending.splice(slot, 1)">×</button>
        </span>
      </div>

      <form class="chat__form" @submit.prevent="send">
        <button class="hud-btn" type="button" title="附加图片 / 视频 / 文件" @click="openFiles">附件</button>
        <input ref="fileRef" type="file" multiple hidden @change="onFiles" />
        <input
          v-model="draft"
          class="hud-field chat__input"
          type="text"
          :disabled="busy"
          placeholder="问点什么…（Enter 发送）"
          maxlength="2000"
        />
        <button class="hud-btn" type="submit" :disabled="busy || draft.trim() === ''">
          {{ busy ? '思考中…' : '发送' }}
        </button>
      </form>

      <p v-if="shownNotice" class="chat__notice">{{ shownNotice }}</p>

        <!--
          Only in the expanded view: what is answering, and how long the conversation
          is. Inline there is no room for it, and enlarged the panel can finally say the
          two things a long conversation makes worth reading without opening 用量.
        -->
        <footer v-if="expanded" class="chat__foot">
          <span class="hud-label">当前模型 {{ currentLabel || '—' }}</span>
          <span class="hud-label">{{ lines.length }} 条 · 会话 {{ activeTitle }}</span>
          <span class="hud-label">Esc 收回</span>
        </footer>
      </div>

    <VoicePickerPopup :open="voicePickOpen" @close="voicePickOpen = false" />
    <SessionsPopup :open="sessionsOpen" @close="sessionsOpen = false" @switched="loadCurrent" />
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  chatAsk,
  fetchModels,
  fetchSessionMessages,
  fetchSessions,
  formatBytes,
  newSession,
  pickModel,
  switchSession,
  type Attachment,
  type ModelChoice,
  type SessionInfo,
} from '@/api/bridge'
import { useVoiceStore } from '@/stores/voice'
import SessionsPopup from '@/components/SessionsPopup.vue'
import VoicePickerPopup from '@/components/VoicePickerPopup.vue'

// The dialog itself lives in App.vue: this panel only asks for it.
const emit = defineEmits<{ (e: 'usage'): void }>()

const voice = useVoiceStore()

const draft = ref('')
const busy = ref(false)
const localError = ref('')
const listRef = ref<HTMLElement | null>(null)

const voicePickOpen = ref(false)
const sessionsOpen = ref(false)

/**
 * What is on screen: the stored turns of the open conversation, then the live
 * turns that arrived since it was loaded.
 *
 * Two sources, one list, no duplicates: the store is read once per session switch
 * and the live feed is sliced from where the seed ended. Reading the store on
 * every poll instead would race the very turns it is supposed to be showing.
 */
interface Line {
  role: string
  text: string
  attachments: Attachment[]
}

const seeded = ref<Line[]>([])
const consumed = ref(0)
const pending = ref<Attachment[]>([])
const overlay = ref<{ text: string; attachments: Attachment[] } | null>(null)
const fileRef = ref<HTMLInputElement | null>(null)

const models = ref<ModelChoice[]>([])
const current = ref('')
const switching = ref(false)
const switchNotice = ref('')

/**
 * The list is pulled, not configured in the page.
 *
 * Which models exist is a fact about the operator's config file and environment,
 * and a second copy of that list in the bundle would be a list that drifts the
 * moment someone adds a provider.
 */
async function loadModels(): Promise<void> {
  try {
    const list = await fetchModels()
    models.value = list.choices ?? []
    current.value = list.current
  } catch {
    // No bridge yet: the picker simply does not appear, and the default model answers.
  }
}

async function choose(event: Event): Promise<void> {
  const provider = (event.target as HTMLSelectElement).value
  if (!provider || provider === current.value) return
  switching.value = true
  try {
    const list = await pickModel(provider)
    if (list.error) {
      // Put the select back where it actually is, rather than leaving it showing
      // a model that is not answering the next question.
      ;(event.target as HTMLSelectElement).value = current.value
      switchNotice.value = `没能切过去：${list.error}`
      return
    }
    models.value = list.choices ?? []
    current.value = list.current
    const chosen = list.choices?.find((entry) => entry.provider === list.current)
    switchNotice.value = chosen
      ? chosen.key_set
        ? `已切到 ${chosen.model}`
        : `已切到 ${chosen.model}，但它缺 ${chosen.key_variable}，问话会失败`
      : '已切换模型'
  } catch (err) {
    ;(event.target as HTMLSelectElement).value = current.value
    switchNotice.value = err instanceof Error ? err.message : String(err)
  } finally {
    switching.value = false
  }
}

onMounted(() => {
  void loadModels()
  void loadCurrent()
  window.addEventListener('jarvis-settings-saved', onSettingsSaved)
})

onBeforeUnmount(() => {
  window.removeEventListener('jarvis-settings-saved', onSettingsSaved)
})

/** The model list is whatever the settings panel last wrote; refetch, don't cache. */
function onSettingsSaved(): void {
  void loadModels()
}

async function loadCurrent(): Promise<void> {
  try {
    const list = await fetchSessions()
    sessions.value = list.sessions ?? []
    activeSession.value = list.current ?? ''
    if (list.error || !list.current) {
      seeded.value = []
      consumed.value = voice.history.length
      return
    }
    const stored = await fetchSessionMessages(list.current)
    seeded.value = stored.messages.map((turn) => ({
      role: turn.role,
      text: turn.content,
      attachments: turn.attachments ?? [],
    }))
    consumed.value = voice.history.length
  } catch {
    seeded.value = []
    consumed.value = voice.history.length
  }
  await scrollToEnd()
}

/**
 * The enlarged conversation view.
 *
 * 「放大」 moves *this* element over the whole window instead of opening a copy of it:
 * a second ChatPanel would have its own `seeded`/`consumed` pair, and two views of
 * one transcript that each decide where the stored part ends is exactly how the two
 * windows start disagreeing about what was said. One component, one state, two sizes.
 */
const expanded = ref(false)
const sessions = ref<SessionInfo[]>([])
const activeSession = ref('')
const railError = ref('')
const working = ref(false)

const activeTitle = computed(
  () => sessions.value.find((entry) => entry.id === activeSession.value)?.title ?? '当前会话',
)
const currentLabel = computed(
  () => models.value.find((entry) => entry.provider === current.value)?.model ?? '',
)

function dayOf(stamp: string): string {
  // The rail has ~200px. A full ISO timestamp there would push the title out, and
  // "which day was this" is the only thing the date is asked.
  return stamp ? stamp.slice(5, 10) : ''
}

async function toggleExpanded(): Promise<void> {
  expanded.value = !expanded.value
  if (expanded.value) {
    window.addEventListener('keydown', onEscape)
    await loadCurrent()
  } else {
    window.removeEventListener('keydown', onEscape)
  }
}

function onEscape(event: KeyboardEvent): void {
  if (event.key === 'Escape' && expanded.value) {
    expanded.value = false
    window.removeEventListener('keydown', onEscape)
  }
}

async function openSession(id: string): Promise<void> {
  if (id === activeSession.value || working.value) return
  working.value = true
  railError.value = ''
  try {
    await switchSession(id)
    await loadCurrent()
  } catch (err) {
    railError.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

async function createSession(): Promise<void> {
  if (working.value) return
  working.value = true
  railError.value = ''
  try {
    await newSession()
    await loadCurrent()
  } catch (err) {
    railError.value = err instanceof Error ? err.message : String(err)
  } finally {
    working.value = false
  }
}

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onEscape)
})

/**
 * The transcript is Python's, not this component's.
 *
 * Spoken turns arrive on the push channel, so a locally-owned list would show the
 * typed half of the conversation and drop the spoken half -- two histories in one
 * chat panel is a reader having to work out which one is real.
 */
const lines = computed<Line[]>(() => [
  ...seeded.value,
  ...voice.history.slice(consumed.value).map((turn) => ({
    role: turn.role,
    text: turn.text,
    attachments: turn.text === overlay.value?.text ? overlay.value.attachments : [],
  })),
])
const error = computed(() => localError.value || voice.error)
const notice = computed(() => voice.notice)
/** A reply to the last click wins over the ambient status line, then fades. */
const shownNotice = computed(() => switchNotice.value || notice.value)
const listening = computed(() => voice.turn === 'listening')
const canTalk = computed(() => voice.enabled && !busy.value)

const talkTitle = computed(() => {
  if (voice.enabled) return '跳过唤醒词，直接说这一句'
  return voice.label
})

async function scrollToEnd(): Promise<void> {
  await nextTick()
  const element = listRef.value
  if (element) element.scrollTop = element.scrollHeight
}

async function talk(): Promise<void> {
  await voice.talk()
  await scrollToEnd()
}

function openFiles(): void {
  fileRef.value?.click()
}

/** Read one picked file: images downscaled to a thumbnail, everything else as facts. */
function readAttachment(file: File): Promise<Attachment> {
  const kind: Attachment['kind'] = file.type.startsWith('image/')
    ? 'image'
    : file.type.startsWith('video/')
      ? 'video'
      : 'file'
  const base: Attachment = {
    name: file.name,
    kind,
    mime: file.type || 'application/octet-stream',
    size: file.size,
  }
  if (kind !== 'image') return Promise.resolve(base)
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onerror = () => resolve(base)
    reader.onload = () => {
      const image = new Image()
      image.onerror = () => resolve(base)
      image.onload = () => {
        const scale = Math.min(1, 512 / Math.max(image.width, image.height))
        const canvas = document.createElement('canvas')
        canvas.width = Math.max(1, Math.round(image.width * scale))
        canvas.height = Math.max(1, Math.round(image.height * scale))
        const context = canvas.getContext('2d')
        if (!context) {
          resolve(base)
          return
        }
        context.drawImage(image, 0, 0, canvas.width, canvas.height)
        resolve({ ...base, data: canvas.toDataURL('image/jpeg', 0.8) })
      }
      image.src = String(reader.result)
    }
    reader.readAsDataURL(file)
  })
}

async function onFiles(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  for (const file of files) {
    if (pending.value.length >= 4) break
    pending.value.push(await readAttachment(file))
  }
}

async function send(): Promise<void> {
  const question = draft.value.trim()
  if (question === '' || busy.value) return
  const attachments = [...pending.value]
  draft.value = ''
  pending.value = []
  overlay.value = attachments.length ? { text: question, attachments } : null
  localError.value = ''
  busy.value = true
  await scrollToEnd()
  try {
    const reply = await chatAsk(question, attachments)
    if (reply.error) {
      // Keep the question in the box: it is the only record of what was attempted.
      localError.value = reply.error
      draft.value = question
      return
    }
    // The turns come back through the state bridge; pull once so the panel does
    // not wait for a push that a quiet microphone will never send.
    await voice.pullSnapshot()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
    draft.value = question
  } finally {
    busy.value = false
    await scrollToEnd()
  }
}

async function clear(): Promise<void> {
  // 清空 opens a fresh conversation on the server; the old one stays stored and
  // reachable from 历史. Wiping it here would make the button mean two things.
  await voice.clearTranscript()
  localError.value = ''
  await loadCurrent()
}
</script>

<style scoped>
/* Sizing and shape come from .hud-field in hud.css so this sits at exactly the
   same height as the buttons either side of it. */
.chat__model {
  color: var(--hud-cyan);
  font-size: 11px;
  max-width: 150px;
  flex: none;
}

.chat__model:disabled {
  opacity: 0.45;
}

/*
 * One column normally; two when enlarged. The rail is a v-if'd sibling, so inline
 * the grid collapses to a single track and `.chat__main` carries the column of
 * header/log/form exactly as before -- the enlarged view adds a column, it does not
 * restyle the conversation.
 */
.chat {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 8px;
  padding: 12px 14px;
  min-height: 0;
}

.chat__main {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
  min-width: 0;
}

/*
 * 放大 lifts this one element over the whole window. Fixed positioning rather than a
 * second component: see the script. The scrim is a box-shadow spread wide enough to
 * darken everything behind it, because a dialog that leaves the dashboard fully
 * readable behind it is not a dialog, it is a second dashboard.
 */
.chat--max {
  position: fixed;
  inset: 18px;
  z-index: 46;
  grid-template-columns: 216px minmax(0, 1fr);
  gap: 14px;
  padding: 14px 16px;
  /*
   * Nearly solid, unlike the inline panel. The panel's normal scrim lets the
   * dashboard read through by design, which is right for a 150px-tall strip and
   * wrong for a conversation you are supposed to read: the trend chart's grid lines
   * came through the bubbles and looked like part of the answer.
   */
  background: linear-gradient(180deg, rgba(9, 22, 38, 0.985), rgba(3, 7, 13, 0.99));
  box-shadow: 0 0 0 2000px rgba(2, 5, 10, 0.72), var(--hud-elev-2);
}

.chat__rail {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 0;
  padding-right: 12px;
  border-right: 1px solid var(--hud-line);
}

.chat__rail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.chat__rail-list {
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

.chat__session {
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 6px 10px;
  border: 1px solid transparent;
  border-radius: var(--hud-radius);
  background: rgba(77, 216, 255, 0.04);
  color: var(--hud-text);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}

.chat__session:hover {
  border-color: var(--hud-rim);
}

.chat__session--on {
  background: rgba(77, 216, 255, 0.14);
  border-color: rgba(77, 216, 255, 0.42);
}

.chat__session-title {
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}

.chat__session-meta {
  font-size: 10px;
  color: var(--hud-dim);
}

.chat__rail-error {
  margin: 0;
  font-size: 11px;
  color: var(--hud-red);
}

.chat__foot {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 8px;
  padding-top: 6px;
  border-top: 1px solid var(--hud-line);
}

/* The enlarged view is where a long read happens, so the type is bigger by design. */
.chat--max .chat__turn {
  font-size: 13px;
  line-height: 1.75;
}

.chat--max .chat__bubble {
  max-width: 76%;
  padding: 8px 14px 9px;
}

.chat--max .chat__log {
  gap: 10px;
}

.chat__talk {
  margin-left: auto;
}

.chat__log {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-right: 2px;
}

.chat__empty {
  line-height: 1.7;
}

.chat__turn {
  display: flex;
  font-size: 12px;
  line-height: 1.6;
}

/*
 * Bubbles, because a wall of left-aligned lines with a two-character label in front
 * of each is a log file, not a conversation. Which side is talking is carried by the
 * fill (the user's side gets the cyan wash), not by a squared-off corner — every
 * corner here is rounded, which is what the rest of the window is too.
 */
.chat__turn--user {
  justify-content: flex-end;
}

.chat__turn--assistant {
  justify-content: flex-start;
}

.chat__bubble {
  max-width: 88%;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 12px 7px;
  border-radius: 16px;
  background: rgba(10, 26, 44, 0.72);
  border: 1px solid rgba(77, 216, 255, 0.14);
}

.chat__turn--user .chat__bubble {
  background: linear-gradient(180deg, rgba(77, 216, 255, 0.16), rgba(77, 216, 255, 0.06));
  border-color: rgba(77, 216, 255, 0.34);
}

.chat__bubble--thinking {
  border-style: dashed;
}

.chat__who {
  color: var(--hud-dim);
  letter-spacing: 0.06em;
  font-size: 10px;
}

.chat__text {
  white-space: pre-wrap;
  word-break: break-word;
}

.chat__turn--assistant .chat__who {
  color: var(--hud-cyan);
}

.chat__turn--assistant .chat__text {
  color: var(--hud-text);
}

/* Three dots, staggered. Deliberately CSS-only: a JS-driven spinner would add one
   more timer to a window that is measured on how little it idles. */
.chat__dots {
  display: inline-flex;
  gap: 4px;
  padding: 5px 2px 3px;
}

.chat__dots i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--hud-cyan);
  animation: chat-think 1.05s infinite ease-in-out;
}

.chat__dots i:nth-child(2) {
  animation-delay: 0.15s;
}

.chat__dots i:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes chat-think {
  0%,
  100% {
    opacity: 0.25;
    transform: translateY(0);
  }
  50% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .chat__dots i {
    animation: none;
    opacity: 0.7;
  }
}

.chat__error {
  color: var(--hud-red);
  font-size: 12px;
}

.chat__form {
  display: flex;
  gap: 8px;
}

.chat__input {
  flex: 1;
  min-width: 0;
}

.chat__input:disabled {
  color: var(--hud-dim);
}

.chat__pending {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chat__chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 3px 10px;
  font-size: 10px;
  color: var(--hud-text);
  border: 1px solid rgba(77, 216, 255, 0.3);
  background: rgba(77, 216, 255, 0.07);
  border-radius: var(--hud-pill);
}

.chat__chip img {
  width: 18px;
  height: 18px;
  object-fit: cover;
  border-radius: var(--hud-radius);
}

.chat__chip-x {
  border: none;
  background: transparent;
  color: var(--hud-dim);
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
}

.chat__chip-x:hover {
  color: var(--hud-red);
}

.chat__files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 4px;
}

.chat__thumb {
  max-width: 120px;
  max-height: 90px;
  border-radius: var(--hud-radius);
  border: 1px solid rgba(77, 216, 255, 0.3);
}

.chat__file {
  padding: 2px 10px;
  font-size: 10px;
  color: var(--hud-dim);
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-pill);
}

.chat__notice {
  margin: 0;
  font-size: 11px;
  color: var(--hud-amber);
}

/*
 * At the window's minimum width the middle column is about 260px, and a title plus
 * three buttons does not fit on one line. Letting the header wrap is the honest
 * answer; the alternative -- which is what happened -- is the buttons riding up over
 * the panel border and into the storage column.
 */
.chat__main > .hud-title {
  flex-wrap: wrap;
  row-gap: 6px;
  min-width: 0;
}

.chat {
  overflow: hidden;
}

.chat__log {
  min-height: 0;
  overflow-y: auto;
}
</style>
