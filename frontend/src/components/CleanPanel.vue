<template>
  <section class="hud-panel clean">
    <header class="hud-title">
      垃圾清理 · JUNK SWEEP
      <span class="clean__tools">
        <button
          class="hud-btn"
          type="button"
          :disabled="busy || advising || plan === null"
          :title="plan ? '让模型读一遍这份清单，指出看着不对的条目' : '先扫描，才有东西可分析'"
          @click="advise"
        >
          {{ advising ? '分析中…' : '协助分析' }}
        </button>
        <button class="hud-btn" type="button" :disabled="busy" @click="scan">
          {{ state === 'scanning' ? '扫描中…' : '扫描' }}
        </button>
      </span>
    </header>

    <p v-if="error" class="clean__error">{{ error }}</p>

    <!--
      The resting state used to be one sentence at the top of the tallest panel on
      the screen, which is how the window got its black rectangle: 1.5 fr of grid
      reserved for a list that does not exist yet.

      So the space says the thing a first-time operator actually needs to know before
      pressing anything -- that nothing is deleted without a tick and a second click,
      and that some paths are refused no matter what is ticked. It is not filler:
      every sentence in here is a guarantee this feature makes elsewhere.
    -->
    <div v-else-if="state === 'idle'" class="clean__rest">
      <p class="hud-label">
        点击「扫描」列出可删除的缓存与临时文件。扫描只读，不会删除任何东西。
      </p>
      <ol class="clean__steps">
        <li>
          <span class="clean__step-num">1</span>
          <div>
            <strong>扫描</strong>
            <em>只读。按类别列出缓存、临时文件与回收站，逐项给出大小。</em>
          </div>
        </li>
        <li>
          <span class="clean__step-num">2</span>
          <div>
            <strong>勾选</strong>
            <em>删哪些由你逐个勾。默认一个都没选，全选也要你自己按。</em>
          </div>
        </li>
        <li>
          <span class="clean__step-num">3</span>
          <div>
            <strong>确认删除</strong>
            <em>再点一次才动手，逐条复查保护路径，事后写审计日志。</em>
          </div>
        </li>
      </ol>
      <p class="clean__protect">
        系统目录、文档、桌面、以及 JARVIS 自己的数据目录，无论勾没勾都会被拒绝——这一条在删除的那一刻还会再查一遍。
      </p>
    </div>

    <template v-else-if="plan">
      <div class="clean__summary hud-label">
        <span>{{ selectedCount }} / {{ totalItems }} 项已选</span>
        <span class="hud-num">约 {{ formatBytes(selectedBytes) }}</span>
        <button
          v-if="state === 'ready'"
          class="hud-btn clean__all"
          type="button"
          @click="toggleAll"
        >
          {{ allSelected ? '取消全选' : `全选 ${totalItems} 项 / ${formatBytes(totalBytes)}` }}
        </button>
      </div>

      <!--
        The model's read of the same list. It is placed above the rows rather than in
        a dialog because its whole job is to be read *against* the entries it is
        talking about, and it is capped in height because a chatty answer must not
        push the tick boxes out of the panel.

        It cannot tick anything for you, and the line saying so is not decoration:
        an AI that has just named three suspicious folders is exactly when a person
        reaches for the button at the bottom.
      -->
      <div v-if="advising || advice || adviceNote" class="clean__advice">
        <div class="clean__advice-head">
          <span>AI 协助分析</span>
          <span v-if="adviceTools.length" class="clean__advice-note">
            顺带查了 {{ adviceTools.length }} 次目录
          </span>
          <button
            v-if="!advising"
            class="hud-btn clean__advice-close"
            type="button"
            @click="dismissAdvice"
          >
            收起
          </button>
        </div>
        <p v-if="advising" class="clean__advice-wait">
          正在把这份清单交给模型。它只会读，不会删任何东西。
        </p>
        <p v-else-if="adviceNote" class="clean__advice-error">{{ adviceNote }}</p>
        <p v-else class="clean__advice-text">{{ advice }}</p>
      </div>

      <div class="clean__list">
        <div v-for="group in plan.groups" :key="group.category" class="clean__group">
          <div class="clean__group-head">
            <span>{{ group.category }}</span>
            <span class="hud-num clean__group-size">{{ formatBytes(group.total_bytes) }}</span>
          </div>
          <label v-for="item in group.items" :key="item.path" class="clean__item" :title="describe(item)">
            <input type="checkbox" :value="item.path" v-model="selected" />
            <span class="clean__path">{{ label(item) }}</span>
            <span v-if="detail(item)" class="clean__detail">{{ detail(item) }}</span>
            <span class="hud-num clean__size">{{ formatBytes(item.size_bytes) }}</span>
          </label>
        </div>
      </div>

      <p v-if="plan.skipped_protected" class="hud-label clean__skip">
        已自动排除 {{ plan.skipped_protected }} 个受保护路径（系统目录、文档、桌面等）
      </p>
      <p v-if="plan.truncated" class="hud-label clean__skip">条目已达上限，总量为下限值</p>

      <div v-if="state === 'confirming'" class="clean__confirm">
        <p>
          即将<strong>永久删除</strong> {{ selectedCount }} 项<template v-if="selectedFiles !== selectedCount">（共约 {{ selectedFiles.toLocaleString('zh-CN') }} 个文件）</template>，约 {{ formatBytes(selectedBytes) }}。不可撤销。
        </p>
        <p class="clean__confirm-scope">受保护的系统目录、文档、桌面与 JARVIS 数据目录会在删除前逐个复查。</p>
        <div class="clean__confirm-actions">
          <button class="hud-btn danger" type="button" :disabled="busy" @click="commit">确认删除</button>
          <button class="hud-btn" type="button" @click="state = 'ready'">取消</button>
        </div>
      </div>
      <button
        v-else-if="state === 'ready'"
        class="hud-btn"
        type="button"
        :disabled="selectedCount === 0"
        @click="state = 'confirming'"
      >
        清理选中（{{ selectedCount }}）
      </button>
      <p v-else-if="state === 'done'" class="clean__done">
        已释放 {{ formatBytes(freed) }}<template v-if="failedCount">，{{ failedCount }} 项失败</template>
        <br />审计记录：{{ logPath }}
      </p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  deleteJunk,
  fetchDiskPlan,
  formatBytes,
  junkAdvice,
  type DiskPlan,
  type JunkItem,
} from '@/api/bridge'

type State = 'idle' | 'scanning' | 'ready' | 'confirming' | 'done'

const state = ref<State>('idle')
const plan = ref<DiskPlan | null>(null)
const selected = ref<string[]>([])
const error = ref('')
const busy = ref(false)
const freed = ref(0)
const failedCount = ref(0)
const logPath = ref('')
const advising = ref(false)
const advice = ref('')
const adviceNote = ref('')
const adviceTools = ref<string[]>([])

const allItems = computed<JunkItem[]>(() =>
  (plan.value?.groups ?? []).flatMap((group) => group.items),
)
const totalItems = computed(() => allItems.value.length)
const totalBytes = computed(() => allItems.value.reduce((sum, item) => sum + item.size_bytes, 0))
const selectedItems = computed(() => {
  const chosen = new Set(selected.value)
  return allItems.value.filter((item) => chosen.has(item.path))
})
const selectedCount = computed(() => selectedItems.value.length)
const allSelected = computed(
  () => totalItems.value > 0 && selectedCount.value === totalItems.value,
)
const selectedFiles = computed(() =>
  selectedItems.value.reduce((sum, item) => sum + Math.max(1, item.member_count), 0),
)
const selectedBytes = computed(() =>
  selectedItems.value.reduce((sum, item) => sum + item.size_bytes, 0),
)

function baseName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, '')
  const index = Math.max(trimmed.lastIndexOf('\\'), trimmed.lastIndexOf('/'))
  return index === -1 ? trimmed : trimmed.slice(index + 1)
}

function isAggregate(item: JunkItem): boolean {
  return item.kind === 'loose_files'
}

function label(item: JunkItem): string {
  return baseName(item.path)
}

/** How many files a row stands for, spelled out only when it is more than one. */
function detail(item: JunkItem): string {
  return isAggregate(item) ? `${item.member_count.toLocaleString('zh-CN')} 个散落临时文件` : ''
}

function describe(item: JunkItem): string {
  const lines = [item.path, `${formatBytes(item.size_bytes)} · ${Math.max(1, item.member_count)} 个条目`]
  if (item.sample.length > 0) {
    lines.push(`示例：${item.sample.join('、')}`)
    if (item.member_count > item.sample.length) {
      lines.push(`（其余 ${item.member_count - item.sample.length} 项省略）`)
    }
  }
  return lines.join('\n')
}

/**
 * Tick or untick every row the scan actually offered.
 *
 * "Every row" is the whole of it: protected paths were dropped during the scan and
 * are checked again per member at delete time, so select-all cannot reach them. The
 * button spells out the count and the size it is about to select, because a
 * select-all whose cost only appears afterwards is the failure mode this panel
 * exists to avoid.
 */
function toggleAll(): void {
  selected.value = allSelected.value ? [] : allItems.value.map((item) => item.path)
}

async function scan(): Promise<void> {
  busy.value = true
  error.value = ''
  state.value = 'scanning'
  dismissAdvice()
  try {
    const result = await fetchDiskPlan()
    if (result.error) {
      error.value = result.error
      state.value = 'idle'
      return
    }
    plan.value = result
    selected.value = []
    state.value = 'ready'
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    state.value = 'idle'
  } finally {
    busy.value = false
  }
}

/**
 * Hand the current scan to the model and show what it says.
 *
 * A turn that can take half a minute, so the button reads as busy and the block
 * opens with a line about what it is doing. Failure lands in the same place as an
 * empty answer would not: "no API key" and "the model had nothing to say" are
 * different things an operator can act on, and neither is a silent blank.
 */
async function advise(): Promise<void> {
  advising.value = true
  advice.value = ''
  adviceNote.value = ''
  adviceTools.value = []
  try {
    const result = await junkAdvice()
    advice.value = result.text
    adviceTools.value = result.tools_used
    if (!result.text) adviceNote.value = result.error || '模型没有给出内容'
    else if (result.error) adviceNote.value = result.error
  } catch (err) {
    adviceNote.value = err instanceof Error ? err.message : String(err)
  } finally {
    advising.value = false
  }
}

function dismissAdvice(): void {
  advice.value = ''
  adviceNote.value = ''
  adviceTools.value = []
}

async function commit(): Promise<void> {
  const items = [...selectedItems.value]
  if (items.length === 0) return
  busy.value = true
  try {
    const outcome = await deleteJunk(items)
    if (outcome.error) {
      error.value = outcome.error
      state.value = 'ready'
      return
    }
    freed.value = outcome.freed_bytes
    failedCount.value = outcome.failed.length
    logPath.value = outcome.log_path
    // Drop what actually went away; keep failures visible and still selectable.
    const gone = new Set(outcome.deleted)
    selected.value = selected.value.filter((path) => gone.has(path) === false)
    if (plan.value) {
      plan.value = {
        ...plan.value,
        groups: plan.value.groups
          .map((group) => ({ ...group, items: group.items.filter((item) => !gone.has(item.path)) }))
          .filter((group) => group.items.length > 0),
      }
    }
    state.value = 'done'
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    state.value = 'ready'
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.clean {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  min-height: 0;
  overflow: hidden;
}

.clean__tools {
  margin-left: auto;
  display: flex;
  gap: 6px;
}

.clean__advice {
  flex: none;
  max-height: 34%;
  overflow-y: auto;
  padding: 7px 11px;
  border-radius: var(--hud-radius);
  border-left: 2px solid rgba(77, 216, 255, 0.42);
  background: rgba(77, 216, 255, 0.05);
}

.clean__advice-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--hud-cyan);
}

.clean__advice-note {
  color: var(--hud-dim);
  letter-spacing: 0.02em;
}

.clean__advice-close {
  margin-left: auto;
}

.clean__advice-text,
.clean__advice-wait {
  margin: 5px 0 0;
  font-size: 11px;
  line-height: 1.65;
  color: var(--hud-text, #d8ecf6);
  white-space: pre-wrap;
}

.clean__advice-wait {
  color: var(--hud-dim);
}

.clean__advice-error {
  margin: 5px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--hud-red);
}

.clean__error {
  margin: 0;
  color: var(--hud-red);
  font-size: 12px;
}

.clean__summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
}

/*
 * The counts wrap as phrases, never as characters. `.hud-btn` got `white-space: nowrap`
 * to stop 「按一下说」 breaking one glyph per line, and a nowrap sibling in a tight flex
 * row takes the whole row -- which then squeezes these spans into the same vertical
 * stack the buttons just escaped.
 */
.clean__summary > span {
  white-space: nowrap;
  flex: none;
}

.clean__all {
  margin-left: auto;
}

.clean__list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.clean__group-head {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--hud-cyan);
  letter-spacing: 0.06em;
  padding-bottom: 2px;
  border-bottom: 1px solid rgba(77, 216, 255, 0.12);
}

.clean__group-size {
  color: var(--hud-dim);
}

.clean__item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  padding: 2px 0;
  cursor: pointer;
}

.clean__item input {
  accent-color: var(--hud-cyan);
}

.clean__path {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.clean__detail {
  color: var(--hud-amber);
  font-size: 10px;
  white-space: nowrap;
}

.clean__size {
  color: var(--hud-dim);
}

.clean__skip {
  color: var(--hud-amber);
  letter-spacing: 0.02em;
}

.clean__confirm {
  border: 1px solid rgba(255, 93, 93, 0.45);
  border-radius: var(--hud-radius);
  background: rgba(255, 93, 93, 0.08);
  padding: 8px 12px;
}

.clean__confirm p {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--hud-red);
}

.clean__confirm-actions {
  display: flex;
  gap: 8px;
}

.clean__done {
  margin: 0;
  font-size: 11px;
  color: var(--hud-green);
  line-height: 1.6;
  word-break: break-all;
}

/*
 * The resting state. It has to fill a tall panel without inventing anything, so it
 * is made of the three guarantees the feature already makes -- each line is a rule
 * enforced in Python, not marketing copy.
 */
.clean__rest {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
  /* Scroll rather than spill: the three steps are worth reading, and a panel that
     paints over the console below it is worth nothing. `safe` is the part that
     matters -- centring an overflowing column clips the first item off the top,
     where no amount of scrolling brings it back. */
  overflow-y: auto;
  justify-content: safe center;
}

.clean__steps {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.clean__steps li {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.clean__steps strong {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--hud-text, #d8ecf6);
}

.clean__steps em {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  font-style: normal;
  line-height: 1.55;
  color: var(--hud-dim);
}

.clean__step-num {
  flex: none;
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  margin-top: 1px;
  font-size: 11px;
  color: var(--hud-cyan);
  border: 1px solid rgba(77, 216, 255, 0.32);
  border-radius: 50%;
  background: rgba(77, 216, 255, 0.07);
}

.clean__protect {
  margin: 0;
  padding: 8px 12px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--hud-amber);
  border-radius: var(--hud-radius);
  border-left: 2px solid rgba(255, 181, 71, 0.45);
  background: rgba(255, 181, 71, 0.06);
}
</style>
