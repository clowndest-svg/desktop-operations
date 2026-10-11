/**
 * The pets made from a photograph, in the three styles the picker offers.
 *
 * What none of them are
 * ---------------------
 * There is no rig here. The shell hands over a polygon (the photo's outline, or one
 * of four authored shapes) and three.js gives it thickness, so every motion below is
 * a transform of one object: these figures can breathe, turn and lean, and they
 * cannot nod, sit down or lift a paw. The picker says the same thing before anybody
 * chooses a candidate -- a pet that turns out to be a sticker gets blamed on the
 * feature, not on the four lines of geometry it actually is.
 *
 * What the three styles differ by is what the photo is allowed to become:
 *
 * * ``outline`` -- a polygon and nothing else. No image of anybody on disk.
 * * ``photo``   -- the same slab, with a thumbnail of the photo clipped to the
 *   silhouette on its faces. This is the one that reads as *your* cat, and the one
 *   that stores an image derived from your photograph.
 * * ``cartoon`` -- an authored shape tinted with one colour sampled from the photo,
 *   plus eyes that blink. Cute, and no image kept.
 *
 * The inks are the hologram's (``lineInk`` / ``bodyInk`` from the VRM module), so a
 * generated pet belongs in the window instead of being pasted onto it, and a skin
 * change rebuilds them all.
 */

import * as THREE from 'three'
import { bodyInk, lineInk } from '@/avatar/vrm'
import { neutralPose, type Character, type Framing, type Pose } from '@/avatar/character'
import { petModel, type SilhouetteModel } from '@/api/bridge'
import { loadGalleryCharacter } from '@/avatar/gallery'
import { currentSkin } from '@/theme'

export type { SilhouetteModel }

/** How far the resting sway turns her. Not a full revolution: the slab is thin, and
 * the moment it goes edge-on the figure reads as a bug rather than as a pet. The two
 * painted styles sway less than the wireframe one because they have a face to keep
 * pointed at the window. */
const SWAY_WIRE = 0.62
const SWAY_FLAT = 0.16

/** The driver's head pitch, applied to the whole body -- there is no neck here. */
const LEAN = 0.85

/** Where the cartoon's eyes sit: the height, then two fractions of however wide the
 * outline actually is there. A fixed spread cannot work -- the sitting cat's head is
 * 0.35 wide and offset left of centre, so eyes placed at ±0.15 land one on the ear
 * and one outside the shape. */
const EYE_HEIGHT = 0.79
const EYE_SPREAD = 0.33
const EYE_RADIUS = 0.25

function hex(value: string, fallback: number): number {
  const text = String(value || '').replace('#', '')
  if (text.length !== 6) return fallback
  const parsed = Number.parseInt(text, 16)
  return Number.isNaN(parsed) ? fallback : parsed
}

/**
 * Re-map the extrusion's UVs onto the thumbnail.
 *
 * ``ExtrudeGeometry`` writes UVs equal to the shape's own x/y, and the polygon is
 * normalised to "1.0 tall, x centred" -- so the raw values land in
 * [-aspect/2, +aspect/2] x [0, 1] and the texture would be half off-screen. This
 * turns that box into 0..1. Only the lids are sampled -- the walls get their own
 * solid material (see :func:`shade`).
 */
function fitUv(geometry: THREE.ExtrudeGeometry, aspect: number): void {
  const uv = geometry.attributes.uv
  if (!uv) return
  for (let index = 0; index < uv.count; index += 1) {
    uv.setXY(index, uv.getX(index) / Math.max(0.05, aspect) + 0.5, uv.getY(index))
  }
  uv.needsUpdate = true
}

/**
 * How wide the outline is at one height: the leftmost and rightmost place a horizontal
 * line at ``y`` crosses the ring.
 *
 * ``null`` means the figure is not there at that height (a template whose head tops
 * out lower), which is the caller's cue to skip the face rather than invent one.
 */
function ringSpan(points: Array<[number, number]>, y: number): { centre: number; half: number } | null {
  let left = Number.POSITIVE_INFINITY
  let right = Number.NEGATIVE_INFINITY
  for (let index = 0; index < points.length; index += 1) {
    const [x1, y1] = points[index]
    const [x2, y2] = points[(index + 1) % points.length]
    if ((y1 <= y && y2 >= y) || (y2 <= y && y1 >= y)) {
      const t = y2 === y1 ? 0 : (y - y1) / (y2 - y1)
      const x = x1 + (x2 - x1) * t
      left = Math.min(left, x)
      right = Math.max(right, x)
    }
  }
  if (!(right > left)) return null
  return { centre: (left + right) / 2, half: (right - left) / 2 }
}

/**
 * Darken a colour for the side of the slab.
 *
 * ``ExtrudeGeometry`` puts the two lids in group 0 and the walls in group 1, so a
 * material array can give the thickness its own shade. That is how these figures show
 * they are 3D: an ink outline cannot, because a rotated slab's outline crosses its own
 * body wherever the shape is concave, and a "crack" is not a depth cue.
 */
function shade(colour: number, factor: number): number {
  const channel = (shift: number): number =>
    Math.min(255, Math.round(((colour >> shift) & 255) * factor)) << shift
  return (channel(16) | channel(8) | channel(0)) >>> 0
}

function eyes(palette: ReturnType<typeof currentSkin>, span: { centre: number; half: number }): THREE.Group {
  const group = new THREE.Group()
  const radius = span.half * EYE_RADIUS
  const spread = span.half * EYE_SPREAD
  const white = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.95 })
  const pupil = new THREE.MeshBasicMaterial({ color: 0x08131f, toneMapped: false })
  for (const side of [-1, 1]) {
    const ball = new THREE.Mesh(new THREE.CircleGeometry(radius, 18), white)
    ball.position.set(span.centre + side * spread, 0, 0.012)
    const dot = new THREE.Mesh(new THREE.CircleGeometry(radius * 0.52, 14), pupil)
    dot.position.set(span.centre + side * spread, 0, 0.02)
    group.add(ball, dot)
  }
  group.renderOrder = 3
  void palette
  return group
}

export function buildSilhouette(model: SilhouetteModel, picture?: HTMLImageElement): Character | null {
  const points = model.points ?? []
  if (points.length < 3) return null
  const palette = currentSkin()
  const style = model.style || 'outline'
  const shape = new THREE.Shape(points.map(([x, y]) => new THREE.Vector2(x, y)))
  const depth = Math.max(0.02, Math.min(0.6, model.depth || 0.18))
  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth,
    bevelEnabled: false,
    steps: 1,
    curveSegments: 1,
  })
  // The slab has to straddle z=0, or "turning her" swings a door on its hinge.
  geometry.translate(0, 0, -depth / 2)

  // Anything that owns GPU memory goes in here; three.js does not collect it for us.
  const disposables: Array<{ dispose(): void }> = [geometry]
  const body = new THREE.Group()
  const face = new THREE.Mesh<THREE.ExtrudeGeometry, THREE.Material | THREE.Material[]>(
    geometry,
    bodyInk(palette),
  )
  face.renderOrder = 1
  body.add(face)

  let lid: THREE.Mesh | null = null
  let blink: THREE.Group | null = null

  if (style === 'photo' && picture) {
    fitUv(geometry, Math.max(0.05, model.aspect || 1))
    const texture = new THREE.Texture(picture)
    texture.colorSpace = THREE.SRGBColorSpace
    texture.needsUpdate = true
    const painted = new THREE.MeshBasicMaterial({ map: texture, transparent: true, side: THREE.DoubleSide })
    // Caps carry the picture, the walls carry a darkened average of it: a photo smeared
    // around the edge of the slab is exactly what makes a cut-out look like a cut-out.
    const flank = new THREE.MeshBasicMaterial({
      color: shade(hex(model.colour, 0x6f7f8f), 0.42),
      transparent: true,
      opacity: 0.96,
      side: THREE.DoubleSide,
    })
    disposables.push(texture, painted, flank)
    face.material = [painted, flank]
  } else if (style === 'cartoon') {
    const furColour = hex(model.colour, 0xf2c14e)
    const fur = new THREE.MeshBasicMaterial({
      color: furColour,
      transparent: true,
      opacity: 0.94,
      side: THREE.DoubleSide,
    })
    const flank = new THREE.MeshBasicMaterial({
      color: shade(furColour, 0.52),
      transparent: true,
      opacity: 0.96,
      side: THREE.DoubleSide,
    })
    disposables.push(fur, flank)
    face.material = [fur, flank]
    const span = ringSpan(points, EYE_HEIGHT)
    if (span) {
      blink = eyes(palette, span)
      blink.position.set(0, EYE_HEIGHT, depth / 2)
      disposables.push(...blink.children.map((child) => (child as THREE.Mesh).material as THREE.Material))
      body.add(blink)
    } else {
      console.warn('[avatar] 这只卡通形状在眼睛那个高度上没有身体，省掉眼睛')
    }
  } else {
    lid = new THREE.Mesh(geometry, lineInk(palette))
    lid.renderOrder = 2
    body.add(lid)
  }

  const root = new THREE.Group()
  root.add(body)
  root.updateMatrixWorld(true)
  const bounds = new THREE.Box3().setFromObject(body)
  const height = Math.max(1e-3, bounds.max.y - bounds.min.y)
  const width = Math.max(1e-3, bounds.max.x - bounds.min.x)
  const framing: Framing = {
    target: (bounds.max.y + bounds.min.y) / 2,
    span: height * 1.16,
    beam: width * 1.28,
  }

  let pose: Pose = neutralPose()
  let settle = 0
  const swayAmplitude = style === 'outline' ? SWAY_WIRE : SWAY_FLAT

  return {
    root,
    framing,
    setPose(next): void {
      pose = next
    },
    reframe(): void {
      // Nothing above the feet to reveal: the polygon is already the whole figure.
    },
    advance(elapsed, delta, calm = false): void {
      settle = Math.min(1, settle + delta * 2)
      const breath = calm ? 0 : Math.sin(elapsed * 1.1) * 0.012
      const drift = calm ? 0 : Math.sin(elapsed * 0.21) * swayAmplitude + Math.sin(elapsed * 0.63) * 0.05
      body.scale.y = breath + 1
      body.position.y = breath * 0.4
      if (lid) {
        lid.scale.y = breath + 1
        lid.position.y = breath * 0.4
      }
      // A slab this thick goes edge-on long before a body does, and a pet that turns
      // into a hairline reads as a bug. The wireframe one can sweep wide (the crossing
      // lines are the hologram); the two painted ones just rock, enough to show the
      // darker wall on one side.
      const turn = style === 'outline' ? 1 : 0.45
      root.rotation.set(
        pose.pitch * LEAN * settle * turn,
        (pose.yaw * 0.7 + drift) * settle * turn,
        pose.roll * 0.5 * settle * turn,
      )
      if (blink) {
        // The one thing a boneless figure can still do with a face: shut its eyes.
        // The lids are two circles scaled on y, which is what the built-in hologram
        // does for the same reason -- one surface fewer to get wrong.
        blink.scale.y = Math.max(0.06, 1 - pose.blink)
      }
    },
    dispose(): void {
      for (const item of disposables) item.dispose()
    },
    describe(): string {
      const kind = style === 'photo' ? '照片' : style === 'cartoon' ? '卡通' : '线框'
      return '剪影 3D（' + kind + '）：' + (model.name || model.id)
    },
  }
}

/**
 * Ask the shell which figure the operator chose, and build it.
 *
 * Two families answer: a shipped ``builtin:<Key>`` model (see ``pet_gallery.py``) and a
 * figure traced out of a photograph. ``null`` is the ordinary answer -- an install that
 * never picked either, and the state the picker returns to the moment the last figure
 * is deleted. The caller keeps the built-in figure in that case, so a bridge that is
 * not up yet cannot leave the desktop empty.
 *
 * The photo texture is decoded here rather than shipped as a file URL on purpose: the
 * generated figure lives in ``数据/pets``, outside the served bundle, and the last thing
 * this window needs is a route that exposes somebody's photographs over HTTP. Built-in
 * models are the opposite case -- they are program files, served with the bundle.
 */
export async function loadChosenCharacter(): Promise<Character | null> {
  let model: SilhouetteModel
  try {
    model = await petModel()
  } catch (error) {
    console.warn('[avatar] 问壳要形象时没要到，用内置形象：', error)
    return null
  }
  if (model.kind === 'builtin' && model.model) {
    try {
      return await loadGalleryCharacter(model.model)
    } catch (error) {
      console.warn('[avatar] 内置模型 ' + model.model.key + ' 没加载出来，退回默认形象：', error)
      return null
    }
  }
  if (!model?.points?.length) return null
  let picture: HTMLImageElement | undefined
  if (model.style === 'photo' && model.texture) {
    picture = (await decode(model.texture)) ?? undefined
    if (!picture) console.warn('[avatar] 照片纹理没解出来，这一只退回线框')
  }
  const built = buildSilhouette(model, picture)
  if (!built) console.warn('[avatar] 剪影 ' + model.id + ' 的点不够三条，用内置形象')
  return built
}

function decode(base64: string): Promise<HTMLImageElement | null> {
  return new Promise((resolve) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => resolve(null)
    image.src = 'data:image/png;base64,' + base64
  })
}
