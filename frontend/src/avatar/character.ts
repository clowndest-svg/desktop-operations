/**
 * The assistant's figure: a holographic bust built from geometry, not from a file.
 *
 * Why hologram and not "anime girl".
 * ----------------------------------
 * The brief was a 3D character resembling a specific anime figure. Two separate
 * problems stand in the way, and only one of them is the copyright.
 *
 * The other is that a stylized human face is the hardest thing in graphics to fake,
 * and this file has no artist, no rig and no texture set -- only geometry. A first
 * attempt at exactly that is on record from this session: a skin-toned sphere with
 * domed eyes and lathe-spike bangs, which rendered as a dark egg with two white
 * sockets and a red gash. Nothing about the driver was wrong; the *medium* was. A
 * face that nearly works is worse than one that never pretended to.
 *
 * So this is a hologram: cyan wireframe over a faint additive fill, glowing eyes, a
 * silhouette that is unmistakably a person with chin-length hair. It is deliberately
 * not trying to be skin, which means it cannot fail at being skin -- and it belongs
 * in a window whose entire language is instrument cyan.
 *
 * What is still true:
 *
 * * **The mouth is five morph targets, named like a VRM's** (``aa ih ou ee oh``).
 *   That is the seam a real model drops into: whoever supplies a rigged figure later
 *   replaces this file and keeps the driver, because the driver only ever asks for
 *   five numbers and a pose.
 * * **Every measurement still comes from the audio.** Nothing here animates from a
 *   timer except breathing and blinking, which a person does whether or not they
 *   are talking.
 */

import * as THREE from 'three'
import { currentSkin } from '@/theme'

/** The five mouth shapes the driver can ask for, in morph-target order. */
export const VISEMES = ['aa', 'ih', 'ou', 'ee', 'oh'] as const
export type Viseme = (typeof VISEMES)[number]

export type Mood = 'dormant' | 'armed' | 'listening' | 'thinking' | 'speaking'

export interface Pose {
  /** Morph influence per viseme, 0..1. */
  visemes: Record<Viseme, number>
  pitch: number
  yaw: number
  roll: number
  /** 0 = eyes open, 1 = shut. */
  blink: number
  /** -1 furrowed, 0 neutral, 1 raised. */
  brows: number
  /** Vertical gaze offset, radians. */
  gaze: number
  /** Jaw drop in world units. */
  jaw: number
}

/*
 * The hologram's inks come from the active skin, read at build time. Reading them
 * per material call would be free too, but the figure is rebuilt when the skin
 * changes anyway -- a half-retinted bust mid-switch is worse than a one-frame
 * rebuild.
 */
function line(): THREE.LineBasicMaterial {
  return new THREE.LineBasicMaterial({ color: currentSkin().line, transparent: true, opacity: 0.5 })
}

/** How a figure wants the camera to compose it. */
export interface Framing {
  /** World-space height the camera aims at (the vertical centre of the subject). */
  target: number
  /** Vertical extent the panel has to show, headroom included. */
  span: number
  /** The same, across. A wide panel crops arms; a tall one crops nothing. */
  beam: number
}

export interface Character {
  root: THREE.Group
  /**
   * Where the figure is in the scene.
   *
   * The built bust and a dropped-in full-height model are composed by the same
   * camera only if that camera is told what each one needs: a head-and-shoulders
   * hologram and a 1.6 m person standing on the floor want opposite distances.
   */
  framing: Framing
  setPose(pose: Pose): void
  advance(elapsed: number, delta: number, calm?: boolean): void
  /**
   * Re-measure the framing for a figure shown down to ``fraction`` of its height
   * (1 = feet included). The HUD asks for a bust because its panel is 250 px tall;
   * the desktop pet has a whole window to itself and asks for the whole person.
   * A figure that stops at the shoulders says so by doing nothing.
   */
  reframe(fraction: number): void
  dispose(): void
  /** What is on screen: the built figure or a dropped-in model. Shown in the HUD. */
  describe(): string
}

function fill(): THREE.MeshBasicMaterial {
  return new THREE.MeshBasicMaterial({
    color: currentSkin().fill,
    transparent: true,
    opacity: 0.15,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    side: THREE.DoubleSide,
  })
}

/**
 * A sphere drawn as latitude and longitude lines, not as triangles.
 *
 * ``WireframeGeometry`` gives every edge of every triangle, diagonals included, and
 * on a face-sized object that is a static-like fuzz rather than a structure -- the
 * one thing that made the first hologram look like a rendering artefact instead of a
 * projection. A real projection shows the lines that describe the form.
 */
function gridSphere(
  radius: number,
  meridians = 14,
  parallels = 9,
  phiStart = 0,
  phiLength = Math.PI * 2,
  thetaStart = 0,
  thetaLength = Math.PI,
): THREE.LineSegments {
  const points: number[] = []
  const at = (phi: number, theta: number): void => {
    const sin = Math.sin(theta)
    points.push(
      radius * sin * Math.cos(phi),
      radius * Math.cos(theta),
      radius * sin * Math.sin(phi),
    )
  }
  const arc = (a: [number, number, number], b: [number, number, number]): void => {
    points.push(...a, ...b)
  }
  // Parallels: horizontal rings, sampled densely enough to look round.
  for (let row = 1; row < parallels; row += 1) {
    const theta = thetaStart + (row / parallels) * thetaLength
    let previous: [number, number, number] | null = null
    const steps = 48
    for (let step = 0; step <= steps; step += 1) {
      const phi = phiStart + (step / steps) * phiLength
      const sin = Math.sin(theta)
      const current: [number, number, number] = [
        radius * sin * Math.cos(phi),
        radius * Math.cos(theta),
        radius * sin * Math.sin(phi),
      ]
      if (previous) arc(previous, current)
      previous = current
    }
  }
  // Meridians: vertical arcs from the top of the cap to the bottom of the cut.
  const steps = 40
  for (let column = 0; column < meridians; column += 1) {
    const phi = phiStart + (column / meridians) * phiLength
    let previous: [number, number, number] | null = null
    for (let step = 0; step <= steps; step += 1) {
      const theta = thetaStart + (step / steps) * thetaLength
      at(phi, theta)
      const x = points[points.length - 3]
      const y = points[points.length - 2]
      const z = points[points.length - 1]
      if (previous) arc(previous, [x, y, z])
      previous = [x, y, z]
    }
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3))
  return new THREE.LineSegments(geometry, line())
}

/** The same idea for the neck and collar, which are cylinders, not spheres. */
function gridCylinder(
  top: number,
  bottom: number,
  height: number,
  rings = 4,
  sides = 16,
): THREE.LineSegments {
  const points: number[] = []
  const pair = (a: [number, number, number], b: [number, number, number]): void => {
    points.push(...a, ...b)
  }
  const ring = (t: number, segments = 64): number => {
    const radius = top + (bottom - top) * t
    const y = height / 2 - t * height
    let previous: [number, number, number] | null = null
    for (let step = 0; step <= segments; step += 1) {
      const angle = (step / segments) * Math.PI * 2
      const current: [number, number, number] = [radius * Math.cos(angle), y, radius * Math.sin(angle)]
      if (previous) pair(previous, current)
      previous = current
    }
    return y
  }
  for (let row = 0; row <= rings; row += 1) ring(row / rings)
  for (let side = 0; side < sides; side += 1) {
    const angle = (side / sides) * Math.PI * 2
    pair(
      [top * Math.cos(angle), height / 2, top * Math.sin(angle)],
      [bottom * Math.cos(angle), -height / 2, bottom * Math.sin(angle)],
    )
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3))
  return new THREE.LineSegments(geometry, line())
}

/**
 * A mesh drawn the way a hologram is: a see-through body with the structure on top.
 */
function shell(
  body: THREE.BufferGeometry,
  wires: THREE.LineSegments[],
): THREE.Group {
  const group = new THREE.Group()
  group.add(new THREE.Mesh(body, fill()))
  for (const entry of wires) group.add(entry)
  return group
}

/**
 * The head: wide at the cheekbones, narrowing to a chin. A sphere reads as a ball,
 * and a hologram of a ball reads as a helmet.
 */
function headGeometry(): THREE.SphereGeometry {
  const geometry = new THREE.SphereGeometry(1, 16, 12)
  const positions = geometry.attributes.position as THREE.BufferAttribute
  for (let index = 0; index < positions.count; index += 1) {
    const y = positions.getY(index)
    const taper = y >= 0 ? 1 : 0.6 + 0.4 * Math.pow(y + 1, 0.5)
    positions.setX(index, positions.getX(index) * taper)
    positions.setZ(index, positions.getZ(index) * (y >= 0 ? 1 : 0.84 + 0.16 * (y + 1)))
  }
  geometry.computeVertexNormals()
  return geometry
}

interface MouthShape {
  halfWidth: number
  halfHeight: number
  corner: number
  pucker: number
}

const MOUTH_SHAPES: Record<Viseme, MouthShape> = {
  aa: { halfWidth: 0.115, halfHeight: 0.12, corner: -0.012, pucker: 0.0 },
  ih: { halfWidth: 0.16, halfHeight: 0.045, corner: 0.022, pucker: 0.0 },
  ou: { halfWidth: 0.058, halfHeight: 0.088, corner: -0.008, pucker: 0.06 },
  ee: { halfWidth: 0.165, halfHeight: 0.062, corner: 0.032, pucker: 0.0 },
  oh: { halfWidth: 0.092, halfHeight: 0.115, corner: -0.006, pucker: 0.032 },
}

const CLOSED: MouthShape = { halfWidth: 0.14, halfHeight: 0.014, corner: 0.016, pucker: 0.0 }

function deformMouth(positions: THREE.BufferAttribute, shape: MouthShape): Float32Array {
  const out = new Float32Array(positions.count * 3)
  for (let index = 0; index < positions.count; index += 1) {
    const u = positions.getX(index) / 2
    const v = positions.getY(index) / 2
    // Height falls off toward the corners: mouths open from the middle, and an
    // ellipse that is equally tall everywhere reads as a sticker on the face.
    const span = Math.sqrt(Math.max(0, 1 - u * u))
    const x = u * shape.halfWidth * 2
    const y = Math.sign(v) * shape.halfHeight * span + shape.corner * (1 - span)
    out[index * 3] = x
    out[index * 3 + 1] = y
    out[index * 3 + 2] = -(x * x + y * y) * 1.4 + shape.pucker * span
  }
  return out
}

function mouthGeometry(shape: MouthShape): THREE.BufferGeometry {
  const source = new THREE.PlaneGeometry(2, 2, 18, 8)
  const positions = source.attributes.position as THREE.BufferAttribute
  const result = new THREE.BufferGeometry()
  result.setAttribute('position', new THREE.BufferAttribute(deformMouth(positions, shape), 3))
  result.setIndex(source.getIndex() as THREE.BufferAttribute)
  source.dispose()
  return result
}

/**
 * The mouth, as a filled shape.
 *
 * It was a `LineSegments` first, which is wrong twice over: the geometry's index
 * buffer describes triangles, and `LineSegments` reads index *pairs*, so every third
 * vertex got joined to its neighbour's neighbour -- a closed outline rendered as a
 * solid-looking pill. And morph targets are a mesh-shader feature; a line material
 * has no morph chunk to apply them with, so the five visemes would have sat there
 * doing nothing. Filled and additive is both correct and what an opening in a
 * hologram looks like.
 */
function buildMouth(): THREE.Mesh {
  const base = mouthGeometry(CLOSED)
  base.morphAttributes.position = VISEMES.map(
    (name) => mouthGeometry(MOUTH_SHAPES[name]).attributes.position as THREE.BufferAttribute,
  )
  const mouth = new THREE.Mesh(
    base,
    new THREE.MeshBasicMaterial({
      color: currentSkin().glow,
      transparent: true,
      opacity: 0.8,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side: THREE.DoubleSide,
    }),
  )
  mouth.morphTargetInfluences = VISEMES.map(() => 0)
  return mouth
}

/**
 * A lock of hair: four strands fanning out of one root, tapering to a point.
 *
 * A lathed surface with its wireframe on top gives the same triangular fuzz the
 * head had, on a shape small enough that the fuzz *is* the shape. Strands are what
 * hair looks like when it is drawn rather than modelled, and they are cheaper.
 */
function hairLock(length: number, width: number, strands = 4): THREE.Group {
  const points: number[] = []
  const steps = 9
  for (let strand = 0; strand < strands; strand += 1) {
    const angle = (strand / (strands - 1) - 0.5) * Math.PI * 1.2
    let previous: [number, number, number] | null = null
    for (let step = 0; step <= steps; step += 1) {
      const t = step / steps
      const radius = (width * (1 - t * t * 0.92) + 0.006) * (0.35 + t * 0.65)
      const current: [number, number, number] = [
        Math.sin(angle) * radius * 2.2,
        -t * length,
        Math.cos(angle) * radius - t * t * width * 0.5,
      ]
      if (previous) points.push(previous[0], previous[1], previous[2], ...current)
      previous = current
    }
  }
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3))
  const group = new THREE.Group()
  group.add(new THREE.LineSegments(geometry, line()))
  return group
}

function glow(opacity: number): THREE.MeshBasicMaterial {
  return new THREE.MeshBasicMaterial({
    color: currentSkin().glow,
    transparent: true,
    opacity,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })
}

export function createCharacter(): Character {
  const root = new THREE.Group()
  const skull = new THREE.Group()
  root.add(skull)

  const head = shell(headGeometry(), [gridSphere(1, 12, 8)])
  head.scale.set(0.92, 0.86, 0.88)
  skull.add(head)

  // Hair: a cap over the crown, a mass down the back, two side locks and a fringe.
  // The silhouette is the only place this figure says *who* -- the face carries no
  // identity, which is the whole reason it cannot look wrong.
  const cap = shell(
    new THREE.SphereGeometry(1.06, 16, 8, 0, Math.PI * 2, 0, Math.PI * 0.6),
    [gridSphere(1.06, 10, 3, 0, Math.PI * 2, 0, Math.PI * 0.6)],
  )
  cap.scale.set(0.99, 1.1, 1.0)
  cap.position.set(0, 0.06, -0.03)
  skull.add(cap)

  // The mass stops where the face starts. Hair drawn *over* the features is the
  // difference between a bob and a mask, and the front gap is the whole trick.
  const FACE_FROM = 0.55
  const FACE_TO = Math.PI - FACE_FROM + 1.1
  const back = shell(
    new THREE.SphereGeometry(1.08, 16, 9, FACE_TO, Math.PI * 2 - (FACE_TO - FACE_FROM), 0, Math.PI * 0.86),
    [gridSphere(1.08, 7, 4, FACE_TO, Math.PI * 2 - (FACE_TO - FACE_FROM), 0, Math.PI * 0.86)],
  )
  back.scale.set(1.02, 1.2, 0.98)
  back.position.set(0, -0.26, -0.12)
  skull.add(back)

  for (const side of [-1, 1]) {
    const lock = hairLock(0.92, 0.17)
    lock.position.set(side * 0.7, 0.44, 0.26)
    lock.rotation.set(-0.1, 0, side * 0.12)
    skull.add(lock)
  }
  for (let index = 0; index < 5; index += 1) {
    const t = index / 4
    const fringe = hairLock(0.36, 0.14)
    fringe.position.set(-0.46 + t * 0.92, 0.56, 0.54 - Math.abs(0.5 - t) * 0.34)
    fringe.rotation.set(-1.45, 0, (0.5 - t) * 0.45)
    skull.add(fringe)
  }

  // Eyes: two soft glowing lenses. A hologram's eyes are light, not anatomy, and
  // that is the single decision that keeps this figure from going uncanny.
  const eyes: THREE.Mesh[] = []
  for (const side of [-1, 1]) {
    const eye = new THREE.Mesh(new THREE.CircleGeometry(0.092, 20), glow(0.82))
    eye.scale.set(1, 1.25, 1)
    eye.position.set(side * 0.2, -0.02, 0.85)
    eye.rotation.y = side * -0.2
    skull.add(eye)
    eyes.push(eye)
  }

  const brows: THREE.Mesh[] = []
  for (const side of [-1, 1]) {
    const brow = new THREE.Mesh(new THREE.PlaneGeometry(0.22, 0.022), glow(0.35))
    brow.position.set(side * 0.2, 0.17, 0.85)
    brow.rotation.z = side * -0.14
    skull.add(brow)
    brows.push(brow)
  }

  const mouth = buildMouth()
  mouth.position.set(0, -0.42, 0.82)
  skull.add(mouth)

  const body = new THREE.Group()
  root.add(body)
  const neck = shell(new THREE.CylinderGeometry(0.24, 0.3, 0.4, 12, 1, true), [gridCylinder(0.24, 0.3, 0.4, 3, 10)])
  neck.position.set(0, -1.06, 0.02)
  body.add(neck)

  const shoulders = shell(new THREE.SphereGeometry(1, 16, 8), [gridSphere(1, 12, 4)])
  shoulders.scale.set(1.02, 0.5, 0.56)
  shoulders.position.set(0, -1.56, 0)
  body.add(shoulders)

  const collar = shell(
    new THREE.CylinderGeometry(0.35, 0.44, 0.28, 14, 1, true),
    [gridCylinder(0.35, 0.44, 0.28, 2, 12)],
  )
  collar.position.set(0, -1.22, 0.02)
  body.add(collar)

  // The plinth: what tells you this is a projection and not a floating head.
  const base = new THREE.Mesh(
    new THREE.RingGeometry(0.45, 1.05, 40),
    new THREE.MeshBasicMaterial({
      color: currentSkin().line,
      transparent: true,
      opacity: 0.14,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      side: THREE.DoubleSide,
    }),
  )
  base.rotation.x = -Math.PI / 2
  base.position.set(0, -2.02, 0)
  body.add(base)

  let settle = 0
  /**
   * The pose the driver handed over last frame. It is the same object every frame,
   * so this is a reference and not a copy -- which is also why ``advance`` cannot
   * disagree with ``setPose`` about which frame it is drawing.
   */
  let pose = neutralPose()

  return {
    root,
    // Head at the origin, plinth two units below it: the whole figure is 3.2 tall and
    // a little over two wide, and the framing says so rather than leaving the camera
    // at a distance somebody picked when this was the only model.
    framing: { target: -0.45, span: 3.5, beam: 2.3 },

    reframe(): void {
      // Nothing to reveal. This figure is a head on a plinth, and the framing above
      // already shows all of it -- the pet asks for feet this model does not have.
    },

    setPose(next): void {
      pose = next
      const influences = mouth.morphTargetInfluences
      if (influences) {
        VISEMES.forEach((name, index) => {
          influences[index] = pose.visemes[name]
        })
      }
      mouth.position.y = -0.42 - pose.jaw * 0.14
      // The lid is the eye's own vertical scale: what a glowing lens does when it
      // closes, and one fewer surface to get wrong.
      const open = Math.max(0.06, 1 - pose.blink)
      for (const eye of eyes) {
        eye.scale.set(1, 1.25 * open, 1)
        eye.position.y = -0.02 + pose.gaze * 0.04
      }
      for (const [index, brow] of brows.entries()) {
        const side = index === 0 ? -1 : 1
        brow.position.y = 0.17 + pose.brows * 0.03
        brow.rotation.z = side * -0.14 + side * pose.brows * 0.24
      }
    },

    advance(elapsed, delta, calm = false): void {
      // Everything eases in over the first half-second, so a panel that just mounted
      // does not open on a head that is already turned.
      settle = Math.min(1, settle + delta * 2)
      const breath = calm ? 0 : Math.sin(elapsed * 0.9) * 0.006
      const drift = calm ? 0 : Math.sin(elapsed * 0.37) * 0.03 + Math.sin(elapsed * 0.11) * 0.05
      skull.rotation.x = pose.pitch * settle
      skull.rotation.y = (pose.yaw + drift) * settle
      skull.rotation.z = pose.roll * settle
      skull.position.y = -breath * 2
      shoulders.scale.y = 0.6 + breath * 1.6
      body.rotation.y = drift * 0.3
      if (!calm) base.rotation.z = elapsed * 0.12
    },

    describe(): string {
      return '全息投影（内置）'
    },

    dispose(): void {
      root.traverse((object) => {
        const mesh = object as THREE.Mesh
        if (!mesh.geometry) return
        mesh.geometry.dispose()
        const material = mesh.material as THREE.Material | THREE.Material[]
        if (Array.isArray(material)) material.forEach((entry) => entry.dispose())
        else material?.dispose()
      })
    },
  }
}

export function neutralPose(): Pose {
  return {
    visemes: { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 },
    pitch: 0,
    yaw: 0,
    roll: 0,
    blink: 0,
    brows: 0,
    gaze: 0,
    jaw: 0,
  }
}
