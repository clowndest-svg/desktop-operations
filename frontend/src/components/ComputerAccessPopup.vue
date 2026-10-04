<template>
  <div v-if="open" class="ca" role="dialog" aria-modal="true" aria-label="电脑控制权限">
    <div class="ca__scrim" @click="emit('close')"></div>

    <section class="ca__box">
      <header class="ca__head">
        <h2 class="hud-title">电脑控制 · ACCESS</h2>
        <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
      </header>

      <p v-if="error" class="ca__error">{{ error }}</p>

      <ul v-if="levels.length" class="ca__list">
        <li
          v-for="level in levels"
          :key="level.tier"
          class="ca__row"
          :class="{ 'ca__row--on': level.tier === current.tier }"
        >
          <button class="ca__pick" type="button" :disabled="setting !== -1" @click="set(level.tier)">
            {{ level.tier === current.tier ? '● 当前' : '○ 切到这一档' }}
          </button>
          <div class="ca__body">
            <strong>{{ level.label }}</strong>
            <em>{{ level.describe }}</em>
          </div>
        </li>
      </ul>

      <!--
        打字这把锁和四个档位分开，是因为它们管的不是一件事：档位管"能碰哪儿"
        （指针、按键），这把管"能不能把字写进别人的输入框"。默认关，开了等于你本人
        对"她会替你敲出消息正文"这件事点头 —— 所以它不藏在档位里面。
      -->
      <h3 class="ca__sub hud-title">打字 · TYPING</h3>

      <label class="ca__toggle">
        <input
          type="checkbox"
          :checked="current.allow_typing"
          :disabled="typingSetting"
          @change="toggleTyping(($event.target as HTMLInputElement).checked)"
        />
        <span>
          允许她替你在输入框里打字
          <em>
            关了：她只能按键（切窗口、回车、Esc），一个字都打不出来。
            开了：她能输入文字 —— 包括消息正文；发出去之前仍然要你按回车。
          </em>
        </span>
      </label>

      <p v-if="typingNote" class="ca__note-line" :class="{ 'ca__note-line--bad': typingNoteBad }">
        {{ typingNote }}
      </p>

      <p v-if="note" class="ca__note-line" :class="{ 'ca__note-line--bad': noteBad }">{{ note }}</p>

      <!--
        The sentence that has to be on this dialog, in plain words: whatever is
        picked here, nothing is deleted and nothing is confirmed on the assistant's
        behalf. The deletion gate and the tool confirmation gate are separate doors
        and this key does not open them.
      -->
      <p class="ca__note">
        这一档只管"能不能动鼠标键盘"。删除文件仍然要你在垃圾清理里逐项勾选并二次确认，
        与这里的选择无关。切换立即生效，不需要重启。
      </p>

      <h3 class="ca__sub hud-title">命令行 · POWERSHELL</h3>

      <ul v-if="shellLevels.length" class="ca__list">
        <li
          v-for="level in shellLevels"
          :key="level.tier"
          class="ca__row"
          :class="{ 'ca__row--on': level.tier === shellCurrent.tier }"
        >
          <button class="ca__pick" type="button" :disabled="shellSetting !== -1" @click="setShell(level.tier)">
            {{ level.tier === shellCurrent.tier ? '● 当前' : '○ 切到这一档' }}
          </button>
          <div class="ca__body">
            <strong>{{ level.label }}</strong>
            <em>{{ level.describe }}</em>
          </div>
        </li>
      </ul>

      <p v-if="shellNote" class="ca__note-line" :class="{ 'ca__note-line--bad': shellNoteBad }">{{ shellNote }}</p>

      <p class="ca__note">
        命令行和鼠标键盘是<strong>两把独立的锁</strong>：可以只让它打字、永远不让它跑命令。
        「管理员」不是一次授权管到底——<strong>每条命令都会弹一次 UAC</strong>，你点「是」它才动。
        每条命令（包括被取消的）都会记在下面这份台账里，切档立即生效，不需要重启。
      </p>

      <div v-if="commands.length" class="ca__ledger">
        <div class="ca__ledger-head">
          <span>命令台账（最近 {{ commands.length }} 条）</span>
        </div>
        <div v-for="(entry, index) in commands" :key="index" class="ca__cmd">
          <span class="ca__cmd-mode">{{ entry.label }}</span>
          <code class="ca__cmd-text">{{ entry.script }}</code>
          <span class="ca__cmd-state">
            {{ entry.note }}<template v-if="entry.exit_code !== null"> · 退出码 {{ entry.exit_code }}</template
            ><template v-if="entry.seconds !== null"> · {{ entry.seconds }}s</template>
          </span>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * The desktop-control permission dial.
 *
 * Four named levels instead of four checkboxes: sixteen switch combinations exist
 * and about four of them are things a person means. Each level maps to exactly one
 * combination in Python, so the label on this dialog and the policy actually in
 * force are the same fact read twice, not two facts that can drift.
 */
import { ref, watch } from 'vue'
import {
  fetchComputerLevels,
  fetchShellLevels,
  setComputerTier,
  setComputerTyping,
  setShellTier,
  type ComputerAccessState,
  type ComputerLevel,
  type ShellCommand,
  type ShellLevel,
} from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'changed', label: string): void }>()

const levels = ref<ComputerLevel[]>([])
const current = ref<ComputerAccessState>({
  tier: 0,
  label: '—',
  describe: '',
  enabled: false,
  dry_run: true,
  allow_mouse: false,
  allow_keyboard: false,
  allow_typing: false,
})
const error = ref('')
const note = ref('')
const noteBad = ref(false)
const setting = ref(-1)
const typingSetting = ref(false)
const typingNote = ref('')
const typingNoteBad = ref(false)

async function load(): Promise<void> {
  error.value = ''
  try {
    const list = await fetchComputerLevels()
    levels.value = list.levels
    current.value = list.current
    error.value = list.error
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function set(tier: number): Promise<void> {
  setting.value = tier
  note.value = ''
  try {
    const list = await setComputerTier(tier)
    levels.value = list.levels
    current.value = list.current
    if (list.error) {
      note.value = list.error
      noteBad.value = true
    } else {
      note.value = `已切到「${list.current.label}」，下一次工具调用就按这一档执行`
      noteBad.value = false
      emit('changed', list.current.label)
    }
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
    noteBad.value = true
  } finally {
    setting.value = -1
  }
}

watch(
  () => props.open,
  (shown) => {
    if (shown) {
      void load()
      void loadShell()
    }
  },
)

/**
 * The second, independent lock. Kept in the same dialog because the question is the
 * same question -- "how much of my machine may it touch" -- but never merged into
 * one ladder, because "it may type" and "it may run commands" are risks a person
 * may well want to answer differently.
 */
const shellLevels = ref<ShellLevel[]>([])
const shellCurrent = ref<{ tier: number; mode: string; label: string; describe: string }>({
  tier: 0,
  mode: 'off',
  label: '—',
  describe: '',
})
const commands = ref<ShellCommand[]>([])
const shellNote = ref('')
const shellNoteBad = ref(false)
const shellSetting = ref(-1)

async function loadShell(): Promise<void> {
  try {
    const list = await fetchShellLevels()
    shellLevels.value = list.levels
    shellCurrent.value = list.current
    commands.value = list.commands
  } catch (err) {
    shellNote.value = err instanceof Error ? err.message : String(err)
    shellNoteBad.value = true
  }
}

async function toggleTyping(allowed: boolean): Promise<void> {
  typingSetting.value = true
  typingNote.value = ''
  try {
    const list = await setComputerTyping(allowed)
    levels.value = list.levels
    current.value = list.current
    if (list.error) {
      typingNote.value = list.error
      typingNoteBad.value = true
    } else {
      typingNote.value = allowed
        ? '已允许她打字：下一句话里她就能输入文字了（发送仍然由你按回车）'
        : '已禁止她打字：她只能按键，打不出一个字'
      typingNoteBad.value = false
    }
  } catch (err) {
    typingNote.value = err instanceof Error ? err.message : String(err)
    typingNoteBad.value = true
  } finally {
    typingSetting.value = false
  }
}

async function setShell(tier: number): Promise<void> {
  shellSetting.value = tier
  shellNote.value = ''
  try {
    const list = await setShellTier(tier)
    shellLevels.value = list.levels
    shellCurrent.value = list.current
    commands.value = list.commands
    if (list.error) {
      shellNote.value = list.error
      shellNoteBad.value = true
    } else {
      shellNote.value = `已切到「${list.current.label}」`
      shellNoteBad.value = false
    }
  } catch (err) {
    shellNote.value = err instanceof Error ? err.message : String(err)
    shellNoteBad.value = true
  } finally {
    shellSetting.value = -1
  }
}
</script>

<style scoped>
.ca {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.ca__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.ca__box {
  position: relative;
  width: min(560px, 94vw);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  padding: 16px 20px 14px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.ca__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.ca__error {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--hud-red);
}

.ca__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ca__row {
  display: grid;
  grid-template-columns: 96px 1fr;
  gap: 10px;
  align-items: start;
  padding: 7px 9px;
  border: 1px solid transparent;
  border-radius: var(--hud-radius);
}

.ca__row--on {
  border-color: rgba(77, 216, 255, 0.4);
  background: rgba(77, 216, 255, 0.07);
}

.ca__pick {
  padding: 3px 6px;
  border: none;
  background: transparent;
  color: var(--hud-dim);
  font-size: 11px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}

.ca__row--on .ca__pick {
  color: var(--hud-cyan);
}

.ca__pick:hover {
  color: var(--hud-cyan);
}

.ca__body strong {
  display: block;
  font-size: 12px;
  letter-spacing: 0.06em;
  color: var(--hud-text);
}

.ca__body em {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  font-style: normal;
  line-height: 1.55;
  color: var(--hud-dim);
}

.ca__note-line {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--hud-green);
}

.ca__note-line--bad {
  color: var(--hud-red);
}

.ca__toggle {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin: 6px 0 0;
  padding: 8px 10px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  font-size: 12px;
  color: var(--hud-text);
  cursor: pointer;
}

.ca__toggle input {
  margin-top: 2px;
  accent-color: var(--hud-cyan);
  cursor: pointer;
}

.ca__toggle em {
  display: block;
  margin-top: 4px;
  font-style: normal;
  font-size: 11px;
  line-height: 1.6;
  color: var(--hud-dim);
}

.ca__note {
  margin: 10px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--hud-line);
  font-size: 10px;
  line-height: 1.6;
  color: var(--hud-dim);
}

/* The second axis needs its own heading or it reads as five levels of one thing. */
.ca__sub {
  margin: 16px 0 2px;
  font-size: 10px;
  letter-spacing: 0.22em;
}

.ca__ledger {
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  background: rgba(3, 9, 16, 0.55);
  max-height: 168px;
  overflow-y: auto;
}

.ca__ledger-head {
  font-size: 10px;
  letter-spacing: 0.14em;
  color: var(--hud-dim);
  margin-bottom: 6px;
}

.ca__cmd {
  display: grid;
  grid-template-columns: 56px 1fr;
  gap: 2px 8px;
  padding: 5px 0;
  border-top: 1px solid rgba(77, 216, 255, 0.08);
  font-size: 11px;
}

.ca__cmd-mode {
  color: var(--hud-amber);
  letter-spacing: 0.06em;
}

.ca__cmd-text {
  font-family: var(--hud-mono);
  color: var(--hud-text);
  word-break: break-all;
}

.ca__cmd-state {
  grid-column: 2;
  color: var(--hud-dim);
  font-size: 10px;
}
</style>
