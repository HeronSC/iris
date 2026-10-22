// File: ui/room/loading.js

import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

export async function loadAssets(ctx) {
  const { config, emit } = ctx;
  const loader = new GLTFLoader();
  const progress = new Map();

  function showProgress() {
    let loaded = 0;
    let total = 0;
    for (const p of progress.values()) {
      loaded += p.loaded;
      total += p.total;
    }
    emit('progress', { loaded, total });
  }

  function loadFile(url) {
    progress.set(url, { loaded: 0, total: 0 });
    return loader.loadAsync(encodeURI(url), (e) => {
      progress.set(url, { loaded: e.loaded, total: e.total || e.loaded });
      showProgress();
    });
  }

  const heelSuffix = config.heelsSuffix || '-HighHeels';
  const heelsFiles = new Map();
  await Promise.all([...new Set(Object.values(config.clips).map((c) => c.file))].map(async (file) => {
    const candidate = file.replace(/\.glb$/i, `${heelSuffix}.glb`);
    try {
      const res = await fetch(encodeURI(candidate), { method: 'HEAD', cache: 'no-store' });
      if (res.ok) heelsFiles.set(file, candidate);
    } catch {}
  }));
  const files = [...new Set([config.character, ...(config.room ? [config.room.file] : []), ...Object.values(config.clips).map((c) => c.file), ...heelsFiles.values(), ...(config.extraFiles || [])])];
  const gltfs = new Map();
  try {
    const loaded = await Promise.all(files.map(loadFile));
    files.forEach((f, i) => gltfs.set(f, loaded[i]));
  } catch (err) {
    emit('loadError', `Load failed: ${err.message || err}`);
    throw err;
  }
  Object.assign(ctx, { loader, gltfs, heelsFiles, heelSuffix, files });
}
