// File: ui/room/room.js

import * as THREE from 'three';

export function loadRoom(ctx) {
  const { config, gltfs, scene, floor: ground, grid, log, emit } = ctx;
  const room = { scene: null, spots: new Map(), camera: null, bounds: null };
  if (config.room) {
    const roomGltf = gltfs.get(config.room.file);
    room.scene = roomGltf.scene;
    room.scene.traverse((o) => {
      if (o.isMesh) {
        o.castShadow = true;
        o.receiveShadow = true;
      }
    });
    scene.add(room.scene);
    room.scene.updateMatrixWorld(true);
    const prefix = config.room.spotPrefix || 'Spot_';
    const suffixes = (config.room.spotSuffixes || []).map((sf) => sf.toLowerCase());
    room.scene.traverse((o) => {
      const lower = o.name.toLowerCase();
      if (o.name.startsWith(prefix)) room.spots.set(o.name.slice(prefix.length).toLowerCase(), o.getWorldPosition(new THREE.Vector3()));
      else if (suffixes.some((sf) => lower.endsWith(sf))) room.spots.set(lower, o.getWorldPosition(new THREE.Vector3()));
      if (o.isCamera && !room.camera) room.camera = o;
    });
    if (!room.camera && config.room.camera?.position) {
      room.camera = new THREE.PerspectiveCamera(config.room.camera.fov || 50);
      room.camera.position.fromArray(config.room.camera.position);
    }
    grid.visible = false;
    ground.visible = false;
    const floor = config.room.floor && room.scene.getObjectByName(config.room.floor);
    if (config.room.floor && !floor) log(`Room floor "${config.room.floor}" is not in the room file.`, true);
    const floorTop = floor ? new THREE.Box3().setFromObject(floor).max.y : 0;
    if (Math.abs(floorTop) > 0.01) {
      log(`Room floor sits at ${floorTop.toFixed(2)} m; lowered the room so she stands on it.`);
      room.scene.position.y -= floorTop;
      room.scene.updateMatrixWorld(true);
    }
    const walls = new RegExp(config.room.walls || 'Wall', 'i');
    room.bounds = new THREE.Box3();
    room.scene.traverse((o) => { if (o.isMesh && walls.test(o.name)) room.bounds.expandByObject(o); });
    if (room.bounds.isEmpty()) {
      log(`No walls matching "${walls.source}" in the room file; using everything for its bounds.`, true);
      room.bounds.setFromObject(room.scene);
    }
    const noShadow = new RegExp(config.room.noShadowFrom || 'Wall|Ceiling|Door|Window|Trim|Sill|Floor|Plane|Shell', 'i');
    room.scene.traverse((o) => { if (o.isMesh && noShadow.test(o.name)) o.castShadow = false; });
    const hidden = config.room.hide || [];
    room.scene.traverse((o) => { if (hidden.some((h) => o.name.startsWith(h))) o.visible = false; });
  }

  function setRoomVisible(on) {
    if (!room.scene) return;
    room.scene.visible = on;
    grid.visible = !on;
    ground.visible = !on;
    emit('roomVisible', on);
  }

  function insideRoom(position, margin = 0) {
    if (!room.bounds) return true;
    return position.x > room.bounds.min.x + margin && position.x < room.bounds.max.x - margin
      && position.z > room.bounds.min.z + margin && position.z < room.bounds.max.z - margin;
  }

  Object.assign(ctx, { room, setRoomVisible, insideRoom });
}

export function placeSpots(ctx) {
  const { config, room, log, rig, findBone, alignedSource, hipOrigin } = ctx;
  const furniture = new Map();
  for (const [name, f] of Object.entries(config.room?.furniture || {})) {
    const anchorObject = room.scene?.getObjectByName(f.anchor);
    const source = alignedSource(f.alignClip);
    const origin = source ? hipOrigin(source) : null;
    if (!anchorObject || !origin) {
      log(`Furniture "${name}": missing ${anchorObject ? 'clip ' + f.alignClip : 'object ' + f.anchor}`, true);
      continue;
    }
    const anchor = anchorObject.getWorldPosition(new THREE.Vector3());
    const armatureScale = findBone('CC_Base_BoneRoot').parent.getWorldScale(new THREE.Vector3());
    const entry = new THREE.Vector3(anchor.x + origin.x * armatureScale.x, 0, anchor.z + origin.z * armatureScale.z);
    furniture.set(name, { entry, facing: THREE.MathUtils.degToRad(f.facing || 0) });
  }
  function placeSpot(label, spot) {
    const placed = { x: 0, z: 0, facing: 0, ...spot };
    const anchor = placed.anchor && room.scene?.getObjectByName(placed.anchor);
    if (placed.anchor && !anchor) log(`${label} spot: missing object ${placed.anchor}`, true);
    if (anchor) {
      const at = anchor.getWorldPosition(new THREE.Vector3());
      placed.x += at.x;
      placed.z += at.z;
    }
    return placed;
  }
  const spots = config.room?.spots || {};
  const home = placeSpot('Home', spots.home);
  const door = spots.door ? placeSpot('Door', spots.door) : null;
  const away = spots.away ? placeSpot('Away', spots.away) : null;
  const closeupSpot = spots.closeup ? placeSpot('Close-up', spots.closeup) : null;
  if (!furniture.has('home')) furniture.set('home', { entry: new THREE.Vector3(home.x, 0, home.z), facing: THREE.MathUtils.degToRad(home.facing) });
  const start = away || home;
  rig.position.set(start.x, 0, start.z);
  rig.rotation.y = THREE.MathUtils.degToRad(start.facing);

  function doorway(from, to) {
    if (!door || ctx.insideRoom(from) === ctx.insideRoom(to)) return null;
    return [{ x: door.x, z: door.z }];
  }

  Object.assign(ctx, { furniture, home, door, away, closeupSpot, doorway });
}
