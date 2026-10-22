// File: ui/room/main.js

import { createAvatar } from './avatar.js';
import { createPanel } from './panel.js';

const avatar = createAvatar();
createPanel(avatar, document);
await avatar.init(document.getElementById('stage'));
window.avatar = avatar;
