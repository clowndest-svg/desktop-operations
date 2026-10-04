/**
 * 把一张图降到"她收得下"的尺寸 —— 电脑侧的那一份。
 *
 * 后端对单张图的 data URL 有上限（`jarvis/app/chat_service.py` 的
 * MAX_IMAGE_DATA_CHARS = 400_000），超了就整个丢掉 `data` 字段：模型只会看到一个
 * 文件名。所以这里自己先降，**降不下来就在屏幕上说清楚**，不发一张她其实看不清的图。
 * 留 20K 余量，和手机那边 `mobile/api.ts` 的 MAX_DATA_CHARS 同一个理由：
 * 两边各算各的"刚好 400_000"，合起来就会在电脑侧被无声丢掉。
 *
 * 为什么不直接沿用手机的 `mobile/photo.ts`：那个文件顶层 import 了
 * `@capacitor/camera`，桌面包里装不到、也不该装那个原生插件，一 import 就把
 * Capacitor 拖进了桌面的构建产物。两份阶梯长得很像，但一边是 WebView2 里的
 * 粘贴板，一边是相机，改动不会同步发生 —— 所以数字各自写死，各自有测试钉住。
 */

/** 一张图最多带多少字符过去。后端上限 400_000，这里留 20K 余量。 */
export const MAX_DATA_CHARS = 380_000

/** 长边超过这个数就缩：截图里的小字 1280 还认得出来，4K 原图只是多烧 token。 */
export const LONGEST_EDGE = 1280

/** 从清楚到省：先到这里就够发出去了就停，不为了小再压一档。 */
const LADDER: [number, number][] = [
  [LONGEST_EDGE, 0.82],
  [1024, 0.74],
  [800, 0.66],
  [640, 0.55],
]

/** 按最长边缩放后重新编码成 JPEG。透明通道在这一步会丢，是"塞得下"的代价。 */
async function recompress(dataUrl: string, longest: number, quality: number): Promise<string> {
  const image = await load(dataUrl)
  const scale = Math.min(1, longest / Math.max(image.width, image.height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(image.width * scale))
  canvas.height = Math.max(1, Math.round(image.height * scale))
  const paint = canvas.getContext('2d')
  if (!paint) throw new Error('这台电脑的画布用不了，压不了图')
  paint.drawImage(image, 0, 0, canvas.width, canvas.height)
  return canvas.toDataURL('image/jpeg', quality)
}

function load(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onerror = () => reject(new Error('这张图打不开，可能不是浏览器认的格式'))
    image.onload = () => resolve(image)
    image.src = dataUrl
  })
}

/** 一张图要不要动它：本来就小、长边也没超，就原样发出去。 */
async function fitsAsIs(dataUrl: string): Promise<boolean> {
  if (dataUrl.length > MAX_DATA_CHARS) return false
  const image = await load(dataUrl)
  return Math.max(image.width, image.height) <= LONGEST_EDGE
}

/**
 * 压到上限以内；压不出来就抛，让界面把话说清楚。
 *
 * 不返回 null 也不"算了发原图"：原图到了后端是被丢掉的，界面上一切正常、她那边
 * 只收到一个文件名 —— 那种失败在这条路上看不见，所以宁可在这里响。
 */
export async function shrinkToBudget(dataUrl: string): Promise<string> {
  if (!dataUrl.startsWith('data:image/')) return dataUrl
  if (await fitsAsIs(dataUrl)) return dataUrl
  let data = dataUrl
  for (const [longest, quality] of LADDER) {
    data = await recompress(data, longest, quality)
    if (data.length <= MAX_DATA_CHARS) return data
  }
  throw new Error('这张图压到最小还是超过她收得下的量，换一张或截个屏发过来')
}

/** 读一个文件成 data URL。失败给空字符串，由调用方决定怎么报。 */
export function readAsDataUrl(file: Blob): Promise<string> {
  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onerror = () => resolve('')
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.readAsDataURL(file)
  })
}
