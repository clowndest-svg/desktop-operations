import type { CapacitorConfig } from '@capacitor/cli'

/**
 * 手机版的壳配置。
 *
 * webDir 是 `dist-mobile` 而不是桌面的 `../jarvis/ui/web`：那是同一个 src 的第二次构建，
 * 产物里**没有 pywebview 桥**（`window.pywebview` 在手机上永远不存在），桌面 HUD 的每一个
 * 面板都靠它。混用一份产物会让手机要么满屏"桥没准备好"，要么在桌面端多带一套手机代码——
 * 上一轮就是因为"一份产物两个用途"踩过构建竞态，这里不再复现一次。
 *
 * androidScheme 用 https：手机要连的电脑端点是 `https://192.168.x.x:port`，
 * 混合内容（https 页面里发 http 请求）会被 WebView 直接掐掉，所以页面本身也必须在 https 上。
 */
const config: CapacitorConfig = {
  appId: 'com.xiaoye.assistant',
  appName: '小夜',
  webDir: 'dist-mobile',
  backgroundColor: '#04070d',
  android: {
    allowMixedContent: false,
    webContentsDebuggingEnabled: false,
  },
  server: {
    androidScheme: 'https',
  },
  plugins: {
    // 出场不给白屏：深色底配 HUD 的调色，装完第一次点开就是那圈蓝光。
    SplashScreen: {
      launchShowDuration: 700,
      backgroundColor: '#04070d',
      showSpinner: false,
    },
    StatusBar: {
      style: 'DARK',
    },
  },
}

export default config
