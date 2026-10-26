// File: ui/room/doors.js

import * as THREE from 'three';

export function createDoors(ctx) {
  const { config, rooms, gltfs, rig, walk, log } = ctx;
  const settings = { openAt: 2.2, closeAt: 2.6, slideSeconds: 0.8, ...(config.doors || {}) };
  const folder = (config.doorFolder || 'assets/Motion/Doors').toLowerCase();
  const up = new THREE.Vector3(0, 1, 0);
  const clipFor = new Map();
  for (const [file, gltf] of gltfs) {
    if (!file.toLowerCase().startsWith(`${folder}/`)) continue;
    for (const clip of gltf.animations || []) {
      const targets = new Set(clip.tracks.map((track) => track.name.split(/[.[]/)[0].replace(/_\d+$/, '')));
      if (clip.duration < 0.05) {
        log(`Door clip ${file.split('/').pop()} holds a single frame, so ${[...targets].join(', ')} slides open on its own instead.`, true);
        continue;
      }
      for (const target of targets) clipFor.set(target, { clip, file });
    }
  }

  const doors = [];
  for (const record of rooms.values()) {
    for (const name of record.config.passable || []) {
      const object = record.scene.getObjectByName(name);
      if (!object) continue;
      let meshes = 0;
      object.traverse((child) => { if (child.isMesh) meshes += 1; });
      if (!meshes) continue;
      const box = new THREE.Box3().setFromObject(object);
      const size = box.getSize(new THREE.Vector3());
      const axis = size.x >= size.z ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 0, 1);
      const rotation = THREE.MathUtils.degToRad(record.config.rotation || 0);
      const door = { name, room: record.name, object, center: box.getCenter(new THREE.Vector3()), open: false, offset: 0, rest: object.position.clone(), axis: axis.applyAxisAngle(up, -rotation), slide: Math.max(size.x, size.z) * 0.95, mixer: null, action: null, duration: 0 };
      const entry = clipFor.get(name);
      if (entry) {
        const clip = entry.clip.clone();
        for (const track of clip.tracks) track.name = track.name.replace(/^[^.[]+/, name);
        door.mixer = new THREE.AnimationMixer(object);
        door.action = door.mixer.clipAction(clip);
        door.action.setLoop(THREE.LoopOnce, 1);
        door.action.clampWhenFinished = true;
        door.duration = clip.duration;
        log(`Door ${name} in the ${record.name}: ${entry.file.split('/').pop()}, ${clip.duration.toFixed(2)} s.`);
      }
      doors.push(door);
    }
  }

  function setOpen(door, open) {
    if (door.open === open) return;
    door.open = open;
    if (!door.action) return;
    const action = door.action;
    const atStart = action.time <= 0;
    const atEnd = action.time >= door.duration;
    if (open && (atStart || atEnd)) action.time = 0;
    if (!open && (atStart || atEnd)) action.time = door.duration;
    action.timeScale = open ? 1 : -1;
    action.enabled = true;
    action.paused = false;
    action.play();
  }

  const near = (point, door, reach) => Boolean(point) && Math.hypot(point.x - door.center.x, point.z - door.center.z) < reach;

  function onHerWay(door) {
    if (!walk.target) return false;
    if (near(walk.target, door, 1.2) || walk.waypoints.some((point) => near(point, door, 1.2))) return true;
    return near(rig.position, door, 1.2);
  }

  function updateDoors(dt) {
    for (const door of doors) {
      const distance = Math.hypot(rig.position.x - door.center.x, rig.position.z - door.center.z);
      if (!door.open && distance < settings.openAt && onHerWay(door)) setOpen(door, true);
      else if (door.open && (distance > settings.closeAt || (!walk.target && distance > 1.0))) setOpen(door, false);
      if (door.mixer) {
        door.mixer.update(dt);
        continue;
      }
      const want = door.open ? 1 : 0;
      if (door.offset === want) continue;
      door.offset = THREE.MathUtils.clamp(door.offset + (door.open ? dt : -dt) / settings.slideSeconds, 0, 1);
      door.object.position.copy(door.rest).addScaledVector(door.axis, door.offset * door.slide);
    }
  }

  if (doors.length) log(`Doors: ${doors.map((door) => `${door.name}${door.action ? '' : ' (slide)'}`).join(', ')}.`);
  Object.assign(ctx, { doors, updateDoors, setDoorOpen: setOpen });
}
