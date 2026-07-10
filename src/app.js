const md = window.markdownit({
  html: false,
  breaks: true,
  linkify: true,
  highlight(str, lang) {
    const langAttr = lang ? ` class="language-${lang}"` : '';
    let highlighted = str;
    if (lang && hljs.getLanguage(lang)) {
      try { highlighted = hljs.highlight(str, { language: lang }).value; } catch {}
    } else {
      highlighted = md.utils.escapeHtml(str);
    }
    return `<div class="code-header"><span>${lang || 'code'}</span><button class="copy-code-btn" onclick="copyCode(this)">\u{1F4CB} \u041A\u043E\u043F\u0438\u0440\u043E\u0432\u0430\u0442\u044C</button></div><pre><code${langAttr}>${highlighted}</code></pre>`;
  }
});

const IS_ELECTRON = navigator.userAgent.toLowerCase().includes('electron') || window.electronAPI?.isElectron;
const DEFAULT_ENDPOINT = IS_ELECTRON ? 'https://artistic-happiness-production.up.railway.app/api/chat' : '/api/chat';
const STORAGE_KEY = 'zeroxai.v3';
const DEFAULT_SETTINGS = {
  profileName: '\u041F\u043E\u043B\u044C\u0437\u043E\u0432\u0430\u0442\u0435\u043B\u044C',
  theme: 'system',
  apiEndpoint: DEFAULT_ENDPOINT,
  modelName: 'openai/gpt-oss-120b',
  apiKey: '',
  thinkingAnim: true,
  typingSpeed: 'normal'
};
const TYPING_SPEEDS = { fast: 20, normal: 35, slow: 60 };

let state = { chats: [], activeChatId: null, settings: { ...DEFAULT_SETTINGS }, busy: false, abort: null };

const $ = id => document.getElementById(id);
const el = {
  sidebar: $('sidebar'),
  overlay: $('sidebarOverlay'),
  chatList: $('chatList'),
  messages: $('messages'),
  composer: $('composer'),
  input: $('messageInput'),
  sendBtn: $('sendBtn'),
  newChatBtn: $('newChatBtn'),
  menuBtn: $('menuBtn'),
  chatTitle: $('chatTitle'),
  chatStatus: $('chatStatus'),
  searchInput: $('searchInput'),
  settingsBtn: $('settingsBtn'),
  topSettingsBtn: $('topSettingsBtn'),
  settingsPanel: $('settingsPanel'),
  closeSettingsBtn: $('closeSettingsBtn'),
  settingsForm: $('settingsForm'),
  profileName: $('profileName'),
  apiEndpoint: $('apiEndpoint'),
  modelName: $('modelName'),
  apiKeyField: $('apiKeyField'),
  thinkingAnim: $('thinkingAnim'),
  typingSpeed: $('typingSpeed'),
  themeBtns: document.querySelectorAll('[data-theme]'),
  thinkingOverlay: $('thinkingOverlay'),
  thinkingVideo: $('thinkingVideo')
};

function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 8); }

function Save() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify({
    chats: state.chats.map(c => ({ ...c, messages: c.messages.map(m => ({ ...m })) })),
    activeChatId: state.activeChatId,
    settings: state.settings
  })); } catch {}
}

function Load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) { createChat(); return; }
    const data = JSON.parse(raw);
    state.settings = { ...DEFAULT_SETTINGS, ...data.settings };
    state.chats = data.chats || [];
    state.activeChatId = data.activeChatId || null;
    if (!state.chats.length) createChat();
    if (!state.chats.find(c => c.id === state.activeChatId)) { state.activeChatId = state.chats[0].id; }
  } catch { createChat(); }
}

function getChat() { return state.chats.find(c => c.id === state.activeChatId); }

function createChat() {
  const chat = { id: uid(), title: '\u041D\u043E\u0432\u044B\u0439 \u0447\u0430\u0442', createdAt: Date.now(), messages: [] };
  state.chats.unshift(chat);
  state.activeChatId = chat.id;
  Save(); render(); return chat;
}

function updateTitle(chat) {
  const m = chat.messages.find(msg => msg.role === 'user');
  chat.title = m ? m.content.slice(0, 50) + (m.content.length > 50 ? '...' : '') : '\u041D\u043E\u0432\u044B\u0439 \u0447\u0430\u0442';
}

function render() {
  applyTheme();
  renderChatList();
  renderMessages();
  fillSettings();
}

function renderChatList() {
  const q = el.searchInput.value.toLowerCase();
  el.chatList.innerHTML = '';
  state.chats.forEach(chat => {
    if (q && !chat.title.toLowerCase().includes(q) && !chat.messages.some(m => m.content.toLowerCase().includes(q))) return;
    const div = document.createElement('div');
    div.className = 'chat-item' + (chat.id === state.activeChatId ? ' active' : '');
    div.innerHTML = `<span class="chat-title-text">${esc(chat.title)}</span>
      <span class="chat-actions">
        <button class="chat-action-btn" data-action="rename" title="\u041F\u0435\u0440\u0435\u0438\u043C\u0435\u043D\u043E\u0432\u0430\u0442\u044C">\u270F\uFE0F</button>
        <button class="chat-action-btn danger" data-action="delete" title="\u0423\u0434\u0430\u043B\u0438\u0442\u044C">\u2716</button>
      </span>`;
    div.addEventListener('click', e => {
      if (e.target.closest('.chat-action-btn')) return;
      state.activeChatId = chat.id; Save(); render(); closeSidebar();
    });
    div.querySelector('[data-action="rename"]').addEventListener('click', e => {
      e.stopPropagation();
      const t = prompt('\u041D\u043E\u0432\u043E\u0435 \u043D\u0430\u0437\u0432\u0430\u043D\u0438\u0435:', chat.title);
      if (t && t.trim()) { chat.title = t.trim(); Save(); renderChatList(); }
    });
    div.querySelector('[data-action="delete"]').addEventListener('click', e => {
      e.stopPropagation();
      if (!confirm('\u0423\u0434\u0430\u043B\u0438\u0442\u044C \u044D\u0442\u043E\u0442 \u0447\u0430\u0442?')) return;
      const idx = state.chats.indexOf(chat);
      state.chats.splice(idx, 1);
      if (state.activeChatId === chat.id) {
        state.activeChatId = state.chats.length ? state.chats[Math.min(idx, state.chats.length - 1)].id : null;
        if (!state.activeChatId) createChat();
      }
      Save(); render();
    });
    el.chatList.appendChild(div);
  });
}

function renderMessages() {
  const chat = getChat();
  el.chatTitle.textContent = chat?.title || '\u041D\u043E\u0432\u044B\u0439 \u0447\u0430\u0442';
  el.chatStatus.textContent = chat?.messages?.length ? chat.messages.length + ' \u0441\u043E\u043E\u0431\u0449' : '\u0413\u043E\u0442\u043E\u0432 \u043A \u0440\u0430\u0431\u043E\u0442\u0435';
  el.messages.innerHTML = '';

  if (!chat || !chat.messages.length) {
    el.messages.innerHTML = `<div class="welcome">
      <img src="assets/logo.svg" alt="" class="welcome-logo" />
      <h2>\u0427\u0435\u043C \u043C\u043E\u0433\u0443 \u043F\u043E\u043C\u043E\u0447\u044C?</h2>
      <p>\u042F ZeroxAI \u2014 \u043C\u043D\u043E\u0433\u043E\u0444\u0443\u043D\u043A\u0446\u0438\u043E\u043D\u0430\u043B\u044C\u043D\u044B\u0439 AI-\u0430\u0441\u0441\u0438\u0441\u0442\u0435\u043D\u0442</p>
      <div class="welcome-suggestions">
        <button>\u0421\u043E\u0441\u0442\u0430\u0432\u044C \u043F\u043B\u0430\u043D \u0437\u0430\u043F\u0443\u0441\u043A\u0430 \u043F\u0440\u043E\u0434\u0443\u043A\u0442\u0430</button>
        <button>\u041F\u043E\u043C\u043E\u0433\u0438 \u043D\u0430\u043F\u0438\u0441\u0430\u0442\u044C \u043F\u0438\u0441\u044C\u043C\u043E \u043A\u043B\u0438\u0435\u043D\u0442\u0443</button>
        <button>\u041E\u0431\u044A\u044F\u0441\u043D\u0438 \u043A\u0432\u0430\u043D\u0442\u043E\u0432\u0443\u044E \u0444\u0438\u0437\u0438\u043A\u0443</button>
        <button>\u0421\u0433\u0435\u043D\u0435\u0440\u0438\u0440\u0443\u0439 \u0438\u0434\u0435\u0438 \u0434\u043B\u044F \u0441\u0442\u0430\u0440\u0442\u0430\u043F\u0430</button>
      </div>
    </div>`;
    el.messages.querySelectorAll('.welcome-suggestions button').forEach(b => {
      b.addEventListener('click', () => { el.input.value = b.textContent; el.input.focus(); resizeInput(); });
    });
    return;
  }

  chat.messages.forEach((msg, i) => {
    const article = document.createElement('div');
    article.className = 'message ' + msg.role;
    article.dataset.index = i;

    const avatar = document.createElement('div');
    avatar.className = 'avatar ' + (msg.role === 'user' ? 'user-avatar' : 'ai-avatar');
    avatar.textContent = msg.role === 'user' ? (state.settings.profileName[0] || 'U') : 'Z';

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    const content = document.createElement('div');
    content.className = 'message-content';
    content.innerHTML = msg.content ? md.render(msg.content) : '';
    bubble.appendChild(content);
    article.append(msg.role === 'user' ? [bubble, avatar] : [avatar, bubble]);
    el.messages.appendChild(article);
  });
  scrollToBottom();
}

function scrollToBottom() {
  requestAnimationFrame(() => { el.messages.scrollTop = el.messages.scrollHeight; });
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function addMsg(role, content) {
  const chat = getChat();
  const msg = { id: uid(), role, content, createdAt: Date.now() };
  chat.messages.push(msg);
  chat.updatedAt = Date.now();
  updateTitle(chat);
  Save();
  return msg;
}

async function handleSubmit(e) {
  e.preventDefault();
  const text = el.input.value.trim();
  if (!text || state.busy) return;

  el.input.value = ''; resizeInput();
  addMsg('user', text);

  const chat = getChat();
  renderMessages();

  state.busy = true; el.sendBtn.disabled = true; el.input.disabled = true;
  el.chatStatus.textContent = '\u041E\u0442\u0432\u0435\u0447\u0430\u044E...';

  const aiMsg = addMsg('assistant', '');

  if (state.settings.thinkingAnim) {
    el.thinkingOverlay.classList.remove('hidden');
    el.thinkingVideo.play().catch(() => {});
  }

  try {
    const answer = await requestAI(text);
    aiMsg.content = answer;
    el.thinkingOverlay.classList.add('hidden');
    el.thinkingVideo.pause();
    chat.updatedAt = Date.now();
    Save();
    renderMessages();
    typeResponse(aiMsg.id, answer);
  } catch (err) {
    el.thinkingOverlay.classList.add('hidden');
    el.thinkingVideo.pause();
    aiMsg.content = '\u274C \u041E\u0448\u0438\u0431\u043A\u0430: ' + err.message;
    Save();
    renderMessages();
  } finally {
    state.busy = false; el.sendBtn.disabled = false; el.input.disabled = false;
    el.chatStatus.textContent = '\u0413\u043E\u0442\u043E\u0432';
    el.input.focus();
  }
}

function typeResponse(msgId, text) {
  const idx = getChat().messages.findIndex(m => m.id === msgId);
  if (idx === -1) return;
  const node = el.messages.querySelector(`[data-index="${idx}"] .message-content`);
  if (!node) return;

  const speed = TYPING_SPEEDS[state.settings.typingSpeed] || 35;
  let pos = 0;
  const rendered = md.render(text);
  const lines = text.split('\n');
  let lineIdx = 0;
  let charPos = 0;

  node.classList.add('typing-cursor');

  function typeChar() {
    if (pos >= text.length || !getChat() || state.busy) {
      node.classList.remove('typing-cursor');
      node.innerHTML = rendered;
      scrollToBottom();
      return;
    }
    const slice = text.slice(0, pos + 1);
    node.innerHTML = md.render(slice) + '<span class="typing-cursor"></span>';
    pos++;
    scrollToBottom();
    const char = text[pos - 1];
    const delay = char === '\n' ? speed * 3 : char === ' ' ? speed * 0.5 : speed;
    setTimeout(typeChar, delay);
  }
  typeChar();
}

async function requestAI(text) {
  const resp = await fetch(state.settings.apiEndpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text, user_id: 0, chat_id: 'web_' + uid() })
  });
  if (!resp.ok) {
    let detail = '';
    try { const d = await resp.json(); detail = d.error?.message || d.error || d.detail || ''; } catch {}
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  const data = await resp.json();
  return (data.response || data.content || data.message?.content || '').trim() || '\u041F\u0443\u0441\u0442\u043E\u0439 \u043E\u0442\u0432\u0435\u0442';
}

function fillSettings() {
  el.profileName.value = state.settings.profileName;
  el.apiEndpoint.value = state.settings.apiEndpoint;
  el.modelName.value = state.settings.modelName;
  el.apiKeyField.value = state.settings.apiKey;
  el.thinkingAnim.checked = state.settings.thinkingAnim;
  el.typingSpeed.value = state.settings.typingSpeed;
  el.themeBtns.forEach(b => b.classList.toggle('active', b.dataset.theme === state.settings.theme));
}

function applyTheme() {
  const t = state.settings.theme;
  const isDark = t === 'dark' || (t === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.theme = isDark ? 'dark' : 'light';
}

function openSettings() { el.settingsPanel.classList.remove('hidden'); }
function closeSettings() { el.settingsPanel.classList.add('hidden'); }
function closeSidebar() { el.sidebar.classList.remove('open'); el.overlay.classList.remove('open'); }

function resizeInput() {
  el.input.style.height = 'auto';
  el.input.style.height = Math.min(el.input.scrollHeight, 200) + 'px';
}

function handleSettingsSubmit(e) {
  e.preventDefault();
  state.settings.profileName = el.profileName.value.trim() || 'User';
  state.settings.apiEndpoint = el.apiEndpoint.value.trim() || '/api/chat';
  state.settings.modelName = el.modelName.value.trim() || 'openai/gpt-oss-120b';
  state.settings.apiKey = el.apiKeyField.value.trim();
  state.settings.thinkingAnim = el.thinkingAnim.checked;
  state.settings.typingSpeed = el.typingSpeed.value;
  Save(); closeSettings(); render();
}

window.copyCode = function(btn) {
  const pre = btn.closest('.code-header').nextElementSibling;
  const code = pre?.querySelector('code');
  if (!code) return;
  navigator.clipboard.writeText(code.textContent).then(() => {
    btn.textContent = '\u2705 \u0421\u043A\u043E\u043F\u0438\u0440\u043E\u0432\u0430\u043D\u043E';
    setTimeout(() => { btn.textContent = '\u{1F4CB} \u041A\u043E\u043F\u0438\u0440\u043E\u0432\u0430\u0442\u044C'; }, 1500);
  });
};

function bind() {
  el.composer.addEventListener('submit', handleSubmit);
  el.input.addEventListener('input', resizeInput);
  el.input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); el.composer.requestSubmit(); }
  });
  el.newChatBtn.addEventListener('click', () => { createChat(); closeSidebar(); });
  el.menuBtn.addEventListener('click', () => { el.sidebar.classList.toggle('open'); el.overlay.classList.toggle('open'); });
  el.overlay.addEventListener('click', closeSidebar);
  el.searchInput.addEventListener('input', renderChatList);
  el.settingsBtn.addEventListener('click', openSettings);
  el.topSettingsBtn.addEventListener('click', openSettings);
  el.closeSettingsBtn.addEventListener('click', closeSettings);
  el.settingsForm.addEventListener('submit', handleSettingsSubmit);
  el.themeBtns.forEach(b => b.addEventListener('click', () => {
    state.settings.theme = b.dataset.theme; el.themeBtns.forEach(x => x.classList.toggle('active', x === b)); applyTheme(); Save();
  }));
  document.addEventListener('click', e => {
    if (!e.target.closest('.settings-panel') && !e.target.closest('[id$="settingsBtn"]') && !e.target.closest('#topSettingsBtn')) {
      if (!el.settingsPanel.classList.contains('hidden')) closeSettings();
    }
  });
  el.input.addEventListener('input', () => {
    el.sendBtn.disabled = !el.input.value.trim();
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => applyTheme());
}

Load();
bind();
render();
