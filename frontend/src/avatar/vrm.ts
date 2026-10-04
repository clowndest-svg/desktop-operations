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
  sit: 0,
}

/**
 * Where "forward", "down" and "outward" are in the VRM's own space (+X 她的左侧,
 * +Y 上, +Z 前). Every direction below is a *world* direction: ``swing`` turns it
 * into whatever local rotation this particular rig needs, which is the only reason
 * the same numbers work on a second model.
 */
const OUT_LEFT = 0.52
const LAP = new THREE.Vector3(0, -0.62, 0.78).normalize()
const KEYS_LEFT = new THREE.Vector3(-0.34, -0.3, 0.89).normalize()
const KEYS_RIGHT = new THREE.Vector3(0.34, -0.3, 0.89).normalize()

/**
 * 盘腿坐：膝盖朝前朝外，小腿在身前交叉收回去，两只手捧住膝上那台电脑。
 *
 * 每根骨的"角度"故意给到比目标方向还大 —— ``swing`` 会自己夹到"正好指向那个方向"，
 * 所以调姿态只需要改方向向量，不用在角度和方向之间来回猜。
 */
const SIT_PLAN: Array<[string, string, THREE.Vector3, number]> = [
  ['leftUpperLeg', 'leftLowerLeg', new THREE.Vector3(OUT_LEFT, -0.3, 0.8), 2.3],
  ['rightUpperLeg', 'rightLowerLeg', new THREE.Vector3(-OUT_LEFT, -0.3, 0.8), 2.3],
  ['leftLowerLeg', 'leftFoot', new THREE.Vector3(-0.72, -0.3, -0.62), 2.4],
  ['rightLowerLeg', 'rightFoot', new THREE.Vector3(0.72, -0.3, -0.62), 2.4],
  ['leftUpperArm', 'leftLowerArm', LAP, 1.4],
  ['rightUpperArm', 'rightLowerArm', LAP, 1.4],
  ['leftLowerArm', 'leftHand', KEYS_LEFT, 1.2],
  ['rightLowerArm', 'rightHand', KEYS_RIGHT, 1.2],
]

/** 坐下时上身往前送的那一点（看膝上的屏幕）。符号和 ``pose.pitch`` 一致。 */
const SIT_LEAN = 0.26

/**
 * 盘腿坐要把整具身体往下送多少 —— 按她自己的身高算，不按米算。
 *
 * 写死一个数（比如 0.4 m）在另一具模型上就是"坐在天花板"或者"陷进地板"；而这个文件
 * 里所有别的东西都是量出来的，这条没道理例外。
 */
const SIT_DROP_RATIO = 0.26

/** 膝上那台电脑：一块底座 + 一片立起来朝她那边倒回去的屏幕，全息线稿，不是贴图。 */
const LAPTOP = {
  width: 0.3,
  baseDepth: 0.21,
  baseThickness: 0.014,
  screenHeight: 0.19,
  screenThickness: 0.008,
  /** 屏幕从远端那条边立起来后往她那边倒回去多少（观众看到的是盖子的背面，和参考图一致）。 */
  screenLean: 0.34,
}

interface SitRig {
  bones: THREE.Object3D[]
  base: THREE.Quaternion[]
  seated: THREE.Quaternion[]
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

  /** The two poses the sit interpolates between, measured off this rig once. */
  private sitRig: SitRig | null = null

  /** 坐下要沉下去多少，以及站着/坐着两套取景 —— 都是量出来的，不是猜的。 */
  private sitDrop = 0

  private standFraming: Framing = { target: 0, span: 3.5, beam: 2.4 }

  /** 坐姿量到的头顶/脚底；取景按当前 fraction 现算，所以 ``reframe`` 之后不会失真。 */
  private seatExtent: { top: number; bottom: number } | null = null

  private fraction = BUST_FRACTION

  private standHeight = 1.5

  /** 膝上那台电脑，只在坐着的时候出现。 */
  private readonly laptop: THREE.Group = this.buildLaptop()

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
    this.root.updateMatrixWorld(true)
    this.standHeight = new THREE.Box3()
      .setFromObject(this.root)
      .getSize(new THREE.Vector3()).y
    this.root.add(this.laptop)
    this.standFraming = this.measure()
    this.framing = this.standFraming
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
   * Measure what "sit" means for this particular rig, once.
   *
   * Not a table of local rotations: a bone's local axes come from whoever modelled the
   * file, so ``rotation.x = -1.5`` that bends one model's knee sideways turns another's
   * into a dislocated hip. So the seated pose is *derived* -- swing each limb toward the
   * direction the reference picture shows, the way ``relaxArms`` does -- and stored next
   * to the pose it came from, which leaves the per-frame work as one slerp per joint.
   *
   * While the pose is actually on, two more things get measured rather than guessed:
   * what the camera has to show for a folded body (``seatedFraming``), and where her lap
   * ends up (which is where the laptop goes).
   */
  private buildSit(): void {
    const humanoid = this.vrm.humanoid
    if (!humanoid) return
    const joints: THREE.Object3D[] = []
    const tips: THREE.Object3D[] = []
    for (const [joint, tip] of SIT_PLAN) {
      const from = humanoid.getNormalizedBoneNode(joint as never)
      const to = humanoid.getNormalizedBoneNode(tip as never)
      if (!from || !to) return
      joints.push(from)
      tips.push(to)
    }
    const base = joints.map((bone) => bone.quaternion.clone())
    const seated: THREE.Quaternion[] = []
    SIT_PLAN.forEach(([, , toward, radians], index) => {
      // Parent first, then its child, with a matrix pass in between: ``swing`` measures
      // where the limb actually points, so the shin only bends sensibly once it can see
      // the thigh's new angle. Same order relaxArms uses, for the same reason.
      this.root.updateMatrixWorld(true)
      swing(joints[index], tips[index], toward, radians)
      seated.push(joints[index].quaternion.clone())
    })
    this.sitRig = { bones: joints, base, seated }
    this.sitDrop = this.standHeight * SIT_DROP_RATIO
    // 量之前先把缩放按回 1：这张卡是在她"从光里长出来"那一帧第一次建的，页面那时把整具
    // 身体缩到 0.2~1 之间，直接量骨骼会得到一个缩了水的坐姿高度，取景就把相机推到她脸上。
    const arrival = this.root.scale.x || 1
    this.root.scale.setScalar(1)
    this.root.position.y = -this.sitDrop
    // ``update`` is what copies the normalized rig onto the bones the bounds are
    // measured from; measuring without it would frame a body she is not wearing.
    this.vrm.update(0)
    this.root.updateMatrixWorld(true)
    // 电脑先摆好再取景：它是 root 的孩子，位置没定就量，量到的是"一具身体加一块飘在
    // 脚边的板子"，相机就会照着那块板子去取景（第一版她的头被切掉一半就是这么来的）。
    this.placeLaptop()
    this.seatExtent = this.seatedExtent()
    joints.forEach((bone, index) => bone.quaternion.copy(base[index]))
    this.root.position.y = 0
    this.root.scale.setScalar(arrival)
    this.vrm.update(0)
  }

  /** Blend standing and seated on the normalized rig, which ``update`` then copies down. */
  private applySit(amount: number): void {
    if (this.sitRig === null) {
      if (amount <= 0.001) return
      this.buildSit()
      if (this.sitRig === null) return
    }
    const rig = this.sitRig
    const eased = Math.max(0, Math.min(1, amount))
    for (let index = 0; index < rig.bones.length; index += 1) {
      rig.bones[index].quaternion.copy(rig.base[index]).slerp(rig.seated[index], eased)
    }
    // 盘腿是坐在地上，不是坐在空气上：整具身体随姿态一起沉下去。
    this.root.position.y = -this.sitDrop * eased
    this.laptop.visible = eased > 0.4
    this.framing = this.blendFraming(eased)
  }

  /**
   * 坐着时她真正占多高 —— 从骨骼量，不从网格量。
   *
   * ``Box3.setFromObject`` 用的是 geometry 自带的包围盒乘节点矩阵，**不跟着蒙皮变形**：
   * 摆好坐姿再量，量到的还是绑定姿势那具盒子（第一版取景偏低、她的头被切掉一半就是
   * 这么来的）。骨骼位置是跟着 ``update()`` 走的，头顶 = 头骨再往上一点，脚底 = 两只
   * 脚踝里低的那只再往下一点。
   */
  private seatedExtent(): { top: number; bottom: number } | null {
    const humanoid = this.vrm.humanoid
    if (!humanoid) return null
    const head = humanoid.getRawBoneNode('head')
    const left = humanoid.getRawBoneNode('leftFoot')
    const right = humanoid.getRawBoneNode('rightFoot')
    if (!head || !left || !right) return null
    const at = new THREE.Vector3()
    head.getWorldPosition(at)
    const top = at.y + 0.12
    left.getWorldPosition(at)
    let bottom = at.y
    right.getWorldPosition(at)
    return { top, bottom: Math.min(bottom, at.y) - 0.06 }
  }

  /**
   * 坐姿那套取景，按**当前** fraction 现算 —— 和 ``measure`` 同一个算法。
   *
   * HUD 只看上半身（面板 250 px 高，fraction 0.5），宠物窗要全身。基准不同的两套
   * 取景之间插值，插出来的不是"慢慢坐下"，是"一坐就跳到另一种构图"—— 用户看到的
   * "姿势变了但整具被裁掉"就是这个。
   */
  private seatedFraming(): Framing | null {
    const at = this.seatExtent
    if (at === null) return null
    const shown = Math.max(0.2, at.top - at.bottom) * this.fraction
    return { target: at.top - shown / 2, span: shown * 1.12, beam: this.standFraming.beam }
  }

  /** What the camera has to show at this much sit, between the two measured framings. */
  private blendFraming(amount: number): Framing {
    const from = this.standFraming
    const to = this.seatedFraming() ?? from
    const at = (a: number, b: number): number => a + (b - a) * amount
    return {
      target: at(from.target, to.target),
      span: at(from.span, to.span),
      beam: at(from.beam, to.beam),
    }
  }

  /** 膝上那台电脑：两块薄板加描边，和人物同一套线稿语言，不是贴图。 */
  private buildLaptop(): THREE.Group {
    const skin = currentSkin()
    const ink = new THREE.MeshBasicMaterial({
      color: skin.fill,
      transparent: true,
      opacity: 0.24,
      depthWrite: false,
    })
    const edge = new THREE.LineBasicMaterial({ color: skin.line, transparent: true, opacity: 0.7 })
    const slab = (width: number, height: number, depth: number): THREE.Group => {
      const geometry = new THREE.BoxGeometry(width, height, depth)
      const piece = new THREE.Group()
      piece.add(new THREE.Mesh(geometry, ink))
      piece.add(new THREE.LineSegments(new THREE.EdgesGeometry(geometry), edge))
      return piece
    }
    const laptop = new THREE.Group()
    laptop.add(slab(LAPTOP.width, LAPTOP.baseThickness, LAPTOP.baseDepth))
    // 屏幕铰在远离她那条边上，立起来再往她那边倒回去一点：观众看到的是盖子的背面，
    // 和参考图里那个角度一致。
    const hinge = new THREE.Group()
    hinge.position.set(0, LAPTOP.baseThickness / 2, LAPTOP.baseDepth / 2)
    hinge.rotation.x = -LAPTOP.screenLean
    const screen = slab(LAPTOP.width, LAPTOP.screenHeight, LAPTOP.screenThickness)
    screen.position.set(0, LAPTOP.screenHeight / 2, 0)
    hinge.add(screen)
    laptop.add(hinge)
    laptop.visible = false
    return laptop
  }

  /** Put the laptop on the lap the seated pose actually has. */
  private placeLaptop(): void {
    const hips = this.hips
    if (hips === null) return
    const at = new THREE.Vector3()
    hips.getWorldPosition(at)
    // 换算到 root 的本地坐标再说：root 自己已经沉下去一截，直接写世界高度就是把她按到
    // 电脑下面去（第一版就是这么错的，电脑掉在脚底下）。
    this.root.worldToLocal(at)
    at.y -= 0.05
    at.z += 0.18
    this.laptop.position.copy(at)
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
    this.fraction = Math.min(1, Math.max(0.2, fraction))
    this.standFraming = this.measure(this.fraction)
    this.framing = this.blendFraming(this.pose.sit)
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
    // Before ``update``: that call is what copies the normalized rig onto the bones the
    // skin actually wears, so anything written after it is a frame late at best.
    this.applySit(this.pose.sit)
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
      this.chest.rotation.x =
        (this.pose.pitch * 0.2 - breath * 0.012 + SIT_LEAN * this.pose.sit) * this.settle
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
