<template>
  <div v-if="open" class="pf" role="dialog" aria-modal="true" aria-label="桌面形象">
    <div class="pf__scrim" @click="emit('close')"></div>

    <section class="pf__box">
      <!--
        The head and the tabs stay outside ``pf__body`` on purpose: the body is the only
        thing that scrolls, so 关闭 cannot be scrolled out of the window. Every popup in
        this app that had the button inside its scroller is being fixed the same way.
      -->
      <header class="pf__head">
        <h2 class="hud-title">桌面形象 · PET</h2>
        <button class="hud-btn" type="button" @click="emit('close')">关闭</button>
      </header>

      <div class="pf__tabs" role="group" aria-label="换形象的方式">
        <button class="hud-btn pf__tab" :class="{ 'pf__tab--on': tab === 'models' }" type="button" @click="tab = 'models'">
          内置模型（{{ models.length }}）
        </button>
        <button class="hud-btn pf__tab" :class="{ 'pf__tab--on': tab === 'forge' }" type="button" @click="tab = 'forge'">
          从照片做
        </button>
      </div>

      <div class="pf__body">

      <template v-if="tab === 'models'">
        <ul class="pf__terms">
          <li>随包装好的模型，<b>不联网、不下图</b>；文件就在程序目录里，换机器也带着走。</li>
          <li>来源是 Quaternius，按其 QAL v1.0 许可：<b>可以商用、不需要署名</b>；但不能把这些模型当成素材包再卖或再送。逐条抄在「内置模型与许可」里。</li>
          <li>这些是<b>真骨骼</b>的模型：待机、走路、张望这些动作在文件里，由程序循环播。</li>
          <li>它们没有口型：说话时不会动嘴，那是小夜那具 VRM 才有的东西。</li>
        </ul>
        <p v-if="error" class="pf__why pf__why--bad">{{ error }}</p>
        <p v-if="note" class="pf__why">{{ note }}</p>

        <div class="pf__grid" aria-label="内置模型">
          <article
            v-for="entry in models"
            :key="entry.id"
            class="pf__card"
            :class="{ 'pf__card--on': entry.id === current }"
          >
            <h3 class="pf__card-name">{{ entry.name }}</h3>
            <p class="pf__card-meta hud-num">{{ entry.kind }} · {{ entry.clips }} 段动作 · {{ mib(entry.bytes) }}</p>
            <p class="pf__card-note">{{ entry.note }}</p>
            <p class="pf__card-actions">
              <button class="hud-btn" type="button" :disabled="busy || entry.id === current" @click="wearModel(entry)">
                {{ entry.id === current ? '在用' : '换上她' }}
              </button>
            </p>
          </article>
        </div>
        <p class="pf__why">内置模型是随程序来的，删不掉；不想用她就「用回原来的小夜」。</p>
        <div class="pf__batch">
          <button class="hud-btn" type="button" :disabled="busy" @click="restore">用回原来的小夜</button>
        </div>
      </template>

      <template v-else>
      <!--
        The three things this panel has to say before anybody clicks, in the order
        they will be asked about: it runs on this machine (nothing is uploaded), the
        photograph is not kept, and the result is a silhouette with thickness rather
        than a rigged model. Written here because the alternative is a person finding
        out from the desktop, ten seconds after choosing a photo.
      -->
      <ul class="pf__terms">
        <li>不联网：抠图、描轮廓、上色都在你自己这台机器上算。</li>
        <li>线框只留轮廓；<b>卡通</b>再多留一个颜色；<b>照片</b>会留一张裁成剪影的缩略图（删掉这只就一起没了）。</li>
        <li>做出来的是<b>有厚度的剪影</b>：正面立体，正侧面看是扁的。</li>
        <li>它没有骨骼：会呼吸、慢慢侧转、思考时整体前倾，<b>不会坐下或点头</b>。</li>
      </ul>

      <div class="pf__styles" role="group" aria-label="风格">
        <button
          v-for="style in STYLES"
          :key="style.key"
          class="hud-btn pf__style"
          :class="{ 'pf__style--on': chosen === style.key }"
          type="button"
          :disabled="busy"
          :title="style.note"
          @click="chosen = style.key"
        >
          {{ style.label }}
        </button>
      </div>
      <p class="pf__why">{{ styleNote }}</p>
      <section class="pf__make" aria-label="从照片生成">
        <input ref="file" class="pf__file" type="file" accept="image/*" @change="onPick" />
        <label class="pf__name">
          <span class="hud-label">名字（可选）</span>
          <input v-model.trim="name" class="pf__input" type="text" maxlength="16" placeholder="我家猫" />
        </label>
        <button
          class="hud-btn pf__go"
          type="button"
          :disabled="busy || !canGenerate"
          :title="canGenerate ? '选一张照片，本机做四个候选' : reason"
          @click="choose"
        >
          {{ busy ? '生成中…' : '选照片生成' }}
        </button>
      </section>

      <p v-if="!canGenerate" class="pf__why pf__why--bad">{{ reason }}</p>
      <p v-else-if="rows.length === 0 && !busy" class="pf__why">
        还没有生成过。挑一张<b>主体清楚、背景别太乱</b>的照片试试，猫狗人都行。
      </p>
      <p v-if="error" class="pf__why pf__why--bad">{{ error }}</p>
      <p v-if="note" class="pf__why">{{ note }}</p>

      <div v-if="rows.length" class="pf__batch">
        <button class="hud-btn" type="button" :disabled="busy" @click="restore">用回原来的小夜</button>
      </div>

      <div class="pf__grid" aria-label="宠物候选">
        <article v-for="row in rows" :key="row.id" class="pf__card" :class="{ 'pf__card--on': row.id === current }">
          <img v-if="row.preview" class="pf__thumb" :src="previewOf(row)" alt="剪影预览" />
          <p v-else class="pf__thumb pf__thumb--gone">预览没了</p>
          <h3 class="pf__card-name">{{ row.name }}</h3>
          <p class="pf__card-meta hud-num">
            {{ STYLE_LABELS[row.style] || row.style }} ·
            {{ row.style === 'cartoon' ? '模板形状' : row.lines + ' 个拐点' }} · 厚
            {{ row.depth.toFixed(2) }}
            <span v-if="row.cost_ms">· 抠图 {{ row.cost_ms }} ms</span>
          </p>
          <p v-if="row.has_texture" class="pf__card-note">存了这张照片裁出来的缩略图</p>
          <p class="pf__card-actions">
            <button
              class="hud-btn"
              type="button"
              :disabled="busy || row.id === current"
              @click="wear(row)"
            >
              {{ row.id === current ? '在用' : '换上她' }}
            </button>
            <button class="hud-btn" type="button" :disabled="busy" @click="armed = armed === row.id ? '' : row.id">
              删除
            </button>
          </p>
          <p v-if="armed === row.id" class="pf__why pf__why--bad">
            删掉「{{ row.name }}」？轮廓和预览一起没，不能恢复。
            <button class="hud-btn danger" type="button" :disabled="busy" @click="remove(row)">确认删除</button>
          </p>
        </article>
      </div>

      <p v-if="rows.length" class="pf__count hud-num">{{ rows.length }} / 12</p>
      </template>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
/**
 * The picker for figures made from photographs.
 *
 * Deliberately a sibling of ``VoicePickerPopup`` rather than a section inside 设置:
 * the two decisions are made the same way -- try it, look at it, then commit -- and
 * the voice panel already had the layout for it. The 音色 entry in 设置 opens the
 * same shared popup for the same reason (see 「换音色」 there).
 */
import { computed, onMounted, ref, watch } from 'vue'
import { petAssets, petDelete, petGenerate, petPick, type GalleryModel, type PetFigureRow } from '@/api/bridge'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const MAX_EDGE = 768
const QUALITY = 0.82
/** Four candidates per photo, and the panel says so in the same words the shell uses. */
const PER_PHOTO = 4

/** 内置模型 first: that is the shelf the operator asked for, and it is what a fresh install can use. */
const tab = ref<'models' | 'forge'>('models')
const models = ref<GalleryModel[]>([])
const rows = ref<PetFigureRow[]>([])
const current = ref('')
const canGenerate = ref(false)
const reason = ref('')
const error = ref('')
const note = ref('')
const busy = ref(false)
const name = ref('')
const armed = ref('')
const file = ref<HTMLInputElement | null>(null)

const STYLES = [
  { key: 'outline', label: '线框', note: '只留描出来的轮廓，硬盘上没有任何照片。' },
  { key: 'photo', label: '照片', note: '脸上贴你照片裁成剪影的缩略图，最像；会存这张缩略图。' },
  { key: 'cartoon', label: '卡通', note: '形状是画好的（坐猫/团鸡/垂耳狗/团子），颜色取自你的照片，不存图。' },
] as const
const STYLE_LABELS: Record<string, string> = { outline: '线框', photo: '照片', cartoon: '卡通' }
const chosen = ref<string>('outline')

const styleNote = computed(() => STYLES.find((item) => item.key === chosen.value)?.note ?? '')

async function load(): Promise<void> {
  try {
    const answer = await petAssets()
    models.value = answer.models ?? []
    rows.value = answer.pets
    current.value = answer.current
    canGenerate.value = answer.can_generate !== false
    reason.value = answer.reason ?? ''
    error.value = answer.error ?? ''
  } catch (caught) {
    error.value = '问程序要宠物清单时出错了：' + String(caught)
  }
}

/**
 * Resize in the browser before the bytes cross the bridge.
 *
 * A phone photo is 4000x3000 and 8 MB; the segmentation looks at 320x320. The
 * encoder here costs one canvas draw, while the same photo pushed at the shell
 * would be an 11 MB base64 string through pywebview's JSON channel -- which is the
 * kind of thing that reads as "the window froze" rather than "the payload was big".
 */
function shrink(photo: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('照片没读出来'))
    reader.onload = () => {
      const image = new Image()
      image.onerror = () => reject(new Error('这张照片打不开'))
      image.onload = () => {
        const scale = Math.min(1, MAX_EDGE / Math.max(image.width, image.height))
        const canvas = document.createElement('canvas')
        canvas.width = Math.max(1, Math.round(image.width * scale))
        canvas.height = Math.max(1, Math.round(image.height * scale))
        const context = canvas.getContext('2d')
        if (!context) {
          reject(new Error('浏览器没给画布'))
          return
        }
        context.drawImage(image, 0, 0, canvas.width, canvas.height)
        resolve(canvas.toDataURL('image/jpeg', QUALITY))
      }
      image.src = String(reader.result)
    }
    reader.readAsDataURL(photo)
  })
}

function choose(): void {
  error.value = ''
  file.value?.click()
}

async function onPick(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const photo = input.files?.[0]
  // Cleared first: picking the same file twice has to fire change again, or the
  // second attempt looks like a dead button.
  input.value = ''
  if (!photo) return
  busy.value = true
  error.value = ''
  note.value = ''
  try {
    const data = await shrink(photo)
    const answer = await petGenerate(
      data,
      name.value || photo.name.replace(/\.[^.]+$/, ''),
      chosen.value,
    )
    rows.value = answer.pets
    current.value = answer.current
    if (!answer.ok) {
      error.value = answer.error || '没生成出来'
    } else {
      note.value =
        '做好了 4 个' +
        (STYLE_LABELS[answer.style || chosen.value] || '') +
        '候选，用时 ' +
        answer.cost_ms +
        ' ms（其中抠图 ' +
        answer.segment_ms +
        ' ms）。挑一个换上她。'
    }
  } catch (caught) {
    error.value = String(caught instanceof Error ? caught.message : caught)
  } finally {
    busy.value = false
  }
}

async function wear(row: PetFigureRow): Promise<void> {
  await apply(await petPick(row.id), '换不上去：')
}

async function restore(): Promise<void> {
  await apply(await petPick(''), '换不回去：')
}

async function remove(row: PetFigureRow): Promise<void> {
  armed.value = ''
  const answer = await petDelete(row.id)
  await apply(answer, '删不掉：')
  if (answer.ok && rows.value.length < PER_PHOTO) note.value = ''
}

/** Bytes off disk, shown as the picker reads them. */
function mib(bytes: number): string {
  return (bytes / 1024 / 1024).toFixed(1) + ' MB'
}

async function wearModel(entry: GalleryModel): Promise<void> {
  const answer = await petPick(entry.id)
  await apply(answer, '换不上去：')
  if (answer.ok) note.value = '换上了' + entry.name + '（' + entry.kind + '，' + entry.clips + ' 段动作）'
}

/** The one shape all three write-actions return: re-render from the answer, show its reason. */
async function apply(answer: Awaited<ReturnType<typeof petAssets>>, prefix: string): Promise<void> {
  models.value = answer.models ?? models.value
  rows.value = answer.pets
  current.value = answer.current
  canGenerate.value = answer.can_generate !== false
  reason.value = answer.reason ?? ''
  error.value = answer.ok ? '' : prefix + (answer.error || '')
}

/** Python sends bare base64; the browser mock sends a whole data URL. Both are valid. */
function previewOf(row: PetFigureRow): string {
  return row.preview.startsWith('data:') ? row.preview : 'data:image/png;base64,' + row.preview
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      armed.value = ''
      void load()
    }
  },
)

onMounted(() => void load())
</script>

<style scoped>
.pf {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
}

.pf__scrim {
  position: absolute;
  inset: 0;
  background: rgba(2, 5, 10, 0.72);
}

.pf__box {
  position: relative;
  width: min(620px, 94vw);
  max-height: 84vh;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 20px 14px;
  border: 1px solid var(--hud-line);
  border-top: 1px solid rgba(77, 216, 255, 0.45);
  border-radius: var(--hud-radius);
  background: linear-gradient(180deg, rgba(10, 24, 40, 0.96), rgba(4, 8, 14, 0.98));
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
}

.pf__tabs {
  display: flex;
  gap: 6px;
}

.pf__tab--on {
  border-color: rgba(77, 216, 255, 0.65);
  background: rgba(10, 30, 48, 0.9);
}

/* The only scroller in this popup, so the head and the tabs never leave their place. */
.pf__body {
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

.pf__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.pf__terms {
  margin: 0;
  padding-left: 18px;
  color: var(--hud-dim);
  font-size: 12px;
  line-height: 1.6;
}

.pf__terms b {
  color: var(--hud-ink);
}

.pf__styles {
  display: flex;
  gap: 6px;
}

.pf__style--on {
  border-color: rgba(77, 216, 255, 0.65);
  background: rgba(10, 30, 48, 0.9);
}

.pf__card-note {
  margin: 0;
  color: #ffd28a;
  font-size: 11px;
}

.pf__make {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}

.pf__file {
  display: none;
}

.pf__name {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1 1 160px;
}

.pf__input {
  padding: 6px 8px;
  border: 1px solid var(--hud-line);
  border-radius: 6px;
  background: rgba(4, 12, 20, 0.8);
  color: var(--hud-ink);
  font: inherit;
}

.pf__go {
  padding: 7px 14px;
}

.pf__why {
  margin: 0;
  color: var(--hud-dim);
  font-size: 12px;
  line-height: 1.6;
}

.pf__why--bad {
  color: #ff8f8f;
}

.pf__batch {
  display: flex;
  gap: 8px;
}

.pf__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
}

.pf__card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--hud-line);
  border-radius: 8px;
  background: rgba(6, 16, 28, 0.72);
}

.pf__card--on {
  border-color: rgba(77, 216, 255, 0.6);
  background: rgba(10, 30, 48, 0.85);
}

.pf__thumb {
  width: 100%;
  height: 128px;
  object-fit: contain;
  border-radius: 6px;
  background: rgba(2, 6, 12, 0.6);
}

.pf__thumb--gone {
  display: grid;
  place-items: center;
  color: var(--hud-dim);
  font-size: 12px;
}

.pf__card-name {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pf__card-meta {
  margin: 0;
  color: var(--hud-dim);
  font-size: 11px;
}

.pf__card-actions {
  display: flex;
  gap: 6px;
  margin: 2px 0 0;
}

.pf__count {
  align-self: flex-end;
  color: var(--hud-dim);
  font-size: 11px;
}
</style>
