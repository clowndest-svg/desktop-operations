import { createApp } from 'vue'
import { createPinia } from 'pinia'
import './styles/hud.css'
import App from './App.vue'
import PetStage from './avatar/PetStage.vue'
import { isPet } from './page'
import { applyStoredSkin, installSkinChannel } from './theme'

// Before the first frame: a flash of the wrong palette reads as a restart bug, and
// it is true of both windows -- the pet reads the same stored skin the HUD writes.
applyStoredSkin()
installSkinChannel()

/**
 * One bundle, two application roots.
 *
 * The pet window is ``?mode=pet`` over the same served files. Branching inside
 * ``App.vue`` would put an ``if`` around every panel in the dashboard; switching the
 * root here keeps the HUD exactly as it was tested.
 */
createApp(isPet ? PetStage : App).use(createPinia()).mount('#app')
