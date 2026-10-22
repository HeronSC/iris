// File: ui/room/idle_life.js

import * as THREE from 'three';

export function createIdleLife(ctx) {
  const { config, motion, activityOf, look, flow, walk, activities, request, camera, breathing, ensureBones, rotateWorld, gestureDefs, activeGestures, playGesture, bones, emit } = ctx;
  const lifeConfig = { on: true, pool: [], switchSeconds: [25, 50], glanceSeconds: [2.5, 6], holdSeconds: [1, 3], cameraChance: 0.45, fidgetSeconds: [8, 16], fidgets: [], headShare: 0.55, headSpeed: 2.6, eyeSpeed: 14, ...(config.idleLife || {}) };
  const life = { on: lifeConfig.on, listening: false, settled: 0, switchIn: 0, glanceIn: 1.5, holdLeft: 0, fidgetIn: 0, target: { yaw: 0, pitch: 0, camera: false }, eye: { yaw: 0, pitch: 0 }, head: { yaw: 0, pitch: 0, roll: 0 } };
  const between = ([a, b]) => a + Math.random() * (b - a);
  life.switchIn = between(lifeConfig.switchSeconds);
  life.fidgetIn = between(lifeConfig.fidgetSeconds);

  function restingAct() {
    const { act, phase } = activityOf(motion.name);
    return phase === 'loop' && (lifeConfig.rest || ['sit', 'lie']).includes(act) ? act : null;
  }

  function lifeActive() {
    if (!life.on || look.on || flow.target || walk.target) return false;
    const { act, phase } = activityOf(motion.name);
    return phase === 'loop' && (act === 'home' || lifeConfig.pool.includes(act) || (lifeConfig.rest || ['sit', 'lie']).includes(act));
  }

  function pickGlance() {
    const cameraChance = life.listening ? 0.9 : lifeConfig.cameraChance;
    const roll = Math.random();
    if (roll < cameraChance) life.target = { yaw: 0, pitch: 0, camera: true };
    else if (roll < cameraChance + (1 - cameraChance) * 0.7) life.target = { yaw: (Math.random() < 0.5 ? -1 : 1) * (12 + Math.random() * 23), pitch: -4 + Math.random() * 14, camera: false };
    else life.target = { yaw: -10 + Math.random() * 20, pitch: 12 + Math.random() * 8, camera: false };
    life.holdLeft = between(lifeConfig.holdSeconds) * (life.listening ? 1.6 : 1);
  }

  function cameraAngles(axes) {
    const d = camera.position.clone().sub(axes.center);
    const side = d.dot(axes.lateral);
    const ahead = d.dot(axes.forward);
    const up = new THREE.Vector3().crossVectors(axes.forward, axes.lateral).normalize();
    const yaw = THREE.MathUtils.radToDeg(Math.atan2(side, Math.max(0.05, ahead)));
    const pitch = -THREE.MathUtils.radToDeg(Math.atan2(d.dot(up), Math.hypot(side, ahead)));
    return { yaw: THREE.MathUtils.clamp(yaw, -40, 40), pitch: THREE.MathUtils.clamp(pitch, -20, 20), visible: ahead > 0 };
  }

  function applyIdleLife(dt, axes, morphs) {
    const active = lifeActive();
    let want = { yaw: 0, pitch: 0 };
    let roll = 0;
    if (active && axes) {
      life.settled += dt;
      const { act } = activityOf(motion.name);
      if (lifeConfig.pool.includes(act) && lifeConfig.pool.length > 1 && !(ctx.graphConfig.on && ctx.idleGraph.frames.length)) {
        life.switchIn -= dt;
        if (life.switchIn <= 0) {
          life.switchIn = between(lifeConfig.switchSeconds);
          const others = lifeConfig.pool.filter((n) => n !== act && activities[n]);
          if (others.length) request(others[Math.floor(Math.random() * others.length)]);
        }
      }
      life.glanceIn -= dt;
      if (life.glanceIn <= 0) {
        pickGlance();
        life.glanceIn = life.holdLeft + between(lifeConfig.glanceSeconds);
      }
      life.holdLeft -= dt;
      if (life.holdLeft > 0) {
        if (life.target.camera) {
          const c = cameraAngles(axes);
          want = c.visible ? c : { yaw: 0, pitch: 0 };
        } else want = life.target;
        if (restingAct() === 'lie') want = { yaw: THREE.MathUtils.clamp(want.yaw, -20, 20) * 0.6, pitch: THREE.MathUtils.clamp(want.pitch, -10, 10) * 0.5 };
      }
      if (life.listening) roll = 3;
      if ((ctx.idleGraphActive() || restingAct()) && ctx.graphConfig.swaySpineDegrees) {
        breathing.bones ||= ensureBones(['CC_Base_Spine02', 'CC_Base_L_Clavicle', 'CC_Base_R_Clavicle']);
        const spine = breathing.bones[0];
        const t = ctx.idleGraph.clock;
        const amp = THREE.MathUtils.degToRad(ctx.graphConfig.swaySpineDegrees);
        if (spine) {
          rotateWorld(spine, axes.forward, amp * Math.sin(t * 0.37 + 0.6) * Math.sin(t * 0.11));
          rotateWorld(spine, axes.lateral, amp * 0.6 * Math.sin(t * 0.23 + 2.1));
        }
      }
      life.fidgetIn -= dt;
      if (life.fidgetIn <= 0) {
        life.fidgetIn = between(lifeConfig.fidgetSeconds) * (life.listening ? 2 : 1);
        const options = restingAct() === 'lie' ? [] : lifeConfig.fidgets.filter((n) => gestureDefs[n]);
        if (options.length && !activeGestures.length) playGesture(options[Math.floor(Math.random() * options.length)]);
      }
    } else {
      life.settled = 0;
    }
    const ke = Math.min(1, dt * lifeConfig.eyeSpeed);
    const kh = Math.min(1, dt * lifeConfig.headSpeed);
    life.eye.yaw += (want.yaw - life.eye.yaw) * ke;
    life.eye.pitch += (want.pitch - life.eye.pitch) * ke;
    life.head.yaw += (want.yaw * lifeConfig.headShare - life.head.yaw) * kh;
    life.head.pitch += (want.pitch * lifeConfig.headShare - life.head.pitch) * kh;
    life.head.roll += (roll - life.head.roll) * kh;
    const eyeYaw = life.eye.yaw - life.head.yaw;
    const eyePitch = life.eye.pitch - life.head.pitch;
    const addMorph = (name, w) => { if (w > 0.001) morphs.set(name, (morphs.get(name) || 0) + Math.min(1, w)); };
    addMorph('Eye_L_Look_L', eyeYaw / 30);
    addMorph('Eye_R_Look_L', eyeYaw / 30);
    addMorph('Eye_L_Look_R', -eyeYaw / 30);
    addMorph('Eye_R_Look_R', -eyeYaw / 30);
    addMorph('Eye_L_Look_Down', eyePitch / 25);
    addMorph('Eye_R_Look_Down', eyePitch / 25);
    addMorph('Eye_L_Look_Up', -eyePitch / 20);
    addMorph('Eye_R_Look_Up', -eyePitch / 20);
    if (!axes) return;
    const { yaw, pitch } = life.head;
    if (Math.abs(yaw) + Math.abs(pitch) + Math.abs(life.head.roll) < 0.01) return;
    const deg = THREE.MathUtils.degToRad;
    const faceUp = new THREE.Vector3().crossVectors(axes.forward, axes.lateral).normalize();
    for (const [bone, share] of [[bones.neck, 0.4], [bones.head, 0.6]]) {
      rotateWorld(bone, faceUp, deg(yaw * share));
      rotateWorld(bone, axes.lateral, deg(pitch * share));
      rotateWorld(bone, axes.forward, deg(life.head.roll * share));
    }
  }

  const setLife = (on) => {
    life.on = on;
    emit('life', on);
  };
  const setListening = (on) => {
    life.listening = on;
    life.glanceIn = 0;
    emit('listening', on);
  };

  Object.assign(ctx, { lifeConfig, life, between, restingAct, lifeActive, applyIdleLife, setLife, setListening });
}
