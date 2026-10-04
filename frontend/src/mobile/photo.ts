/**
 * 带一张图问她（豆包式"看图说话"在手机上的那一半）。
 *
 * 两条入口：拍一张、从相册挑一张。两条**走完全同一条压缩和校验的路** ——
 * 分成两份实现的话，迟早只有一条被修好，另一条在真机上继续发着过大的图。
 *
 * 挑完不直接发，先压：电脑那边一张图的 data URL 上限是 400_000 字符，
 * 而手机摄像头随手一张就是 3~8 MB。原图发过去只会得到一个 413 或者被丢掉，
 * 用户在屏幕上看到的是"她没看见我拍的东西"。所以这里自己降到能过的尺寸，
 * 并且**降不下来就直说**，不发一张她其实看不清的图。
 */
import { Camera, CameraResultType, CameraSource, type CameraPermissionType } from '@capacitor/camera'
import { MAX_DATA_CHARS, type Attachment } from './api'

const LADDER: [number, number][] = [
  [1280, 0.82],
  [1024, 0.74],
  [800, 0.66],
  [640, 0.55],
]

async function recompress(dataUrl: string, longest: number, quality: number): Promise<string> {
  const blob = await (await fetch(dataUrl)).blob()
  const bitmap = await createImageBitmap(blob)
  const scale = Math.min(1, longest / Math.max(bitmap.width, bitmap.height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(bitmap.width * scale))
  canvas.height = Math.max(1, Math.round(bitmap.height * scale))
  const paint = canvas.getContext('2d')
  if (!paint) throw new Error('这台手机画布用不了，压不了图')
  paint.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
  bitmap.close?.()
  return canvas.toDataURL('image/jpeg', quality)
}

/** 取一张图 → 压到电脑收得下的尺寸。取消返回 null，不报错。 */
async function grab(source: CameraSource, name: string): Promise<Attachment | null> {
  // 先要权限，再叫相机。清单里现在声明了 CAMERA（手电筒走的是 `setTorchMode`，
  // 那颗灯归相机管），而**声明了却没被授权的系统会拒绝让外部相机为我们服务**——
  // 表现是"拍照这个功能是坏的"，看不出跟手电筒有任何关系。相册那条路同理，
  // Android 13 起读媒体是运行时权限。
  const wanted: CameraPermissionType = source === CameraSource.Photos ? 'photos' : 'camera'
  const gate = await Camera.requestPermissions({ permissions: [wanted] })
  if (gate[wanted] === 'denied') {
    throw new Error(
      source === CameraSource.Photos
        ? '没给相册权限，挑不了图'
        : '没给相机权限，拍不了照。想问她屏幕上的东西，可以先截个图再发过来',
    )
  }
  const shot = await Camera.getPhoto({
    quality: 80,
    allowEditing: false,
    resultType: CameraResultType.DataUrl,
    source,
    width: 1280,
  })
  let data = shot.dataUrl ?? ''
  if (!data.startsWith('data:image/')) return null
  for (const [longest, quality] of LADDER) {
    if (data.length <= MAX_DATA_CHARS) break
    data = await recompress(data, longest, quality)
  }
  if (data.length > MAX_DATA_CHARS) {
    throw new Error('这张图压到最小还是超过电脑收得下的量，换一张或截个屏发过来')
  }
  return {
    name: `${name}-${Date.now()}`,
    kind: 'image',
    mime: 'image/jpeg',
    data,
  }
}

/** 拍一张。 */
export function takePhoto(): Promise<Attachment | null> {
  return grab(CameraSource.Camera, '拍照')
}

/**
 * 从相册挑一张。
 *
 * `CameraSource.Photos` 只开相册，不弹"拍照还是相册"那个中间菜单 ——
 * 用户按的是"相册"图标，再问一遍"你要拍照还是选照片"是多余的一跳。
 */
export function pickPhoto(): Promise<Attachment | null> {
  return grab(CameraSource.Photos, '相册')
}
