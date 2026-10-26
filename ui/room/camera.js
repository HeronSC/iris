// File: ui/room/camera.js

import * as THREE from 'three';

export function createCameraModes(ctx) {
  const { config, camera, controls, room, rig, bones, walk, plan, motion, activityOf, lifeConfig, gestureDefs, playGesture, setListening, walkToSpot, distanceTo, insideRoom, home, away, door, log, emit } = ctx;

  const view = { neutralAim: null, neutralFov: null, mode: 'orbit', baseFov: camera.fov, aim: new THREE.Vector3(), roomPos: new THREE.Vector3(), closeup: false, door: 0 };

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
    const leavingRoom = view.mode === 'room' && mode !== 'room';
    view.mode = mode;
    controls.enabled = mode !== 'room';
    if (mode === 'room') {
      readRoomCamera();
      camera.position.copy(view.roomPos);
      view.door = doorGoal();
      roomAim(view.aim);
      controls.target.copy(view.aim);
      camera.lookAt(view.aim);
      trackWithRoomCamera(1);
    } else if (leavingRoom) {
      camera.fov = view.baseFov;
      camera.updateProjectionMatrix();
    }
    if (mode === 'follow') aimPoint(controls.target);
    emit('cameraMode', mode);
  }

  const voice = { on: false };
  function pinned(what) {
    if (!voice.on) return false;
    log(`Work is on, so she stays at her office spot (${what} ignored). "work off" frees her.`, true);
    return true;
  }

  function setVoice(on) {
    if (on === voice.on) return;
    voice.on = on;
    emit('voice', on);
    if (on) {
      if (room.camera) setCameraMode('room');
      const { act } = activityOf(motion.name);
      const settled = (act === 'home' || lifeConfig.pool.includes(act)) && !walk.target && distanceTo(home) < 0.3;
      if (!settled) walkToSpot(home);
      if (gestureDefs.nod) playGesture('nod');
      setListening(true);
      log(settled ? 'Work on: she is at her office spot.' : 'Work on: she comes in and takes her office spot.');
    } else {
      setListening(false);
      if (away) {
        walkToSpot(away);
        log('Work off: she leaves the office.');
      } else log('Work off: she is free to move.');
    }
  }

  const moving = () => Boolean(walk.target || walk.arrive || plan.steps.some((step) => step.type === 'walk' || step.type === 'settle'));

  function walkGoal() {
    if (walk.target) return walk.target;
    const step = plan.steps.find((s) => s.type === 'walk' && s.target);
    return step ? step.target : null;
  }

  function doorGoal() {
    if (!door || !moving()) return 0;
    if (!insideRoom(rig.position)) return 1;
    const goal = walkGoal();
    const leaving = goal && !insideRoom(goal);
    const reach = leaving ? config.room?.doorLeave ?? 3.0 : config.room?.doorWatch ?? 1.0;
    return Math.hypot(rig.position.x - door.x, rig.position.z - door.z) < reach ? 1 : 0;
  }

  const _doorAim = new THREE.Vector3();
  function roomAim(out) {
    if (view.closeup) return aimPoint(out);
    _doorAim.set(door?.x ?? 0, config.room?.aimHeight ?? 1.0, door?.z ?? 0);
    return out.copy(view.neutralAim).lerp(_doorAim, THREE.MathUtils.smoothstep(view.door, 0, 1));
  }

  function trackWithRoomCamera(dt) {
    const goal = doorGoal();
    const step = dt / (config.room?.doorSwing ?? 1.0);
    view.door += THREE.MathUtils.clamp(goal - view.door, -step, step);
    const k = 1 - Math.exp(-dt * (view.closeup ? 4 : 12));
    view.aim.lerp(roomAim(new THREE.Vector3()), k);
    camera.position.copy(view.roomPos);
    controls.target.copy(view.aim);
    const distance = camera.position.distanceTo(view.aim);
    const tracked = THREE.MathUtils.clamp(THREE.MathUtils.radToDeg(2 * Math.atan((config.closeup?.frameHeight ?? 0.6) / 2 / distance)), 10, 60);
    const fov = view.closeup ? tracked : view.neutralFov;
    camera.fov += (fov - camera.fov) * k;
    camera.updateProjectionMatrix();
  }

  Object.assign(ctx, { view, aimPoint, readRoomCamera, setCameraMode, trackWithRoomCamera, voice, pinned, setVoice });
}
