// Seated riding pose for a soldier on a motorbike: hips on the seat, hands on the bars, feet on the pegs.
// Runs after the animation mixer each frame; bone rotations are solved in world space with two-bone IK, so it
// does not depend on how the rig's bones are oriented.
import * as THREE from 'three';

// bike-local anchors (bike model faces +Z, see makeVehicle 'bike' in village.js)
const SEAT = new THREE.Vector3(0, 0.98, -0.12);
const HAND = { L: new THREE.Vector3(0.3, 1.08, 0.45), R: new THREE.Vector3(-0.3, 1.08, 0.45) };
const FOOT = { L: new THREE.Vector3(0.2, 0.5, -0.12), R: new THREE.Vector3(-0.2, 0.5, -0.12) };
const LEAN = 0.6; // radians the torso leans toward the bars
console.info('ride.js v4 loaded');

const _a = new THREE.Vector3(), _b = new THREE.Vector3(), _q = new THREE.Quaternion(), _q2 = new THREE.Quaternion();
const wp = (o, out = new THREE.Vector3()) => o.getWorldPosition(out);
const find = (root, n) => root.getObjectByName(n) || root.getObjectByName(THREE.PropertyBinding.sanitizeNodeName(n));

/** Rotate `bone` (world space) so the direction bone→child points at `target`. */
function aim(bone, child, target) {
  const from = wp(child, _a).sub(wp(bone, _b)).normalize();
  const to = target.clone().sub(wp(bone, _b)).normalize();
  const r = _q.setFromUnitVectors(from, to);
  const pq = bone.parent.getWorldQuaternion(_q2);
  bone.quaternion.copy(pq.clone().invert().multiply(r).multiply(pq).multiply(bone.quaternion));
  bone.updateMatrixWorld(true);
}

/** Two-bone IK: upper→lower→end reaches `target`, the middle joint bending toward `pole` (world direction). */
function ik(upper, lower, end, target, pole) {
  const S = wp(upper, new THREE.Vector3());
  const l1 = wp(lower, _a).distanceTo(S), l2 = wp(end, _b).distanceTo(wp(lower, _a));
  const dir = target.clone().sub(S);
  const d = THREE.MathUtils.clamp(dir.length(), Math.abs(l1 - l2) + 0.02, l1 + l2 - 0.01);
  dir.normalize();
  const p = pole.clone().addScaledVector(dir, -pole.dot(dir)).normalize();
  const a = (l1 * l1 - l2 * l2 + d * d) / (2 * d), h = Math.sqrt(Math.max(0, l1 * l1 - a * a));
  const elbow = S.clone().addScaledVector(dir, a).addScaledVector(p, h);
  aim(upper, lower, elbow);
  aim(lower, end, S.clone().addScaledVector(dir, d));
}

/** Yaw (radians, about the bike's up axis) of the left→right line between two bones, relative to the bike's sideways axis. */
function twist(bikeRoot, left, right) {
  const v = wp(left, new THREE.Vector3()).sub(wp(right, _b)).applyQuaternion(bikeRoot.quaternion.clone().invert());
  return Math.atan2(-v.z, v.x);
}

/** Resolve the bones once per soldier. */
export function rideRig(model) {
  const g = (n) => find(model, n);
  const rig = { hips: g('Hips'), spine: g('Abdomen'), legs: {}, arms: {} };
  for (const s of ['L', 'R']) {
    rig.legs[s] = [g(`UpperLeg.${s}`), g(`LowerLeg.${s}`), g(`LowerLeg.${s}_end`)];
    rig.feet = rig.feet || {};
    rig.feet[s] = [g(`Foot.${s}`), g(`Foot.${s}_end`)];
    rig.arms[s] = [g(`UpperArm.${s}`), g(`LowerArm.${s}`), g(`Wrist.${s}`)];
  }
  rig.ok = !!(rig.hips && rig.spine && Object.values(rig.legs).every((l) => l.every(Boolean)) && Object.values(rig.arms).every((l) => l.every(Boolean)) && Object.values(rig.feet).every((l) => l.every(Boolean)));
  rig.bones = [];
  model.traverse((o) => { if (o.isBone && o.userData.bq) rig.bones.push(o); });
  // the soldier pack's weapons hang in the right hand; they are hidden while riding
  rig.guns = rig.arms.R[2] ? rig.arms.R[2].children.filter((c) => !c.isBone) : [];
  return rig;
}

/** Put every bone back in its bind pose (clears whatever a previous animation or the riding pose left behind). */
export function resetToBind(rig) {
  for (const b of rig.bones) { b.quaternion.copy(b.userData.bq); b.position.copy(b.userData.bp); }
}

/** Pose the soldier on the bike. Call after the mixer update. `root` is the soldier's root group. */
export function poseRider(rig, root, bikeRoot) {
  if (!rig.ok) return;
  bikeRoot.updateMatrixWorld(true); // the bike moved this frame; its matrix is only refreshed at render time
  const tw = (v) => bikeRoot.localToWorld(v.clone());
  const up = new THREE.Vector3(0, 1, 0).applyQuaternion(bikeRoot.quaternion);
  // orientation: square the whole body to the bike, then take out the idle clip's hip twist
  root.quaternion.copy(bikeRoot.quaternion);
  root.updateMatrixWorld(true);
  for (let i = 0; i < 2; i++) {
    root.quaternion.premultiply(_q.setFromAxisAngle(up, -twist(bikeRoot, rig.legs.L[0], rig.legs.R[0])));
    root.updateMatrixWorld(true);
  }
  const shift = tw(SEAT).sub(wp(rig.hips, new THREE.Vector3()));
  root.position.add(shift);
  root.updateMatrixWorld(true);
  // the idle clip does not drive the spine, so start every frame from its rest pose (the lean must not accumulate)
  rig.spine.quaternion.copy(rig.spine.userData.bq);
  rig.spine.updateMatrixWorld(true);
  // the idle clip also turns the shoulders to aim the rifle: square them to the bike (twist about the bike's up axis)
  for (let i = 0; i < 2; i++) {
    const q = rig.spine.parent.getWorldQuaternion(new THREE.Quaternion());
    const r = new THREE.Quaternion().setFromAxisAngle(up, -twist(bikeRoot, rig.arms.L[0], rig.arms.R[0]));
    rig.spine.quaternion.copy(q.clone().invert().multiply(r).multiply(q).multiply(rig.spine.quaternion));
    rig.spine.updateMatrixWorld(true);
  }
  // lean the torso toward the bars (pure pitch about the bike's sideways axis)
  const side = new THREE.Vector3(1, 0, 0).applyQuaternion(bikeRoot.quaternion);
  const head = find(root, 'Head');
  const leanBy = (a) => {
    const q = rig.spine.parent.getWorldQuaternion(new THREE.Quaternion());
    const r = new THREE.Quaternion().setFromAxisAngle(side, a);
    rig.spine.quaternion.copy(q.clone().invert().multiply(r).multiply(q).multiply(rig.spine.quaternion));
    rig.spine.updateMatrixWorld(true);
  };
  // pick the sign that tips the head toward the bike's +Z
  const fwd = new THREE.Vector3(0, 0, 1).applyQuaternion(bikeRoot.quaternion);
  const q0 = rig.spine.quaternion.clone();
  const base = wp(head, new THREE.Vector3()).dot(fwd);
  leanBy(0.2);
  const sign = wp(head, new THREE.Vector3()).dot(fwd) > base ? 1 : -1;
  rig.spine.quaternion.copy(q0); rig.spine.updateMatrixWorld(true);
  leanBy(sign * LEAN);
  // the lean moved the shoulders; keep the hips on the seat
  const shift2 = tw(SEAT).sub(wp(rig.hips, new THREE.Vector3()));
  root.position.add(shift2);
  root.updateMatrixWorld(true);
  // legs: knees forward and out, feet on the pegs; arms: elbows down and out, hands on the grips
  const fwdW = fwd.clone(), down = new THREE.Vector3(0, -1, 0);
  for (const s of ['L', 'R']) {
    const out = new THREE.Vector3(s === 'L' ? 1 : -1, 0, 0).applyQuaternion(bikeRoot.quaternion);
    const [u, l, e] = rig.legs[s];
    ik(u, l, e, tw(FOOT[s]), fwdW.clone().addScaledVector(out, 0.35));
    // the foot bones hang off the armature root, not the leg: carry them to the peg, toes forward
    const [fb, fe] = rig.feet[s];
    const ankle = wp(e, new THREE.Vector3());
    fb.position.copy(fb.parent.worldToLocal(ankle.clone()));
    fb.updateMatrixWorld(true);
    aim(fb, fe, ankle.clone().addScaledVector(fwdW, 0.2));
    const [au, al, ae] = rig.arms[s];
    ik(au, al, ae, tw(HAND[s]), down.clone().addScaledVector(out, 0.8));
  }
  if (!rig.hid) rig.hid = rig.guns.filter((g) => g.visible);
  for (const g of rig.hid) g.visible = false;
}

/** Undo what riding changed (shown again once the soldier is on foot). */
export function unrideRider(rig) {
  resetToBind(rig);
  if (!rig.hid) return;
  for (const g of rig.hid) g.visible = true;
  rig.hid = null;
}
