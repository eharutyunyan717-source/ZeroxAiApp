import { access, readFile } from "node:fs/promises";

const requiredFiles = [
  "index.html",
  "manifest.webmanifest",
  "service-worker.js",
  "src/app.js",
  "src/styles.css",
  "assets/logo.svg",
  "assets/icon-192.svg",
  "assets/icon-512.svg",
  "app.py",
  "requirements.txt"
];

for (const file of requiredFiles) {
  await access(file);
}

const manifest = JSON.parse(await readFile("manifest.webmanifest", "utf8"));
if (manifest.name !== "ZeroxAI") {
  throw new Error("Manifest name must be ZeroxAI.");
}

const app = await readFile("src/app.js", "utf8");
for (const token of ["/api/chat", "localStorage"]) {
  if (!app.includes(token)) {
    throw new Error(`Missing expected app capability: ${token}`);
  }
}

const server = await readFile("scripts/dev-server.js", "utf8");
for (const token of ["openai/gpt-oss-120b", "ZEROXAI_API_KEYS", "chat/completions", "activeKeyIndex", "zeroxAiSystemPrompt"]) {
  if (!server.includes(token)) {
    throw new Error(`Missing expected server capability: ${token}`);
  }
}

console.log("ZeroxAI project structure is valid.");
