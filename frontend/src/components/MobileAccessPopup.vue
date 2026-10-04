<template>
  <div v-if="open" class="ma" role="dialog" aria-modal="true" aria-label="手机接入">
    <div class="ma__scrim" @click="emit('close')"></div>

    <section class="ma__box">
      <header class="ma__head">
        <h2 class="hud-title">手机接入 · LOCAL NET</h2>
        <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
      </header>

      <p v-if="state.error" class="ma__error">{{ state.error }}</p>

      <div class="ma__switch">
        <button class="hud-btn" type="button" :disabled="busy" @click="flip()">
          {{ state.running ? '关闭监听' : '打开手机接入' }}
        </button>
        <div class="ma__where">
          <template v-if="state.running">
            <strong>{{ state.url }}</strong>
            <button class="ma__copy" type="button" title="复制这个地址" @click="copy('地址', state.url)">
              {{ mark('地址') }}
            </button>
            <em>手机和电脑连同一个 Wi-Fi，浏览器或 App 里填这个地址</em>
          </template>
          <em v-else>现在是关的：这台机器上没有对外开放的端口</em>
        </div>
      </div>

      <template v-if="state.running">
        <div class="ma__pair">
          <button class="hud-btn hud-btn--go" type="button" :disabled="busy" @click="pair()">
            出一个配对码
          </button>
          <div v-if="code" class="ma__code">
            <span class="ma__digits">{{ code }}</span>
            <button class="ma__copy" type="button" title="复制配对码" @click="copy('配对码', code)">
              {{ mark('配对码') }}
            </button>
            <span v-if="state.pairing.active" class="ma__ttl">
              {{ Math.max(0, Math.ceil(state.pairing.expires_in / 60)) }} 分钟内有效 ·
              还剩 {{ state.pairing.attempts_left }} 次试错
            </span>
            <span v-else class="ma__ttl ma__ttl--gone">这个码已经作废了，再点一次</span>
          </div>
          <em v-if="pairError" class="ma__error">{{ pairError }}</em>
        </div>

        <p class="ma__pin">
          <span>证书指纹（手机要固定这一串）</span>
          <code>{{ state.fingerprint }}</code>
          <button
            class="ma__copy"
            type="button"
            title="手机第一次连会弹出一串指纹让你核对，两边必须是同一串"
            @click="copy('指纹', state.fingerprint)"
          >
            {{ mark('指纹') }}
          </button>
        </p>

        <div v-if="state.firewall" class="ma__firewall">
          <span>Windows 弹过「允许访问」就点允许；没弹也不通，就把这行拿管理员 PowerShell 跑一下</span>
          <code>{{ state.firewall }}</code>
          <button
            class="ma__copy"
            type="button"
            title="复制这条命令，拿去管理员 PowerShell 里粘"
            @click="copy('命令', state.firewall)"
          >
            {{ mark('命令') }}
          </button>
        </div>
      </template>

      <h3 class="ma__sub hud-title">已配对设备</h3>
      <ul v-if="state.devices.length" class="ma__list">
        <li v-for="device in state.devices" :key="device.device_id" class="ma__row">
          <div class="ma__row-body">
            <strong>{{ device.name }}</strong>
            <em>配对 {{ stamp(device.added_at) }} · 最近活动 {{ stamp(device.last_seen) }}</em>
          </div>
          <button class="ma__pick" type="button" :disabled="busy" @click="revoke(device.device_id)">
            撤销
          </button>
        </li>
      </ul>
      <p v-else class="ma__empty">一台都没有。先「打开手机接入」，再点「出一个配对码」。</p>

      <p v-if="state.devices.length" class="ma__row ma__row--all">
        <button class="ma__pick" type="button" :disabled="busy" @click="revokeAll()">
          全部撤销
        </button>
        <button class="ma__pick" type="button" :disabled="busy" @click="check()">
          查一下明文有没有被拒
        </button>
      </p>
      <p v-if="note" class="ma__note-line" :class="{ 'ma__note-line--bad': noteBad }">{{ note }}</p>

      <!--
        The sentence that has to be on this dialog. Everything a phone can ask for is
        read-only state, conversation, and creating a reminder; deletion, permission
        tiers and the computer's own microphone stay on the machine in front of them.
      -->
      <p class="ma__note">
        手机是这台电脑的<strong>第二张脸，不是第二个大脑</strong>：能问、能说、能建提醒。
        <strong>删除类操作、提权、开电脑麦克风，手机一侧永远点不到</strong>，只能在电脑前确认。
        只做内网，没有公网穿透；关掉上面的开关，端口立刻扫不到。
        手机丢了就在这里把它撤销。
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * The phone endpoint's control panel.
 *
 * Deliberately shows the pairing code only after an explicit press: a code that sat
 * on screen permanently would be a door left open in every screenshot and every
 * shared view of this window.
 */
import { onBeforeUnmount, reactive, ref, watch } from 'vue'
import { copyText, type CopyOutcome } from '@/api/clipboard'
import {
  fetchMobileState,
  requestPairCode,
  revokeAllDevices,
  revokeDevice,
  runMobileSelftest,
  setMobileOpen,
  type MobileDevice,
  type MobileState,
} from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'changed', label: string): void }>()

const empty: MobileState = {
  running: false,
  url: '',
  port: 0,
  fingerprint: '',
  devices: [],
  pairing: { active: false, expires_in: 0, attempts_left: 0 },
  error: '',
  firewall: '',
}

const state = reactive<MobileState>({ ...empty })

/**
 * 每样可复制的东西各自一个结果，键是给人看的那句名字。
 *
 * 分开存是有原因的：这四个值（地址 / 配对码 / 指纹 / 防火墙命令）复制的时机各不相同，
 * 共用一个提示会出现在按了「指纹」结果「地址」那行变成"已复制"的假动作。
 */
const copied = ref<Record<string, CopyOutcome>>({})
const copyTimers: Record<string, number> = {}

function mark(name: string): string {
  const outcome = copied.value[name]
  if (outcome === 'copied') return '已复制'
  if (outcome === 'failed') return '没复制上'
  if (outcome === 'empty') return '没有内容'
  return '复制'
}

/** 按下去必须有个说法：成功、失败、还是压根没东西可复制。 */
async function copy(name: string, value: string): Promise<void> {
  copied.value = { ...copied.value, [name]: await copyText(value) }
  window.clearTimeout(copyTimers[name])
  copyTimers[name] = window.setTimeout(() => {
    const next = { ...copied.value }
    delete next[name]
    copied.value = next
  }, 2600)
}

onBeforeUnmount(() => {
  for (const timer of Object.values(copyTimers)) window.clearTimeout(timer)
})
const code = ref('')
const pairError = ref('')
const note = ref('')
const noteBad = ref(false)
const busy = ref(false)

function apply(next: MobileState): void {
  Object.assign(state, next)
  if (!next.running) {
    // A closed listener cannot serve a code, so the screen stops claiming one.
    code.value = ''
    state.pairing = { active: false, expires_in: 0, attempts_left: 0 }
  }
}

async function load(): Promise<void> {
  try {
    apply(await fetchMobileState())
  } catch (err) {
    state.error = err instanceof Error ? err.message : String(err)
  }
}

async function flip(): Promise<void> {
  busy.value = true
  try {
    const next = await setMobileOpen(!state.running)
    apply(next)
    emit('changed', next.running ? `已打开 ${next.url}` : '已关闭监听')
  } catch (err) {
    state.error = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

async function pair(): Promise<void> {
  busy.value = true
  pairError.value = ''
  try {
    const issued = await requestPairCode()
    code.value = issued.code
    if (issued.error) pairError.value = issued.error
    apply(await fetchMobileState())
  } catch (err) {
    pairError.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

function rows(next: MobileDevice[]): void {
  state.devices = next
}

async function revoke(deviceId: string): Promise<void> {
  busy.value = true
  try {
    const answered = await revokeDevice(deviceId)
    rows(answered.devices)
    note.value = answered.revoked ? '这台已经撤销，它下一次请求就会被拒' : '这台已经不在了'
    noteBad.value = !answered.revoked
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
    noteBad.value = true
  } finally {
    busy.value = false
  }
}

async function revokeAll(): Promise<void> {
  busy.value = true
  try {
    const answered = await revokeAllDevices()
    rows(answered.devices)
    note.value = `已撤销 ${answered.revoked} 台`
    noteBad.value = false
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
    noteBad.value = true
  } finally {
    busy.value = false
  }
}

async function check(): Promise<void> {
  busy.value = true
  try {
    const answered = await runMobileSelftest()
    note.value = answered.plaintext_refused
      ? `明文请求没有回音${answered.note ? `（${answered.note}）` : ''}`
      : `明文请求竟然得到了回应：${answered.note}`
    noteBad.value = !answered.plaintext_refused
  } catch (err) {
    note.value = err instanceof Error ? err.message : String(err)
    noteBad.value = true
  } finally {
    busy.value = false
  }
}

function stamp(seconds: number): string {
  if (!seconds) return '—'
  return new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false })
}

watch(
  () => props.open,
  (shown) => {
    if (shown) {
      code.value = ''
      pairError.value = ''
      note.value = ''
      void load()
    }
  },
)
</script>

<style scoped>
.ma {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.ma__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.ma__box {
  position: relative;
  width: min(600px, 94vw);
  max-height: 84vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  padding: 16px 20px 14px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.ma__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.ma__error {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--hud-red);
}

.ma__switch {
  display: grid;
  grid-template-columns: 116px 1fr;
  gap: 12px;
  align-items: center;
}

.ma__where strong {
  display: block;
  font-size: 13px;
  letter-spacing: 0.04em;
  color: var(--hud-cyan);
}

.ma__where em {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  font-style: normal;
  line-height: 1.55;
  color: var(--hud-dim);
}

.ma__pair {
  margin-top: 14px;
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.ma__code {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.ma__digits {
  font-size: 30px;
  letter-spacing: 0.3em;
  color: var(--hud-cyan);
  text-shadow: 0 0 18px rgba(77, 216, 255, 0.35);
}

.ma__ttl {
  font-size: 11px;
  color: var(--hud-dim);
}

.ma__ttl--gone {
  color: var(--hud-amber);
}

.ma__pin,
.ma__firewall {
  margin-top: 12px;
  padding: 8px 10px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-radius);
  background: rgba(3, 9, 16, 0.55);
  font-size: 10px;
  color: var(--hud-dim);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.ma__pin code,
.ma__firewall code {
  font-size: 10px;
  line-height: 1.6;
  color: var(--hud-text);
  word-break: break-all;
}

.ma__sub {
  margin: 18px 0 2px;
  font-size: 10px;
  letter-spacing: 0.22em;
}

.ma__list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.ma__row {
  display: grid;
  grid-template-columns: 1fr 88px;
  gap: 10px;
  align-items: center;
  padding: 7px 9px;
  border: 1px solid transparent;
  border-radius: var(--hud-radius);
}

.ma__row--all {
  grid-template-columns: 88px 1fr;
  padding: 4px 0 0;
}

.ma__row-body strong {
  display: block;
  font-size: 12px;
  color: var(--hud-text);
}

.ma__row-body em {
  display: block;
  margin-top: 2px;
  font-size: 10px;
  font-style: normal;
  color: var(--hud-dim);
}

.ma__pick {
  padding: 3px 6px;
  border: none;
  background: transparent;
  color: var(--hud-dim);
  font-size: 11px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
}

.ma__pick:hover {
  color: var(--hud-cyan);
}

/*
 * 复制按钮。三种状态换的是文字本身（复制 / 已复制 / 没复制上），不是一闪而过的提示条：
 * 这几串字是要用手机照抄的，按下去没有回执就等于让人猜有没有进去。
 */
.ma__copy {
  padding: 2px 7px;
  margin-left: 6px;
  border: 1px solid var(--hud-line);
  border-radius: var(--hud-pill);
  background: rgba(77, 216, 255, 0.06);
  color: var(--hud-cyan);
  font-family: var(--hud-mono);
  font-size: 10px;
  letter-spacing: 0.4px;
  white-space: nowrap;
  cursor: pointer;
}

.ma__copy:hover {
  border-color: var(--hud-rim);
  box-shadow: 0 0 8px var(--hud-glow);
}

.ma__empty {
  margin: 6px 0 0;
  font-size: 11px;
  color: var(--hud-dim);
}

.ma__note-line {
  margin: 8px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: var(--hud-green);
}

.ma__note-line--bad {
  color: var(--hud-red);
}

.ma__note {
  margin: 14px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--hud-line);
  font-size: 10px;
  line-height: 1.7;
  color: var(--hud-dim);
}
</style>
