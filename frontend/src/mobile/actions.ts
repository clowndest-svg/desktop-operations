/**
 * 手机的"手"：AI 能在这台设备上做的那几件事，以及**唯一一条执行通道**。
 *
 * 三件事写在一起不是巧合，是这张表存在的理由：
 *
 * 1. **能力面是一张表，不是一堆 if**。同一份 `ACTIONS` 同时喂给模型（工具清单）、
 *    界面（"这台手机她能做什么"那一页）和执行器（查表调用）。分成三份的话，
 *    迟早出现"模型说她能定闹钟、点了发现没这个按钮"。
 * 2. **确认闸在数据里，不在调用者嘴上**。每个动作自带 `needsConfirm`，
 *    而 `execute()` 是这里唯一的出口：需要确认的动作必须拿一个 `approve` 回调进来，
 *    没有回调就直接拒绝。所以"忘了确认"这条路在类型上就走不通。
 * 3. **`confirmed` 不是模型能填的参数**。桌面那边定过这条规矩（清理磁盘的
 *    `confirmed` 永远不许出现在工具签名里），手机这边照办：模型能填的只有
 *    `hour`、`number` 这种事实参数，点不点头由人在屏幕上决定。
 *
 * ## 哪些事这里刻意不做
 *
 * 读短信、读通讯录、代用户点别的 App 的界面——那些要无障碍服务或者一批危险权限，
 * 装上等于把整台手机交出去，而没人会为了"帮我定个闹钟"预期这种代价。
 * 界面上把这条写在"她能做什么"那一页里，不藏。
 */
import { registerPlugin } from '@capacitor/core'
import { MAX_DATA_CHARS, type Attachment } from './api'

export interface ActionResult {
  ok: boolean
  /** 一句人话，直接进对话气泡和动作台账。 */
  text: string
}

export interface ActionArg {
  name: string
  type: 'string' | 'integer' | 'boolean'
  desc: string
  required?: boolean
  /** 取值范围写死在这里：模型只能从这几个里挑，拼不出一个越界的目标。 */
  enum?: string[]
}

export interface PhoneAction {
  name: string
  label: string
  desc: string
  args: ActionArg[]
  needsConfirm: boolean
  /** 会离开这台手机 / 花真钱 / 留下别人看得见的痕迹 —— 台账里要标出来。 */
  leavesDevice: boolean
  run: (args: Record<string, unknown>) => Promise<ActionResult>
}

export type Approve = (prompt: string) => Promise<boolean>

interface BatteryReply {
  ok?: boolean
  percent?: number
  charging?: boolean
  error?: string
}

interface VolumeReply {
  ok?: boolean
  percent?: number
  max?: number
  error?: string
}

interface FlagReply {
  ok?: boolean
  error?: string
  note?: string
  at?: string
  name?: string
  candidates?: string[]
  apps?: string[]
  percent?: number
  ms?: number
  on?: boolean
  chars?: number
}

interface PhotoRow {
  uri: string
  name: string
  width: number
  height: number
  modified: number
}

interface MediaReply extends FlagReply {
  photos?: PhotoRow[]
  targets?: { package: string; label: string }[]
  granted?: boolean
}

interface PhotoReply extends FlagReply {
  data?: string
  bytes?: number
}

interface XyActionPlugin {
  capability(): Promise<Record<string, unknown>>
  battery(): Promise<BatteryReply>
  volume(): Promise<VolumeReply>
  setVolume(options: { percent: number }): Promise<FlagReply>
  vibrate(options: { ms?: number }): Promise<FlagReply>
  torch(options: { on: boolean }): Promise<FlagReply>
  setAlarm(options: { hour: number; minute: number; label: string }): Promise<FlagReply>
  dial(options: { number: string }): Promise<FlagReply>
  composeSms(options: { number: string; body: string }): Promise<FlagReply>
  addCalendar(options: { title: string; inMinutes: number; spanMinutes: number }): Promise<FlagReply>
  openUrl(options: { url: string }): Promise<FlagReply>
  openSettings(options: { page: string }): Promise<FlagReply>
  launchApp(options: { name: string }): Promise<FlagReply>
  listApps(): Promise<FlagReply>
  requestMedia(): Promise<MediaReply>
  listPhotos(options: { limit?: number }): Promise<MediaReply>
  readPhoto(options: { uri: string; longest?: number }): Promise<PhotoReply>
  shareText(options: { text: string; package?: string }): Promise<FlagReply>
  shareTargets(): Promise<MediaReply>
  copyText(options: { text: string }): Promise<FlagReply>
}

/**
 * 桌面浏览器里的降级：每一条都老实说"这只能在手机上做"。
 *
 * 不返回一个假的 ok:true —— 那正是"看起来一切正常"的那类 bug：在电脑上调试时
 * 界面全绿，装机才发现她什么都没做。
 */
const refused: ActionResult = { ok: false, text: '这一步要在手机上做，现在这个是浏览器里的调试页' }

const XyAction = registerPlugin<XyActionPlugin>('XyAction', {
  web: {
    capability: async () => ({ native: false, reason: '浏览器里没有手机的手' }),
    battery: async () => ({ ok: false, error: refused.text }),
    volume: async () => ({ ok: false, error: refused.text }),
    setVolume: async () => ({ ok: false, error: refused.text }),
    vibrate: async () => ({ ok: false, error: refused.text }),
    torch: async () => ({ ok: false, error: refused.text }),
    setAlarm: async () => ({ ok: false, error: refused.text }),
    dial: async () => ({ ok: false, error: refused.text }),
    composeSms: async () => ({ ok: false, error: refused.text }),
    addCalendar: async () => ({ ok: false, error: refused.text }),
    openUrl: async () => ({ ok: false, error: refused.text }),
    openSettings: async () => ({ ok: false, error: refused.text }),
    launchApp: async () => ({ ok: false, error: refused.text }),
    listApps: async () => ({ ok: false, error: refused.text }),
    requestMedia: async () => ({ ok: false, error: refused.text }),
    listPhotos: async () => ({ ok: false, error: refused.text }),
    readPhoto: async () => ({ ok: false, error: refused.text }),
    shareText: async () => ({ ok: false, error: refused.text }),
    shareTargets: async () => ({ ok: false, error: refused.text }),
    copyText: async () => ({ ok: false, error: refused.text }),
  },
})

/** 相册里的一张照片。`uri` 交给原生层去读，界面只拿缩略图。 */
export interface AlbumPhoto {
  uri: string
  name: string
  width: number
  height: number
  modified: number
  thumb?: string
}

function text(reply: { error?: string; note?: string }, fallback: string): string {
  return String(reply.error || reply.note || fallback)
}

function num(value: unknown, fallback: number): number {
  const parsed = typeof value === 'number' ? value : Number(String(value ?? '').trim())
  return Number.isFinite(parsed) ? parsed : fallback
}

function str(value: unknown, fallback = ''): string {
  return value === undefined || value === null || value === '' ? fallback : String(value)
}

export const ACTIONS: PhoneAction[] = [
  {
    name: 'battery',
    label: '看电量',
    desc: '读这台手机现在的电量和有没有在充电。只读，不改任何东西。',
    args: [],
    needsConfirm: false,
    leavesDevice: false,
    run: async () => {
      const reply = await XyAction.battery()
      return reply.ok
        ? { ok: true, text: `电量 ${reply.percent ?? '?'}%${reply.charging ? '，正插着电' : ''}` }
        : { ok: false, text: text(reply, '读不到电量') }
    },
  },
  {
    name: 'get_volume',
    label: '看音量',
    desc: '读这台手机当前的媒体音量（百分比）。',
    args: [],
    needsConfirm: false,
    leavesDevice: false,
    run: async () => {
      const reply = await XyAction.volume()
      return reply.ok ? { ok: true, text: `媒体音量 ${reply.percent ?? '?'}%` } : { ok: false, text: text(reply, '读不到音量') }
    },
  },
  {
    name: 'set_volume',
    label: '调音量',
    desc: '把媒体音量调到指定的百分比（0 到 100）。随时可以再调回来。',
    args: [
      {
        name: 'percent',
        type: 'integer',
        desc: '目标音量，0 到 100 的整数',
        required: true,
      },
    ],
    needsConfirm: false,
    leavesDevice: false,
    run: async (args) => {
      const percent = Math.max(0, Math.min(100, Math.round(num(args.percent, -1))))
      if (percent < 0) return { ok: false, text: '要说调到百分之多少' }
      const reply = await XyAction.setVolume({ percent })
      return reply.ok ? { ok: true, text: `音量调到 ${percent}%` } : { ok: false, text: text(reply, '音量没调成') }
    },
  },
  {
    name: 'torch',
    label: '手电筒',
    desc: '打开或关掉手电筒（用的是背面那颗闪光灯）。',
    args: [{ name: 'on', type: 'boolean', desc: 'true 开，false 关', required: true }],
    needsConfirm: false,
    leavesDevice: false,
    run: async (args) => {
      const on = args.on === true || String(args.on).toLowerCase() === 'true'
      const reply = await XyAction.torch({ on })
      return reply.ok ? { ok: true, text: on ? '手电筒开了' : '手电筒关了' } : { ok: false, text: text(reply, '闪光灯没响应') }
    },
  },
  {
    name: 'vibrate',
    label: '震一下',
    desc: '让手机震一下，用来提醒人东西放哪了之类的。',
    args: [{ name: 'ms', type: 'integer', desc: '震多久，毫秒，默认 220' }],
    needsConfirm: false,
    leavesDevice: false,
    run: async (args) => {
      const ms = Math.max(20, Math.min(2000, Math.round(num(args.ms, 220))))
      const reply = await XyAction.vibrate({ ms })
      return reply.ok ? { ok: true, text: `震了 ${ms} 毫秒` } : { ok: false, text: text(reply, '这台设备震不了') }
    },
  },
  {
    name: 'open_settings',
    label: '开设置页',
    desc: '打开系统设置的某一页，开关还是人在上面自己拨。只能开下面列出的那几页。',
    args: [
      {
        name: 'page',
        type: 'string',
        desc: '要开的那一页',
        required: true,
        enum: [
          'wlan',
          'bluetooth',
          'location',
          'sound',
          'display',
          'notification',
          'battery',
          'airplane',
          'date',
          'accessibility',
          'tts',
          'app',
        ],
      },
    ],
    needsConfirm: false,
    leavesDevice: false,
    run: async (args) => {
      const page = str(args.page).toLowerCase()
      const reply = await XyAction.openSettings({ page })
      return reply.ok ? { ok: true, text: `设置页开了：${page}` } : { ok: false, text: text(reply, '那一页设置打不开') }
    },
  },
  {
    name: 'list_apps',
    label: '列应用',
    desc: '列出这台手机上能叫得起来的应用名字。想知道某个应用在不在，先问这个，别猜。',
    args: [],
    needsConfirm: false,
    leavesDevice: false,
    run: async () => {
      const reply = await XyAction.listApps()
      const names = reply.apps ?? []
      return reply.ok
        ? { ok: true, text: names.length ? names.slice(0, 60).join('、') : '这台设备不允许列出应用' }
        : { ok: false, text: text(reply, '列不出应用') }
    },
  },
  {
    name: 'launch_app',
    label: '打开应用',
    desc: '按名字打开一个已安装的应用。名字要准，不确定就先 list_apps。',
    args: [{ name: 'name', type: 'string', desc: '应用名字，比如“微信”', required: true }],
    needsConfirm: false,
    leavesDevice: false,
    run: async (args) => {
      const name = str(args.name)
      if (!name) return { ok: false, text: '要说打开哪个应用' }
      const reply = await XyAction.launchApp({ name })
      return reply.ok
        ? { ok: true, text: `打开了 ${reply.name ?? name}` }
        : { ok: false, text: text(reply, `叫不动 ${name}`) }
    },
  },
  {
    name: 'set_alarm',
    label: '定闹钟',
    desc: '在系统时钟里定一个闹钟。定完会显示在时钟 App 里，状态栏也有图标。',
    args: [
      { name: 'hour', type: 'integer', desc: '几点，24 小时制（0-23）', required: true },
      { name: 'minute', type: 'integer', desc: '几分（0-59）', required: true },
      { name: 'label', type: 'string', desc: '闹钟叫什么名字' },
    ],
    needsConfirm: true,
    leavesDevice: false,
    run: async (args) => {
      const hour = Math.round(num(args.hour, -1))
      const minute = Math.round(num(args.minute, -1))
      const reply = await XyAction.setAlarm({ hour, minute, label: str(args.label, '小夜定的闹钟') })
      return reply.ok ? { ok: true, text: `闹钟定在 ${reply.at ?? `${hour}:${minute}`}` } : { ok: false, text: text(reply, '闹钟没定上') }
    },
  },
  {
    name: 'dial',
    label: '拨号',
    desc: '把号码填进拨号盘并打开它。**拨出那一下由人按**，她不会自己打出去。',
    args: [{ name: 'number', type: 'string', desc: '要拨的号码', required: true }],
    needsConfirm: true,
    leavesDevice: true,
    run: async (args) => {
      const number = str(args.number)
      const reply = await XyAction.dial({ number })
      return reply.ok ? { ok: true, text: `拨号盘里填好了 ${number}，按拨号键的是你` } : { ok: false, text: text(reply, '拨号盘打不开') }
    },
  },
  {
    name: 'send_sms',
    label: '写短信',
    desc: '打开短信草稿并填好收件人和正文。**发送那一下由人按**。',
    args: [
      { name: 'number', type: 'string', desc: '发给谁（号码）', required: true },
      { name: 'body', type: 'string', desc: '短信内容', required: true },
    ],
    needsConfirm: true,
    leavesDevice: true,
    run: async (args) => {
      const number = str(args.number)
      const body = str(args.body)
      const reply = await XyAction.composeSms({ number, body })
      return reply.ok ? { ok: true, text: `短信草稿填好了（发给 ${number}），发送键在你手上` } : { ok: false, text: text(reply, '短信草稿打不开') }
    },
  },
  {
    name: 'add_calendar',
    label: '加日程',
    desc: '打开日历的新建页并填好标题和时间。**存不存由人点**，不会自己写进日历。',
    args: [
      { name: 'title', type: 'string', desc: '什么事', required: true },
      { name: 'inMinutes', type: 'integer', desc: '多久之后开始，默认 60 分钟' },
      { name: 'spanMinutes', type: 'integer', desc: '持续多久，默认 60 分钟' },
    ],
    needsConfirm: true,
    leavesDevice: true,
    run: async (args) => {
      const title = str(args.title)
      const reply = await XyAction.addCalendar({
        title,
        inMinutes: Math.round(num(args.inMinutes, 60)),
        spanMinutes: Math.round(num(args.spanMinutes, 60)),
      })
      return reply.ok ? { ok: true, text: `日历新建页填好了：“${title}”，存不存你点` } : { ok: false, text: text(reply, '日历打不开') }
    },
  },
  {
    name: 'open_url',
    label: '开链接',
    desc: '用系统浏览器打开一个 http/https 链接。别的协议（file、content、intent）一律不开。',
    args: [{ name: 'url', type: 'string', desc: '完整链接，必须 http/https 开头', required: true }],
    needsConfirm: true,
    leavesDevice: true,
    run: async (args) => {
      const url = str(args.url)
      const reply = await XyAction.openUrl({ url })
      return reply.ok ? { ok: true, text: `打开了 ${url}` } : { ok: false, text: text(reply, '链接没打开') }
    },
  },
  {
    name: 'list_photos',
    label: '翻相册',
    desc: '列出这台手机相册里最近的照片（名字、时间、尺寸）。只读清单，不看内容。',
    args: [{ name: 'limit', type: 'integer', desc: '列几张，默认 20，最多 60' }],
    needsConfirm: false,
    leavesDevice: false,
    run: async (args) => {
      const reply = await XyAction.listPhotos({ limit: Math.round(num(args.limit, 20)) })
      if (!reply.ok) return { ok: false, text: text(reply, '翻不了相册') }
      const rows = reply.photos ?? []
      if (!rows.length) return { ok: true, text: '相册里没有能列出来的照片' }
      album = rows
      const lines = rows
        .slice(0, 20)
        .map((row, index) => `${index + 1}. ${row.name}（${new Date(row.modified).toLocaleDateString('zh-CN')}）`)
      return { ok: true, text: `最近这些：\n${lines.join('\n')}\n要说哪一张，报序号就行` }
    },
  },
  {
    name: 'attach_photo',
    label: '把某张照片挂上',
    desc: '按序号把相册里那张图挂到输入框上，跟着下一句一起发出去（这样她才真看得见那张图）。要先 list_photos。',
    args: [
      { name: 'index', type: 'integer', desc: 'list_photos 列出来的第几张，从 1 开始', required: true },
    ],
    needsConfirm: false,
    leavesDevice: false,
    run: async (args) => {
      const which = Math.round(num(args.index, 0))
      const row = album[which - 1]
      if (!row) return { ok: false, text: `相册清单里没有第 ${which} 张，先 list_photos` }
      const shot = await readAlbumPhoto(row.uri, 1280)
      return shot
        ? { ok: true, text: `已经把「${row.name}」挂在输入框上了，现在问她想问什么` }
        : { ok: false, text: '这张读不出来（可能是云相册里还没下载到本地的图）' }
    },
  },
  {
    name: 'list_share_targets',
    label: '能发给谁',
    desc: '列出这台手机上能接住一段文字的应用（微信、QQ、邮件之类），拿到它们的包名。',
    args: [],
    needsConfirm: false,
    leavesDevice: false,
    run: async () => {
      const reply = await XyAction.shareTargets()
      const rows = reply.targets ?? []
      return reply.ok
        ? {
            ok: true,
            text: rows.length
              ? rows.map((row) => `${row.label}(${row.package})`).join('、')
              : '没有能接文字的应用',
          }
        : { ok: false, text: text(reply, '列不出能分享到的应用') }
    },
  },
  {
    name: 'share_text',
    label: '分享到应用',
    desc: '把一段文字交到微信（或指定应用）手上：应用会弹出它自己的选人界面，选谁、发不发由人点。她代发不出去。',
    args: [
      { name: 'text', type: 'string', desc: '要发出去的那段话', required: true },
      { name: 'app', type: 'string', desc: '应用包名，默认微信 com.tencent.mm' },
    ],
    needsConfirm: true,
    leavesDevice: true,
    run: async (args) => {
      const reply = await XyAction.shareText({
        text: str(args.text),
        package: str(args.app, 'com.tencent.mm'),
      })
      return reply.ok
        ? { ok: true, text: '已经递到应用的选人界面了，选谁、按发送都是你点' }
        : { ok: false, text: text(reply, '没交出去') }
    },
  },
  {
    name: 'copy_text',
    label: '复制到剪贴板',
    desc: '把一段文字复制到剪贴板。分享接不住的应用，用这个一粘就走。',
    args: [{ name: 'text', type: 'string', desc: '要复制的内容', required: true }],
    needsConfirm: false,
    leavesDevice: false,
    run: async (args) => {
      const reply = await XyAction.copyText({ text: str(args.text) })
      return reply.ok
        ? { ok: true, text: `复制了 ${reply.chars ?? 0} 个字，去要发的地方长按粘贴` }
        : { ok: false, text: text(reply, '复制失败') }
    },
  },
]

/**
 * `list_photos` 刚列出来的那一份，`attach_photo` 按序号回头找。
 *
 * 为什么不把图片内容直接当工具结果回给模型：一张 1280 的 JPEG base64 是 20~50 万字符，
 * 塞进对话历史就是把上下文窗口和 WebView 的内存一起点炸——那正是"聊着聊着闪退"的
 * 另一类来源。图走附件那条路，一次一张，进历史时只留一句"📷"。
 */
let album: AlbumPhoto[] = []

/**
 * 读一张相册图。缩略图传 220，真发出去传 1280。失败返回 null，不抛。
 *
 * 这里自己再降一档而不是直接信传进来的 `longest`：相册原图常有 4000×3000，
 * 降到 1280 + q80 之后 base64 仍可能超过电脑收得下的量（`photo.ts` 那条
 * 380_000 字符的上限），遥控模式下就是一个 413。拍照那条路有压缩阶梯，
 * 相册这条之前没有——同一个"发出去的图多大"的问题不该只有半条路管得住。
 */
export async function readAlbumPhoto(uri: string, longest: number): Promise<Attachment | null> {
  const reply = await XyAction.readPhoto({ uri, longest })
  if (!reply.ok || !reply.data) return null
  if (longest > 640 && reply.data.length > MAX_DATA_CHARS) return readAlbumPhoto(uri, 640)
  return { name: '相册', kind: 'image', mime: 'image/jpeg', data: reply.data }
}

/** 相册面板用：列出来 + 顺手把缩略图解码好。没权限时返回空表并把原因带出去。 */
export async function listAlbum(limit = 24): Promise<{ photos: AlbumPhoto[]; error: string }> {
  const reply = await XyAction.listPhotos({ limit })
  if (!reply.ok) return { photos: [], error: text(reply, '翻不了相册') }
  const rows = reply.photos ?? []
  const withThumbs = await Promise.all(
    rows.map(async (row) => {
      const shot = await readAlbumPhoto(row.uri, 220)
      return { ...row, thumb: shot?.data }
    }),
  )
  return { photos: withThumbs, error: '' }
}

/** 要一次相册权限。界面在打开相册面板时先调这个，别等 list 失败才知道没给。 */
export async function askMediaAccess(): Promise<boolean> {
  const reply = await XyAction.requestMedia()
  return Boolean(reply.ok && reply.granted)
}

const BY_NAME = new Map(ACTIONS.map((action) => [action.name, action]))

export function findAction(name: string): PhoneAction | undefined {
  return BY_NAME.get(name)
}

/** 一句"她现在会做什么"，写给模型看，也写给设置页看 —— 同一份，不分两头写。 */
export function actionCatalog(): string {
  return ACTIONS.map((item) => `${item.name}（${item.label}）`).join('、')
}

/**
 * 唯一一条执行通道。
 *
 * `approve` 是**必填**参数：调用方是模型驱动的工具环也好、是面板上的按钮也好，
 * 都必须先把"要不要真做"这个问题递到人眼前才有资格调这个函数。
 * 这条规矩不写在注释里，写在类型上——少传一个回调就是一条编译错误。
 */
export async function execute(
  name: string,
  args: Record<string, unknown>,
  approve: Approve,
): Promise<ActionResult> {
  const action = BY_NAME.get(name)
  if (!action) return { ok: false, text: `没有这个手机动作：${name}` }
  if (action.needsConfirm && !(await approve(describe(action, args)))) {
    return { ok: false, text: `你按了取消，“${action.label}”没做` }
  }
  try {
    return await action.run(args)
  } catch (error) {
    // 原生层抛出来的也是人话；兜底那句只在真的没有消息时才会露出来。
    return {
      ok: false,
      text: error instanceof Error && error.message ? error.message : `${action.label}没做成`,
    }
  }
}

/** 把参数摊成一句给人看的话：确认框里不能只写"确定吗"。 */
export function describe(action: PhoneAction, args: Record<string, unknown>): string {
  const parts = action.args
    .map((arg) => `${arg.name}=${String(args[arg.name] ?? '')}`)
    .filter((piece) => !piece.endsWith('='))
  return parts.length ? `${action.name} ${parts.join(' ')}` : action.name
}

/** 模型看到的工具清单（OpenAI 兼容的 function 格式）。 */
export function toolSchemas(): object[] {
  return ACTIONS.map((action) => ({
    type: 'function',
    function: {
      name: action.name,
      description: action.desc,
      parameters: {
        type: 'object',
        properties: Object.fromEntries(
          action.args.map((arg) => [
            arg.name,
            {
              type: arg.type,
              description: arg.desc,
              ...(arg.enum ? { enum: arg.enum } : {}),
            },
          ]),
        ),
        required: action.args.filter((arg) => arg.required).map((arg) => arg.name),
      },
    },
  }))
}
