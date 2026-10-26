export type Character = { id: string; name: string; description: string; voice: string }
const rows = [
  ['月汐', '月饰 · 长发 · 温柔', 'zh-CN-XiaoxiaoNeural'],
  ['知夏', '短发 · 奶油针织 · 亲切', 'zh-CN-XiaoyiNeural'],
  ['听澜', '长直发 · 蓝衫 · 知性', 'zh-CN-XiaoyiNeural'],
  ['晚晴', '栗色卷发 · 柔和', 'zh-CN-XiaoxiaoNeural'],
  ['清禾', '低马尾 · 清新', 'zh-CN-XiaoyiNeural'],
  ['南栀', '黑衫 · 从容', 'zh-CN-XiaoyiNeural'],
  ['初雪', '齐肩发 · 冬日', 'zh-CN-XiaoxiaoNeural'],
  ['栀语', '眼镜 · 微卷 · 书香', 'zh-TW-HsiaoChenNeural'],
  ['云舒', '长卷发 · 优雅', 'zh-CN-XiaoxiaoNeural'],
  ['星宁', '高马尾 · 运动', 'zh-CN-XiaoyiNeural'],
  ['亦安', '短发 · 利落', 'zh-CN-XiaoyiNeural'],
  ['若岚', '盘发 · 丝绸 · 沉静', 'zh-CN-XiaoxiaoNeural'],
  ['明月', '长直发 · 白衫', 'zh-TW-HsiaoYuNeural'],
  ['微澜', '卷发 · 蓝色针织', 'zh-CN-XiaoxiaoNeural'],
  ['诗雨', '眼镜 · 西装 · 知性', 'zh-CN-XiaoyiNeural'],
  ['浅秋', '低发髻 · 咖色', 'zh-CN-XiaoxiaoNeural'],
  ['林夕', '刘海 · 休闲', 'zh-CN-XiaoyiNeural'],
  ['青棠', '侧编发 · 青衫', 'zh-CN-XiaoyiNeural'],
  ['暖笙', '暖棕卷发 · 亲切', 'zh-TW-HsiaoChenNeural'],
  ['望舒', '银灰挑染 · 月夜', 'zh-CN-XiaoxiaoNeural'],
]
export const characters: Character[] = rows.map((row, i) => ({
  id: `p${String(i + 1).padStart(2, '0')}`, name: row[0]!, description: row[1]!, voice: row[2]!,
}))
const assets = import.meta.glob('../assets/portraits/*.webp', { eager: true, query: '?url', import: 'default' }) as Record<string, string>
export function portraitAsset(id: string, frame: number | 'thumb'): string {
  return assets[`../assets/portraits/${id}-${frame}.webp`] ?? ''
}
// The desktop build copies public/ and the mobile build sets publicDir:false,
// so these 4K copies never inflate the APK.
const hdCharacters = new Set(['p01'])
export function hasPortraitHd(id: string): boolean {
  return hdCharacters.has(id)
}
export function portraitHdAsset(id: string, frame: number): string {
  if (!hdCharacters.has(id) || typeof document === 'undefined') return ''
  return new URL(`portraits-hd/${id}-${frame}.webp`, document.baseURI).href
}
const patchAssets = import.meta.glob('../assets/portraits/patches/*.webp', {
  eager: true,
  query: '?url',
  import: 'default',
}) as Record<string, string>
export function portraitPatchAsset(id: string, kind: string): string {
  return patchAssets[`../assets/portraits/patches/${id}-${kind}.webp`] ?? ''
}
