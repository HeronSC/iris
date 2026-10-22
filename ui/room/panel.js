// File: ui/room/panel.js

export function createPanel(avatar, doc) {
  const $ = (id) => doc.getElementById(id);
  const titleCase = (name) => name.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
  const on = avatar.on;

  on('log', ({ message, isError }) => {
    const box = $('log');
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    const line = doc.createElement('div');
    if (isError) line.className = 'err';
    line.textContent = message;
    box.append(line);
    if (atBottom) box.scrollTop = box.scrollHeight;
  });
  $('clearLog').onclick = () => { $('log').replaceChildren(); };
  on('hud', (text) => { $('hud').textContent = text; });

  on('progress', ({ loaded, total }) => {
    $('loadFill').style.width = total ? `${(100 * loaded) / total}%` : '0';
    $('loadLabel').textContent = `Loading ${(loaded / 1e6).toFixed(0)} / ${(total / 1e6).toFixed(0)} MB`;
  });
  on('loadError', (message) => { $('loadLabel').textContent = message; });

  const partBoxes = new Map();
  on('part', ({ name, part }) => {
    const label = doc.createElement('label');
    const box = doc.createElement('input');
    box.type = 'checkbox';
    box.checked = part.visible;
    partBoxes.set(name, box);
    box.onchange = () => { part.visible = box.checked; };
    label.append(box, name);
    label.title = name;
    $('parts').append(label);
  });
  on('partsChanged', () => {
    for (const [name, part] of avatar.parts) {
      const box = partBoxes.get(name);
      if (box) box.checked = part.visible;
    }
  });

  on('outfit', (key) => { $('outfitSelect').value = key; });
  $('outfitSelect').onchange = () => avatar.setOutfit($('outfitSelect').value);

  const toggleClass = (id, onState) => $(id).classList.toggle('on', onState);
  on('voice', (onState) => {
    toggleClass('voiceToggle', onState);
    toggleClass('motion_work', onState);
  });
  on('life', (onState) => toggleClass('lifeToggle', onState));
  on('listening', (onState) => toggleClass('listenToggle', onState));
  on('look', (onState) => toggleClass('lookToggle', onState));
  on('talk', (onState) => toggleClass('talkToggle', onState));
  on('loopRaw', (onState) => toggleClass('loopToggle', onState));
  on('roomVisible', (onState) => toggleClass('roomToggle', onState));
  on('mapVisible', (onState) => toggleClass('mapToggle', onState));
  on('cameraMode', (mode) => { for (const m of ['orbit', 'follow', 'room']) toggleClass(`cam_${m}`, m === mode); });

  function bindSlider(id, event, onInput, digits = 2) {
    const input = $(id);
    const out = $(`${id}Out`);
    const show = (value) => {
      input.value = value;
      out.textContent = Number(value).toFixed(digits);
    };
    input.addEventListener('input', () => onInput(Number(input.value)));
    if (event) on(event, (value) => show(value));
    show(input.value);
    return show;
  }

  function addSliderRow(container, name, value, onInput) {
    const row = doc.createElement('div');
    row.className = 'row';
    const label = doc.createElement('label');
    label.textContent = name;
    const input = doc.createElement('input');
    input.type = 'range';
    input.min = '0';
    input.max = '1';
    input.step = '0.01';
    const out = doc.createElement('output');
    const show = (v) => {
      input.value = v;
      out.textContent = Number(v).toFixed(2);
    };
    show(value);
    input.addEventListener('input', () => {
      show(input.value);
      onInput(Number(input.value));
    });
    row.append(label, input, out);
    container.append(row);
    return show;
  }

  function addButton(container, label, onClick, id = null) {
    const button = doc.createElement('button');
    button.textContent = label;
    if (id) button.id = id;
    button.onclick = onClick;
    container.append(button);
    return button;
  }

  const expressionRows = new Map();
  on('expression', ({ name, value }) => expressionRows.get(name)?.(value));
  const bodyRows = new Map();
  on('bodyMorph', ({ name, value }) => bodyRows.get(name)?.(value));

  on('ready', () => {
    $('loading').remove();
    const { activities, room, outfitCatalog, rawClips, morphNames, morphSet, bodyShape, gestureDefs, expressions, lighting, motion, config } = avatar;

    for (const entry of outfitCatalog.values()) {
      const option = doc.createElement('option');
      option.value = entry.key;
      option.textContent = entry.label;
      $('outfitSelect').append(option);
    }
    if (avatar.outfitState.current) $('outfitSelect').value = avatar.outfitState.current;

    const motionButtons = [
      ['Home', avatar.returnHome],
      ['Work', () => avatar.setVoice(!avatar.voice.on)],
      ['Idle', () => avatar.request('idle')],
      ['Halt', avatar.goHome],
      ['Close-up', avatar.closeUp],
      ...Object.keys(activities).filter((key) => !avatar.isWalkLoop(activities[key].loop)).map((key) => [titleCase(key), () => avatar.request(key)]),
    ];
    for (const [label, action] of motionButtons) addButton($('presets'), label, action, `motion_${label.toLowerCase().replace(/\W+/g, '_')}`);
    toggleClass('motion_work', avatar.voice.on);

    for (const [key, clip] of rawClips) {
      const [file, clipName] = key.split('::');
      const option = doc.createElement('option');
      option.value = key;
      option.textContent = `${avatar.baseName(file)} › ${clipName} (${clip.duration.toFixed(2)}s)`;
      $('rawClips').append(option);
    }
    $('playRaw').onclick = () => {
      const key = $('rawClips').value;
      motion.queue = [];
      const clip = $('mirrorRaw').classList.contains('on') ? avatar.mirrorClip(rawClips.get(key)) : rawClips.get(key);
      avatar.startClip(key.split('::')[1], clip, { loop: motion.loopRaw });
    };
    $('mirrorRaw').onclick = () => $('mirrorRaw').classList.toggle('on');
    $('loopToggle').onclick = () => avatar.setLoopRaw(!motion.loopRaw);
    toggleClass('loopToggle', motion.loopRaw);
    $('stopAll').onclick = avatar.stopAll;
    $('resetPos').onclick = avatar.resetPosition;

    if (room.scene) {
      for (const name of room.spots.keys()) addButton($('spots'), titleCase(name), () => avatar.goToSpot(name));
      $('roomToggle').onclick = () => avatar.setRoomVisible(!room.scene.visible);
      $('mapToggle').onclick = () => avatar.setMapVisible(!avatar.navView.group.visible);
      toggleClass('roomToggle', room.scene.visible);
    } else {
      $('roomSection').hidden = true;
    }

    for (const m of ['orbit', 'follow', 'room']) $(`cam_${m}`).onclick = () => avatar.setCameraMode(m);
    if (!room.camera) $('cam_room').hidden = true;
    for (const m of ['orbit', 'follow', 'room']) toggleClass(`cam_${m}`, m === avatar.view.mode);

    bindSlider('speed', 'speed', avatar.setSpeed);
    bindSlider('fade', 'fade', avatar.setFade);
    for (const name of ['exposure', 'environment', 'key', 'rim', 'fill']) {
      $(name).value = lighting[name];
      bindSlider(name, null, (v) => avatar.setLighting(name, v));
    }
    on('lighting', ({ name, value }) => { $(`${name}Out`).textContent = Number(value).toFixed(2); });
    bindSlider('mouth', 'mouth', avatar.setMouth);

    $('lookToggle').onclick = () => avatar.setLook(!avatar.look.on);
    $('talkToggle').onclick = () => avatar.setTalk(!avatar.talk.on);
    $('lifeToggle').onclick = () => avatar.setLife(!avatar.life.on);
    $('listenToggle').onclick = () => avatar.setListening(!avatar.life.listening);
    $('voiceToggle').onclick = () => avatar.setVoice(!avatar.voice.on);
    toggleClass('lifeToggle', avatar.life.on);

    $('audioFile').onchange = (e) => {
      const file = e.target.files[0];
      if (file) avatar.playAudioFile(file);
    };

    for (const name of morphNames) {
      const option = doc.createElement('option');
      option.value = name;
      option.textContent = name;
      $('morphName').append(option);
    }
    const showMorphWeight = bindSlider('morphWeight', null, (v) => {
      const name = $('morphName').value;
      if (name) avatar.setExplorerMorph(name, v);
    });
    $('morphName').onchange = () => showMorphWeight(avatar.explorer.get($('morphName').value) || 0);
    $('morphReset').onclick = () => {
      avatar.clearExplorer();
      showMorphWeight(0);
    };

    for (const [name, value] of bodyShape) {
      if (!morphSet.has(name)) continue;
      bodyRows.set(name, addSliderRow($('bodyMorphs'), name, value, (v) => avatar.setBodyMorph(name, v)));
    }

    for (const name of Object.keys(gestureDefs)) addButton($('gestures'), titleCase(name), () => avatar.playGesture(name));

    for (const name of expressions.keys()) {
      expressionRows.set(name, addSliderRow($('expressions'), name, 0, (v) => avatar.setExpression(name, v)));
    }

    const history = [];
    let historyIndex = 0;
    $('cmd').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const value = $('cmd').value;
        if (value.trim()) history.push(value);
        historyIndex = history.length;
        $('cmd').value = '';
        avatar.runCommand(value);
      } else if (e.key === 'ArrowUp' && history.length) {
        historyIndex = Math.max(0, historyIndex - 1);
        $('cmd').value = history[historyIndex];
        e.preventDefault();
      } else if (e.key === 'ArrowDown' && history.length) {
        historyIndex = Math.min(history.length, historyIndex + 1);
        $('cmd').value = history[historyIndex] || '';
        e.preventDefault();
      }
    });
    void config;
  });
}
