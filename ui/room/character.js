// File: ui/room/character.js

import * as THREE from 'three';

export function createCharacter(ctx) {
  const { config, gltfs, log, scene, emit } = ctx;
  const model = gltfs.get(config.character).scene;
  let meshCount = 0;
  model.traverse((o) => { if (o.isMesh) meshCount += 1; });
  if (!meshCount) {
    emit('loadError', `${config.character} has no meshes in it. Re-export it from Blender.`);
    throw new Error(`${config.character} is empty`);
  }
  const strays = model.children.filter((child) => {
    let skinned = false;
    child.traverse((o) => { if (o.isSkinnedMesh || o.isBone) skinned = true; });
    return !skinned;
  });
  for (const stray of strays) model.remove(stray);
  const boneAliases = config.boneAliases || {};
  const canonicalBone = (name) => boneAliases[name] || name;
  model.traverse((o) => {
    if (o.isBone && boneAliases[o.name]) {
      log(`Bone "${o.name}" in ${config.character} is treated as ${boneAliases[o.name]}.`);
      o.name = boneAliases[o.name];
    }
  });
  const restPose = new Map();
  const restPosition = new Map();
  const boneIndex = new Map();
  model.traverse((o) => {
    if (!o.isBone) return;
    restPose.set(o.name, o.quaternion.clone());
    restPosition.set(o.name, o.position.clone());
    if (!boneIndex.has(o.name)) boneIndex.set(o.name, o);
  });
  const findBone = (name) => boneIndex.get(name) || null;

  const rig = new THREE.Group();
  rig.add(model);
  scene.add(rig);

  const startsAny = (name, list = []) => list.some((p) => name.startsWith(p));
  const rules = config.materials || {};
  function applyMaterialRules(o) {
    if (!o.isMesh) return;
    o.castShadow = true;
    o.receiveShadow = true;
    o.frustumCulled = false;
    for (const m of [].concat(o.material)) {
      const name = m.name || '';
      if (startsAny(name, rules.opaque)) {
        m.transparent = false;
        m.alphaTest = 0;
        m.depthWrite = true;
      } else if (startsAny(name, rules.mask)) {
        m.transparent = false;
        m.alphaTest = 0.5;
        m.depthWrite = true;
      } else if (startsAny(name, rules.hair)) {
        m.transparent = false;
        m.alphaTest = 0.1;
        m.alphaToCoverage = true;
        m.depthWrite = true;
        m.side = THREE.DoubleSide;
      } else if (startsAny(name, rules.blend)) {
        m.transparent = true;
        m.alphaTest = 0;
        m.depthWrite = false;
        o.renderOrder = 1;
      } else if (startsAny(name, rules.glass)) {
        m.transparent = true;
        m.depthWrite = false;
        o.renderOrder = 2;
      }
      m.needsUpdate = true;
    }
  }
  model.traverse(applyMaterialRules);

  function texturePixels(texture) {
    const img = texture.image;
    const canvas = document.createElement('canvas');
    canvas.width = img.width;
    canvas.height = img.height;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    context.drawImage(img, 0, 0);
    const data = context.getImageData(0, 0, img.width, img.height).data;
    return (u, v) => {
      const x = Math.min(img.width - 1, Math.max(0, Math.floor((u - Math.floor(u)) * img.width)));
      const y = Math.min(img.height - 1, Math.max(0, Math.floor((v - Math.floor(v)) * img.height)));
      const i = (y * img.width + x) * 4;
      return [data[i], data[i + 1], data[i + 2]];
    };
  }

  function matchSkinSeams() {
    const meshes = new Map();
    model.traverse((o) => { if (o.isMesh) meshes.set([].concat(o.material)[0].name.split('.')[0], o); });
    for (const rule of config.seamMatch || []) {
      const part = meshes.get(rule.part);
      const reference = meshes.get(rule.to);
      if (!part || !reference || !part.material.map || !reference.material.map) {
        log(`Seam match: missing ${part ? rule.to : rule.part}`, true);
        continue;
      }
      const key = (x, y, z) => `${Math.round(x * 2000)},${Math.round(y * 2000)},${Math.round(z * 2000)}`;
      const refPos = reference.geometry.attributes.position;
      const refUv = reference.geometry.attributes.uv;
      const lookup = new Map();
      for (let i = 0; i < refPos.count; i++) lookup.set(key(refPos.getX(i), refPos.getY(i), refPos.getZ(i)), i);
      const partPos = part.geometry.attributes.position;
      const partUv = part.geometry.attributes.uv;
      const samplePart = texturePixels(part.material.map);
      const sampleRef = texturePixels(reference.material.map);
      const sumPart = [0, 0, 0];
      const sumRef = [0, 0, 0];
      let shared = 0;
      for (let i = 0; i < partPos.count; i++) {
        const j = lookup.get(key(partPos.getX(i), partPos.getY(i), partPos.getZ(i)));
        if (j === undefined) continue;
        const a = samplePart(partUv.getX(i), partUv.getY(i));
        const b = sampleRef(refUv.getX(j), refUv.getY(j));
        for (let c = 0; c < 3; c++) {
          sumPart[c] += a[c];
          sumRef[c] += b[c];
        }
        shared++;
      }
      if (shared < 10) {
        log(`Seam match: ${rule.part} and ${rule.to} share only ${shared} points`, true);
        continue;
      }
      const toLinear = (v) => Math.pow(v / 255, 2.2);
      const ratio = [0, 1, 2].map((c) => toLinear(sumRef[c] / shared) / toLinear(sumPart[c] / shared));
      part.material.color.setRGB(...ratio);
      log(`Seam match: ${rule.part} tinted (${ratio.map((v) => v.toFixed(2)).join(', ')}) to meet ${rule.to} at ${shared} seam points.`);
    }
  }
  matchSkinSeams();

  const parts = new Map();
  model.traverse((o) => {
    if (!o.isMesh) return;
    const grouped = o.parent && o.parent !== model && !o.parent.isBone && o.parent.children.every((c) => c.isMesh);
    const part = grouped ? o.parent : o;
    parts.set(part.name, part);
  });
  function addPart(name, part) {
    parts.set(name, part);
    emit('part', { name, part });
  }
  function setPartVisible(name, visible) {
    const part = parts.get(name);
    if (part) part.visible = visible;
  }
  for (const [name, part] of [...parts].sort((a, b) => a[0].localeCompare(b[0]))) {
    part.visible = !startsAny(name, config.hiddenParts);
    emit('part', { name, part });
  }

  Object.assign(ctx, { model, rig, boneAliases, canonicalBone, restPose, restPosition, findBone, startsAny, applyMaterialRules, parts, addPart, setPartVisible });
}
