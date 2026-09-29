<template>
  <section class="hud-panel clean">
    <header class="hud-title">
      垃圾清理 · JUNK SWEEP
      <button class="hud-btn clean__scan" type="button" :disabled="busy" @click="scan">
        {{ state === 'scanning' ? '扫描中…' : '扫描' }}
      </button>
    </header>

    <p v-if="error" class="clean__error">{{ error }}</p>

    <p v-else-if="state === 'idle'" class="hud-label">
      点击「扫描」列出可删除的缓存与临时文件。扫描只读，不会删除任何东西。
    </p>

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
}

.clean__scan {
  margin-left: auto;
  padding: 2px 10px;
  font-size: 11px;
}

.clean__error {
  margin: 0;
  color: var(--hud-red);
  font-size: 12px;
}

.clean__summary {
  display: flex;
  align-items: center;
  gap: 10px;
}

.clean__all {
  margin-left: auto;
  padding: 2px 10px;
  font-size: 11px;
  white-space: nowrap;
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
  background: rgba(255, 93, 93, 0.08);
  padding: 8px 10px;
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
</style>
