import { createApp } from 'vue'
import MobileApp from './mobile/MobileApp.vue'
import { XySpeech } from './mobile/speech'
import './styles/hud.css'
import './mobile/mobile.css'
import { applyStoredSkin, installSkinChannel } from './theme'
import { applyAppearance } from './mobile/appearance'

/**
 * 手机版的入口。
 *
 * 装一次语音/网络的插件句柄就完事：这里**不**注册 pywebview 相关的任何东西，
 * 手机上也确实没有 —— `api.ts` 里那两条降级实现（浏览器 fetch、系统语音不可用）
 * 就是为了让同一份代码在桌面浏览器里调试时把失败说清楚，而不是装成能跑。
 *
 * 外观和皮肤都要在第一帧之前落定。外观其实已经在 index.html 的同步脚本里设过一次了
 * （防止启动闪色），这里再调一次是为了把 `--xy-` 那套变量对齐到同一份真相上：
 * 只靠 index.html 的话，用户在设置里切换之后刷新，两边就可能各说各话。
 */
applyStoredSkin()
installSkinChannel()
applyAppearance()

const app = createApp(MobileApp)
app.mount('#app')

// 冷启动时把朗读引擎预热一次：Android 的 TextToSpeech 首句常有几百毫秒的初始化延迟，
// 用户第一次点"说话"就听不到尾巴比听不到开头更难受。
void XySpeech.capability().catch(() => {
  // 预热失败不是错误：Web 预览里本来就没有这个插件。
})
