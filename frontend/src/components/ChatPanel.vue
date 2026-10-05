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
        <button
          class="hud-btn chat__voice"
          type="button"
          title="换她说话的声音（试听不用先选上）"
          @click="emit('voice')"
        >
          音色
        </button>
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

          Two dropdowns, not one, because a key belongs to a provider and a provider
          offers several models. The first picks the vendor, the second picks which of
          its models answers; collapsing them into one list would repeat the vendor on
          every row and lose the "switch vendor, keep the model when it exists there"
          gesture.

          A model whose key is missing is still listed, marked: hiding it would make
          "why is there no gpt option" the question, and the answer is a one-line
          environment variable the operator can set themselves.
        -->
        <select
          v-if="providers.length > 1"
          class="hud-field chat__model"
          :value="provider"
          :disabled="switching"
          title="服务商"
          @change="chooseProvider"
        >
          <option v-for="entry in providers" :key="entry.name" :value="entry.name">
            {{ entry.name }}{{ entry.key_set ? '' : '（缺 key）' }}
          </option>
        </select>
        <select
          v-if="models.length > 1"
          class="hud-field chat__model"
          :value="model"
          :disabled="switching"
          title="该服务商下的大模型"
          @change="chooseModel"
        >
          <option v-for="entry in models" :key="entry.id" :value="entry.id">{{ entry.label }}</option>
        </select>
        <!--
          Thinking budget and context size are per model, so switching a model carries
          its own pair along. Both are one line next to the conversation on purpose: a
          knob you have to open a dialog to reach is a knob nobody turns mid-question.
        -->
        <select
          v-if="thinkingLevels.length"
          class="hud-field chat__knob"
          :value="thinking"
          :disabled="switching"
          :title="thinkingTitle"
          @change="saveThinking"
        >
          <option v-for="level in thinkingLevels" :key="level" :value="level">{{ THINKING_LABELS[level] ?? level }}</option>
        </select>
        <select
          v-if="turnsBounds.length === 2"
          class="hud-field chat__knob"
          :value="String(turns)"
          :disabled="switching"
          title="每次请求带上的历史轮数"
          @change="saveTurns"
        >
          <option v-for="count in turnOptions" :key="count" :value="String(count)">{{ count }} 轮上下文</option>
        </select>
        <!--
          协作形态。三种形态只差一件事：谁能看见谁说的话。
          价格写在这一行里，因为点下去之后才知道"这一句问了九次"是没有回头路的。
        -->
        <select
          class="hud-field chat__knob"
          :class="{ 'chat__knob--on': !single }"
          :value="mode"
          :title="modeTitle"
          @change="setMode"
        >
          <option v-for="key in MODE_ORDER" :key="key" :value="key">{{ MODE_LABELS[key] }}</option>
        </select>
      </header>

      <!--
        椅子。顺序就是点它的顺序，而"顺序意味着什么"跟着形态变：圆桌里第一家负责合并，
        主管模式里第一家只拆和收、不动手，投票里顺序无所谓。所以标题跟着形态换字。
      -->
      <div v-if="!single" class="chat__seats">
        <button
          v-for="option in seatOptions"
          :key="option.key"
          class="hud-btn chat__seat"
          :class="{ 'chat__seat--on': picked(option.key) }"
          type="button"
          :title="option.vision"
          @click="pickSeat(option.key)"
        >
          {{ option.label }}<small v-if="option.sight"> · {{ option.sight }}</small>
        </button>
        <select
          v-if="mode === 'table'"
          class="hud-field chat__seat-rounds"
          :value="String(rounds)"
          title="讨论几轮：每轮每家说一次，说完再合并"
          @change="setRounds"
        >
          <option v-for="count in [1, 2, 3]" :key="count" :value="String(count)">{{ count }} 轮</option>
        </select>
        <span v-else-if="mode === 'boss'" class="hud-label chat__seat-hint">
          第一家是主管（只拆和收，不干活），{{ Math.max(0, seats.length - 1) }} 家干活
        </span>
        <span v-else-if="mode === 'vote'" class="hud-label chat__seat-hint">
          各答各的，然后匿名互评
        </span>
        <span class="chat__seat-cost hud-num">≈ {{ requests }} 次请求</span>
      </div>

      <!--
        One row per conversation, each with its own status. The strip reads the pushed
        snapshot rather than keeping score locally, because two tabs can be answering at
        the same moment and "which one is still working" has exactly one right answer.
      -->
      <nav v-if="tabs.length > 1 || showTabStrip" class="chat__tabs" aria-label="对话列表">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          class="hud-btn chat__tab"
          :class="{ 'chat__tab--active': tab.active, 'chat__tab--busy': tab.status === 'running' }"
          type="button"
          :title="tabTitle(tab)"
          @click="openTab(tab.id)"
        >
          <i class="chat__tab-dot" aria-hidden="true"></i>
          <span class="chat__tab-name">{{ tab.title || '新对话' }}</span>
          <small v-if="tab.status === 'running'" class="chat__tab-phase">{{ tab.phase }}</small>
          <small v-else-if="tab.status === 'failed'" class="chat__tab-phase">没答上</small>
        </button>
        <button class="hud-btn chat__tab chat__tab--new" type="button" title="另开一个对话" @click="addTab">
          ＋
        </button>
      </nav>

      <div ref="listRef" class="chat__log" @scroll.passive="onLogScroll">
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
            <!--
              Collapsed by default and only drawn when there is something in it: the
              chain is there to explain a surprising answer, not to double the height of
              every bubble. Opening it is a read, so nothing here is spoken aloud.
            -->
            <details v-if="line.role === 'assistant' && line.reasoning" class="chat__reasoning">
              <summary class="hud-label">思考过程</summary>
              <pre class="chat__reasoning-text">{{ line.reasoning }}</pre>
            </details>
            <!--
              Who said what around the table. Its own fold rather than folded into the
              answer: the conclusion is what gets read aloud and replayed, and three
              opinions in the bubble would bury the one thing that was asked for.
            -->
            <details v-if="line.role === 'assistant' && line.record" class="chat__reasoning">
              <summary class="hud-label">圆桌记录</summary>
              <pre class="chat__reasoning-text">{{ line.record }}</pre>
            </details>
          </div>
        </div>
        <!--
          The answer while it is still arriving. A separate bubble rather than an edit of
          ``history``: the replay list is what the next request is built from, and a
          half-finished sentence in there would be fed back to the model as something she
          already decided.
        -->
        <div v-if="streamed && streamed.text" class="chat__turn chat__turn--assistant">
          <div class="chat__bubble chat__bubble--streaming">
            <span class="chat__who">{{ streamed.model || '小夜' }}</span>
            <span class="chat__text">{{ streamed.text }}</span>
            <span class="chat__caret" aria-hidden="true"></span>
          </div>
        </div>
        <!--
          A question with no visible answer is indistinguishable from a hung app, and a
          model turn can take twenty seconds. The bubble appears the moment the request
          leaves, so the silence in between has a shape.

          Two doors say "she is working": the task table (a typed question, which knows
          the phase) and the shell's turn state (a spoken one, which never enters the
          table). Reading only the first is why 语音提问 never got this bubble.
        -->
        <div
          v-if="(runningTask || voice.turn === 'processing') && !streamed?.text"
          class="chat__turn chat__turn--assistant"
        >
          <div class="chat__bubble chat__bubble--thinking">
            <span class="chat__who">小夜</span>
            <!--
              Dots alone read as "some app is loading". The words name what is happening,
              and the phase underneath names which of the several waiting it is: a tool call
              and a round-table seat speaking are not the same silence.
            -->
            <span
              class="chat__loader"
              :class="`chat__loader--${loaderKind}`"
              role="status"
              aria-label="思考中"
              ><i v-for="cell in 6" :key="cell"></i
            ></span>
            <span class="chat__thinking-word">思考中</span>
            <span v-if="thinkingPhase" class="chat__thinking-phase">{{ thinkingPhase }}</span>
            <!--
              On the bubble rather than only in the toolbar, because this is the thing
              beside it you would point at: 停止 ends the round she is in, 回复 puts the
              cursor in the box so the next question can be written while she works --
              and it will wait its turn rather than talk over this one.
            -->
            <span class="chat__thinking-actions">
              <button
                v-if="runningTask"
                class="hud-btn hud-btn--warn"
                type="button"
                title="掐掉这一轮"
                @click="stop"
              >
                停止
              </button>
              <button
                class="hud-btn"
                type="button"
                title="接着提下一个问题（这一句会排在后面）"
                @click="askBack"
              >
                回复
              </button>
            </span>
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

      <!--
        The questions she has not been asked yet, in the order they were typed. They are
        also in the transcript above -- this strip is not a second copy of the record, it
        is the only place each one can be taken back out before she reaches it.
      -->
      <div v-if="queued.length" class="chat__queue">
        <span class="chat__queue-label">排队中 {{ queued.length }}</span>
        <span v-for="(item, slot) in queued" :key="item.task_id" class="chat__chip chat__chip--queued">
          <span class="chat__queue-seq">{{ slot + 1 }}</span>
          {{ item.question }}
          <button class="chat__chip-x" type="button" title="撤回这一句，不问了" @click="withdraw(item.task_id)">×</button>
        </span>
      </div>

      <form class="chat__form" @submit.prevent="send">
        <button class="hud-btn" type="button" title="附加图片 / 视频 / 文件" @click="openFiles">附件</button>
        <input ref="fileRef" type="file" multiple hidden @change="onFiles" />
        <!--
          Not disabled while she thinks. That was the single-conversation assumption
          leaking into the input: with several conversations open, the box being shut
          because *another* tab is waiting is just a locked keyboard. And with this tab
          waiting, what comes out of it is a queued question, not a second answer.
        -->
        <input
          ref="inputRef"
          v-model="draft"
          class="hud-field chat__input"
          type="text"
          placeholder="问点什么…（Enter 发送）"
          maxlength="2000"
          @paste="onPaste"
        />
        <button v-if="runningTask" class="hud-btn hud-btn--warn" type="button" title="掐掉这一轮" @click="stop">
          停止
        </button>
        <button class="hud-btn" type="submit" :disabled="draft.trim() === ''">
          发送
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
          <span class="hud-label">
            {{ THINKING_LABELS[thinking] ?? thinking }} · {{ turns }} 轮上下文
          </span>
          <span class="hud-label">{{ lines.length }} 条 · 会话 {{ activeTitle }}</span>
          <span class="hud-label">Esc 收回</span>
        </footer>
      </div>

    <SessionsPopup :open="sessionsOpen" @close="sessionsOpen = false" @switched="loadCurrent" />
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  chatCancel,
  chatSend,
  chatCollaborate,
  fetchModels,
  fetchSessionMessages,
  fetchSessions,
  formatBytes,
  newConversation,
  newSession,
  openConversation,
  pickModel,
  saveTuning,
  switchSession,
  type Attachment,
  type ConversationCard,
  type ModelSpec,
  type ProviderChoice,
  type QueuedQuestion,
  type SessionInfo,
} from '@/api/bridge'
import { readAsDataUrl, shrinkToBudget } from '@/api/thumbnail'
import {
  MODE_LABELS,
  requestsFor,
  type CollaborationMode,
  type ModelCap,
  type SeatChoice,
} from '@/api/bridge'

/** 下拉框里的顺序，和后端 `COLLABORATION_MODES` 是同一批值。 */
const MODE_ORDER: CollaborationMode[] = ['single', 'table', 'boss', 'vote']

/** 一桌几家、最多几轮。后端 jarvis/app/round_table.py 里各有一份，测试钉住不许漂。 */
const MAX_SEATS = 4
const MAX_ROUNDS = 3
import { useVoiceStore } from '@/stores/voice'
import SessionsPopup from '@/components/SessionsPopup.vue'

// The dialog itself lives in App.vue: this panel only asks for it.
const emit = defineEmits<{ (e: 'usage'): void; (e: 'voice'): void }>()

const voice = useVoiceStore()

/** The four shapes the shell can name. Kept in step with `preferences.THINKING_LOADERS`. */
const LOADER_KINDS = ['dots', 'matrix', 'ring', 'bars']

const draft = ref('')
const busy = ref(false)
const localError = ref('')
const listRef = ref<HTMLElement | null>(null)

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
  /**
   * The model's own reasoning, when the setting asked for it.
   *
   * Only the live turn has it: the transcript table stores questions and answers, not
   * thinking, so a reloaded session renders no chain rather than pretending to.
   */
  reasoning?: string
  /**
   * Everything said around a round table, in order.
   *
   * Live turns only, like ``reasoning``: the transcript stores the question and the
   * merged answer, so a reloaded session shows the conclusion without the argument.
   */
  record?: string
}

const seeded = ref<Line[]>([])
const consumed = ref(0)
const pending = ref<Attachment[]>([])
const overlay = ref<{ text: string; attachments: Attachment[] } | null>(null)
const fileRef = ref<HTMLInputElement | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)

const providers = ref<ProviderChoice[]>([])
const provider = ref('')
const model = ref('')
const thinking = ref('medium')
const turns = ref(10)
const thinkingLevels = ref<string[]>([])
const turnsBounds = ref<number[]>([])
const switching = ref(false)
const switchNotice = ref('')

/**
 * The collaboration: which shape, which chairs, how many times around.
 *
 * Seats are kept as pairs rather than as display strings because a provider name and a
 * model name are two lookups, and the first seat also decides who signs the answer --
 * which is exactly why the click order is visible to the operator.
 */
const mode = ref<CollaborationMode>('single')
const seats = ref<SeatChoice[]>([])
const rounds = ref(2)
const caps = ref<ModelCap[]>([])

const single = computed(() => mode.value === 'single')

/** Every model that can actually answer, i.e. whose provider has a key. */
const seatOptions = computed(() =>
  providers.value
    .filter((entry) => entry.key_set)
    .flatMap((entry) =>
      entry.models.map((spec) => {
        const cap = caps.value.find((row) => row.provider === entry.name && row.model === spec.id)
        return {
          key: `${entry.name}\u0000${spec.id}`,
          provider: entry.name,
          model: spec.id,
          label: `${entry.name}/${spec.id}`,
          // Three states, three sayings: 看不到图 is a measurement, the blank is silence.
          sight: cap?.vision === true ? '看图✓' : cap?.vision === false ? '看不到图' : '',
          vision: cap
            ? `上次实测：${cap.chat ? '能答' : '连不上'}${cap.vision === null ? '，图没测' : cap.vision ? '，看图可以' : '，不吃图'}（${cap.at}）`
            : '没测过：保存时测过的那次才算数',
        }
      }),
    ),
)

const requests = computed(() => requestsFor(mode.value, seats.value.length, rounds.value))

const modeTitle = computed(() => {
  const shape = single.value
    ? '一次问一个模型，工具和图片都在这条路上'
    : mode.value === 'table'
      ? '每轮每家看着前面所有话发一次言，最后一家合并'
      : mode.value === 'boss'
        ? '第一家拆任务、派活、收口；其余各家只看见分给自己的那一条'
        : '每家先独立答，再匿名互评，票最多的那份原话就是答案'
  return `${shape} · ${seats.value.length} 家 ≈ ${requests.value} 次请求（协作不带工具，也不带图）`
})

function picked(key: string): boolean {
  return seats.value.some((seat) => `${seat.provider}\u0000${seat.model}` === key)
}

/** Click a chair to sit, click again to leave. Order is the order they were clicked. */
function pickSeat(key: string): void {
  const option = seatOptions.value.find((entry) => entry.key === key)
  if (!option) return
  if (picked(key)) {
    seats.value = seats.value.filter((seat) => `${seat.provider}\u0000${seat.model}` !== key)
    return
  }
  if (seats.value.length >= MAX_SEATS) {
    localError.value = `一桌最多 ${MAX_SEATS} 家，多出来的没坐下`
    return
  }
  seats.value = [...seats.value, { provider: option.provider, model: option.model }]
}

function setRounds(event: Event): void {
  const value = Number((event.target as HTMLSelectElement).value)
  rounds.value = Number.isFinite(value) && value > 0 ? Math.min(value, MAX_ROUNDS) : 1
}

/** Switching shape keeps the chairs: the same three models are worth a table, a
 * dispatch and a vote, and re-picking them every time is busywork. */
function setMode(event: Event): void {
  const next = String((event.target as HTMLSelectElement).value) as CollaborationMode
  mode.value = MODE_ORDER.includes(next) ? next : 'single'
  if (!single.value && seats.value.length === 0) {
    // Start with whoever is already answering this tab, rather than an empty row of
    // buttons that has to be filled before the first question can be asked.
    const current = model.value
    seats.value = provider.value || current ? [{ provider: provider.value, model: current }] : []
  }
}

/** The models of the picked provider, and the level names as the backend spells them. */
const models = computed<ModelSpec[]>(
  () => providers.value.find((entry) => entry.name === provider.value)?.models ?? [],
)

/**
 * Friendly names for the four thinking levels. The wire values (`off`/`low`/...) are
 * what gets stored; these are only what the dropdown shows.
 */
const THINKING_LABELS: Record<string, string> = {
  off: '不思考',
  low: '轻度思考',
  medium: '中度思考',
  high: '深度思考',
}

const thinkingTitle = computed(
  () => `思考强度：${THINKING_LABELS[thinking.value] ?? thinking.value}`,
)

/** The turn counts the dropdown offers: a few round numbers inside the allowed range. */
const turnOptions = computed<number[]>(() => {
  const [low, high] = turnsBounds.value
  if (turnsBounds.value.length !== 2) return []
  const steps = [0, 4, 8, 12, 20, 30, 50].filter((n) => n >= low && n <= high)
  if (!steps.includes(turns.value)) steps.push(turns.value)
  return [...new Set(steps)].sort((a, b) => a - b)
})

/**
 * The list is pulled, not configured in the page.
 *
 * Which providers and models exist is a fact about the operator's config file and
 * environment, and a second copy of that list in the bundle would be a list that
 * drifts the moment someone adds a provider.
 */
async function loadModels(): Promise<void> {
  try {
    const list = await fetchModels()
    providers.value = list.providers ?? []
    provider.value = list.provider
    model.value = list.model
    thinking.value = list.thinking
    turns.value = list.turns
    thinkingLevels.value = list.thinking_levels ?? []
    turnsBounds.value = list.turns_bounds ?? []
    caps.value = list.caps ?? []
  } catch {
    // No bridge yet: the picker simply does not appear, and the default model answers.
  }
}

/** What to say after a switch: the key gap is the part worth mentioning out loud. */
function describePick(): string {
  const row = providers.value.find((entry) => entry.name === provider.value)
  const spec = models.value.find((entry) => entry.id === model.value)
  if (!row || !spec) return '已切换模型'
  return row.key_set
    ? `已切到 ${spec.label}`
    : `已切到 ${spec.label}，但它缺 ${row.key_variable}，问话会失败`
}

/**
 * Picking a provider keeps the model when that vendor offers one by the same name and
 * otherwise lands on the vendor's default -- the backend decides which, so the page
 * never has to guess what a half-complete selection means.
 */
async function chooseProvider(event: Event): Promise<void> {
  const wanted = (event.target as HTMLSelectElement).value
  if (!wanted || wanted === provider.value) return
  await applyPick(wanted, '', event.target as HTMLSelectElement)
}

async function chooseModel(event: Event): Promise<void> {
  const wanted = (event.target as HTMLSelectElement).value
  if (!wanted || wanted === model.value) return
  await applyPick(provider.value, wanted, event.target as HTMLSelectElement)
}

async function applyPick(wanted: string, wantedModel: string, select: HTMLSelectElement): Promise<void> {
  switching.value = true
  try {
    const list = await pickModel(wanted, wantedModel)
    if (list.error) {
      // Put the select back where it actually is, rather than leaving it showing a
      // model that is not answering the next question.
      select.value = wantedModel ? model.value : provider.value
      switchNotice.value = `没能切过去：${list.error}`
      return
    }
    providers.value = list.providers ?? []
    provider.value = list.provider
    model.value = list.model
    thinking.value = list.thinking
    turns.value = list.turns
    switchNotice.value = describePick()
  } catch (err) {
    select.value = wantedModel ? model.value : provider.value
    switchNotice.value = err instanceof Error ? err.message : String(err)
  } finally {
    switching.value = false
  }
}

/**
 * Two knobs, one call each.
 *
 * Saving one must not reset the other, which is why the other argument is left out
 * rather than sent with its current value: the backend writes only what it is given,
 * and a page that echoed both would turn "raise the thinking budget" into a context
 * size the operator never picked.
 */
async function saveKnob(level?: string, count?: number): Promise<void> {
  switching.value = true
  try {
    const answer = await saveTuning(level, count)
    if (!answer.ok) {
      // Put the two selects back where the knobs actually are. A `<select>` keeps
      // whatever was clicked, so a refusal left alone reads as "it saved" -- the
      // exact lie this picker was built to avoid.
      thinking.value = answer.thinking
      turns.value = answer.turns
      switchNotice.value = `没能保存：${answer.error}`
      return
    }
    thinking.value = answer.thinking
    turns.value = answer.turns
  } catch (err) {
    switchNotice.value = err instanceof Error ? err.message : String(err)
  } finally {
    switching.value = false
  }
}

async function saveThinking(event: Event): Promise<void> {
  await saveKnob((event.target as HTMLSelectElement).value, undefined)
}

async function saveTurns(event: Event): Promise<void> {
  await saveKnob(undefined, Number((event.target as HTMLSelectElement).value))
}

onMounted(() => {
  void loadModels()
  void loadCurrent()
  window.addEventListener('jarvis-settings-saved', onSettingsSaved)
  // 宠物那张卡上的 ＋ 按下去，壳子往这一页发这句。谁有输入框谁装这个钩子 —— 壳子因此
  // 不需要知道它在哪个元素上，宠物那一页（没有输入框）也不会接到就崩。
  shellCues().__jarvisFocusInput = focusInput
})

onBeforeUnmount(() => {
  window.removeEventListener('jarvis-settings-saved', onSettingsSaved)
  delete shellCues().__jarvisFocusInput
})

/** The cue names this page answers to from the shell, all optional. */
function shellCues(): { __jarvisFocusInput?: () => void } {
  return window as unknown as { __jarvisFocusInput?: () => void }
}

function focusInput(): void {
  inputRef.value?.focus()
}

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
    // A reopened conversation has no reasoning to show: the turns are stored, the
    // thoughts are not. Empty means the box is not drawn, rather than a box that lies
    // about what was kept.
    seeded.value = stored.messages.map((turn) => ({
      role: turn.role,
      text: turn.content,
      reasoning: '',
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
const currentLabel = computed(() => {
  const spec = models.value.find((entry) => entry.id === model.value)
  if (!spec) return ''
  return provider.value ? `${provider.value} · ${spec.label}` : spec.label
})

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
const lines = computed<Line[]>(() =>
  [
    ...seeded.value,
    ...voice.history.slice(consumed.value).map((turn) => ({
      role: turn.role,
      text: turn.text,
      reasoning: turn.reasoning ?? '',
      record: turn.record ?? '',
      model: turn.model ?? '',
      attachments: turn.text === overlay.value?.text ? overlay.value.attachments : [],
    })),
  ].filter((line) => line.text.trim() !== ''),
)
const error = computed(() => localError.value || voice.error)
const notice = computed(() => voice.notice)
/** A reply to the last click wins over the ambient status line, then fades. */
const shownNotice = computed(() => switchNotice.value || notice.value)
const listening = computed(() => voice.turn === 'listening')

/**
 * The tabs, and the one being read.
 *
 * Read from the pushed snapshot rather than kept locally: two conversations can be
 * answering at once, and a strip that remembered its own idea of which tab was busy
 * would be wrong the moment the other one finished first.
 */
const tabs = computed<ConversationCard[]>(() => voice.conversations)
const activeTab = computed<ConversationCard | undefined>(() =>
  tabs.value.find((card) => card.active),
)
/** The active tab's in-flight turn, which is the only one 停止 can point at. */
const runningTask = computed(() => (activeTab.value?.status === 'running' ? activeTab.value.task_id : ''))

/** What this tab has typed but not asked yet, in order. Empty almost always. */
const queued = computed<QueuedQuestion[]>(() => activeTab.value?.queued ?? [])

/** 气泡上那行小字：工具中 / 输出中 / 某家在发言。空就意味着只有「思考中」。 */
const thinkingPhase = computed(() => {
  const phase = String(activeTab.value?.phase || '')
  return phase && phase !== '思考中' ? phase : ''
})

/** Which of the four shapes the shell asked for. Unknown values fall back to dots. */
const loaderKind = computed(() => {
  const wanted = String(voice.thinkingLoader || 'dots')
  return LOADER_KINDS.includes(wanted) ? wanted : 'dots'
})
const canTalk = computed(() => voice.enabled && !busy.value)

/** The answer still arriving in this tab, or null for a tab that is only thinking. */
const streamed = computed(() => {
  const streaming = voice.streaming
  if (!streaming || streaming.conversation_id !== activeTab.value?.id) return null
  return streaming
})

const talkTitle = computed(() => {
  if (voice.enabled) return '跳过唤醒词，直接说这一句'
  return voice.label
})

/** Whether the transcript is being followed. Off the moment the operator scrolls up:
 *  a log that yanks the view down while somebody is re-reading an earlier answer is
 *  worse than one that does not follow at all. */
const following = ref(true)

function onLogScroll(): void {
  const element = listRef.value
  if (!element) return
  // A few pixels of slack: the last row's own padding and a sub-pixel scroll offset
  // should not read as "they went browsing".
  const gap = element.scrollHeight - element.scrollTop - element.clientHeight
  following.value = gap < 24
}

/** Pin to the newest row, but only if that is where the operator was already looking. */
function followToEnd(): void {
  if (!following.value) return
  const element = listRef.value
  if (element) element.scrollTop = element.scrollHeight
}

async function scrollToEnd(): Promise<void> {
  await nextTick()
  const element = listRef.value
  if (element) {
    element.scrollTop = element.scrollHeight
    following.value = true
  }
}

/**
 * How much transcript is on screen, as a cheap string: stored rows, whether the
 * 「思考中」 card is drawn, and the answer as it streams in.
 *
 * Watching this instead of the DOM keeps an observer out of the render loop, and it
 * catches the case that made the panel feel broken: the question goes out, the thinking
 * card appears *below* the fold, and nothing moved -- so it looks like she never started.
 */
const logExtent = computed(() => {
  const thinking = Boolean(runningTask.value) || voice.turn === 'processing'
  return [
    lines.value.length,
    thinking && !streamed.value?.text ? 1 : 0,
    streamed.value?.text.length ?? 0,
  ].join(':')
})

watch(logExtent, () => {
  void nextTick(followToEnd)
})

async function talk(): Promise<void> {
  await voice.talk()
  await scrollToEnd()
}

function openFiles(): void {
  fileRef.value?.click()
}

/** Whether the strip is worth a row: more than one tab, or a tab someone just made. */
const showTabStrip = computed(() => tabs.value.length > 1)

function tabTitle(tab: ConversationCard): string {
  const model = tab.model ? ` · ${tab.model}` : tab.provider ? ` · ${tab.provider}` : ''
  const asked = tab.turns ? ` · ${tab.turns} 问` : ''
  return `${tab.title || '新对话'}${model}${asked}${tab.status === 'running' ? ` · ${tab.phase}` : ''}`
}

/**
 * Paste an image straight from the clipboard.
 *
 * The same reader the 附件 button uses, because a clipboard PNG is routinely several
 * megabytes and the model context wants a thumbnail, not the original. Anything that
 * is not an image is refused here rather than carried in as a name: the assistant cannot
 * open a file, and a chip that implies otherwise is a lie with a thumbnail on it.
 */
async function onPaste(event: ClipboardEvent): Promise<void> {
  const files = Array.from(event.clipboardData?.files ?? [])
  const images = files.filter((file) => file.type.startsWith('image/'))
  if (images.length === 0) return
  // Stop the browser from pasting a multi-megabyte base64 blob into the text input.
  event.preventDefault()
  for (const file of images) {
    if (pending.value.length >= 4) {
      localError.value = '一条问题最多带 4 个附件，后面的没加进来'
      break
    }
    pending.value.push(await readAttachment(named(file, '粘贴图片')))
  }
  await scrollToEnd()
}

function named(file: File, fallback: string): File {
  return file.name ? file : new File([file], `${fallback}.${file.type.split('/')[1] ?? 'png'}`, { type: file.type })
}

/** Read one picked file: images shrunk to what she can receive, everything else as facts. */
async function readAttachment(file: File): Promise<Attachment> {
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
  if (kind !== 'image') return base
  const raw = await readAsDataUrl(file)
  if (!raw.startsWith('data:image/')) {
    localError.value = `${base.name}：读不出图片内容，没有随问题发出去`
    return base
  }
  try {
    return { ...base, data: await shrinkToBudget(raw) }
  } catch (error) {
    // No data URL at all rather than a big one: the backend drops an oversize picture
    // silently, so a chip that looks attached would promise a picture she never gets.
    localError.value = `${base.name}：${error instanceof Error ? error.message : String(error)}`
    return base
  }
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

/**
 * Ask, and stop waiting.
 *
 * ``chatAsk`` used to block until the answer existed, which is why the whole exchange
 * appeared at once: the question was only put on screen in the code that ran *after*
 * the reply came back. Now the shell shows the question as soon as the turn starts and
 * streams the answer into a temporary bubble, so the wait has a shape and a stop button.
 */
async function send(): Promise<void> {
  const question = draft.value.trim()
  if (question === '') return
  const attachments = [...pending.value]
  if (!single.value && seats.value.length === 0) {
    localError.value = '还没人选：在上面点几家模型再问'
    return
  }
  if (mode.value === 'boss' && seats.value.length < 2) {
    localError.value = '主管分发至少要两把椅子：一个拆，一个干活'
    return
  }
  if (!single.value && attachments.length) {
    // The backend refuses the same combination; saying it here keeps the question and the
    // picture in the box instead of bouncing a half-sent turn off the bridge.
    localError.value = '协作模式不带图。要问这张图，把形态换回「单个模型」再发。'
    return
  }
  draft.value = ''
  pending.value = []
  overlay.value = attachments.length ? { text: question, attachments } : null
  localError.value = ''
  await scrollToEnd()
  try {
    const started = single.value
      ? await chatSend(question, attachments, activeTab.value?.id ?? '', provider.value, model.value)
      : await chatCollaborate(
          question,
          seats.value,
          mode.value,
          rounds.value,
          activeTab.value?.id ?? '',
        )
    if (started.error) {
      // Keep the question in the box: it is the only record of what was attempted.
      localError.value = started.error
      draft.value = question
      overlay.value = null
      return
    }
    // One pull, because nothing else may push: a quiet microphone has no reason to
    // change state, and the question we just asked is already Python's to report.
    await voice.pullSnapshot()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
    draft.value = question
    overlay.value = null
  } finally {
    await scrollToEnd()
  }
}

/** Stop this tab's turn. Other tabs keep theirs -- that is the point of the id. */
async function stop(): Promise<void> {
  const task = runningTask.value
  if (!task) return
  try {
    const answer = await chatCancel(task)
    if (!answer.ok) localError.value = answer.error
    await voice.pullSnapshot()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
  }
}

/**
 * Take a waiting question back out. She never asked it, so nothing has to be undone:
 * a queued turn has no thread, no transcript row and no spend behind it.
 */
async function withdraw(taskId: string): Promise<void> {
  try {
    const answer = await chatCancel(taskId)
    if (!answer.ok) localError.value = answer.error
    await voice.pullSnapshot()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
  }
}

/**
 * 「回复」= put the cursor in the box.
 *
 * Not a different kind of message: what you type next is an ordinary question, and the
 * task table decides whether it runs now or waits. That is why this button is worth
 * having while she is busy rather than only when she is not.
 */
function askBack(): void {
  inputRef.value?.focus()
}

async function openTab(conversationId: string): Promise<void> {
  if (conversationId === activeTab.value?.id) return
  try {
    await openConversation(conversationId)
    seeded.value = []
    consumed.value = 0
    localError.value = ''
    await loadCurrent()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
  }
}

async function addTab(): Promise<void> {
  try {
    await newConversation()
    seeded.value = []
    consumed.value = 0
    overlay.value = null
    localError.value = ''
    await voice.pullSnapshot()
    await scrollToEnd()
  } catch (err) {
    localError.value = err instanceof Error ? err.message : String(err)
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

/*
 * The tab strip. A dim dot means idle and a lit one means that conversation has a turn
 * in flight -- the one thing the strip has to say, since the panel below only shows the
 * tab you are reading.
 */
.chat__tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 0 0 6px;
}

.chat__tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 190px;
  font-size: 11px;
  opacity: 0.72;
}

.chat__tab--active {
  opacity: 1;
  border-color: var(--hud-cyan);
}

.chat__tab--busy {
  opacity: 1;
}

.chat__tab--new {
  max-width: none;
  padding: 0 8px;
}

.chat__tab-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--hud-dim, #4a5560);
  flex: none;
}

.chat__tab--busy .chat__tab-dot {
  background: var(--hud-amber, #e8a33d);
  animation: chat-tab-pulse 1.2s ease-in-out infinite;
}

.chat__tab-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat__tab-phase {
  color: var(--hud-cyan);
  font-size: 10px;
}

@keyframes chat-tab-pulse {
  50% {
    opacity: 0.35;
  }
}

/* The answer arriving: a caret rather than dots, so "still coming" reads differently
   from "has not started". */
.chat__caret::after {
  display: inline-block;
  margin-left: 2px;
  content: '▍';
  color: var(--hud-cyan);
  animation: chat-tab-pulse 1s ease-in-out infinite;
}

.chat__model {
  color: var(--hud-cyan);
  font-size: 11px;
  max-width: 150px;
  flex: none;
}

.chat__model:disabled {
  opacity: 0.45;
}

/* The two per-model knobs sit beside the pickers but read dimmer: they change *how*
   the picked model answers, not *which* one does. */
.chat__knob {
  color: var(--hud-dim);
  font-size: 11px;
  max-width: 104px;
  flex: none;
}

.chat__knob:disabled {
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
  /*
   * 39, not 46: 放大 is a panel, not a dialog on top of dialogs. Every popup in this
   * window sits at 40 (the assistant's at 44), and while the chat was at 46 clicking
   * 用量 / 历史 / 音色 / 设置 with the chat expanded opened them *underneath* it -- the
   * button looked dead. One number below the popups keeps the scrim over the dashboard
   * and the popups over the scrim.
   */
  z-index: 39;
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

/* The chairs. A wrap row of toggles, because the list is however many models exist
   and a dropdown that hides which ones are already sitting would defeat the point. */
.chat__seats {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
  padding: 4px 0 6px;
}

.chat__seat {
  font-size: 11px;
  opacity: 0.72;
}

.chat__seat--on {
  opacity: 1;
  border-color: currentColor;
}

.chat__seat-rounds {
  font-size: 11px;
  margin-left: auto;
}

.chat__seat-hint {
  color: var(--hud-dim);
  font-size: 10px;
}

.chat__seat-cost {
  margin-left: 8px;
  color: var(--hud-cyan);
  font-size: 10px;
  letter-spacing: 0.3px;
  white-space: nowrap;
}

.chat__knob--on {
  opacity: 1;
  border-color: currentColor;
}

.chat__bubble--thinking {
  border-style: dashed;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.chat__thinking-word {
  color: var(--hud-cyan);
  letter-spacing: 0.08em;
}

.chat__thinking-phase {
  color: var(--hud-dim);
  font-size: 11px;
}

/*
 * The two controls on the thinking bubble, pushed to its right edge. Small and quiet on
 * purpose: they are the only things here that act on a round that has not finished, so
 * they have to be findable without reading as part of the answer.
 */
.chat__thinking-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}

.chat__thinking-actions .hud-btn {
  padding: 2px 9px;
  font-size: 10px;
}

/* 排队中的那几枚，一句一枚：能看见叠了几句，也能一句一句撤掉。 */
.chat__queue {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.chat__queue-label {
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--hud-amber);
}

.chat__chip--queued {
  border-color: rgba(255, 181, 71, 0.34);
  background: rgba(255, 181, 71, 0.08);
}

.chat__queue-seq {
  color: var(--hud-dim);
  font-size: 9px;
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

/*
 * The thinking chain is deliberately quieter than the answer under which it sits: same
 * font, dimmer, and wrapped in a <details> nobody has to open. Pre-wrap because the
 * model writes it in sentences and the operator reads it as prose.
 */
.chat__reasoning {
  margin-top: 6px;
  border-left: 2px solid var(--hud-line);
  padding-left: 8px;
}

.chat__reasoning > summary {
  cursor: pointer;
  color: var(--hud-dim);
  list-style: none;
}

.chat__reasoning > summary::-webkit-details-marker {
  display: none;
}

.chat__reasoning-text {
  margin: 4px 0 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 0.86em;
  color: var(--hud-dim);
  opacity: 0.85;
}

/* Three dots, staggered. Deliberately CSS-only: a JS-driven spinner would add one
   more timer to a window that is measured on how little it idles. */
/*
 * The 「思考中」 loader. One markup, four shapes, chosen in 设置 → 思考状态 Loader --
 * and the same choice is pushed to the desktop figure, which draws its own copy of this
 * wait in a window that has no CSS at all.
 */
.chat__loader {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 2px 3px;
}

.chat__loader i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--hud-cyan);
  animation: chat-think 1.05s infinite ease-in-out;
}

.chat__loader i:nth-child(2) {
  animation-delay: 0.15s;
}

.chat__loader i:nth-child(3) {
  animation-delay: 0.3s;
}

/* 只有前三个点是 dots 那种；其余形状自己决定用几格。 */
.chat__loader i:nth-child(n + 4) {
  display: none;
}

.chat__loader--matrix i {
  width: 4px;
  height: 9px;
  border-radius: 1px;
  animation: chat-matrix 1.15s infinite steps(6);
}

.chat__loader--matrix i:nth-child(2) {
  animation-delay: 0.2s;
}

.chat__loader--matrix i:nth-child(3) {
  animation-delay: 0.42s;
}

.chat__loader--matrix i:nth-child(n + 4) {
  display: block;
}

.chat__loader--ring {
  width: 14px;
  height: 14px;
  padding: 0;
}

.chat__loader--ring i {
  display: none;
  width: 13px;
  height: 13px;
  border: 2px solid transparent;
  border-top-color: var(--hud-cyan);
  border-right-color: var(--hud-cyan);
  border-radius: 50%;
  background: none;
  animation: chat-spin 0.85s infinite linear;
}

.chat__loader--ring i:first-child {
  display: block;
}

.chat__loader--bars {
  align-items: flex-end;
  height: 14px;
  gap: 3px;
}

.chat__loader--bars i {
  display: block;
  width: 3px;
  height: 14px;
  border-radius: 1px;
  animation: chat-bars 0.9s infinite ease-in-out;
}

.chat__loader--bars i:nth-child(2) {
  animation-delay: 0.12s;
}

.chat__loader--bars i:nth-child(3) {
  animation-delay: 0.24s;
}

.chat__loader--bars i:nth-child(4) {
  animation-delay: 0.36s;
}

@keyframes chat-matrix {
  0%,
  100% {
    opacity: 0.15;
    transform: scaleY(0.4);
  }
  40% {
    opacity: 1;
    transform: scaleY(1);
  }
}

@keyframes chat-spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes chat-bars {
  0%,
  100% {
    transform: scaleY(0.35);
  }
  50% {
    transform: scaleY(1);
  }
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
  .chat__loader i {
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
