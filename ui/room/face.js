// File: ui/room/face.js

import * as THREE from 'three';

export function createFace(ctx) {
  const { config, findBone, log, emit, renderer, camera, presets, motion, morphSet, resolveMorph } = ctx;
  const face = config.face || {};
  const bones = {};
  for (const key of ['jawBone', 'eyeLeft', 'eyeRight', 'neck', 'head']) {
    const bone = face[key] ? findBone(face[key]) : null;
    if (!bone) log(`Bone not found for ${key}: ${face[key]}`, true);
    bones[key] = bone;
  }
  const boneDrivers = (face.boneDrivers || []).map((d) => {
    const bone = findBone(d.bone);
    if (!bone) log(`Bone not found for driver: ${d.bone}`, true);
    return { ...d, object: bone };
  }).filter((d) => d.object);
  const baseTransforms = new Map([...Object.values(bones), ...boneDrivers.map((d) => d.object)].filter(Boolean).map((b) => [b, { q: b.quaternion.clone(), p: b.position.clone() }]));

  const _q = new THREE.Quaternion();
  const _pw = new THREE.Quaternion();
  const _bw = new THREE.Quaternion();
  function rotateWorld(bone, axis, angle) {
    if (!bone || !angle) return;
    bone.getWorldQuaternion(_bw);
    bone.parent.getWorldQuaternion(_pw);
    _q.setFromAxisAngle(axis, angle).multiply(_bw);
    bone.quaternion.copy(_pw.invert().multiply(_q));
    bone.updateMatrixWorld(true);
  }

  const _eyeL = new THREE.Vector3();
  const _eyeR = new THREE.Vector3();
  const up = new THREE.Vector3(0, 1, 0);
  function faceAxes() {
    if (!bones.eyeLeft || !bones.eyeRight) return null;
    bones.eyeLeft.getWorldPosition(_eyeL);
    bones.eyeRight.getWorldPosition(_eyeR);
    const lateral = _eyeL.clone().sub(_eyeR).normalize();
    const forward = lateral.clone().cross(up).normalize();
    const center = _eyeL.clone().add(_eyeR).multiplyScalar(0.5);
    return { lateral, forward, center };
  }

  const gestureDefs = config.gestures || {};
  const activeGestures = [];
  const breathing = { on: config.breathing?.on ?? true, t: 0, bones: null };

  function curveAt(keys, t, column = 1) {
    if (!keys || !keys.length) return 0;
    if (t <= keys[0][0]) return keys[0][column];
    for (let i = 1; i < keys.length; i++) {
      if (t <= keys[i][0]) {
        const a = keys[i - 1];
        const b = keys[i];
        const f = (t - a[0]) / (b[0] - a[0]);
        const e = 0.5 - 0.5 * Math.cos(Math.PI * f);
        return a[column] + (b[column] - a[column]) * e;
      }
    }
    return keys[keys.length - 1][column];
  }

  function gestureLength(def) {
    const ends = [def.head, def.shoulders, ...Object.values(def.morphs || {})].filter(Boolean).map((k) => k[k.length - 1][0]);
    return Math.max(0, ...ends);
  }

  function playGesture(name) {
    const def = gestureDefs[name];
    if (!def) return log(`No gesture "${name}". Gestures: ${Object.keys(gestureDefs).join(', ')}`, true);
    for (let i = activeGestures.length - 1; i >= 0; i--) if (activeGestures[i].name === name) activeGestures.splice(i, 1);
    activeGestures.push({ name, def, t: 0, length: gestureLength(def) });
  }

  function ensureBones(names) {
    return names.map((n) => {
      const b = findBone(n);
      if (b && !baseTransforms.has(b)) baseTransforms.set(b, { q: b.quaternion.clone(), p: b.position.clone() });
      return b;
    });
  }

  function applyGestures(dt, axes, morphs) {
    const deg = THREE.MathUtils.degToRad;
    let pitch = 0;
    let yaw = 0;
    let roll = 0;
    let shrug = 0;
    for (let i = activeGestures.length - 1; i >= 0; i--) {
      const g = activeGestures[i];
      g.t += dt;
      if (g.t > g.length) {
        activeGestures.splice(i, 1);
        continue;
      }
      if (g.def.head) {
        pitch += curveAt(g.def.head, g.t, 1);
        yaw += curveAt(g.def.head, g.t, 2);
        roll += curveAt(g.def.head, g.t, 3);
      }
      if (g.def.shoulders) shrug += curveAt(g.def.shoulders, g.t, 1);
      for (const [name, keys] of Object.entries(g.def.morphs || {})) morphs.set(name, (morphs.get(name) || 0) + curveAt(keys, g.t, 1));
    }
    let chest = 0;
    if (breathing.on) {
      breathing.t += dt;
      const period = config.breathing?.period ?? 4.2;
      const phase = 0.5 - 0.5 * Math.cos((2 * Math.PI * breathing.t) / period);
      const rest = ctx.restingAct() ? config.breathing?.restMultiplier ?? 1.4 : 1;
      chest = (config.breathing?.chestDegrees ?? 1.2) * phase * rest;
      shrug += (config.breathing?.shoulderDegrees ?? 0.8) * phase * rest;
    }
    if (!axes) return;
    const faceUp = new THREE.Vector3().crossVectors(axes.forward, axes.lateral).normalize();
    if (pitch || yaw || roll) {
      for (const [bone, share] of [[bones.neck, 0.4], [bones.head, 0.6]]) {
        rotateWorld(bone, axes.lateral, deg(pitch * share));
        rotateWorld(bone, faceUp, deg(yaw * share));
        rotateWorld(bone, axes.forward, deg(roll * share));
      }
    }
    if (chest || shrug) {
      breathing.bones ||= ensureBones(['CC_Base_Spine02', 'CC_Base_L_Clavicle', 'CC_Base_R_Clavicle']);
      const [spine, left, right] = breathing.bones;
      if (spine && chest) rotateWorld(spine, axes.lateral, deg(-chest));
      if (left && shrug) rotateWorld(left, axes.forward, deg(shrug));
      if (right && shrug) rotateWorld(right, axes.forward, deg(-shrug));
    }
  }

  const headSmooth = { seconds: config.headSmoothing ?? 0.45, bones: null, state: new Map() };
  function smoothHead(dt) {
    if (!headSmooth.seconds) return;
    headSmooth.bones ||= ['CC_Base_NeckTwist01', 'CC_Base_NeckTwist02', 'CC_Base_Head'].map((n) => findBone(n)).filter(Boolean);
    const k = 1 - Math.exp(-dt / headSmooth.seconds);
    for (const bone of headSmooth.bones) {
      const prev = headSmooth.state.get(bone);
      if (!prev) {
        headSmooth.state.set(bone, bone.quaternion.clone());
        continue;
      }
      prev.slerp(bone.quaternion, k);
      bone.quaternion.copy(prev);
    }
  }

  const blink = { on: true, t: 0, nextAt: 1.5, duration: 0.18, double: false };
  function blinkWeight(dt) {
    if (!blink.on) return 0;
    blink.t += dt;
    if (blink.t < blink.nextAt) return 0;
    const p = (blink.t - blink.nextAt) / blink.duration;
    if (p >= 1) {
      blink.t = 0;
      blink.double = !blink.double && Math.random() < 0.15;
      blink.nextAt = blink.double ? 0.08 : 2 + Math.random() * 4;
      return 0;
    }
    return p < 0.4 ? p / 0.4 : 1 - (p - 0.4) / 0.6;
  }

  const eyelids = { shut: 0 };
  function eyelidWeight(dt) {
    const want = presets.get(motion.name)?.eyesClosed ? 1 : 0;
    eyelids.shut += (want - eyelids.shut) * Math.min(1, dt * (want ? 2 : 8));
    return eyelids.shut;
  }

  const talk = { on: false, t: 0, target: 0, value: 0 };
  function talkValue(dt) {
    if (!talk.on) {
      talk.value += (0 - talk.value) * Math.min(1, dt * 12);
      return talk.value;
    }
    talk.t -= dt;
    if (talk.t <= 0) {
      talk.t = 0.07 + Math.random() * 0.12;
      talk.target = Math.random() < 0.15 ? 0 : 0.25 + Math.random() * 0.75;
    }
    talk.value += (talk.target - talk.value) * Math.min(1, dt * 18);
    return talk.value;
  }

  const audio = { el: null, ctx: null, analyser: null, buffer: null, value: 0 };
  function audioValue(dt) {
    if (!audio.analyser || audio.el.paused) {
      audio.value += (0 - audio.value) * Math.min(1, dt * 12);
      return audio.value;
    }
    audio.analyser.getFloatTimeDomainData(audio.buffer);
    let sum = 0;
    for (const x of audio.buffer) sum += x * x;
    const rms = Math.sqrt(sum / audio.buffer.length);
    const target = Math.min(1, Math.max(0, (rms - 0.01) * 6));
    const rate = target > audio.value ? 25 : 10;
    audio.value += (target - audio.value) * Math.min(1, dt * rate);
    return audio.value;
  }

  function playAudioFile(file) {
    if (!audio.ctx) {
      audio.ctx = new AudioContext();
      audio.el = new Audio();
      const source = audio.ctx.createMediaElementSource(audio.el);
      audio.analyser = audio.ctx.createAnalyser();
      audio.analyser.fftSize = 1024;
      audio.buffer = new Float32Array(audio.analyser.fftSize);
      source.connect(audio.analyser);
      audio.analyser.connect(audio.ctx.destination);
    }
    audio.el.src = URL.createObjectURL(file);
    audio.ctx.resume();
    audio.el.play();
    log(`Playing ${file.name}`);
  }

  const look = { on: false, yaw: 0, pitch: 0, pointer: new THREE.Vector2() };
  renderer.domElement.addEventListener('pointermove', (e) => {
    const r = renderer.domElement.getBoundingClientRect();
    look.pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  });
  const raycaster = new THREE.Raycaster();
  function updateLook(dt, axes) {
    let yaw = 0;
    let pitch = 0;
    if (look.on && axes) {
      raycaster.setFromCamera(look.pointer, camera);
      const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(camera.getWorldDirection(new THREE.Vector3()).negate(), axes.center);
      const point = raycaster.ray.intersectPlane(plane, new THREE.Vector3());
      if (point) {
        const d = point.sub(axes.center);
        const side = d.dot(axes.lateral);
        const ahead = Math.max(0.05, d.dot(axes.forward));
        yaw = THREE.MathUtils.clamp(Math.atan2(side, ahead), -1, 1);
        pitch = THREE.MathUtils.clamp(-Math.atan2(d.y, Math.hypot(side, ahead)), -0.5, 0.5);
      }
    }
    const k = Math.min(1, dt * 6);
    look.yaw += (yaw - look.yaw) * k;
    look.pitch += (pitch - look.pitch) * k;
    if (!axes || (Math.abs(look.yaw) < 1e-4 && Math.abs(look.pitch) < 1e-4)) return;
    rotateWorld(bones.neck, up, look.yaw * 0.4);
    rotateWorld(bones.neck, axes.lateral, look.pitch * 0.4);
    rotateWorld(bones.head, up, look.yaw * 0.6);
    rotateWorld(bones.head, axes.lateral, look.pitch * 0.6);
  }

  const explorer = new Map();
  const bodyShape = new Map(Object.entries(config.bodyMorphs || {}));
  for (const name of bodyShape.keys()) if (!morphSet.has(name)) log(`Body morph "${name}" is not in ${config.character}`, true);
  const expressions = new Map(Object.entries(config.expressions || {}).map(([name, shape]) => [name, { shape, target: 0, value: 0 }]));
  for (const [name, e] of expressions) {
    const missing = Object.keys(e.shape).filter((n) => !morphSet.has(resolveMorph(n)));
    if (missing.length) log(`Expression "${name}" uses missing morphs: ${missing.join(', ')}`, true);
  }
  const faceState = { manualMouth: 0 };

  const setBlink = (on) => { blink.on = on; emit('blink', on); };
  const setLook = (on) => { look.on = on; emit('look', on); };
  const setTalk = (on) => { talk.on = on; emit('talk', on); };
  const setBreathing = (on) => { breathing.on = on; emit('breathing', on); };
  const setMouth = (v) => { faceState.manualMouth = v; emit('mouth', v); };
  const setBodyMorph = (name, v) => { bodyShape.set(name, v); emit('bodyMorph', { name, value: v }); };
  function setExplorerMorph(name, w) {
    if (w > 0) explorer.set(name, w);
    else explorer.delete(name);
    emit('explorer', { name, value: w });
  }
  function clearExplorer() {
    explorer.clear();
    emit('explorer', null);
  }
  function setExpression(name, value) {
    const e = expressions.get(name);
    if (!e) return;
    e.target = value;
    emit('expression', { name, value });
  }

  Object.assign(ctx, { face, bones, boneDrivers, baseTransforms, rotateWorld, faceAxes, up, gestureDefs, activeGestures, breathing, playGesture, ensureBones, applyGestures, headSmooth, smoothHead, blink, blinkWeight, eyelidWeight, talk, talkValue, audio, audioValue, playAudioFile, look, updateLook, explorer, bodyShape, expressions, faceState, setBlink, setLook, setTalk, setBreathing, setMouth, setBodyMorph, setExplorerMorph, clearExplorer, setExpression });
}
