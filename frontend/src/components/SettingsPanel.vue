<template>
  <div v-if="open" class="settings" role="dialog" aria-modal="true" aria-label="设置">
    <div class="settings__scrim" @click="emit('close')"></div>

    <section class="settings__box">
      <header class="settings__head">
        <h2 class="hud-title">设置 · SETTINGS</h2>
        <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
      </header>

      <p v-if="loadError" class="settings__error">{{ loadError }}</p>

      <div v-if="loading" class="settings__loading">读取中…</div>

      <form v-else-if="form" class="settings__body" @submit.prevent="save">
        <fieldset class="settings__group">
          <legend>AI 模型</legend>

          <!--
            The list is the source of truth the chat dropdown reads: every row here
            becomes one entry there, whether it came from config.yaml or was added
            in this window. 「在用」 is the row the assistant answers with right now.
          -->
          <ul class="settings__models">
            <li
              v-for="row in models"
              :key="row.name"
              class="settings__model"
              :class="{ 'settings__model--on': row.current, 'settings__model--edit': row.name === editing }"
            >
              <button class="settings__model-pick" type="button" @click="editRow(row.name)">
                {{ row.name }}
              </button>
              <span class="settings__model-meta">
                {{ row.model }} · {{ row.source }}<template v-if="row.edited"> · 已改</template>
                · key {{ row.key_set ? '已设置' : '未设置' }}
              </span>
              <span class="settings__model-ops">
                <button
                  v-if="row.current"
                  class="hud-btn"
                  type="button"
                  title="对话区正在用这一行"
                  disabled
                >
                  在用
                </button>
                <button v-else class="hud-btn" type="button" :disabled="saving" @click="useRow(row.name)">
                  设为当前
                </button>
                <button
                  v-if="row.source === '界面添加'"
                  class="hud-btn settings__del"
                  type="button"
                  :disabled="saving"
                  @click="removeRow(row.name)"
                >
                  删除
                </button>
              </span>
            </li>
          </ul>

          <p class="settings__why">
            点名字编辑那一行的地址和模型名；「设为当前」立刻生效，不用重启。
            配置文件里的行要删请去 config.yaml——界面上删不掉它们，是故意的。
          </p>

          <label class="settings__row">
            <span class="hud-label">正在编辑：{{ editing }}</span>
          </label>
          <label class="settings__row">
            <span class="hud-label">调用地址</span>
            <input v-model.trim="form.base_url" type="text" spellcheck="false" :placeholder="editingRow?.base_url" />
          </label>
          <p class="settings__why">留空 = 不覆盖，用这一行本来的地址。</p>

          <label class="settings__row">
            <span class="hud-label">模型名称</span>
            <input v-model.trim="form.model" type="text" spellcheck="false" :placeholder="editingRow?.model" />
          </label>

          <div class="settings__row">
            <span class="hud-label">API Key（{{ editingRow?.key_env || '未配置' }}）</span>
            <div class="settings__keyline">
              <input
                v-model="apiKey"
                :type="revealKey ? 'text' : 'password'"
                autocomplete="off"
                spellcheck="false"
                :placeholder="keyPlaceholder"
              />
              <button class="hud-btn" type="button" @click="revealKey = !revealKey">
                {{ revealKey ? '隐藏' : '显示' }}
              </button>
              <button class="hud-btn" type="button" @click="apiKey = ' '">清除</button>
            </div>
          </div>
          <p class="settings__why">
            状态 {{ editingRow?.key_set ? '已设置' : '未设置' }}。
            密钥只写进环境变量（本进程立刻生效 + Windows 用户环境以便重启后仍在），
            不写进任何配置文件，也不会回显到页面上。
          </p>

          <details class="settings__add">
            <summary class="hud-label">＋ 添加一个模型</summary>
            <label class="settings__row">
              <span class="hud-label">名字（小写字母数字 - _）</span>
              <input v-model.trim="draft.name" type="text" spellcheck="false" placeholder="如 deepseek-v4" />
            </label>
            <label class="settings__row">
              <span class="hud-label">调用地址</span>
              <input v-model.trim="draft.base_url" type="text" spellcheck="false" placeholder="https://api.example.com/v1" />
            </label>
            <label class="settings__row">
              <span class="hud-label">模型名称</span>
              <input v-model.trim="draft.model" type="text" spellcheck="false" placeholder="deepseek-chat" />
            </label>
            <label class="settings__row">
              <span class="hud-label">API Key（可选，存进 {{ draftKeyEnv }}）</span>
              <input v-model="draft.api_key" type="password" autocomplete="off" spellcheck="false" />
            </label>
            <p class="settings__why">添加随「保存」一起生效；名字不能和已有行重复。</p>
          </details>
        </fieldset>

        <fieldset class="settings__group">
          <legend>语音</legend>
          <label class="settings__row settings__row--inline">
            <input v-model="form.auto_speak_typed" type="checkbox" />
            <span class="hud-label">打字问的问题也念出来</span>
          </label>
          <p class="settings__why">关掉后只有语音问句会得到语音回答，文字回合只出字。</p>

          <div class="settings__row">
            <span class="hud-label">麦克风自动待命</span>
            <span class="settings__state">
              {{ original?.voice_auto_arm ? '开机会自己回到待唤醒' : '开机不自动开麦' }}
            </span>
          </div>
          <p class="settings__why">
            这一项由窗口右上角的「启用语音」/「释放麦克风」控制，不在这里改：
            开麦的同意必须来自那一次按压。
          </p>
        </fieldset>

        <fieldset class="settings__group">
          <legend>界面</legend>
          <label class="settings__row">
            <span class="hud-label">遥测轮询间隔（毫秒）</span>
            <input v-model.number="form.telemetry_interval_ms" type="number" min="500" max="60000" step="100" />
          </label>
          <p class="settings__why">
            500–60000 之间，保存后下一次轮询就用新间隔。调太小会让界面自己变成机器上最忙的进程。
          </p>
        </fieldset>

        <p v-if="savedNote" class="settings__saved">{{ savedNote }}</p>
        <ul v-if="problemList.length" class="settings__problems">
          <li v-for="line in problemList" :key="line">{{ line }}</li>
        </ul>

        <footer class="settings__foot">
          <span v-if="original?.overrides_active?.length" class="hud-label">
            生效中：{{ original.overrides_active.join('、') }}
          </span>
          <button class="hud-btn hud-btn--primary" type="submit" :disabled="saving">
            {{ saving ? '保存中…' : '保存' }}
          </button>
        </footer>
      </form>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * The settings panel: a list of models, and the switches around them.
 *
 * Every save goes through one bridge call and takes effect in the running process:
 * the LLM service drops its client cache when the effective section changes, the
 * key lands in the environment the client reads per request, and the poll interval
 * is handed to the telemetry store before this dialog even closes. "Restart to
 * apply" is the failure mode this panel is designed against.
 */
import { computed, ref, watch } from 'vue'

import { fetchSettings, saveSettings, type ModelRow, type SettingsSnapshot } from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'saved', snapshot: SettingsSnapshot): void }>()

const original = ref<SettingsSnapshot | null>(null)
const form = ref<{
  base_url: string
  model: string
  auto_speak_typed: boolean
  telemetry_interval_ms: number
} | null>(null)
const editing = ref('')
const apiKey = ref('')
const revealKey = ref(false)
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const problems = ref<Record<string, string>>({})
const savedNote = ref('')
const draft = ref({ name: '', base_url: '', model: '', api_key: '' })

const models = computed<ModelRow[]>(() => original.value?.models ?? [])
const editingRow = computed(() => models.value.find((row) => row.name === editing.value))
const problemList = computed(() =>
  Object.entries(problems.value).map(([key, why]) => `${key}：${why}`),
)
const keyPlaceholder = computed(() =>
  editingRow.value?.key_set ? '已设置，输入新值可替换' : '粘贴你的 API Key',
)
const draftKeyEnv = computed(() =>
  draft.value.name ? `${draft.value.name.toUpperCase().replace(/-/g, '_')}_API_KEY` : '—',
)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const snapshot = await fetchSettings()
    original.value = snapshot
    editing.value = snapshot.provider
    form.value = {
      base_url: '',
      model: '',
      auto_speak_typed: snapshot.auto_speak_typed,
      telemetry_interval_ms: snapshot.telemetry_interval_ms,
    }
    loadError.value = snapshot.error ?? ''
  } catch (err) {
    loadError.value = `读取设置失败：${err instanceof Error ? err.message : String(err)}`
  } finally {
    loading.value = false
  }
}

/** Point the editor at another row; unsaved field edits are dropped on purpose. */
function editRow(name: string): void {
  if (name === editing.value) return
  editing.value = name
  if (form.value) {
    form.value.base_url = ''
    form.value.model = ''
  }
  apiKey.value = ''
}

async function useRow(name: string): Promise<void> {
  await send({ provider: name }, `已切到 ${name}，下一句问答就用它`)
}

async function removeRow(name: string): Promise<void> {
  if (!window.confirm(`删掉界面添加的模型「${name}」？配置文件里的行不受影响。`)) return
  await send({ remove_model: name }, `已删除 ${name}`)
}

async function send(patch: Record<string, unknown>, note: string): Promise<void> {
  if (saving.value) return
  saving.value = true
  problems.value = {}
  savedNote.value = ''
  try {
    const result = await saveSettings(patch)
    original.value = result
    problems.value = result.problems ?? {}
    if (!Object.keys(result.problems ?? {}).length) savedNote.value = note
    if (result.models) editing.value = result.provider || editing.value
    emit('saved', result)
  } catch (err) {
    problems.value = { 保存: err instanceof Error ? err.message : String(err) }
  } finally {
    saving.value = false
  }
}

async function save() {
  if (!form.value || saving.value) return
  const patch: Record<string, unknown> = {
    target: editing.value,
    auto_speak_typed: form.value.auto_speak_typed,
    telemetry_interval_ms: form.value.telemetry_interval_ms,
  }
  if (form.value.base_url) patch.base_url = form.value.base_url
  if (form.value.model) patch.model = form.value.model
  if (apiKey.value.trim()) {
    patch.api_key = apiKey.value
    patch.key_for = editing.value
  }
  if (draft.value.name && (draft.value.base_url || draft.value.model)) {
    patch.add_model = { ...draft.value }
  }
  saving.value = true
  problems.value = {}
  savedNote.value = ''
  try {
    const result = await saveSettings(patch)
    original.value = result
    problems.value = result.problems ?? {}
    const done = Object.keys(result.applied ?? {}).length
    savedNote.value = done ? `已保存并立即生效：${Object.keys(result.applied ?? {}).join('、')}` : '没有字段被改动'
    if (result.applied?.api_key) apiKey.value = ''
    if (result.applied?.add_model) draft.value = { name: '', base_url: '', model: '', api_key: '' }
    form.value.base_url = ''
    form.value.model = ''
    emit('saved', result)
  } catch (err) {
    problems.value = { 保存: err instanceof Error ? err.message : String(err) }
  } finally {
    saving.value = false
  }
}

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) void load()
  },
)
</script>

<style scoped>
.settings {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.settings__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.settings__box {
  position: relative;
  width: min(600px, 92vw);
  max-height: 86vh;
  overflow: auto;
  padding: 18px 20px 20px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.settings__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.settings__body {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.settings__group {
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  padding: 10px 12px 12px;
}

.settings__group legend {
  padding: 0 6px;
  color: var(--hud-cyan);
  font-size: 11px;
  letter-spacing: 0.18em;
}

.settings__models {
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.settings__model {
  display: grid;
  grid-template-columns: 110px 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 4px 7px;
  border: 1px solid transparent;
  border-radius: var(--hud-radius);
}

.settings__model--on {
  border-color: rgba(77, 216, 255, 0.4);
  background: rgba(77, 216, 255, 0.07);
}

.settings__model--edit {
  border-color: rgba(255, 181, 71, 0.5);
}

.settings__model-pick {
  border: none;
  background: transparent;
  color: var(--hud-cyan);
  font-size: 12px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings__model-meta {
  color: var(--hud-dim);
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.settings__model-ops {
  display: flex;
  gap: 5px;
}

.settings__del {
  color: var(--hud-red);
}

.settings__add {
  margin-top: 10px;
  border-top: 1px dashed var(--hud-line);
  padding-top: 8px;
}

.settings__add summary {
  cursor: pointer;
  color: var(--hud-cyan);
  font-size: 11px;
  letter-spacing: 0.06em;
}

.settings__row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
}

.settings__row--inline {
  flex-direction: row;
  align-items: center;
  gap: 8px;
}

.settings__row input[type='text'],
.settings__row input[type='password'],
.settings__row input[type='number'],
.settings__row select {
  /* Same box as .hud-field in hud.css: one control shape for the whole window. */
  height: var(--hud-control-h);
  padding: 0 10px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-pill);
  background: rgba(3, 9, 16, 0.8);
  color: var(--hud-text);
  font-size: 12px;
  font-family: inherit;
  outline: none;
}

.settings__keyline {
  display: flex;
  gap: 6px;
}

.settings__keyline input {
  flex: 1;
  min-width: 0;
}

.settings__why {
  margin: 4px 0 0;
  color: var(--hud-dim);
  font-size: 11px;
  line-height: 1.5;
}

.settings__state {
  color: var(--hud-amber);
  font-size: 12px;
}

.settings__saved {
  margin: 0;
  color: var(--hud-cyan);
  font-size: 12px;
}

.settings__problems {
  margin: 0;
  padding-left: 18px;
  color: var(--hud-red);
  font-size: 12px;
}

.settings__error,
.settings__loading {
  color: var(--hud-amber);
  font-size: 12px;
}

.settings__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

code {
  color: var(--hud-cyan);
}
</style>
