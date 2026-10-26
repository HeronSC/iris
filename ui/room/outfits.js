// File: ui/room/outfits.js

import * as THREE from 'three';

export async function createOutfits(ctx) {
  const { config, loader, log, emit, model, findBone, canonicalBone, parts, addPart, applyMaterialRules, startsAny, rig } = ctx;

  function attachOutfit(gltf, label) {
    const armature = findBone('CC_Base_BoneRoot')?.parent;
    const pieces = [];
    gltf.scene.traverse((o) => { if (o.isSkinnedMesh) pieces.push(o); });
    const attached = [];
    for (const mesh of pieces) {
      const skeletonBones = mesh.skeleton.bones.map((b) => findBone(canonicalBone(b.name)));
      const missing = mesh.skeleton.bones.filter((b, i) => !skeletonBones[i]).map((b) => b.name);
      if (missing.length) {
        log(`${label}: ${mesh.name} uses bones Iris does not have: ${missing.slice(0, 5).join(', ')}`, true);
        continue;
      }
      const bindMatrix = mesh.bindMatrix.clone();
      const inverses = mesh.skeleton.boneInverses.map((m) => m.clone());
      mesh.removeFromParent();
      mesh.position.set(0, 0, 0);
      mesh.quaternion.identity();
      mesh.scale.set(1, 1, 1);
      armature.add(mesh);
      mesh.bind(new THREE.Skeleton(skeletonBones, inverses), bindMatrix);
      mesh.visible = false;
      attached.push(mesh);
    }
    return attached;
  }

  const outfitCatalog = new Map();
  const titleCase = (key) => key.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
  for (const [key, o] of Object.entries(config.outfits || {})) {
    outfitCatalog.set(key, { key, label: o.label || titleCase(key), file: o.file || null, parts: o.parts || null, everyday: o.everyday !== false, meshes: null, loading: null });
  }
  for (const { key, label, file, everyday } of ctx.library.outfits) {
    if (!outfitCatalog.has(key)) outfitCatalog.set(key, { key, label, file, parts: null, everyday, meshes: null, loading: null });
  }

  const slots = {
    feet: new RegExp(config.outfitSlots?.feet || 'shoe|boot|heel|sandal|sneaker', 'i'),
    bottoms: new RegExp(config.outfitSlots?.bottoms || 'underwear_bottom|panties|panty|brief', 'i'),
  };
  const ownClothing = config.clothing || [];
  const outfitState = { current: null, requested: null };

  function ensureOutfitLoaded(entry) {
    if (!entry.file || entry.meshes) return Promise.resolve(entry.meshes);
    entry.loading ||= loader.loadAsync(encodeURI(entry.file)).then((gltf) => {
      entry.meshes = attachOutfit(gltf, entry.label);
      for (const mesh of entry.meshes) {
        applyMaterialRules(mesh);
        addPart(`${entry.label}: ${mesh.name}`, mesh);
      }
      if (!entry.meshes.length) log(`${entry.label}: no clothing found in the file.`, true);
      return entry.meshes;
    });
    return entry.loading;
  }

  function findOutfit(text) {
    const want = text.toLowerCase().replace(/[^a-z0-9]+/g, '_');
    return outfitCatalog.get(want) || [...outfitCatalog.values()].find((e) => e.key.startsWith(want) || e.label.toLowerCase().startsWith(text.toLowerCase()));
  }

  const heel = { angle: 0, toe: 0, lift: 0, bones: [] };

  function bindSpaceVertices(mesh) {
    const pos = mesh.geometry.attributes.position;
    const si = mesh.geometry.attributes.skinIndex;
    const sw = mesh.geometry.attributes.skinWeight;
    const out = [];
    const v = new THREE.Vector3();
    for (let i = 0; i < pos.count; i++) {
      v.fromBufferAttribute(pos, i).applyMatrix4(mesh.bindMatrix);
      let best = 0;
      for (let k = 1; k < 4; k++) if (sw.getComponent(i, k) > sw.getComponent(i, best)) best = k;
      out.push({ p: v.clone(), bone: mesh.skeleton.bones[si.getComponent(i, best)].name });
    }
    return out;
  }

  function bindPosition(mesh, name) {
    const i = mesh.skeleton.bones.findIndex((b) => b.name === name);
    return i < 0 ? null : new THREE.Vector3().setFromMatrixPosition(mesh.skeleton.boneInverses[i].clone().invert());
  }

  function heelFromSkeleton(shoe, body) {
    const at = (mesh, name) => bindPosition(mesh, name);
    const pitch = (a, b) => Math.atan2(a.y - b.y, b.z - a.z);
    const sides = [];
    for (const side of ['L', 'R']) {
      const names = [`CC_Base_${side}_Foot`, `CC_Base_${side}_ToeBase`, `CC_Base_${side}_MidToe1`];
      const s = names.map((n) => at(shoe, n));
      const b = names.map((n) => at(body, n));
      if (s.some((v) => !v) || b.some((v) => !v)) continue;
      const foot = pitch(s[0], s[1]) - pitch(b[0], b[1]);
      const toe = pitch(s[1], s[2]) - pitch(b[1], b[2]);
      sides.push({ foot, toe: toe - foot, lift: s[0].y - b[0].y });
    }
    if (!sides.length) return null;
    const avg = (k) => sides.reduce((a, v) => a + v[k], 0) / sides.length;
    if (avg('foot') < THREE.MathUtils.degToRad(config.minHeelAngle ?? 8) || avg('lift') < 0.01) return null;
    return { lift: avg('lift'), angle: avg('foot'), toe: avg('toe') };
  }

  function updateHeelPose(entry) {
    heel.angle = 0;
    heel.toe = 0;
    heel.lift = 0;
    model.position.y = 0;
    const shoes = (entry.meshes || []).filter((m) => m.visible && m.isSkinnedMesh && slots.feet.test(m.name));
    if (!shoes.length) return;
    let body = null;
    model.traverse((o) => { if (o.isSkinnedMesh && /Std_Skin_Leg/.test([].concat(o.material)[0].name)) body = o; });
    if (!body) return;
    const solved = heelFromSkeleton(shoes[0], body);
    if (!solved) {
      const relLow = (mesh, filter) => {
        const ankle = bindPosition(mesh, 'CC_Base_L_Foot');
        const ys = bindSpaceVertices(mesh).filter((v) => filter(v.bone)).map((v) => v.p.y);
        return ankle && ys.length ? Math.min(...ys) - ankle.y : null;
      };
      const footBones = (b) => /^CC_Base_[LR]_(Foot|ToeBase|.*Toe1)/.test(b);
      const shoeLow = Math.min(...shoes.map((m) => relLow(m, footBones)).filter((v) => v !== null));
      const bodyLow = relLow(body, footBones);
      const sole = bodyLow !== null && Number.isFinite(shoeLow) ? bodyLow - shoeLow : 0;
      if (sole > 0.005) {
        heel.lift = sole;
        model.position.y = sole;
        log(`${entry.label}: shoe soles ${(sole * 100).toFixed(1)} cm thick, lifted to stand on them.`);
      }
      return;
    }
    Object.assign(heel, solved);
    model.position.y = heel.lift;
    log(`${entry.label}: high heels, lifted ${(heel.lift * 100).toFixed(1)} cm, feet tipped ${THREE.MathUtils.radToDeg(heel.angle).toFixed(0)}°, toes ${THREE.MathUtils.radToDeg(heel.angle + heel.toe).toFixed(0)}°.`);
  }

  function blendWeight(action) {
    if (!action.enabled || !ctx.mixer._isActiveAction(action)) return 0;
    if (ctx.actionStarted.get(action) === ctx.clock.frame) return 0;
    return action.getEffectiveWeight();
  }

  const floorContact = { points: [], lift: 0 };
  function updateFloorContacts() {
    floorContact.points = [];
    const shoes = [];
    for (const part of parts.values()) {
      if (!part.visible) continue;
      part.traverse?.((o) => { if (o.isSkinnedMesh && slots.feet.test(o.name)) shoes.push(o); });
      if (part.isSkinnedMesh && slots.feet.test(part.name) && !shoes.includes(part)) shoes.push(part);
    }
    const v = new THREE.Vector3();
    for (const mesh of shoes) {
      const pos = mesh.geometry.attributes.position;
      const ys = new Float32Array(pos.count);
      let minY = Infinity;
      for (let i = 0; i < pos.count; i++) {
        ys[i] = v.fromBufferAttribute(pos, i).applyMatrix4(mesh.bindMatrix).y;
        minY = Math.min(minY, ys[i]);
      }
      const band = [];
      for (let i = 0; i < pos.count; i++) if (ys[i] < minY + 0.03) band.push(i);
      const step = Math.max(1, Math.floor(band.length / 120));
      for (let k = 0; k < band.length; k += step) floorContact.points.push({ mesh, index: band[k] });
    }
  }

  function applyFloorContact(dt) {
    if (!floorContact.points.length || config.floorContact === false) {
      floorContact.lift = 0;
      return;
    }
    model.updateMatrixWorld(true);
    const v = new THREE.Vector3();
    let lowest = Infinity;
    for (const { mesh, index } of floorContact.points) {
      mesh.getVertexPosition(index, v);
      mesh.localToWorld(v);
      lowest = Math.min(lowest, v.y - rig.position.y);
    }
    const target = Math.max(0, -lowest + 0.002);
    if (target > floorContact.lift) floorContact.lift = target;
    else floorContact.lift += (target - floorContact.lift) * Math.min(1, dt * 4);
    model.position.y += floorContact.lift;
  }

  function heelsRecordingWeight() {
    let w = 0;
    let total = 0;
    for (const action of ctx.mixer._actions) {
      const weight = blendWeight(action);
      total += weight;
      if (ctx.heelsClips.has(action.getClip())) w += weight;
    }
    return total > 0 ? Math.min(1, w / total) : 0;
  }

  function applyHeelPose(axes) {
    const auto = 1 - heelsRecordingWeight();
    model.position.y = heel.lift * auto;
    if (!heel.angle || !axes || auto <= 0) return;
    if (!heel.bones.length) {
      heel.bones = ['L', 'R'].map((side) => ({ foot: findBone(`CC_Base_${side}_Foot`), toe: findBone(`CC_Base_${side}_ToeBase`) }));
      for (const { foot, toe } of heel.bones) for (const b of [foot, toe]) if (b && !ctx.baseTransforms.has(b)) ctx.baseTransforms.set(b, { q: b.quaternion.clone(), p: b.position.clone() });
    }
    for (const { foot, toe } of heel.bones) {
      ctx.rotateWorld(foot, axes.lateral, heel.angle * auto);
      ctx.rotateWorld(toe, axes.lateral, heel.toe * auto);
    }
  }

  async function setOutfit(key) {
    const entry = outfitCatalog.get(key) || findOutfit(key);
    if (!entry) return log(`No outfit "${key}". Outfits: ${[...outfitCatalog.values()].map((e) => e.label).join(', ')}`, true);
    outfitState.requested = entry.key;
    emit('outfit', entry.key);
    if (entry.file && !entry.meshes) {
      log(`Loading ${entry.label}...`);
      try {
        await ensureOutfitLoaded(entry);
      } catch (err) {
        return log(`Could not load ${entry.label}: ${err.message || err}`, true);
      }
      if (outfitState.requested !== entry.key) return;
    }
    const shown = new Set(entry.file ? entry.meshes : [...parts.entries()].filter(([n]) => startsAny(n, entry.parts || [])).map(([, p]) => p));
    const own = [...parts.entries()].filter(([n]) => startsAny(n, ownClothing)).map(([, p]) => p);
    const clothing = new Set(own);
    for (const e of outfitCatalog.values()) for (const m of e.meshes || []) clothing.add(m);
    const covers = (slot) => [...shown].some((p) => slot.test(p.name));
    for (const piece of clothing) {
      let on = shown.has(piece);
      if (!on && own.includes(piece)) {
        if (slots.feet.test(piece.name) && !covers(slots.feet)) on = true;
        if (slots.bottoms.test(piece.name) && !covers(slots.bottoms)) on = true;
      }
      piece.visible = on;
    }
    emit('partsChanged');
    outfitState.current = entry.key;
    updateHeelPose(entry);
    updateFloorContacts();
    try {
      localStorage.setItem('iris.outfit', entry.key);
    } catch {}
  }

  let savedOutfit = null;
  try {
    savedOutfit = localStorage.getItem('iris.outfit');
  } catch {}
  const startOutfit = savedOutfit && outfitCatalog.has(savedOutfit) ? savedOutfit : config.outfit;

  Object.assign(ctx, { outfitCatalog, outfitState, setOutfit, findOutfit, heel, floorContact, updateFloorContacts, applyFloorContact, applyHeelPose, heelsRecordingWeight, blendWeight });
  if (startOutfit) await setOutfit(startOutfit);
}
