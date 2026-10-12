/**
 * The built-in pet models: real rigged glTF characters, shipped with the app.
 *
 * Why these exist
 * ---------------
 * The photo route (a photograph traced into a polygon and extruded) produced a slab,
 * and the verdict on it was that it does not look like a pet. These are the opposite
 * trade: somebody drew and rigged each one, with a dozen animation clips, so what
 * stands on the desktop has a body instead of an outline of one.
 *
 * Where the list comes from
 * -------------------------
 * Not from this file. ``jarvis/app/pet_gallery.py`` owns it because it is the code that
 * can see which ``.glb`` files actually shipped -- a picker that offered a model whose
 * file is missing would be a button guaranteed to fail. Everything here receives a
 * finished entry (label, category, url, measured clip count) and loads it.
 *
 * What is NOT here
 * ----------------
 * No blend shapes and no visemes: these clips move bones, so a gallery pet can walk,
 * idle and tilt its head, but it cannot mouth words. ``describe()`` says what is on
 * screen instead of letting a silent assumption stand.
 */

import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { neutralPose, type Character, type Framing, type Pose } from '@/avatar/character'

/** One entry of the shipped gallery, as ``pet_gallery.py`` hands it over. */
export interface GalleryModel {
  /** ``builtin:<Key>`` -- the value stored in ``pet.figure``. */
  id: string
  key: string
  name: string
  kind: string
  note: string
  bytes: number
  clips: number
  url: string
}

/** Which clip to loop: the calm one. ``Idle`` exists in every file here; if a future
 * pack renames it, falling back to the first clip keeps a figure moving instead of
 * freezing it in a T-pose, which is the worse failure. */
function pickClip(clips: { name: string }[] | undefined): string {
  const names = (clips ?? []).map((item) => item.name)
  return names.find((name) => /^idle$/i.test(name)) ?? names.find((name) => /idle/i.test(name)) ?? names[0] ?? ''
}

/**
 * Swap an imported PBR material for an unlit one.
 *
 * Nothing in this scene has a light -- the hologram look is drawn with flat inks -- so
 * a ``MeshStandardMaterial`` renders as a dark silhouette here. ``copy`` carries the
 * base colour, the texture and the vertex-colour flag across, which is all a low-poly
 * model needs to look like itself.
 */
function unlit(source: THREE.Material): THREE.MeshBasicMaterial {
  const flat = new THREE.MeshBasicMaterial()
  flat.copy(source)
  return flat
}

/**
 * Which clip answers a poke, and which one she picks when nobody touches her.
 *
 * The names are read out of the shipped files, not typed by hand: every one of these
 * twelve is a Quaternius rig, and the day somebody swaps a file the family match still
 * finds ``Idle_HitReact1`` / ``RecieveHit`` / ``HitRecieve`` without a list to update.
 *
 * What never plays by itself is the other half of the rule. ``Death`` on a timer is a
 * pet that looks broken; ``Attack``/``Run``/``Gallop`` need a place to run *to*, and she
 * is standing on a fixed spot on somebody's desktop.
 */
const REACT_FAMILY: readonly RegExp[] = [/hitreact/i, /recievehit/i, /receivehit/i, /^no$/i]
const GESTURE_FAMILY: readonly RegExp[] = [
  /^dance$/i,
  /^yes$/i,
  /^no$/i,
  /^jump$/i,
  /^eating$/i,
  /^pickup$/i,
  /spell/i,
  /^bite/i,
  /^roll$/i,
  /headlow/i,
  /^idle_2/i,
]
const NEVER_ALONE = /death|attack|gallop|^run$|kick|punch|headbutt/i
const FADE_SECONDS = 0.25

function pickFrom(names: string[], family: readonly RegExp[]): string {
  for (const pattern of family) {
    const hit = names.find((name) => pattern.test(name) && !NEVER_ALONE.test(name))
    if (hit) return hit
  }
  return ''
}

export async function loadGalleryCharacter(entry: GalleryModel): Promise<Character | null> {
  if (!entry?.key) return null
  const url = new URL(entry.url, document.baseURI).href
  const gltf = await new GLTFLoader().loadAsync(url)
  const model = gltf.scene

  // Same rule the silhouette uses: one unit tall, feet on zero, centred on x. Then the
  // camera maths below does not care whether it is looking at a hamster or a wizard.
  //
  // ``precise`` is not an optimisation, it is correctness: these are skinned meshes, and
  // the conservative box reads the *bind-pose geometry* (which for the animals includes
  // the tail sweeping three units through z and a head that the rig lifts), so without it
  // every four-legged model comes out framed as if it were twice as tall as it is.
  model.updateMatrixWorld(true)
  const box = new THREE.Box3().setFromObject(model, true)
  const height = Math.max(1e-4, box.max.y - box.min.y)
  const width = Math.max(1e-4, box.max.x - box.min.x)
  model.scale.setScalar(1 / height)
  model.position.set(-(box.min.x + box.max.x) / 2 / height, -box.min.y / height, 0)

  const disposables: Array<{ dispose(): void }> = []
  model.traverse((child) => {
    const mesh = child as THREE.Mesh
    if (!mesh.isMesh) return
    const sources = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
    const flats = sources.map(unlit)
    for (const material of flats) {
      disposables.push(material)
      const picture = material.map
      if (picture) disposables.push(picture)
    }
    disposables.push(mesh.geometry)
    sources.forEach((material) => material.dispose())
    mesh.material = Array.isArray(mesh.material) ? flats : flats[0]
  })

  const mixer = new THREE.AnimationMixer(model)
  const names = (gltf.animations ?? []).map((item) => item.name)
  const wanted = pickClip(gltf.animations)
  const clip = gltf.animations.find((item) => item.name === wanted)
  let action: THREE.AnimationAction | null = null
  let playing: THREE.AnimationAction | null = null
  let playingName = ''
  const idleName = clip ? String(clip.name) : ''
  if (!clip) console.warn('[avatar] 内置模型 ' + entry.key + ' 没有可播的动画，它会站着不动')
  if (clip) {
    action = mixer.clipAction(clip)
    action.play()
    // 一段演完自己回到待机：没有这一条，"她刚才跳了一下"会变成"她从此只会跳"。
    mixer.addEventListener('finished', (event) => {
      const ended = event.action as THREE.AnimationAction
      if (!action || ended === action) return
      ended.crossFadeTo(action, FADE_SECONDS, false)
      playingName = ''
      playing = null
    })
  }

  const body = new THREE.Group()
  body.add(model)
  const root = new THREE.Group()
  root.add(body)
  root.updateMatrixWorld(true)

  const framing: Framing = { target: 0.5, span: 1.16, beam: Math.max(1, width / height) * 1.28 }
  let pose: Pose = neutralPose()
  let settle = 0

  return {
    root,
    framing,
    clipName: () => playingName || idleName,
    play(kind): boolean {
      if (!action) return false
      const wanted = pickFrom(names, kind === 'react' ? REACT_FAMILY : GESTURE_FAMILY)
      if (!wanted) return false
      const nextClip = gltf.animations.find((item) => item.name === wanted)
      if (!nextClip) return false
      const next = mixer.clipAction(nextClip)
      if (playing === next) return true // 正在演这一段：别从头再来一遍，那看着像卡了
      next.reset()
      next.setLoop(THREE.LoopOnce, 1)
      next.clampWhenFinished = false
      next.enabled = true
      next.setEffectiveWeight(1)
      action.crossFadeTo(next, FADE_SECONDS, false)
      next.play()
      playing = next
      playingName = wanted
      return true
    },
    setPose(next): void {
      pose = next
    },
    reframe(): void {
      // The whole body is the figure; there is nothing above the feet to reveal.
    },
    advance(elapsed, delta, calm = false): void {
      settle = Math.min(1, settle + delta * 2)
      if (action) mixer.update(delta)
      // A rigged body already breathes -- that is what the Idle clip is -- so the only
      // extra motion is the turn toward the pointer, kept small so the feet do not
      // visibly slide across the spot they stand on.
      const drift = calm ? 0 : Math.sin(elapsed * 0.21) * 0.16
      root.rotation.set(pose.pitch * 0.35 * settle, (pose.yaw * 0.55 + drift) * settle, pose.roll * 0.3 * settle)
    },
    dispose(): void {
      mixer.stopAllAction()
      mixer.uncacheRoot(model)
      for (const item of disposables) item.dispose()
      body.remove(model)
    },
    describe(): string {
      return '内置模型（' + entry.kind + '）：' + entry.name + ' · ' + entry.clips + ' 段动作'
    },
  }
}
