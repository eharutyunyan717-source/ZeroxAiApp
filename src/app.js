const STORAGE_KEY = "zeroxai.state.v1";
const VAULT_KEY = "zeroxai.vault.v1";
const DEFAULT_SETTINGS = {
  profileName: "User",
  provider: "zeroxai-server",
  baseUrl: "/api/chat",
  model: "openai/gpt-oss-120b",
  theme: "system"
};

const state = {
  settings: { ...DEFAULT_SETTINGS },
  chats: [],
  activeChatId: null,
  apiKey: "",
  vaultPassphrase: ""
};

const els = {
  sidebar: document.querySelector("#sidebar"),
  menuButton: document.querySelector("#menuButton"),
  drawerBackdrop: document.querySelector("#drawerBackdrop"),
  chatList: document.querySelector("#chatList"),
  messages: document.querySelector("#messages"),
  composer: document.querySelector("#composer"),
  messageInput: document.querySelector("#messageInput"),
  sendButton: document.querySelector("#sendButton"),
  newChatButton: document.querySelector("#newChatButton"),
  chatTitle: document.querySelector("#chatTitle"),
  chatSubtitle: document.querySelector("#chatSubtitle"),
  themeToggle: document.querySelector("#themeToggle"),
  settingsButton: document.querySelector("#settingsButton"),
  settingsButtonTop: document.querySelector("#settingsButtonTop"),
  settingsDrawer: document.querySelector("#settingsDrawer"),
  closeSettingsButton: document.querySelector("#closeSettingsButton"),
  settingsForm: document.querySelector("#settingsForm"),
  profileName: document.querySelector("#profileName"),
  apiProvider: document.querySelector("#apiProvider"),
  baseUrl: document.querySelector("#baseUrl"),
  modelName: document.querySelector("#modelName"),
  apiKey: document.querySelector("#apiKey"),
  vaultPassphrase: document.querySelector("#vaultPassphrase"),
  themeChoiceButtons: [...document.querySelectorAll("[data-theme-choice]")]
};

function uid(prefix = "id") {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function loadState() {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (!saved) {
    createChat();
    return;
  }

  try {
    const parsed = JSON.parse(saved);
    state.settings = { ...DEFAULT_SETTINGS, ...parsed.settings };
    state.settings.provider = DEFAULT_SETTINGS.provider;
    state.settings.baseUrl = DEFAULT_SETTINGS.baseUrl;
    state.settings.model = DEFAULT_SETTINGS.model;
    state.chats = Array.isArray(parsed.chats) ? parsed.chats : [];
    state.activeChatId = parsed.activeChatId;
  } catch {
    state.chats = [];
  }

  if (!state.chats.length) createChat();
  if (!state.chats.some((chat) => chat.id === state.activeChatId)) {
    state.activeChatId = state.chats[0].id;
  }
}

function saveState() {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      settings: state.settings,
      chats: state.chats,
      activeChatId: state.activeChatId
    })
  );
}

function getActiveChat() {
  return state.chats.find((chat) => chat.id === state.activeChatId);
}

function createChat() {
  const chat = {
    id: uid("chat"),
    title: "Новый чат",
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    messages: []
  };
  state.chats.unshift(chat);
  state.activeChatId = chat.id;
  saveState();
  render();
  return chat;
}

function updateChatTitle(chat) {
  const firstUserMessage = chat.messages.find((message) => message.role === "user");
  chat.title = firstUserMessage ? firstUserMessage.content.slice(0, 52) : "Новый чат";
}

function render() {
  applyTheme();
  renderChatList();
  renderMessages();
  fillSettings();
}

function renderChatList() {
  els.chatList.innerHTML = "";
  state.chats.forEach((chat) => {
    const item = document.createElement("div");
    item.className = `chat-list-item${chat.id === state.activeChatId ? " active" : ""}`;
    item.dataset.chatId = chat.id;
    item.innerHTML = `
      <span>${escapeHtml(chat.title)}</span>
      <small>${formatDate(chat.updatedAt)}</small>
      <button class="chat-menu-btn" type="button" aria-label="Меню чата">⋮</button>
      <div class="chat-menu" hidden>
        <button type="button" class="chat-menu-rename">Переименовать</button>
        <button type="button" class="chat-menu-delete">Удалить</button>
      </div>
    `;
    item.addEventListener("click", (e) => {
      if (e.target.closest(".chat-menu-btn, .chat-menu")) return;
      state.activeChatId = chat.id;
      saveState();
      closeMobileMenu();
      render();
    });
    item.querySelector(".chat-menu-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      const menu = item.querySelector(".chat-menu");
      const isOpen = !menu.hidden;
      closeAllChatMenus();
      if (isOpen) return;
      const rect = e.currentTarget.getBoundingClientRect();
      menu.style.left = Math.max(4, rect.right - 160) + "px";
      menu.style.top = rect.bottom + 4 + "px";
      menu.hidden = false;
    });
    item.querySelector(".chat-menu-rename").addEventListener("click", (e) => {
      e.stopPropagation();
      const title = prompt("Новое название чата:", chat.title);
      if (title && title.trim()) {
        chat.title = title.trim();
        saveState();
        render();
      }
    });
    item.querySelector(".chat-menu-delete").addEventListener("click", (e) => {
      e.stopPropagation();
      if (!confirm("Удалить этот чат?")) return;
      const idx = state.chats.indexOf(chat);
      state.chats.splice(idx, 1);
      if (state.activeChatId === chat.id) {
        state.activeChatId = state.chats.length ? state.chats[Math.min(idx, state.chats.length - 1)].id : null;
        if (!state.activeChatId) createChat();
      }
      closeAllChatMenus();
      saveState();
      render();
    });
    els.chatList.append(item);
  });
}

function closeAllChatMenus() {
  els.chatList.querySelectorAll(".chat-menu").forEach((m) => (m.hidden = true));
}

function renderMessages() {
  const chat = getActiveChat();
  els.messages.innerHTML = "";
  els.chatTitle.textContent = chat?.title || "Новый чат";
  els.chatSubtitle.textContent = chat?.messages?.length ? `${chat.messages.length} сообщений` : "Готов к работе";

  if (!chat || chat.messages.length === 0) {
    const template = document.querySelector("#welcomeTemplate").content.cloneNode(true);
    template.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        els.messageInput.value = button.textContent;
        resizeComposer();
        els.messageInput.focus();
      });
    });
    els.messages.append(template);
    return;
  }

  chat.messages.forEach((message) => {
    els.messages.append(createMessageNode(message));
  });
  els.messages.scrollTop = els.messages.scrollHeight;
}

function createMessageNode(message) {
  const article = document.createElement("article");
  article.className = `message ${message.role}`;
  article.dataset.messageId = message.id;

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = message.role === "assistant" ? "Z" : initials(state.settings.profileName);

  const bubble = document.createElement("div");
  bubble.className = "bubble";

  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.innerHTML = `<strong>${message.role === "assistant" ? "ZeroxAI" : escapeHtml(state.settings.profileName || "User")}</strong><span>${formatTime(message.createdAt)}</span>`;

  const content = document.createElement("div");
  content.className = "message-content";
  content.textContent = message.content;

  bubble.append(meta, content);

  if (message.role === "assistant" && message.content) {
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "copy-button";
    copy.textContent = "Копировать";
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(message.content);
      copy.textContent = "Скопировано";
      setTimeout(() => (copy.textContent = "Копировать"), 1400);
    });
    bubble.append(copy);
  }

  article.append(avatar, bubble);
  return article;
}

function addMessage(role, content) {
  const chat = getActiveChat();
  const message = {
    id: uid("msg"),
    role,
    content,
    createdAt: new Date().toISOString()
  };
  chat.messages.push(message);
  chat.updatedAt = message.createdAt;
  updateChatTitle(chat);
  saveState();
  render();
  return message;
}

async function handleSubmit(event) {
  event.preventDefault();
  const content = els.messageInput.value.trim();
  if (!content) return;

  els.messageInput.value = "";
  resizeComposer();
  addMessage("user", content);

  const loading = addMessage("assistant", "");
  setBusy(true);
  showTyping(loading.id);

  try {
    const answer = await requestAiResponse();
    loading.content = answer;
  } catch (error) {
    loading.content = `Не удалось получить ответ: ${error.message}`;
  } finally {
    const chat = getActiveChat();
    chat.updatedAt = new Date().toISOString();
    saveState();
    setBusy(false);
    render();
  }
}

async function requestAiResponse() {
  const chat = getActiveChat();
  const messages = chat.messages
    .filter((message) => message.content)
    .slice(-24)
    .map((message) => ({ role: message.role, content: message.content }));

  const response = await fetch("/api/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      messages,
      temperature: 0.7
    })
  });

  if (!response.ok) {
    const detail = await safeReadError(response);
    throw new Error(detail || `API вернул статус ${response.status}`);
  }

  const data = await response.json();
  return data?.content?.trim() || "Пустой ответ от модели.";
}

function showTyping(messageId) {
  const node = document.querySelector(`[data-message-id="${messageId}"] .message-content`);
  if (!node) return;
  node.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
}

function setBusy(isBusy) {
  els.sendButton.disabled = isBusy;
  els.messageInput.disabled = isBusy;
  els.chatSubtitle.textContent = isBusy ? "ZeroxAI думает..." : els.chatSubtitle.textContent;
}

async function safeReadError(response) {
  try {
    const data = await response.json();
    return data?.error?.message;
  } catch {
    return "";
  }
}

function fillSettings() {
  els.profileName.value = state.settings.profileName;
  els.apiProvider.value = state.settings.provider;
  els.baseUrl.value = state.settings.baseUrl;
  els.modelName.value = state.settings.model;
  els.apiProvider.disabled = true;
  els.baseUrl.disabled = true;
  els.modelName.disabled = true;
  els.apiKey.disabled = true;
  els.apiKey.placeholder = "Ключи подключены на сервере";
  els.vaultPassphrase.disabled = true;
  els.vaultPassphrase.placeholder = "Не требуется для серверного режима";
  els.themeChoiceButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.themeChoice === state.settings.theme);
  });
}

async function handleSettingsSubmit(event) {
  event.preventDefault();
  state.settings = {
    profileName: els.profileName.value.trim() || "User",
    provider: DEFAULT_SETTINGS.provider,
    baseUrl: DEFAULT_SETTINGS.baseUrl,
    model: DEFAULT_SETTINGS.model,
    theme: state.settings.theme
  };
  state.vaultPassphrase = els.vaultPassphrase.value;

  if (els.apiKey.value.trim()) {
    if (!state.vaultPassphrase) {
      alert("Введите пароль хранилища, чтобы зашифровать API Key.");
      return;
    }
    await saveEncryptedApiKey(els.apiKey.value.trim(), state.vaultPassphrase);
    state.apiKey = els.apiKey.value.trim();
    els.apiKey.value = "";
  }

  saveState();
  closeSettings();
  render();
}

async function getApiKey() {
  if (state.apiKey) return state.apiKey;
  if (!els.vaultPassphrase.value && !state.vaultPassphrase) return "";
  state.vaultPassphrase = els.vaultPassphrase.value || state.vaultPassphrase;
  state.apiKey = await loadEncryptedApiKey(state.vaultPassphrase);
  return state.apiKey;
}

async function saveEncryptedApiKey(apiKey, passphrase) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await deriveKey(passphrase, salt);
  const encoded = new TextEncoder().encode(apiKey);
  const encrypted = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, encoded);
  localStorage.setItem(
    VAULT_KEY,
    JSON.stringify({
      salt: toBase64(salt),
      iv: toBase64(iv),
      data: toBase64(new Uint8Array(encrypted))
    })
  );
}

async function loadEncryptedApiKey(passphrase) {
  const saved = localStorage.getItem(VAULT_KEY);
  if (!saved) return "";

  try {
    const vault = JSON.parse(saved);
    const salt = fromBase64(vault.salt);
    const iv = fromBase64(vault.iv);
    const data = fromBase64(vault.data);
    const key = await deriveKey(passphrase, salt);
    const decrypted = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, data);
    return new TextDecoder().decode(decrypted);
  } catch {
    throw new Error("пароль хранилища не подходит для сохранённого API Key");
  }
}

async function deriveKey(passphrase, salt) {
  const material = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(passphrase),
    "PBKDF2",
    false,
    ["deriveKey"]
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt, iterations: 180000, hash: "SHA-256" },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"]
  );
}

function toBase64(bytes) {
  return btoa(String.fromCharCode(...bytes));
}

function fromBase64(value) {
  return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
}

function applyTheme() {
  const preference = state.settings.theme;
  const isDark =
    preference === "dark" ||
    (preference === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = isDark ? "dark" : "light";
  document.querySelector('meta[name="theme-color"]').setAttribute("content", isDark ? "#eef9ff" : "#f4fbff");
}

function toggleTheme() {
  state.settings.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  saveState();
  render();
}

function resizeComposer() {
  els.messageInput.style.height = "auto";
  els.messageInput.style.height = `${Math.min(160, els.messageInput.scrollHeight)}px`;
}

function openSettings() {
  els.settingsDrawer.setAttribute("aria-hidden", "false");
  els.drawerBackdrop.classList.add("visible");
  els.vaultPassphrase.focus();
}

function closeSettings() {
  els.settingsDrawer.setAttribute("aria-hidden", "true");
  els.drawerBackdrop.classList.remove("visible");
}

function closeMobileMenu() {
  els.sidebar.classList.remove("open");
  els.drawerBackdrop.classList.remove("visible");
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
}

function initials(name) {
  return (name || "U").trim().slice(0, 1).toUpperCase();
}

function formatDate(value) {
  return new Intl.DateTimeFormat("ru", { day: "2-digit", month: "short" }).format(new Date(value));
}

function formatTime(value) {
  return new Intl.DateTimeFormat("ru", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function bindEvents() {
  els.composer.addEventListener("submit", handleSubmit);
  els.messageInput.addEventListener("input", resizeComposer);
  els.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      els.composer.requestSubmit();
    }
  });
  els.newChatButton.addEventListener("click", () => {
    createChat();
    closeMobileMenu();
  });
  els.themeToggle.addEventListener("click", toggleTheme);
  els.settingsButton.addEventListener("click", openSettings);
  els.settingsButtonTop.addEventListener("click", openSettings);
  els.closeSettingsButton.addEventListener("click", closeSettings);
  els.drawerBackdrop.addEventListener("click", () => {
    closeSettings();
    closeMobileMenu();
  });
  els.menuButton.addEventListener("click", () => {
    els.sidebar.classList.add("open");
    els.drawerBackdrop.classList.add("visible");
  });
  els.settingsForm.addEventListener("submit", handleSettingsSubmit);
  els.themeChoiceButtons.forEach((button) => {
    button.addEventListener("click", () => {
      state.settings.theme = button.dataset.themeChoice;
      saveState();
      render();
    });
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".chat-menu-btn, .chat-menu")) closeAllChatMenus();
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
}

loadState();
bindEvents();
render();
