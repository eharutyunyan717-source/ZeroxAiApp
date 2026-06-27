import { createReadStream } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";

const root = resolve(".");
const port = Number(process.env.PORT || 5173);
const host = process.env.HOST || "127.0.0.1";
const groqBaseUrl = "https://api.groq.com/openai/v1";
const model = "openai/gpt-oss-120b";
let activeKeyIndex = 0;

const zeroxAiSystemPrompt = `
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

const mime = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".webmanifest": "application/manifest+json; charset=utf-8"
};

await loadDotEnv();

function getApiKeys() {
  return (process.env.ZEROXAI_API_KEYS || process.env.GROQ_API_KEYS || "")
    .split(/[,\n|]+/)
    .map((key) => key.trim())
    .filter(Boolean);
}

async function loadDotEnv() {
  try {
    const content = await readFile(join(root, ".env"), "utf8");
    for (const line of content.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
      const index = trimmed.indexOf("=");
      const name = trimmed.slice(0, index).trim();
      const value = trimmed.slice(index + 1).trim().replace(/^["']|["']$/g, "");
      if (name && process.env[name] === undefined) process.env[name] = value;
    }
  } catch {
    // .env is optional in production because hosts usually provide env vars.
  }
}

function resolveRequestPath(url) {
  const pathname = decodeURIComponent(new URL(url, `http://${host}:${port}`).pathname);
  const requested = normalize(join(root, pathname));
  if (!requested.startsWith(root)) return null;
  return requested;
}

async function readJsonBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function sendJson(response, status, data) {
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store"
  });
  response.end(JSON.stringify(data));
}

async function proxyChat(request, response) {
  const apiKeys = getApiKeys();
  if (!apiKeys.length) {
    sendJson(response, 500, {
      error: "ZEROXAI_API_KEYS is not configured on the server."
    });
    return;
  }

  let body;
  try {
    body = await readJsonBody(request);
  } catch {
    sendJson(response, 400, { error: "Invalid JSON body." });
    return;
  }

  const userMessages = Array.isArray(body.messages) ? body.messages.slice(-24) : [];
  if (!userMessages.length) {
    sendJson(response, 400, { error: "Messages are required." });
    return;
  }

  const messages = [
    { role: "system", content: zeroxAiSystemPrompt },
    ...userMessages.filter((message) => ["user", "assistant"].includes(message.role))
  ];

  let lastError = "All API keys failed.";
  for (let attempt = 0; attempt < apiKeys.length; attempt += 1) {
    const keyIndex = (activeKeyIndex + attempt) % apiKeys.length;
    const apiKey = apiKeys[keyIndex];

    try {
      const upstream = await fetch(`${groqBaseUrl}/chat/completions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiKey}`
        },
        body: JSON.stringify({
          model,
          messages,
          temperature: typeof body.temperature === "number" ? body.temperature : 0.55,
          top_p: 0.9
        })
      });

      const text = await upstream.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        data = { error: { message: text } };
      }

      if (upstream.ok) {
        activeKeyIndex = keyIndex;
        sendJson(response, 200, {
          content: data?.choices?.[0]?.message?.content?.trim() || "",
          model,
          keySlot: keyIndex + 1
        });
        return;
      }

      lastError = data?.error?.message || `Provider returned ${upstream.status}.`;
      if (![401, 403, 408, 409, 429, 500, 502, 503, 504].includes(upstream.status)) break;
    } catch (error) {
      lastError = error.message;
    }
  }

  activeKeyIndex = (activeKeyIndex + 1) % apiKeys.length;
  sendJson(response, 502, { error: lastError });
}

const server = createServer(async (request, response) => {
  if (request.url?.startsWith("/api/chat")) {
    if (request.method !== "POST") {
      sendJson(response, 405, { error: "Method not allowed." });
      return;
    }
    await proxyChat(request, response);
    return;
  }

  const requested = resolveRequestPath(request.url);
  if (!requested) {
    response.writeHead(403);
    response.end("Forbidden");
    return;
  }

  let filePath = requested;
  try {
    const info = await stat(filePath);
    if (info.isDirectory()) filePath = join(filePath, "index.html");
    await stat(filePath);
  } catch {
    filePath = join(root, "index.html");
  }

  response.writeHead(200, {
    "Content-Type": mime[extname(filePath)] || "application/octet-stream",
    "Cache-Control": "no-cache"
  });
  createReadStream(filePath).pipe(response);
});

server.listen(port, host, () => {
  const keyCount = getApiKeys().length;
  console.log(`ZeroxAI is running at http://${host}:${port}`);
  console.log(`AI model: ${model}`);
  console.log(`API key slots loaded: ${keyCount}`);
});
