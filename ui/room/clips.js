// File: ui/room/clips.js

import * as THREE from 'three';

export function createClips(ctx) {
  const { config, gltfs, heelsFiles, heelSuffix, model, boneAliases, restPose, restPosition, findBone, log } = ctx;

  function hipOrigin(clip) {
    const rootPos = clip.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
    const rootRot = clip.tracks.find((t) => t.name === 'CC_Base_BoneRoot.quaternion');
    const hipPos = clip.tracks.find((t) => t.name === 'CC_Base_Hip.position');
    if (!rootPos || !rootRot || !hipPos) return null;
    const q = new THREE.Quaternion().fromArray(rootRot.values, 0);
    return new THREE.Vector3().fromArray(hipPos.values, 0).applyQuaternion(q).add(new THREE.Vector3().fromArray(rootPos.values, 0));
  }

  function normalizeClip(clip, origin = hipOrigin(clip)) {
    const c = clip.clone();
    const rootPos = c.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
    if (!rootPos || !origin) return c;
    const hip = origin;
    const values = rootPos.values.slice();
    for (let i = 0; i < values.length; i += 3) {
      values[i] -= hip.x;
      values[i + 2] -= hip.z;
    }
    rootPos.values = values;
    return c;
  }

  function hipHeight(gltf) {
    const node = gltf.parser.json.nodes.find((n) => n.name === 'CC_Base_Hip');
    return node && node.translation ? Math.hypot(...node.translation) : null;
  }

  const characterHip = hipHeight(gltfs.get(config.character));
  const keptPositions = new Set(['CC_Base_BoneRoot.position', 'CC_Base_Hip.position']);
  function retarget(clip, gltf, sizeFrom = gltf) {
    const c = clip.clone();
    for (const t of c.tracks) {
      const dot = t.name.lastIndexOf('.');
      const bone = t.name.slice(0, dot);
      if (boneAliases[bone]) t.name = boneAliases[bone] + t.name.slice(dot);
    }
    if (config.retarget === false) return c;
    const sourceHip = hipHeight(sizeFrom);
    const ratio = characterHip && sourceHip ? characterHip / sourceHip : 1;
    c.tracks = c.tracks.filter((t) => !t.name.endsWith('.scale') && (!t.name.endsWith('.position') || keptPositions.has(t.name)));
    if (Math.abs(ratio - 1) > 1e-4) {
      for (const t of c.tracks) {
        if (!keptPositions.has(t.name)) continue;
        t.values = t.values.map((v) => v * ratio);
      }
    }
    return c;
  }

  const baseName = (path) => path.split('/').pop();
  const rawClips = new Map();
  const sourceClips = new Map();
  for (const [file, gltf] of gltfs) {
    for (const clip of gltf.animations) {
      if (clip.name.startsWith('Key')) continue;
      const flatFile = [...heelsFiles].find(([, h]) => h === file)?.[0];
      const source = retarget(clip, gltf, flatFile ? gltfs.get(flatFile) : gltf);
      sourceClips.set(`${file}::${clip.name}`, source);
      rawClips.set(`${file}::${clip.name}`, normalizeClip(source));
    }
  }

  function holdFrame(clip, which) {
    const held = clip.clone();
    held.tracks = held.tracks.map((t) => {
      const size = t.getValueSize();
      const start = which === 'end' ? t.values.length - size : 0;
      const frame = Array.from(t.values.slice(start, start + size));
      return new t.constructor(t.name, [0, 1], frame.concat(frame));
    });
    held.resetDuration();
    return held;
  }

  function anchorFeet(clip) {
    const feet = (config.feet || []).map((n) => model.getObjectByName(n)).filter(Boolean);
    const rootBone = model.getObjectByName('CC_Base_BoneRoot');
    const rootPos = clip.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
    const hipPos = clip.tracks.find((t) => t.name === 'CC_Base_Hip.position');
    if (feet.length < 2 || !rootBone || !rootPos || !hipPos) {
      log(`Cannot anchor feet for ${clip.name}`, true);
      return clip;
    }
    const armature = rootBone.parent;
    const sampler = new THREE.AnimationMixer(model);
    const action = sampler.clipAction(clip).play();
    const rootAt = rootPos.createInterpolant();
    const times = hipPos.times.slice();
    const values = new Float32Array(times.length * 3);
    const mid = new THREE.Vector3();
    const foot = new THREE.Vector3();
    let start = null;
    times.forEach((t, i) => {
      action.time = t;
      sampler.update(0);
      model.updateMatrixWorld(true);
      mid.set(0, 0, 0);
      for (const f of feet) mid.add(f.getWorldPosition(foot));
      armature.worldToLocal(mid.divideScalar(feet.length));
      if (!start) start = mid.clone();
      const base = rootAt.evaluate(t);
      values[i * 3] = base[0] - (mid.x - start.x);
      values[i * 3 + 1] = base[1];
      values[i * 3 + 2] = base[2] - (mid.z - start.z);
    });
    sampler.stopAllAction();
    sampler.uncacheRoot(model);
    const anchored = clip.clone();
    anchored.tracks = anchored.tracks.map((t) => (t.name === rootPos.name ? new THREE.VectorKeyframeTrack(t.name, times, values) : t));
    return anchored;
  }

  const LOOP_BONES = ['CC_Base_Hip', 'CC_Base_L_Thigh', 'CC_Base_L_Calf', 'CC_Base_L_Foot', 'CC_Base_R_Thigh', 'CC_Base_R_Calf', 'CC_Base_R_Foot'];

  function quatAngle(values, i, j) {
    const dot = Math.abs(values[i * 4] * values[j * 4] + values[i * 4 + 1] * values[j * 4 + 1] + values[i * 4 + 2] * values[j * 4 + 2] + values[i * 4 + 3] * values[j * 4 + 3]);
    return 2 * Math.acos(Math.min(1, dot));
  }

  function findLoopPoint(clip) {
    const legs = clip.tracks.filter((t) => t instanceof THREE.QuaternionKeyframeTrack && LOOP_BONES.some((b) => t.name === `${b}.quaternion`));
    const reference = legs.reduce((best, t) => (t.times.length > best.times.length ? t : best), legs[0]);
    if (!reference || reference.times.length < 8) return null;
    const n = reference.times.length;
    const dense = legs.filter((t) => t.times.length === n);
    const stepAt = (k) => Math.max(...dense.map((t) => quatAngle(t.values, k, k - 1)));
    const allSteps = Array.from({ length: n - 1 }, (_, i) => stepAt(i + 1)).sort((a, b) => a - b);
    const typical = allSteps[Math.floor(allSteps.length / 2)];
    const search = (filtered) => {
      let best = null;
      for (let k = Math.floor(n * 0.9); k < n; k++) {
        if (filtered && stepAt(k) < typical * 0.75) continue;
        let pose = 0;
        let speed = 0;
        for (const t of dense) {
          pose = Math.max(pose, quatAngle(t.values, k, 0));
          speed += Math.abs(quatAngle(t.values, k, k - 1) - quatAngle(t.values, 1, 0));
        }
        const score = pose + (2 * speed) / dense.length;
        if (!best || score < best.score) best = { score, time: reference.times[k], pose };
      }
      return best;
    };
    return search(true) || search(false);
  }

  function fixLoop(clip, name) {
    const point = findLoopPoint(clip);
    if (!point) return clip;
    const c = clip.clone();
    const start = Math.min(...clip.tracks.map((t) => t.times[0]));
    const end = point.time;
    const qa = new THREE.Quaternion();
    const qEnd = new THREE.Quaternion();
    const qCorr = new THREE.Quaternion();
    const ident = new THREE.Quaternion();
    c.tracks = c.tracks.map((track) => {
      if (track.times.length < 3) return new track.constructor(track.name, Array.from(track.times, (t) => Math.max(0, t - start)), track.values);
      const size = track.getValueSize();
      const t0 = track.times[0];
      const atEnd = Array.from(track.createInterpolant().evaluate(end));
      const first = Array.from(track.values.slice(0, size));
      const times = [];
      const values = [];
      const isQuat = track instanceof THREE.QuaternionKeyframeTrack;
      if (isQuat) {
        qEnd.fromArray(atEnd);
        qa.fromArray(first);
        if (qa.dot(qEnd) < 0) qEnd.set(-qEnd.x, -qEnd.y, -qEnd.z, -qEnd.w);
        qCorr.copy(qa).multiply(qEnd.clone().invert());
      }
      for (let i = 0; i < track.times.length && track.times[i] < end - 1e-6; i++) {
        const f = (track.times[i] - t0) / (end - t0);
        times.push(track.times[i] - start);
        if (isQuat) {
          const q = new THREE.Quaternion().fromArray(track.values, i * 4);
          new THREE.Quaternion().slerpQuaternions(ident, qCorr, f).multiply(q).toArray(values, values.length);
        } else {
          for (let k = 0; k < size; k++) values.push(track.values[i * size + k] + f * (first[k] - atEnd[k]));
        }
      }
      times.push(end - start);
      values.push(...first);
      return new track.constructor(track.name, times, values);
    });
    c.duration = end - start;
    log(`${name}: loop cut at ${end.toFixed(2)} s of ${clip.duration.toFixed(2)} s; ${THREE.MathUtils.radToDeg(point.pose).toFixed(1)} deg of mismatch spread across the stride.`);
    return c;
  }

  function computeTravel(clip, looped) {
    const feet = (config.feet || []).map((n) => findBone(n)).filter(Boolean);
    const hipPos = clip.tracks.find((t) => t.name === 'CC_Base_Hip.position');
    if (feet.length < 2 || !hipPos) return null;
    const sampler = new THREE.AnimationMixer(model);
    const action = sampler.clipAction(clip).play();
    const times = Array.from(hipPos.times);
    const samples = times.map((t) => {
      action.time = t;
      sampler.update(0);
      model.updateMatrixWorld(true);
      return feet.map((f) => f.getWorldPosition(new THREE.Vector3()));
    });
    sampler.stopAllAction();
    sampler.uncacheRoot(model);
    const values = new Float32Array(times.length * 3);
    const steps = [];
    let planted = samples[0][0].y <= samples[0][1].y ? 0 : 1;
    let x = 0;
    let z = 0;
    for (let i = 1; i < times.length; i++) {
      const other = 1 - planted;
      if (samples[i - 1][other].y < samples[i - 1][planted].y - 0.01) {
        planted = other;
        steps.push(times[i - 1]);
      }
      x -= samples[i][planted].x - samples[i - 1][planted].x;
      z -= samples[i][planted].z - samples[i - 1][planted].z;
      values[i * 3] = x;
      values[i * 3 + 2] = z;
    }
    if (looped && Math.hypot(x, z) > 0.01) {
      const heading = Math.atan2(x, z);
      const c = Math.cos(-heading);
      const sn = Math.sin(-heading);
      for (let i = 0; i < times.length; i++) {
        const vx = values[i * 3];
        const vz = values[i * 3 + 2];
        values[i * 3] = vx * c + vz * sn;
        values[i * 3 + 2] = -vx * sn + vz * c;
      }
    }
    const firstPlanted = samples[0][0].y <= samples[0][1].y ? 0 : 1;
    if (looped && planted !== firstPlanted) steps.push(times[times.length - 1]);
    const track = new THREE.VectorKeyframeTrack('travel', times, values);
    return { interp: track.createInterpolant(), start: times[0], end: times[times.length - 1], steps, distance: Math.hypot(x, z) };
  }

  function alignedSource(clipName) {
    const ref = config.clips[clipName];
    return ref ? sourceClips.get(`${ref.file}::${ref.clip}`) : null;
  }

  function sampleBones(clip, names, times, read) {
    const bonesToRead = names.map((n) => findBone(n)).filter(Boolean);
    const sampler = new THREE.AnimationMixer(model);
    const action = sampler.clipAction(clip).play();
    const out = times.map((t) => {
      action.time = t;
      sampler.update(0);
      model.updateMatrixWorld(true);
      return read(bonesToRead);
    });
    sampler.stopAllAction();
    sampler.uncacheRoot(model);
    return out;
  }

  function groundFeet(clip, fixedLift = null) {
    const rootPos = clip.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
    if (!rootPos) return clip;
    const toes = config.toes || ['CC_Base_L_ToeBase', 'CC_Base_R_ToeBase'];
    const times = Array.from({ length: 41 }, (_, i) => (clip.duration * i) / 40);
    const lift = fixedLift ?? (config.toeHeight ?? 0.027) - Math.min(...sampleBones(clip, toes, times, (bs) => Math.min(...bs.map((b) => b.getWorldPosition(new THREE.Vector3()).y))));
    const scale = findBone('CC_Base_BoneRoot').parent.getWorldScale(new THREE.Vector3()).y;
    const c = clip.clone();
    const track = c.tracks.find((t) => t.name === 'CC_Base_BoneRoot.position');
    const values = track.values.slice();
    for (let i = 1; i < values.length; i += 3) values[i] += lift / scale;
    track.values = values;
    if (fixedLift === null) log(`${clip.name}: lifted ${(lift * 100).toFixed(1)} cm so her feet meet the floor.`);
    c.userData = { ...(c.userData || {}), groundLift: lift };
    return c;
  }

  function smoothSeries(values, radius) {
    return values.map((_, i) => {
      let sum = 0;
      let n = 0;
      for (let k = Math.max(0, i - radius); k <= Math.min(values.length - 1, i + radius); k++) {
        sum += values[k];
        n++;
      }
      return sum / n;
    });
  }

  function extractRootMotion(clip) {
    const find = (name) => clip.tracks.find((t) => t.name === name);
    const rootPos = find('CC_Base_BoneRoot.position');
    const rootRot = find('CC_Base_BoneRoot.quaternion');
    const hipPos = find('CC_Base_Hip.position');
    const hipRot = find('CC_Base_Hip.quaternion');
    if (!rootPos || !rootRot || !hipPos || !hipRot) return { clip, motion: null };
    const times = Array.from(hipPos.times);
    const at = (track) => track.createInterpolant();
    const iRootPos = at(rootPos);
    const iRootRot = at(rootRot);
    const iHipPos = at(hipPos);
    const iHipRot = at(hipRot);
    const rest = restPose.get('CC_Base_BoneRoot').clone().multiply(restPose.get('CC_Base_Hip')).invert();
    const xs = [];
    const zs = [];
    const yaws = [];
    const roots = [];
    for (const t of times) {
      const tr = new THREE.Vector3().fromArray(iRootPos.evaluate(t));
      const qr = new THREE.Quaternion().fromArray(iRootRot.evaluate(t));
      const th = new THREE.Vector3().fromArray(iHipPos.evaluate(t));
      const qh = new THREE.Quaternion().fromArray(iHipRot.evaluate(t));
      const hip = th.clone().applyQuaternion(qr).add(tr);
      const fwd = new THREE.Vector3(0, 0, 1).applyQuaternion(qr.clone().multiply(qh).multiply(rest));
      xs.push(hip.x);
      zs.push(hip.z);
      let yaw = Math.atan2(fwd.x, fwd.z);
      if (yaws.length) {
        const prev = yaws[yaws.length - 1];
        while (yaw - prev > Math.PI) yaw -= 2 * Math.PI;
        while (yaw - prev < -Math.PI) yaw += 2 * Math.PI;
      }
      yaws.push(yaw);
      roots.push({ tr, qr });
    }
    const radius = Math.max(1, Math.round(0.15 / ((times[times.length - 1] - times[0]) / (times.length - 1))));
    const sx = smoothSeries(xs, radius);
    const sz = smoothSeries(zs, radius);
    const sy = smoothSeries(yaws, radius);
    const n = times.length;
    const fix = (arr, raw) => {
      const d0 = raw[0] - arr[0];
      const d1 = raw[n - 1] - arr[n - 1];
      return arr.map((v, i) => v + d0 + ((d1 - d0) * i) / (n - 1));
    };
    const fx = fix(sx, xs);
    const fz = fix(sz, zs);
    const fy = fix(sy, yaws);
    const scale = findBone('CC_Base_BoneRoot').parent.getWorldScale(new THREE.Vector3()).x;
    const newPos = new Float32Array(n * 3);
    const newRot = new Float32Array(n * 4);
    const motionValues = new Float32Array(n * 3);
    const up = new THREE.Vector3(0, 1, 0);
    roots.forEach(({ tr, qr }, k) => {
      const dx = fx[k] - fx[0];
      const dz = fz[k] - fz[0];
      const dyaw = fy[k] - fy[0];
      const undo = new THREE.Quaternion().setFromAxisAngle(up, -dyaw);
      new THREE.Vector3(tr.x - dx, tr.y, tr.z - dz).applyQuaternion(undo).toArray(newPos, k * 3);
      undo.clone().multiply(qr).toArray(newRot, k * 4);
      motionValues[k * 3] = dx * scale;
      motionValues[k * 3 + 1] = dyaw;
      motionValues[k * 3 + 2] = dz * scale;
    });
    const c = clip.clone();
    c.tracks = c.tracks.map((t) => {
      if (t.name === rootPos.name) return new THREE.VectorKeyframeTrack(t.name, times, newPos);
      if (t.name === rootRot.name) return new THREE.QuaternionKeyframeTrack(t.name, times, newRot);
      return t;
    });
    const track = new THREE.VectorKeyframeTrack('rootMotion', times, motionValues);
    return { clip: c, motion: { interp: track.createInterpolant(), start: times[0], end: times[n - 1], yaw: fy[n - 1] - fy[0] } };
  }

  function longestClipKey(file) {
    let best = null;
    for (const [k, clip] of rawClips) {
      if (!k.startsWith(`${file}::`)) continue;
      if (!best || clip.duration > rawClips.get(best).duration) best = k;
    }
    return best;
  }

  const mirrorName = (name) => name.replace(/_L_/, '_TMP_').replace(/_R_/, '_L_').replace(/_TMP_/, '_R_');
  const mirrorQuat = (q) => q.set(q.x, -q.y, -q.z, q.w);
  let mirrorRig = null;

  function setupMirror() {
    const armature = findBone('CC_Base_BoneRoot').parent;
    const list = [];
    armature.traverse((o) => { if (o.isBone) list.push(o); });
    const byName = new Map(list.map((b) => [b.name, b]));
    const saved = list.map((b) => [b, b.quaternion.clone(), b.position.clone()]);
    for (const b of list) {
      b.quaternion.copy(restPose.get(b.name));
      b.position.copy(restPosition.get(b.name));
    }
    armature.updateMatrixWorld(true);
    const armInv = armature.matrixWorld.clone().invert();
    const rest = new Map();
    const m = new THREE.Matrix4();
    for (const b of list) rest.set(b.name, new THREE.Quaternion().setFromRotationMatrix(m.multiplyMatrices(armInv, b.matrixWorld)));
    for (const [b, q, pos] of saved) {
      b.quaternion.copy(q);
      b.position.copy(pos);
    }
    return { armature, list, byName, rest };
  }

  function mirrorClip(clip) {
    mirrorRig ||= setupMirror();
    const { armature, list, byName, rest } = mirrorRig;
    const reference = clip.tracks.filter((t) => t.name.endsWith('.quaternion')).reduce((a, t) => (t.times.length > a.times.length ? t : a));
    const times = Array.from(reference.times);
    const tracked = new Set(clip.tracks.map((t) => t.name.slice(0, t.name.lastIndexOf('.'))));
    const sampler = new THREE.AnimationMixer(model);
    const action = sampler.clipAction(clip).play();
    const out = new Map();
    const add = (name, size) => {
      if (!out.has(name)) out.set(name, { size, values: [] });
      return out.get(name).values;
    };
    const m = new THREE.Matrix4();
    const world = new Map();
    const mirrored = new Map();
    const pos = new Map();
    for (const t of times) {
      action.time = t;
      sampler.update(0);
      armature.updateMatrixWorld(true);
      const armInv = armature.matrixWorld.clone().invert();
      for (const b of list) {
        m.multiplyMatrices(armInv, b.matrixWorld);
        world.set(b.name, new THREE.Quaternion().setFromRotationMatrix(m));
        pos.set(b.name, new THREE.Vector3().setFromMatrixPosition(m));
      }
      for (const b of list) {
        const partner = byName.has(mirrorName(b.name)) ? mirrorName(b.name) : b.name;
        const delta = world.get(b.name).clone().multiply(rest.get(b.name).clone().invert());
        mirrored.set(partner, mirrorQuat(delta).multiply(rest.get(partner)));
      }
      for (const b of list) {
        if (!tracked.has(b.name)) continue;
        const parentWorld = b.parent.isBone ? mirrored.get(b.parent.name) : new THREE.Quaternion();
        const local = parentWorld.clone().invert().multiply(mirrored.get(b.name));
        add(`${b.name}.quaternion`, 4).push(local.x, local.y, local.z, local.w);
      }
      for (const name of ['CC_Base_BoneRoot', 'CC_Base_Hip']) {
        if (!clip.tracks.some((tr) => tr.name === `${name}.position`)) continue;
        const b = byName.get(name);
        const p = pos.get(name).clone();
        p.x = -p.x;
        let local = p;
        if (b.parent.isBone) {
          const pp = pos.get(b.parent.name).clone();
          pp.x = -pp.x;
          local = p.sub(pp).applyQuaternion(mirrored.get(b.parent.name).clone().invert());
        }
        add(`${name}.position`, 3).push(local.x, local.y, local.z);
      }
    }
    sampler.stopAllAction();
    sampler.uncacheRoot(model);
    const tracks = [...out].map(([name, { size, values }]) => (size === 4 ? new THREE.QuaternionKeyframeTrack(name, times, values) : new THREE.VectorKeyframeTrack(name, times, values)));
    return new THREE.AnimationClip(`${clip.name} (mirrored)`, clip.duration, tracks);
  }

  const presets = new Map();
  const clipEntries = [];
  for (const [name, entry] of Object.entries(config.clips)) {
    clipEntries.push([name, entry]);
    const heelsFile = heelsFiles.get(entry.file);
    if (heelsFile) {
      const named = entry.clip !== 'longest' && rawClips.has(`${heelsFile}::${entry.clip}`);
      clipEntries.push([`${name}${heelSuffix}`, { ...entry, file: heelsFile, clip: named ? entry.clip : 'longest', heelsOf: name, groundFeet: false, groundFeetFrom: entry.groundFeet ? name : null }]);
    }
  }
  for (const [name, entry] of clipEntries) {
    const key = entry.clip === 'longest' ? longestClipKey(entry.file) : `${entry.file}::${entry.clip}`;
    if (entry.clip === 'longest' && key && rawClips.get(key).duration < 1) log(`"${name}": the longest clip in ${entry.file} is only ${rawClips.get(key).duration.toFixed(2)} s. The export may be missing its animation.`, true);
    let raw = rawClips.get(key);
    const refSource = entry.alignTo ? alignedSource(entry.alignTo) : null;
    if (raw && refSource) raw = normalizeClip(sourceClips.get(key), hipOrigin(refSource));
    if (!raw) {
      log(`Missing clip for "${name}": ${entry.clip} in ${entry.file}`, true);
      continue;
    }
    if (entry.mirror) raw = mirrorClip(raw);
    let clip = entry.hold ? holdFrame(raw, entry.hold) : raw;
    if (entry.anchorFeet) clip = anchorFeet(clip);
    if (entry.loopFix) clip = fixLoop(clip, name);
    if (entry.groundFeet) clip = groundFeet(clip);
    if (entry.groundFeetFrom) clip = groundFeet(clip, presets.get(entry.groundFeetFrom)?.clip.userData?.groundLift ?? 0);
    let rootMotion = null;
    if (entry.rootMotion) ({ clip, motion: rootMotion } = extractRootMotion(clip));
    const travel = entry.travel ? computeTravel(clip, entry.loop) : null;
    presets.set(name, { ...entry, clip, travel, rootMotion });
  }
  const heelsClips = new Set([...presets.values()].filter((p) => p.heelsOf).map((p) => p.clip));
  if (heelsClips.size) log(`High-heel versions: ${[...presets.values()].filter((p) => p.heelsOf).map((p) => p.heelsOf).join(', ')}.`);
  const footBones = (config.feet || []).map((n) => findBone(n)).filter(Boolean);
  function feetMidNow() {
    if (footBones.length < 2) return null;
    model.updateMatrixWorld(true);
    const mid = new THREE.Vector3();
    for (const b of footBones) mid.add(model.worldToLocal(b.getWorldPosition(new THREE.Vector3())));
    return mid.divideScalar(footBones.length);
  }
  const startFeet = new Map();
  {
    const saved = [];
    model.traverse((o) => { if (o.isBone) saved.push([o, o.quaternion.clone(), o.position.clone()]); });
    for (const p of presets.values()) {
      const sampler = new THREE.AnimationMixer(model);
      const action = sampler.clipAction(p.clip).play();
      action.time = 0;
      sampler.update(0);
      startFeet.set(p.clip, feetMidNow());
      sampler.stopAllAction();
      sampler.uncacheRoot(model);
    }
    for (const [b, q, pos] of saved) {
      b.quaternion.copy(q);
      b.position.copy(pos);
    }
  }

  Object.assign(ctx, { hipOrigin, normalizeClip, baseName, rawClips, sourceClips, alignedSource, mirrorClip, presets, heelsClips, footBones, feetMidNow, startFeet });
}
