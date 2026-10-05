<template>
  <div v-if="open" class="act" role="dialog" aria-modal="true" aria-label="最近动作">
    <div class="act__scrim" @click="emit('close')"></div>

    <section class="act__box">
      <header class="act__head">
        <h2 class="hud-title">最近动作 · ACTIVITY</h2>
        <div class="act__tools">
          <button class="hud-btn" type="button" :disabled="loading" @click="load">刷新</button>
          <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
        </div>
      </header>

      <p v-if="error" class="act__error">{{ error }}</p>
      <p v-else-if="loading" class="act__loading">读取中…</p>
      <p v-else-if="entries.length === 0" class="act__empty">
        还没有动作记录。工具被调用过才会出现在这里——包括被安全策略拒绝的那些。
      </p>

      <ul v-else class="act__list">
        <li v-for="(entry, index) in entries" :key="index" class="act__row" :class="{ 'act__row--no': !entry.ok }">
          <span class="hud-num act__time">{{ clock(entry.at) }}</span>
          <span class="act__tool">{{ entry.tool }}</span>
          <span class="act__verdict">{{ entry.ok ? '完成' : '未执行' }}</span>
          <span class="hud-num act__ms">{{ entry.milliseconds }}ms</span>
          <span class="act__detail">{{ entry.detail || '—' }}</span>
        </li>
      </ul>

      <!--
        Said out loud because the absence of a promise is the thing a reader needs:
        this list is memory, and it is gone after a restart. The record that survives
        is the deletion audit log, which is on disk and cannot be turned off.
      -->
      <p class="act__note">
        这里只留最近 40 条，重启后清空。删除文件的完整审计写在磁盘日志里，长期保留。
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * What the assistant did to this machine, and what it was refused.
 *
 * A desktop agent that moves the mouse is the first version of this project where
 * "I didn't touch anything" needs proving rather than asserting. The list already
 * existed inside the tool registry; this makes it lookable-at without opening a
 * log file, which is the difference between an audit trail and a forensic tool.
 */
import { ref, watch } from 'vue'
import { fetchActivity, type ActivityEntry } from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const entries = ref<ActivityEntry[]>([])
const error = ref('')
const loading = ref(false)

function clock(seconds: number): string {
  if (!seconds) return '--:--:--'
  return new Date(seconds * 1000).toLocaleTimeString('zh-CN', { hour12: false })
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await fetchActivity()
    entries.value = result.entries
    error.value = result.error
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

// Fetched when the dialog opens rather than polled: an idle window should not pay
// for a list nobody is reading, and forty in-memory rows do not change on their own.
watch(
  () => props.open,
  (shown) => {
    if (shown) void load()
  },
)
</script>

<style scoped>
.act {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.act__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.act__box {
  position: relative;
  width: min(760px, 94vw);
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

.act__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.act__tools {
  display: flex;
  gap: 6px;
}

.act__error,
.act__loading,
.act__empty {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.7;
}

.act__error {
  color: var(--hud-red);
}

.act__loading,
.act__empty {
  color: var(--hud-dim);
}

.act__list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.act__row {
  display: grid;
  /* ``1fr`` alone cannot shrink below its content, so on a narrow window the four fixed
     columns took the row and the detail text walked out of the dialog. */
  grid-template-columns: 74px 130px 52px 56px minmax(0, 1fr);
  align-items: baseline;
  gap: 10px;
  padding: 4px 10px;
  font-size: 11px;
  border-radius: var(--hud-radius);
  border-left: 2px solid rgba(77, 216, 255, 0.28);
  background: rgba(77, 216, 255, 0.03);
}

/* A refusal is the row worth finding, so it is the one that gets the colour. */
.act__row--no {
  border-left-color: rgba(255, 181, 71, 0.7);
  background: rgba(255, 181, 71, 0.06);
}

.act__row--no .act__verdict {
  color: var(--hud-amber);
}

.act__time,
.act__ms {
  color: var(--hud-dim);
}

.act__tool {
  color: var(--hud-cyan);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.act__verdict {
  color: var(--hud-green);
}

.act__ms {
  text-align: right;
}

.act__detail {
  color: var(--hud-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.act__note {
  margin: 10px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--hud-line);
  font-size: 10px;
  line-height: 1.6;
  color: var(--hud-dim);
}
</style>
