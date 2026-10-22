// File: ui/room/loop.js

import * as THREE from 'three';

export function createLoop(ctx) {
  const { clock, baseTransforms, rig, mixer, view, camera, controls, keyLight, keyOffset, faceAxes, walk, faceState, bodyShape, explorer, face, expressions, boneDrivers, resolveMorph, rotateWorld, renderer, scene, motion, emit } = ctx;
  const timer = new THREE.Timer();
  let fpsFrames = 0;
  let fpsTime = 0;
  let fps = 0;
  const _up = new THREE.Vector3();

  function frame() {
    clock.frame += 1;
    timer.update();
    const dt = Math.min(timer.getDelta(), 0.1);
    for (const [bone, base] of baseTransforms) {
      bone.quaternion.copy(base.q);
      bone.position.copy(base.p);
    }
    const before = rig.position.clone();
    ctx.updateIdleGraph(dt);
    mixer.update(dt);
    for (const [bone, base] of baseTransforms) {
      base.q.copy(bone.quaternion);
      base.p.copy(bone.position);
    }
    ctx.smoothHead(dt);
    ctx.updateLocomotion(dt);
    ctx.updatePlan(dt);
    ctx.autoIdle(dt);
    if (view.mode === 'follow') {
      const moved = rig.position.clone().sub(before);
      camera.position.add(moved);
      controls.target.add(moved);
    } else if (view.mode === 'room') {
      ctx.trackWithRoomCamera(dt);
    } else if (view.mode === 'voice') {
      ctx.trackVoiceCamera(dt);
    }
    keyLight.position.copy(rig.position).add(keyOffset);
    keyLight.target.position.copy(rig.position);
    rig.updateMatrixWorld(true);

    let axes = faceAxes();
    if (ctx.checkFacing(axes)) axes = faceAxes();
    ctx.applyHeelPose(axes);
    ctx.updateLook(dt, axes);
    axes = faceAxes();

    const mouth = Math.max(faceState.manualMouth, ctx.talkValue(dt), ctx.audioValue(dt), ctx.speechValue(dt));
    const morphs = new Map(bodyShape);
    for (const [name, w] of explorer) morphs.set(name, (morphs.get(name) || 0) + w);
    const b = Math.max(ctx.blinkWeight(dt), ctx.eyelidWeight(dt));
    for (const name of face.blink || []) morphs.set(name, (morphs.get(name) || 0) + b);
    for (const e of expressions.values()) {
      e.value += (e.target - e.value) * Math.min(1, dt * 6);
      if (e.value < 0.001) continue;
      for (const [name, w] of Object.entries(e.shape)) morphs.set(name, (morphs.get(name) || 0) + e.value * w);
    }
    for (const [name, scale] of Object.entries(face.mouthMorphs || {})) morphs.set(name, (morphs.get(name) || 0) + mouth * scale);
    ctx.applyGestures(dt, axes, morphs);
    axes = faceAxes();
    ctx.applyIdleLife(dt, axes, morphs);
    axes = faceAxes();

    const resolved = ctx.resolveFrame(morphs);
    if (axes) {
      const faceUp = _up.crossVectors(axes.forward, axes.lateral).normalize();
      for (const d of boneDrivers) {
        let degrees = 0;
        for (const [name, k] of Object.entries(d.morphs)) degrees += Math.min(1, Math.max(0, resolved.get(resolveMorph(name)) || 0)) * k;
        if (degrees) rotateWorld(d.object, d.axis === 'up' ? faceUp : axes.lateral, THREE.MathUtils.degToRad(degrees));
      }
    }
    ctx.applyMorphs(resolved);
    ctx.applyFloorContact(dt);

    controls.update();
    renderer.render(scene, camera);

    fpsFrames += 1;
    fpsTime += dt;
    if (fpsTime >= 0.5) {
      fps = fpsFrames / fpsTime;
      fpsFrames = 0;
      fpsTime = 0;
    }
    const a = motion.current;
    const clipInfo = a ? `${motion.name}  ${a.time.toFixed(2)} / ${a.getClip().duration.toFixed(2)}s${a.loop === THREE.LoopOnce ? '  once' : '  loop'}` : 'bind pose';
    const steps = ctx.describePlan();
    const queued = (motion.queue.length ? `\nqueue: ${motion.queue.join(' → ')}` : motion.next ? `\nthen: ${motion.next}` : '') + (steps.length ? `\nplan: ${steps.join(' → ')}` : '');
    const where = `pos ${rig.position.x.toFixed(2)}, ${rig.position.z.toFixed(2)} m   steps ${walk.steps}${walk.paces !== null ? ` / ${walk.paces}` : ''}${walk.target ? `   to ${walk.target.x.toFixed(1)}, ${walk.target.z.toFixed(1)}` : ''}${walk.halt ? '   halting' : ''}`;
    emit('hud', `${clipInfo}${queued}\n${where}\nmouth ${mouth.toFixed(2)}   ${fps.toFixed(0)} fps`);
  }

  function startLoop() {
    renderer.setAnimationLoop(frame);
  }

  Object.assign(ctx, { frame, startLoop });
}
