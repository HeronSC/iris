// File: ui/room/library.js

export async function loadLibrary(ctx) {
  const { config, log } = ctx;
  const assetRoot = config.assetRoot || 'assets';
  let files = [];
  try {
    const manifest = await (await fetch(`${assetRoot}/manifest.json`, { cache: 'no-store' })).json();
    files = (manifest.files || []).map((f) => `${assetRoot}/${f}`);
  } catch (err) {
    log(`Could not read ${assetRoot}/manifest.json: ${err.message || err}`, true);
  }
  const stem = (file) => file.split('/').pop().replace(/\.glb$/i, '');
  const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '');
  const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
  const under = (folder) => files.filter((f) => f.toLowerCase().startsWith(`${folder.toLowerCase()}/`));
  const lowerFiles = new Set(files.map((f) => f.toLowerCase()));

  const outfitEntry = (file, everyday) => ({ key: slug(stem(file)), label: stem(file), file, everyday });
  const everydayFolder = config.outfitFolder || `${assetRoot}/Clothes/Outfits`;
  const outfits = [
    ...under(everydayFolder).map((file) => outfitEntry(file, true)),
    ...(config.extraOutfitFolders || []).flatMap((folder) => under(folder).map((file) => outfitEntry(file, false))),
  ].sort((a, b) => a.label.localeCompare(b.label));

  const motionFolder = config.motionFolder || `${assetRoot}/Motion`;
  const byNorm = new Map(under(motionFolder).map((f) => [norm(stem(f)), f]));
  const idles = config.idles || {};
  const flags = idles.flags || {};
  const clips = config.clips ||= {};
  const activities = config.activities ||= {};
  const define = (name, entry) => {
    clips[name] = { ...entry, ...(clips[name] || {}) };
    return clips[name];
  };
  const homeFile = byNorm.get('home');
  if (homeFile) define('home', { file: homeFile, clip: 'longest', hold: 'start', loop: true });
  const pool = [];
  const variants = idles.mirror ? [['', {}], ['_b', { mirror: true }]] : [['', {}]];
  for (const file of under(idles.folder || `${motionFolder}/Idle`)) {
    const n = norm(stem(file));
    if (n === 'home' || n.startsWith('hometo') || n.endsWith('tohome')) continue;
    const base = slug(stem(file));
    const enter = byNorm.get(`hometo${n}`);
    const exit = byNorm.get(`${n}tohome`);
    const loopEntry = define(base, { file, clip: 'longest', loop: true, ...flags });
    const enterEntry = enter ? define(`home_to_${base}`, { file: enter, clip: 'longest', loop: false, next: base, ...flags }) : null;
    const exitEntry = exit ? define(`${base}_to_home`, { file: exit, clip: 'longest', loop: false, next: 'home', ...flags }) : null;
    if (exitEntry) define('home', { file: exitEntry.file, clip: exitEntry.clip, hold: 'end', loop: true });
    for (const [suffix, extra] of variants) {
      const key = `${base}${suffix}`;
      if (suffix) {
        define(key, { ...loopEntry, ...extra });
        if (enterEntry) define(`home_to_${key}`, { ...enterEntry, next: key, ...extra });
        if (exitEntry) define(`${key}_to_home`, { ...exitEntry, ...extra });
      }
      if (!activities[key]) activities[key] = { loop: key, ...(enterEntry ? { enter: `home_to_${key}` } : {}), ...(exitEntry ? { exit: `${key}_to_home` } : {}) };
      pool.push(key);
    }
  }
  config.idleLife ||= {};
  config.idleLife.pool ||= pool;
  if (!clips.home) log('No home pose: add Motion/Idle/Home.glb or an "<idle> To Home" clip.', true);

  const heelSuffix = config.heelsSuffix || '-HighHeels';
  const heelsFiles = new Map();
  for (const file of new Set(Object.values(clips).map((c) => c.file))) {
    const candidate = file.replace(/\.glb$/i, `${heelSuffix}.glb`);
    if (lowerFiles.has(candidate.toLowerCase())) heelsFiles.set(file, candidate);
  }

  if (pool.length) log(`Idles found: ${pool.join(', ')}.`);
  const doorFiles = under(config.doorFolder || `${assetRoot}/Motion/Doors`);
  ctx.library = { files, outfits, pool, heelSuffix, heelsFiles, doorFiles };
}
