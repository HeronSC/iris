// File: ui/room/room.js

import * as THREE from 'three';

export function loadRoom(ctx) {
  const { config, gltfs, scene, grid, emit } = ctx;
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
    room.bounds = new THREE.Box3().setFromObject(room.scene);
    const noShadow = new RegExp(config.room.noShadowFrom || 'Wall|Ceiling|Door|Window|Trim|Sill|Floor|Plane|Shell', 'i');
    room.scene.traverse((o) => { if (o.isMesh && noShadow.test(o.name)) o.castShadow = false; });
    const hidden = config.room.hide || [];
    room.scene.traverse((o) => { if (hidden.some((h) => o.name.startsWith(h))) o.visible = false; });
  }

  function setRoomVisible(on) {
    if (!room.scene) return;
    room.scene.visible = on;
    grid.visible = !on;
    emit('roomVisible', on);
  }

  Object.assign(ctx, { room, setRoomVisible });
}

export function placeSpots(ctx) {
  const { config, room, log, rig, findBone, alignedSource, hipOrigin } = ctx;
  const furniture = new Map();
  for (const [name, f] of Object.entries(config.furniture || {})) {
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
  const home = placeSpot('Home', config.homeSpot);
  const work = placeSpot('Work', config.workSpot || config.homeSpot);
  if (!furniture.has('home')) furniture.set('home', { entry: new THREE.Vector3(home.x, 0, home.z), facing: THREE.MathUtils.degToRad(home.facing) });
  rig.position.set(home.x, 0, home.z);
  rig.rotation.y = THREE.MathUtils.degToRad(home.facing);

  Object.assign(ctx, { furniture, home, work });
}
