<template>
  <div v-if="open" class="al" role="dialog" aria-modal="true" aria-label="告警">
    <div class="al__scrim" @click="emit('close')"></div>

    <section class="al__box">
      <header class="al__head">
        <h2 class="hud-title">告警 · ALERTS</h2>
        <div class="al__tools">
          <button
            v-if="active.length > 1"
            class="hud-btn"
            type="button"
            title="把这一批都记成已看过；读数不会因此变小"
            :disabled="busy"
            @click="ackAll"
          >
            全部知道了
          </button>
          <button class="hud-btn" type="button" :disabled="busy" @click="load">刷新</button>
          <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
        </div>
      </header>

      <p v-if="error" class="al__error">{{ error }}</p>

      <!--
        顶栏那颗 chip 上只有一个数字和一个 title。数字说"有几条"，这一格要说清
        "哪几条、现在多少、线在哪、什么时候开始、认过了没有" —— 少一样，人都得去开设置。
      -->
      <ul v-if="active.length" class="al__list">
        <li
          v-for="row in active"
          :key="row.code"
          class="al__row"
          :class="row.severity === 'critical' ? 'al__row--bad' : 'al__row--warn'"
        >
          <div class="al__line">
            <span class="al__label">{{ row.label }}</span>
            <span class="al__message">{{ row.message }}</span>
          </div>
          <!-- 不渲染 count：引擎那个字段装的是读数（note 出来的告警是 0.0），不是"第几次"。 -->
          <div class="al__meta hud-num">
            <span>现在 {{ number(row.value) }}{{ row.unit }} / 线 {{ number(row.threshold) }}{{ row.unit }}</span>
            <span>已持续 {{ age(row.since) }}</span>
          </div>
          <button
            class="hud-btn al__act"
            type="button"
            title="这条我认了；读数回到线以下、再次越过时还会提醒我"
            :disabled="busy"
            @click="ack(row.code)"
          >
            知道了
          </button>
        </li>
      </ul>
      <p v-else-if="!error" class="al__empty">
        {{
          busy
            ? '读取中…'
            : ackedHere
              ? `这一批 ${ackedHere} 条刚被你认掉了。读数一点没变 —— 回到线以下、再次越过时它还会回来。`
              : '现在没有告警。过了线的事才会进这里，不是每次读数都算一条。'
        }}
      </p>

      <!--
        顶栏那个数字把两种东西合在一起显示（一条 chip 比两条更容易被看到），
        所以这一格也必须分开说：过了线的，和**读不到数**的。后者不是"机器没事"，
        是"这块面板有格子是空的"，值得单独一句。
      -->
      <ul v-if="warnings.length" class="al__list al__list--soft">
        <li v-for="(line, index) in warnings" :key="index" class="al__row al__row--soft">
          <div class="al__line">
            <span class="al__label">读数残缺</span>
            <span class="al__message">{{ line }}</span>
          </div>
        </li>
      </ul>

      <ul v-if="history.length" class="al__list al__list--soft">
        <li v-for="row in history" :key="row.code" class="al__row al__row--soft">
          <div class="al__line">
            <span class="al__label">{{ row.label }}</span>
            <span class="al__message">已恢复：{{ row.message }}</span>
          </div>
          <div class="al__meta hud-num"><span>{{ clock(row.last_seen) }}</span></div>
        </li>
      </ul>

      <p class="al__note">
        「知道了」只是这一条不再挡你，读数没变；回到线以下、再次越过时它还会回来。
        阈值和要不要开口念，在<b>设置 → 告警</b>里改。
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * 顶栏那颗「告警 N」点开的这一格。
 *
 * 它存在的理由很具体：那颗 chip 以前是个 <span>，只带一个 title —— 用户点了没反应，
 * 因为**确实没有任何东西可点**（r99 那版用户实拍报的）。告警列表本身早就在
 * 概览卡和设置里，但那两处都要先滚到对的位子；抬眼看到红点就该能立刻看清是哪几条。
 */
import { computed, ref, watch } from 'vue'
import { acknowledgeAlert, acknowledgeAllAlerts, fetchAlerts, type AlertRow } from '@/api/bridge'
import { useSystemStore } from '@/stores/system'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const system = useSystemStore()
const active = ref<AlertRow[]>([])
const history = ref<AlertRow[]>([])
const error = ref('')
const busy = ref(false)
const loaded = ref(false)
const now = ref(Date.now() / 1000)
/** 这一次打开里认掉了几条：引擎把已认的从「在着的」里挑出去，所以屏上要自己说清去了哪。 */
const ackedHere = ref(0)

const warnings = computed(() => system.warnings)

function number(value: number): string {
  return Math.abs(value) >= 100 ? String(Math.round(value)) : value.toFixed(1)
}

function clock(seconds: number): string {
  if (!seconds) return '--:--:--'
  return new Date(seconds * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

/** 「已持续」按分钟说：一条告警值不值 60 秒的窗口，看的是分钟不是秒。 */
function age(since: number): string {
  const minutes = Math.max(0, Math.floor((now.value - since) / 60))
  if (minutes < 1) return '不到 1 分钟'
  if (minutes < 60) return `${minutes} 分钟`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}

async function load(): Promise<void> {
  busy.value = true
  error.value = ''
  now.value = Date.now() / 1000
  try {
    const report = await fetchAlerts()
    active.value = report.active
    history.value = report.history.slice(0, 5)
    error.value = report.error
    loaded.value = true
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    busy.value = false
  }
}

async function ack(code: string): Promise<void> {
  busy.value = true
  const done = await acknowledgeAlert(code).catch(() => ({ ok: false, error: '' }))
  if (done.ok) ackedHere.value += 1
  await load()
}

async function ackAll(): Promise<void> {
  busy.value = true
  const done = await acknowledgeAllAlerts().catch(() => ({ ok: false, count: 0, error: '' }))
  ackedHere.value += Number(done.count || 0)
  await load()
}

// 打开时才读：这条链路上的数每秒都在变，但"有没有过线"不需要常驻轮询去追 ——
// 概览卡那一路已经带着它，弹窗关掉就不该再花钱。
watch(
  () => props.open,
  (shown) => {
    if (shown) {
      ackedHere.value = 0
      void load()
    }
  },
)
</script>

<style scoped>
.al {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.al__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.al__box {
  position: relative;
  width: min(720px, 94vw);
  max-height: 84vh;
  display: flex;
  flex-direction: column;
  padding: 16px 20px 14px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.al__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.al__tools {
  display: flex;
  gap: 6px;
}

.al__error {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--hud-red);
}

.al__empty {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--hud-dim);
}

.al__list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  margin: 0 0 6px;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.al__row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 4px 12px;
  padding: 6px 10px;
  border-radius: var(--hud-radius);
  border-left: 2px solid rgba(255, 181, 71, 0.7);
  background: rgba(255, 181, 71, 0.05);
}

.al__row--bad {
  border-left-color: rgba(255, 92, 92, 0.85);
  background: rgba(255, 92, 92, 0.07);
}

/* 恢复的与读不到的不是"正在发生的事"，所以不带颜色。 */
.al__row--soft {
  border-left-color: rgba(77, 216, 255, 0.28);
  background: rgba(77, 216, 255, 0.03);
}

.al__line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}

.al__label {
  font-size: 11px;
  color: var(--hud-cyan);
  white-space: nowrap;
}

.al__message {
  font-size: 12px;
  line-height: 1.6;
  color: var(--hud-text);
}

.al__meta {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 10px;
  color: var(--hud-dim);
}

.al__act {
  grid-column: 2;
  font-size: 11px;
}

.al__note {
  margin: 10px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--hud-line);
  font-size: 10px;
  line-height: 1.6;
  color: var(--hud-dim);
}
</style>
