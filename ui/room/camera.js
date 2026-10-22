// File: ui/room/camera.js

import * as THREE from 'three';

export function createCameraModes(ctx) {
  const { config, camera, controls, room, rig, bones, walk, motion, activityOf, lifeConfig, gestureDefs, playGesture, setListening, walkToSpot, distanceTo, home, work, log, emit } = ctx;

  const view = { away: false, neutralAim: null, neutralFov: null, mode: 'orbit', baseFov: camera.fov, aim: new THREE.Vector3(), roomPos: new THREE.Vector3(), frameHeight: config.room?.frameHeight ?? 2.3, voiceShot: config.room?.voiceShot || null, closeup: false };

  function aimPoint(out) {
    if (view.closeup && bones.head) {
      bones.head.getWorldPosition(out);
      out.y += config.closeup?.aimAboveHead ?? 0.06;
      return out;
    }
    return out.set(rig.position.x, rig.position.y + (config.room?.aimHeight ?? 1.0), rig.position.z);
  }

  function readRoomCamera() {
    const shot = config.room?.camera || {};
    room.camera.updateMatrixWorld(true);
    if (shot.position) view.roomPos.fromArray(shot.position);
    else room.camera.getWorldPosition(view.roomPos);
    if (shot.lookAt) view.neutralAim = new THREE.Vector3().fromArray(shot.lookAt);
    else {
      const look = new THREE.Vector3(0, 0, -1).applyQuaternion(room.camera.getWorldQuaternion(new THREE.Quaternion()));
      view.neutralAim = view.roomPos.clone().add(look.multiplyScalar(5));
    }
    view.neutralFov = shot.fov || room.camera.fov || camera.fov;
  }

  function setCameraMode(mode) {
    if (mode === 'room' && !room.camera) return log('The room file has no camera.', true);
    const fixed = ['room', 'voice'];
    const leavingFixed = fixed.includes(view.mode) && !fixed.includes(mode);
    view.mode = mode;
    controls.enabled = !fixed.includes(mode);
    if (mode === 'voice') view.aim.copy(controls.target);
    if (mode === 'room') {
      readRoomCamera();
      camera.position.copy(view.roomPos);
      aimPoint(view.aim);
      controls.target.copy(view.aim);
      camera.lookAt(view.aim);
      trackWithRoomCamera(1);
    } else if (leavingFixed) {
      camera.fov = view.baseFov;
      camera.updateProjectionMatrix();
    }
    if (mode === 'follow') aimPoint(controls.target);
    emit('cameraMode', mode);
  }

  function insideRoom(position, margin) {
    if (!room.bounds) return true;
    return position.x > room.bounds.min.x + margin && position.x < room.bounds.max.x - margin
      && position.z > room.bounds.min.z + margin && position.z < room.bounds.max.z - margin;
  }

  const shotPosition = new THREE.Vector3();
  const shotAim = new THREE.Vector3();
  const shotFrame = { boxes: null, inside: null };
  const _corner = new THREE.Vector3();
  const _chase = new THREE.Vector3();

  function shotBoxes(names) {
    if (!shotFrame.boxes) {
      shotFrame.boxes = names.map((n) => room.scene?.getObjectByName(n)).filter(Boolean).map((o) => new THREE.Box3().setFromObject(o));
      if (shotFrame.boxes.length < names.length) log(`Voice shot: some of ${names.join(', ')} are missing from the room`, true);
    }
    return shotFrame.boxes;
  }

  function roomInside() {
    if (!shotFrame.inside) {
      const box = new THREE.Box3();
      room.scene?.traverse((o) => { if (o.isMesh && /Wall/i.test(o.name)) box.expandByObject(o); });
      shotFrame.inside = box.isEmpty() ? null : box.expandByVector(new THREE.Vector3(-0.5, 0, -0.5));
    }
    return shotFrame.inside;
  }

  function framedShot(shot) {
    if (shot.position && shot.lookAt) {
      shotPosition.fromArray(shot.position);
      shotAim.fromArray(shot.lookAt);
      return true;
    }
    const boxes = shotBoxes(shot.frame || []);
    if (!boxes.length) return false;
    const all = boxes.reduce((sum, b) => sum.union(b), new THREE.Box3());
    const yaw = THREE.MathUtils.degToRad(shot.facing || 0);
    const dir = new THREE.Vector3(-Math.sin(yaw), 0, -Math.cos(yaw));
    const right = new THREE.Vector3(Math.cos(yaw), 0, -Math.sin(yaw));
    const center = all.getCenter(new THREE.Vector3());
    const middle = boxes[0].getCenter(new THREE.Vector3()).sub(center).dot(right);
    center.addScaledVector(right, middle);
    const tanV = Math.tan(THREE.MathUtils.degToRad(shot.fov ?? view.baseFov) / 2);
    const tanH = tanV * camera.aspect;
    const grow = 1 + (shot.margin ?? 0.05);
    let distance = 0;
    for (let i = 0; i < 8; i++) {
      _corner.set(i & 1 ? all.max.x : all.min.x, i & 2 ? all.max.y : all.min.y, i & 4 ? all.max.z : all.min.z).sub(center);
      const near = -_corner.dot(dir);
      distance = Math.max(distance, near + Math.abs(_corner.dot(right)) * grow / tanH, near + Math.abs(_corner.y) * grow / tanV);
    }
    shotPosition.copy(center).addScaledVector(dir, -distance);
    shotAim.copy(center);
    return true;
  }

  function trackVoiceCamera(dt) {
    const shot = view.voiceShot;
    const k = 1 - Math.exp(-dt * (shot?.speed ?? 2));
    const her = aimPoint(new THREE.Vector3());
    const framed = shot && framedShot(shot);
    if (framed) {
      _chase.set(rig.position.x - work.x, 0, rig.position.z - work.z).add(shotPosition);
      const inside = roomInside();
      if (inside) _chase.clamp(inside.min, inside.max);
    } else {
      _chase.copy(camera.position);
    }
    if (voice.on) {
      camera.position.lerp(_chase, k);
      view.aim.lerp(framed ? shotAim.lerp(her, THREE.MathUtils.clamp(distanceTo(work) / 2, 0, 1)) : her, k);
      aimCameraFov(shot?.fov ?? view.baseFov, k);
      return;
    }
    const going = walk.target || walk.arrive;
    const toRoom = voice.cameraBefore === 'room' && room.camera;
    const along = going ? THREE.MathUtils.clamp(distanceTo(home) / 3, 0, 1) : 0;
    if (toRoom) _chase.lerp(view.roomPos, 1 - along);
    camera.position.lerp(_chase, k);
    view.aim.lerp(her, k);
    aimCameraFov(toRoom ? THREE.MathUtils.lerp(view.neutralFov, shot?.fov ?? view.baseFov, along) : view.baseFov, k);
    if (!going && (!toRoom || camera.position.distanceTo(view.roomPos) < 0.05)) setCameraMode(voice.cameraBefore || 'follow');
  }

  function aimCameraFov(fov, k) {
    camera.fov += (fov - camera.fov) * k;
    camera.updateProjectionMatrix();
    controls.target.copy(view.aim);
  }

  const voice = { on: false, cameraBefore: null };
  function pinned(what) {
    if (!voice.on) return false;
    log(`Voice is on, so she stays at her spot (${what} ignored). "voice off" frees her.`, true);
    return true;
  }

  function setVoice(on) {
    if (on === voice.on) return;
    voice.on = on;
    emit('voice', on);
    if (on) {
      if (view.mode !== 'voice') voice.cameraBefore = view.mode;
      const { act } = activityOf(motion.name);
      const settled = (act === 'home' || lifeConfig.pool.includes(act)) && !walk.target && distanceTo(work) < 0.3;
      if (!settled) walkToSpot(work);
      if (gestureDefs.nod) playGesture('nod');
      setCameraMode('voice');
      setListening(true);
      log('Voice on: she walks to her work spot and stays there.');
    } else {
      setListening(false);
      if (room.camera) readRoomCamera();
      walkToSpot(home);
      log('Voice off: she walks back home and the camera returns.');
    }
  }

  function trackWithRoomCamera(dt) {
    const k = 1 - Math.exp(-dt * 4);
    if (config.room?.neutralWhenAway !== false && view.neutralAim) {
      if (!view.away && !insideRoom(rig.position, 0)) {
        view.away = true;
        log('Iris left the room. Camera back to neutral.');
      } else if (view.away && insideRoom(rig.position, 0.25)) {
        view.away = false;
        log('Iris is back. Camera following.');
      }
    }
    view.aim.lerp(view.away ? view.neutralAim : aimPoint(new THREE.Vector3()), k);
    camera.position.copy(view.roomPos);
    controls.target.copy(view.aim);
    const distance = camera.position.distanceTo(view.aim);
    const frameHeight = view.closeup ? config.closeup?.frameHeight ?? 0.6 : view.frameHeight;
    const tracked = THREE.MathUtils.clamp(THREE.MathUtils.radToDeg(2 * Math.atan(frameHeight / 2 / distance)), 10, 60);
    const fov = view.away || !(config.room?.zoom || view.closeup) ? view.neutralFov : tracked;
    camera.fov += (fov - camera.fov) * k;
    camera.updateProjectionMatrix();
  }

  function captureVoiceShot() {
    const round = (v) => v.toArray().map((n) => Number(n.toFixed(2)));
    view.voiceShot = { ...view.voiceShot, position: round(camera.position), lookAt: round(controls.target), fov: Number(camera.fov.toFixed(1)) };
    return view.voiceShot;
  }

  Object.assign(ctx, { view, aimPoint, readRoomCamera, setCameraMode, insideRoom, trackVoiceCamera, trackWithRoomCamera, voice, pinned, setVoice, captureVoiceShot });
}
