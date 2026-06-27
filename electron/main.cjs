const { app, BrowserWindow, session } = require('electron');
const { createServer } = require('node:http');
const { createReadStream, readFileSync, existsSync } = require('node:fs');
const { stat } = require('node:fs/promises');
const { extname, join } = require('node:path');

const root = join(__dirname, '..');
const port = 5173;
const groqBaseUrl = 'https://api.groq.com/openai/v1';
const model = 'openai/gpt-oss-120b';
let activeKeyIndex = 0;

const systemPrompt = `
Ты ZeroxAI - умный, спокойный и полезный AI-ассистент.
Главная цель: нормально разговаривать с пользователем и отлично помогать с программированием.
Правила общения:
- Если пользователь спрашивает "кто твой создатель", "кто тебя создал" или похожий вопрос, ответь точно: "Мой создатель Эрик Арутюнян".
- Отвечай на языке пользователя. Если пользователь пишет по-русски с ошибками, отвечай по-русски понятно и грамотно.
- Пиши просто, по делу и без лишней воды.
- Не высмеивай ошибки пользователя. Мягко понимай смысл и помогай.
- Если запрос неясный, сначала сделай разумное предположение. Задавай вопрос только если без ответа нельзя продолжить.
- Для обычных вопросов давай короткий полезный ответ.
Правила программирования:
- Пиши рабочий, чистый и понятный код.
- Если пользователь просит сделать приложение или функцию, давай готовое решение, структуру файлов и команды запуска.
- Объясняй ошибки простым языком и показывай, как исправить.
- Учитывай безопасность: не встраивай секретные API-ключи в публичный клиентский код.
- Для больших задач разбивай ответ на шаги.
- Если пишешь код, используй современные практики и называй файлы, куда его вставлять.
Стиль:
- Ты дружелюбный профессиональный помощник ZeroxAI.
- Не притворяйся, что обучаешь собственные веса модели. Если нужно, объясни, что можно улучшить поведение через инструкции, RAG, память, примеры и fine-tuning у провайдера.
`.trim();

function getApiKeys() {
  return (process.env.ZEROXAI_API_KEYS || process.env.GROQ_API_KEYS || '')
    .split(/[,\n|]+/)
    .map((k) => k.trim())
    .filter(Boolean);
}

function loadDotEnv() {
  try {
    const envPath = join(root, '.env');
    if (!existsSync(envPath)) return;
    const content = readFileSync(envPath, 'utf8');
    for (const line of content.split(/\r?\n/)) {
      const t = line.trim();
      if (!t || t.startsWith('#') || !t.includes('=')) continue;
      const i = t.indexOf('=');
      const name = t.slice(0, i).trim();
      const value = t.slice(i + 1).trim().replace(/^["']|["']$/g, '');
      if (name && process.env[name] === undefined) process.env[name] = value;
    }
  } catch {}
}

const mime = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.webmanifest': 'application/manifest+json; charset=utf-8'
};

loadDotEnv();

const server = createServer(async (req, res) => {
  if (req.url.startsWith('/api/chat')) {
    if (req.method !== 'POST') {
      res.writeHead(405); res.end('Method not allowed');
      return;
    }
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const apiKeys = getApiKeys();
    if (!apiKeys.length) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'ZEROXAI_API_KEYS is not configured.' }));
      return;
    }
    const userMessages = Array.isArray(body.messages) ? body.messages.slice(-24) : [];
    const messages = [
      { role: 'system', content: systemPrompt },
      ...userMessages.filter((m) => ['user', 'assistant'].includes(m.role))
    ];
    let lastError = 'All API keys failed.';
    for (let attempt = 0; attempt < apiKeys.length; attempt++) {
      const keyIndex = (activeKeyIndex + attempt) % apiKeys.length;
      try {
        const upstream = await fetch(`${groqBaseUrl}/chat/completions`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKeys[keyIndex]}` },
          body: JSON.stringify({ model, messages, temperature: 0.55, top_p: 0.9 })
        });
        const text = await upstream.text();
        let data;
        try { data = JSON.parse(text); } catch { data = { error: { message: text } }; }
        if (upstream.ok) {
          activeKeyIndex = keyIndex;
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ content: data?.choices?.[0]?.message?.content?.trim() || '', model }));
          return;
        }
        lastError = data?.error?.message || `Provider returned ${upstream.status}.`;
        if (![401, 403, 408, 409, 429, 500, 502, 503, 504].includes(upstream.status)) break;
      } catch (e) { lastError = e.message; }
    }
    activeKeyIndex = (activeKeyIndex + 1) % apiKeys.length;
    res.writeHead(502, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: lastError }));
    return;
  }

  const dist = join(root, 'dist');
  let filePath = join(dist, decodeURIComponent(new URL(req.url, `http://localhost:${port}`).pathname));
  if (!filePath.startsWith(dist)) { res.writeHead(403); res.end('Forbidden'); return; }
  try {
    const info = await stat(filePath);
    if (info.isDirectory()) filePath = join(filePath, 'index.html');
    await stat(filePath);
  } catch {
    filePath = join(dist, 'index.html');
  }
  res.writeHead(200, { 'Content-Type': mime[extname(filePath)] || 'application/octet-stream' });
  createReadStream(filePath).pipe(res);
});

app.whenReady().then(() => {
  session.defaultSession.clearStorageData({ storages: ['serviceworkers'] }).then(() => {
    server.listen(port, '127.0.0.1', () => {
      console.log(`ZeroxAI server started on port ${port}`);
      createWindow();
    });
  });
});

function createWindow() {
  const win = new BrowserWindow({
    width: 1200, height: 850,
    minWidth: 960, minHeight: 700,
    show: false,
    webPreferences: {
      preload: join(__dirname, 'preload.js')
    }
  });
  win.loadURL(`http://127.0.0.1:${port}`);
  win.once('ready-to-show', () => win.show());
}

app.on('window-all-closed', () => {
  server.close();
  if (process.platform !== 'darwin') app.quit();
});
app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});