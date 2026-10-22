// File: ui/room/morphs.js

import * as THREE from 'three';

export function createMorphs(ctx) {
  const { config, model, startsAny } = ctx;
  const morphMeshes = [];
  model.traverse((o) => { if (o.isMesh && o.morphTargetDictionary) morphMeshes.push(o); });
  const morphNames = [...new Set(morphMeshes.flatMap((m) => Object.keys(m.morphTargetDictionary)))];

  function solveLinear(a, b) {
    const n = a.length;
    const m = b[0].length;
    for (let col = 0; col < n; col++) {
      let pivot = col;
      for (let r = col + 1; r < n; r++) if (Math.abs(a[r][col]) > Math.abs(a[pivot][col])) pivot = r;
      [a[col], a[pivot]] = [a[pivot], a[col]];
      [b[col], b[pivot]] = [b[pivot], b[col]];
      for (let r = 0; r < n; r++) {
        if (r === col) continue;
        const f = a[r][col] / a[col][col];
        for (let c = col; c < n; c++) a[r][c] -= f * a[col][c];
        for (let c = 0; c < m; c++) b[r][c] -= f * b[col][c];
      }
    }
    return b.map((row, r) => row.map((v) => v / a[r][r]));
  }

  function regionWeights(mesh, boneNames) {
    const ids = boneNames.map((n) => mesh.skeleton.bones.findIndex((b) => b.name === n)).filter((i) => i >= 0);
    const index = mesh.geometry.attributes.skinIndex;
    const weight = mesh.geometry.attributes.skinWeight;
    const out = new Float32Array(index.count);
    for (let v = 0; v < index.count; v++) {
      for (let k = 0; k < 4; k++) {
        if (ids.includes(index.getComponent(v, k))) out[v] += weight.getComponent(v, k);
      }
    }
    return out;
  }

  function isolateMorph(mesh, i, rule, scale = 1) {
    const pos = mesh.geometry.attributes.position;
    const delta = mesh.geometry.morphAttributes.position[i].clone();
    const region = regionWeights(mesh, rule.bones);
    const center = new THREE.Vector3();
    let inside = 0;
    for (let v = 0; v < pos.count; v++) {
      if (region[v] > 0.5) {
        center.x += pos.getX(v);
        center.y += pos.getY(v);
        center.z += pos.getZ(v);
        inside += 1;
      }
    }
    if (!inside) return null;
    center.divideScalar(inside);
    const ata = Array.from({ length: 4 }, () => [0, 0, 0, 0]);
    const atb = Array.from({ length: 4 }, () => [0, 0, 0]);
    const p = new THREE.Vector3();
    for (let v = 0; v < pos.count; v++) {
      p.fromBufferAttribute(pos, v);
      if (region[v] > 0 || p.distanceTo(center) > rule.radius) continue;
      const row = [p.x, p.y, p.z, 1];
      const d = [delta.getX(v), delta.getY(v), delta.getZ(v)];
      for (let r = 0; r < 4; r++) {
        for (let c = 0; c < 4; c++) ata[r][c] += row[r] * row[c];
        for (let c = 0; c < 3; c++) atb[r][c] += row[r] * d[c];
      }
    }
    const fit = solveLinear(ata, atb);
    for (let v = 0; v < pos.count; v++) {
      p.fromBufferAttribute(pos, v);
      const row = [p.x, p.y, p.z, 1];
      const shift = [0, 1, 2].map((c) => row.reduce((sum, x, r) => sum + x * fit[r][c], 0));
      const w = region[v] * scale;
      delta.setXYZ(v, (delta.getX(v) - shift[0]) * w, (delta.getY(v) - shift[1]) * w, (delta.getZ(v) - shift[2]) * w);
    }
    return delta;
  }

  for (const [morph, rule] of Object.entries(config.morphCleanup || {})) {
    for (const m of morphMeshes) {
      const i = m.morphTargetDictionary[morph];
      if (i === undefined) continue;
      const material = [].concat(m.material)[0].name || '';
      const scale = Object.entries(rule.scale || {}).find(([prefix]) => material.startsWith(prefix))?.[1] ?? 1;
      const keep = m.isSkinnedMesh && startsAny(material, rule.meshes) ? isolateMorph(m, i, rule, scale) : null;
      for (const [key, attrs] of Object.entries(m.geometry.morphAttributes)) {
        if (!attrs[i]) continue;
        if (key === 'position' && keep) {
          attrs[i] = keep;
        } else if (key === 'normal' && keep) {
          const region = regionWeights(m, rule.bones);
          const n = attrs[i].clone();
          for (let v = 0; v < n.count; v++) n.setXYZ(v, n.getX(v) * region[v], n.getY(v) * region[v], n.getZ(v) * region[v]);
          attrs[i] = n;
        } else {
          attrs[i] = attrs[i].clone();
          attrs[i].array.fill(0);
        }
      }
    }
  }

  function setMorph(name, weight) {
    for (const m of morphMeshes) {
      const i = m.morphTargetDictionary[name];
      if (i !== undefined) m.morphTargetInfluences[i] = weight;
    }
  }

  const morphSet = new Set(morphNames);
  const morphAliases = config.morphAliases || {};
  function resolveMorph(name) {
    return morphSet.has(name) || !morphAliases[name] ? name : morphAliases[name];
  }

  let lastMorphs = new Map();
  function resolveFrame(requested) {
    const frame = new Map();
    for (const [name, w] of requested) {
      const real = resolveMorph(name);
      frame.set(real, (frame.get(real) || 0) + w);
    }
    return frame;
  }

  function applyMorphs(frame) {
    for (const name of lastMorphs.keys()) if (!frame.has(name)) setMorph(name, 0);
    for (const [name, w] of frame) setMorph(name, Math.min(1, Math.max(0, w)));
    lastMorphs = frame;
  }

  Object.assign(ctx, { morphMeshes, morphNames, morphSet, setMorph, resolveMorph, resolveFrame, applyMorphs });
}
