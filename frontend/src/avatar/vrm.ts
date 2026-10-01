/**
 * A real VRM character, loaded from a file, drawn as light rather than as skin.
 *
 * Two things made this worth wiring up rather than keeping the hologram built in
 * ``character.ts``:
 *
 * * **The viseme names already match.** ``driver.ts`` emits ``aa ih ou ee oh``,
 *   which is the VRM expression preset set -- the seam left in ``character.ts``
 *   when the hologram was the only option. Nothing about how the face is driven
 *   changes; only what is being driven.
 * * **A person can supply their own face.** The model is a file on disk, so the
 *   character is a choice the operator makes, not one this repository makes for
 *   them -- which also settles the copyright question by moving it to the only
 *   party who can answer it.
 *
 * What is drawn on top of that rig is decided here, not in the file: the model's
 * own painted materials are replaced by glowing lines over a faint additive body,
 * which is the look the rest of the window speaks. A fully-shaded anime figure pasted
 * into an instrument panel is two interfaces arguing, and the operator was right that
 * it looked wrong.
 *
 * It is deliberately optional. No file, an unreadable file, or a browser without
 * the extensions VRM needs all fall back to the hologram, and the fallback is not
 * a downgrade you have to hunt for: ``describe()`` says which one is on screen.
 */

import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { VRM, VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm'
import type { Skin } from '@/theme'
import type { Character, Framing, Pose, Viseme } from '@/avatar/character'
import { VISEMES } from '@/avatar/character'
import { currentSkin } from '@/theme'

/** Where the bundle looks for a dropped-in model, in priority order. */
export const VRM_SOURCES = ['avatar/小夜.vrm', 'avatar/xiaoye.vrm', 'avatar/model.vrm']

/**
 * How much of a standing figure the panel shows, measured down from the crown.
 *
 * Half of it: enough that the figure reads as a person rather than a floating head,
 * little enough that the mouth still occupies a meaningful number of pixels in a
 * panel that is roughly 250 x 290.
 */
const BUST_FRACTION = 0.5

/** Radians to swing each arm down out of the bind T-pose. */
const ARM_SWING = 1.15

/** A little of the same at the elbow, so the arms are not two rigid bars. */
const ELBOW_SWING = 0.3

/**
 * Load the first VRM that resolves.
 *
 * Returns ``null`` rather than throwing when nothing is there: "no model supplied"
 * is the ordinary case for a fresh checkout, and a console full of 404s every
 * launch teaches nobody to read the console.
 */
export async function loadVrmCharacter(
  sources: readonly string[] = VRM_SOURCES,
): Promise<Character | null> {
  for (const source of sources) {
    const found = await probe(source)
    if (!found) continue
    try {
      return await build(found)
    } catch (error) {
      // A file that exists and still will not load is worth a line: without it the
      // operator sees the hologram and concludes the drop-in does not work.
      console.warn(`[avatar] ${source} 存在但加载失败：`, error)
    }
  }
  return null
}

async function probe(url: string): Promise<string | null> {
  try {
    const response = await fetch(url, { method: 'HEAD' })
    return response.ok ? url : null
  } catch {
    return null
  }
}

async function build(url: string): Promise<Character> {
  const loader = new GLTFLoader()
  loader.register((parser) => new VRMLoaderPlugin(parser))
  const gltf = await loader.loadAsync(url)
  const vrm = gltf.userData.vrm as VRM | undefined
  if (!vrm) throw new Error('文件不是有效的 VRM')
  VRMUtils.removeUnnecessaryVertices(gltf.scene)
  VRMUtils.removeUnnecessaryJoints(gltf.scene)
  // VRM 0.x models are authored facing -Z and render backwards; 1.x already faces
  // +Z and must be left alone. The two specs put `version` in different places, so
  // it is read defensively instead of typed into one branch.
  const meta = vrm.meta as { version?: string; meta?: { version?: string } }
  const version = String(meta?.version ?? meta?.meta?.version ?? '')
  if (!version.startsWith('1.')) VRMUtils.rotateVRM0(vrm)
  return new VrmCharacter(vrm, url)
}

/** The five mouth shapes, which are also the five VRM preset names. */
const VISEME_PRESETS: readonly Viseme[] = VISEMES

/**
 * The edges. ``wireframe`` draws every triangle boundary, which on a figure this
 * size reads as a projection grid rather than as paint — and, unlike
 * ``WireframeGeometry``, it stays a skinned mesh, so it moves with the mouth.
 *
 * It is drawn *over* an occluding body (see ``drawAsLines``) and additively on top
 * of it. Both matter: the first measured version of this had no depth order at all,
 * so the lines on the far side of the head were drawn as brightly as the near ones,
 * ~40 of them per pixel, and the figure came out as a white blob with arms.
 */
function lineInk(palette: Skin): THREE.MeshBasicMaterial {
  return new THREE.MeshBasicMaterial({
    color: palette.line,
    wireframe: true,
    transparent: true,
    opacity: 0.34,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })
}

/**
 * The body under the edges: a dark, semi-solid mass that hides whatever lines belong
 * on the far side. ``polygonOffset`` pushes it a hair behind its own wireframe so the
 * two surfaces that occupy exactly the same space do not flicker over each other.
 */
function bodyInk(palette: Skin): THREE.MeshBasicMaterial {
  return new THREE.MeshBasicMaterial({
    color: palette.fill,
    transparent: true,
    opacity: 0.74,
    depthWrite: true,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 1,
  })
}

const NEUTRAL: Pose = {
  visemes: { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 },
  pitch: 0,
  yaw: 0,
  roll: 0,
  blink: 0,
  brows: 0,
  gaze: 0,
  jaw: 0,
}

/**
 * Swing a limb toward a direction, in world space, whatever axis the rig happens to
 * have authored it on.
 *
 * This is the part that cannot be a hard-coded ``rotation.z = -1.2``. A bone's local
 * axes come from the modeller's import chain, not from the VRM specification, so the
 * same "lower the arm" written as a local rotation is correct on one model and turns
 * another's elbow into a knee. The only thing two rigs are *guaranteed* to agree on
 * is where the bones are: so measure the shoulder-to-elbow vector in world space,
 * rotate about the axis perpendicular to it and the target, and convert that world
 * rotation back into this bone's local frame.
 */
function swing(
  bone: THREE.Object3D,
  tip: THREE.Object3D,
  toward: THREE.Vector3,
  radians: number,
): void {
  const from = new THREE.Vector3()
  const to = new THREE.Vector3()
  bone.getWorldPosition(from)
  tip.getWorldPosition(to)
  const limb = to.sub(from)
  if (limb.lengthSq() < 1e-8) return
  limb.normalize()
  const axis = new THREE.Vector3().crossVectors(limb, toward)
  if (axis.lengthSq() < 1e-8) return
  axis.normalize()
  const world = new THREE.Quaternion().setFromAxisAngle(axis, Math.min(radians, limb.angleTo(toward)))
  const parent = bone.parent
  if (parent) {
    // W is a world-space rotation; a quaternion is applied in the *parent's* frame.
    const parentWorld = new THREE.Quaternion()
    parent.getWorldQuaternion(parentWorld)
    world.premultiply(parentWorld.clone().invert()).multiply(parentWorld)
  }
  bone.quaternion.premultiply(world)
}

class VrmCharacter implements Character {
  readonly root: THREE.Group

  /** Not readonly: ``reframe`` recomputes it when a bigger window wants more person. */
  framing: Framing

  private readonly vrm: VRM

  private readonly source: string

  private readonly head: THREE.Object3D | null = null

  private readonly hips: THREE.Object3D | null = null

  private readonly chest: THREE.Object3D | null = null

  private pose: Pose = NEUTRAL

  private settle = 0

  /**
   * Body-layer clones and the driven mesh they have to copy. ``expressionManager``
   * writes morph influences onto the meshes it found in the file; the clones it has
   * never seen would otherwise freeze with their mouths shut.
   */
  private readonly echoes: Array<[THREE.Mesh, THREE.Mesh]> = []

  constructor(vrm: VRM, source: string) {
    this.vrm = vrm
    this.source = source
    this.root = vrm.scene
    this.head = vrm.humanoid?.getNormalizedBoneNode('head') ?? null
    this.hips = vrm.humanoid?.getNormalizedBoneNode('hips') ?? null
    this.chest =
      vrm.humanoid?.getNormalizedBoneNode('chest') ??
      vrm.humanoid?.getNormalizedBoneNode('spine') ??
      null
    vrm.humanoid?.resetNormalizedPose()
    this.relaxArms()
    this.drawAsLines()
    this.framing = this.measure()
  }

  /**
   * Take the figure out of the bind pose.
   *
   * Every VRM ships as a T-pose because that is what a skin needs to be painted in,
   * and a T-pose in a 250-pixel panel reads as a mannequin in a shop window. The
   * normalized rig is the right place to fix it: ``vrm.update()`` re-applies it every
   * frame, so the arms stay down without this file having to touch them again.
   */
  private relaxArms(): void {
    const humanoid = this.vrm.humanoid
    if (!humanoid) return
    const bone = (name: string): THREE.Object3D | null =>
      humanoid.getNormalizedBoneNode(name as never) ?? null
    const upper = [bone('leftUpperArm'), bone('rightUpperArm')]
    const lower = [bone('leftLowerArm'), bone('rightLowerArm')]
    const hand = [bone('leftHand'), bone('rightHand')]
    if (upper.some((entry) => !entry) || lower.some((entry) => !entry)) return
    this.root.updateMatrixWorld(true)
    const down = new THREE.Vector3(0, -1, 0)
    const forward = new THREE.Vector3(0, -0.4, 1).normalize()
    for (const [side, shoulder] of upper.entries()) {
      const elbow = lower[side]
      if (shoulder && elbow) swing(shoulder, elbow, down, ARM_SWING)
    }
    // Elbows second, and after a fresh matrix pass: each ``swing`` measures where the
    // arm actually points, so it has to see the shoulder's new angle to bend the
    // elbow in a sensible direction.
    this.root.updateMatrixWorld(true)
    for (const [side, elbow] of lower.entries()) {
      if (!elbow) continue
      swing(elbow, hand[side] ?? elbow, forward, ELBOW_SWING)
    }
    this.vrm.update(0)
  }

  /**
   * Lay the skin's inks over the figure: glowing edges and a faint body, no paint.
   *
   * The original mtoon materials are dropped rather than recoloured, which is what
   * makes this a *line* figure instead of a blue-painted one. Three consequences are
   * handled here rather than left to chance:
   *
   * * **Skinning.** The body layer is a clone of the same ``SkinnedMesh``, sharing
   *   the same geometry and skeleton, so both layers deform with the same bones — a
   *   ``LineSegments`` built from the edges would have followed only the mesh node
   *   and stood stiff while the rig moved under it.
   * * **Morph targets.** ``expressionManager`` only knows the meshes it found at load
   *   time, so the *original* stays the wireframe layer (still driven) and the clone
   *   copies its influences every frame in ``advance``.
   * * **Leaked programs.** A skin change rebuilds the figure, and each rebuild would
   *   otherwise strand a dozen compiled mtoon shaders behind materials nothing
   *   references any more. They are disposed on the way out.
   */
  private drawAsLines(): void {
    const palette = currentSkin()
    const meshes: THREE.Mesh[] = []
    this.root.traverse((object) => {
      const mesh = object as THREE.Mesh
      if (mesh.isMesh) meshes.push(mesh)
    })
    for (const mesh of meshes) {
      const replaced = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
      mesh.material = lineInk(palette)
      mesh.renderOrder = 2
      const parent = mesh.parent
      if (!parent) continue
      const body = mesh.clone(false)
      body.material = bodyInk(palette)
      // Both layers are transparent, so three sorts them by ``renderOrder`` before
      // depth: the body has to be painted first or it cannot hide the far-side lines.
      body.renderOrder = 1
      parent.add(body)
      if (mesh.morphTargetInfluences && body.morphTargetInfluences) {
        this.echoes.push([mesh, body])
      }
      for (const material of replaced) {
        // Never reached again: this model's own shading is gone by design.
        material.dispose()
      }
    }
  }

  /**
   * Frame the figure from what is actually on screen.
   *
   * Measured after the arms have been relaxed, because the alternative is deriving
   * the camera distance from a bind-pose bounding box that the model never shows --
   * which puts a person with their arms down in the middle of a frame sized for a
   * person with them out.
   */
  private measure(fraction = BUST_FRACTION): Framing {
    this.root.updateMatrixWorld(true)
    const bounds = new THREE.Box3().setFromObject(this.root)
    const height = Math.max(1e-4, bounds.max.y - bounds.min.y)
    const shown = height * fraction
    const width = Math.max(1e-4, bounds.max.x - bounds.min.x)
    return {
      target: bounds.max.y - shown / 2,
      span: shown * 1.12,
      beam: width * 1.12,
    }
  }

  reframe(fraction: number): void {
    // ``framing`` is readonly to the callers but recomputable here: the pet window
    // is the same height as two HUD panels stacked, and showing half a person in it
    // was a default inherited from the small one, not a decision.
    this.framing = this.measure(Math.min(1, Math.max(0.2, fraction)))
  }

  setPose(pose: Pose): void {
    // The driver hands over the same mutable object every frame; hold the
    // reference and let ``advance`` be the one place that reads it, exactly as
    // the hologram does, so the mouth and the head motion cannot disagree.
    this.pose = pose
    const manager = this.vrm.expressionManager
    if (!manager) return
    VISEME_PRESETS.forEach((name) => manager.setValue(name, pose.visemes[name]))
    manager.setValue('blink', pose.blink)
    // Brows are not a viseme. The nearest thing a stock rig offers is the emotion
    // presets, and only the one the driver is asking for is touched -- zeroing the
    // others every frame would fight any expression the operator sets.
    if (pose.brows > 0.15) manager.setValue('surprised', Math.min(1, pose.brows))
    if (pose.brows < -0.1) manager.setValue('angry', Math.min(1, -pose.brows))
  }

  advance(elapsed: number, delta: number, calm = false): void {
    this.vrm.update(Math.min(delta, 0.1))
    // ``update`` is what applies the expression manager's weights, so the body layer
    // can only be told after it — one pass over a handful of meshes, per frame.
    for (const [driven, echo] of this.echoes) {
      const from = driven.morphTargetInfluences
      const to = echo.morphTargetInfluences
      if (!from || !to || to.length !== from.length) continue
      for (let index = 0; index < from.length; index += 1) to[index] = from[index]
    }
    const head = this.head
    if (head) {
      // Eased in over the first half-second, so a freshly mounted panel does not
      // open on a head that is already turned. Applied *after* ``update`` because
      // that is what copies the normalized rig onto the bones, and a sway written
      // before it would be overwritten by the pose it just restored.
      this.settle = Math.min(1, this.settle + delta * 2)
      const drift = calm ? 0 : Math.sin(elapsed * 0.4) * 0.05
      head.rotation.y = (this.pose.yaw + drift) * this.settle
      head.rotation.x =
        (this.pose.pitch + (calm ? 0 : Math.sin(elapsed * 0.9) * 0.015)) * this.settle
      head.rotation.z = this.pose.roll * this.settle
    }
    if (calm) return
    // The body underneath. Rotations only, applied after ``update`` for the same
    // reason the head is: that call is what writes the normalized rig onto the
    // bones, and anything set before it is overwritten. Positions are left alone
    // because a hips *translation* means something different in every rig, while
    // a few hundredths of a radian mean the same thing in all of them.
    const shift = Math.sin(elapsed * 0.45)
    if (this.hips) {
      // Nobody stands still: the weight drifts from one foot to the other on a slow
      // cycle, and the torso answers by turning a quarter of the way toward where
      // the head is looking.
      this.hips.rotation.z = shift * 0.022 * this.settle
      this.hips.rotation.y = this.pose.yaw * 0.28 * this.settle
    }
    if (this.chest) {
      const breath = Math.sin(elapsed * 1.45)
      this.chest.rotation.x = (this.pose.pitch * 0.2 - breath * 0.012) * this.settle
      this.chest.rotation.z = -shift * 0.012 * this.settle
    }
  }

  describe(): string {
    return `赛博线条 · ${this.source}`
  }

  dispose(): void {
    VRMUtils.deepDispose(this.vrm.scene)
  }
}
