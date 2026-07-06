const { app, BrowserWindow } = require('electron');
const { spawn } = require('node:child_process');
const { join } = require('node:path');
const { existsSync, readFileSync } = require('node:fs');
const http = require('node:http');

const ROOT = join(__dirname, '..');
const BOT_DIR = join(ROOT, 'ZeroxAiApp');
const LOCAL_PORT = 8080;

function loadEnv() {
  const envPath = join(BOT_DIR, '.env');
  if (!existsSync(envPath)) return;
  for (const line of readFileSync(envPath, 'utf8').split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith('#') || !t.includes('=')) continue;
    const i = t.indexOf('=');
    const k = t.slice(0, i).trim();
    const v = t.slice(i + 1).trim().replace(/^["']|["']$/g, '');
    if (k && process.env[k] === undefined) process.env[k] = v;
  }
}
loadEnv();

const RAILWAY_URL = process.env.RAILWAY_PUBLIC_DOMAIN
  ? `https://${process.env.RAILWAY_PUBLIC_DOMAIN}`
  : null;

let botProcess = null;

function startLocalBot() {
  return new Promise((resolve) => {
    botProcess = spawn('python', ['-u', 'telegram_bot.py'], {
      cwd: BOT_DIR,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });
    botProcess.stdout.on('data', (d) => process.stdout.write(`[bot] ${d}`));
    botProcess.stderr.on('data', (d) => process.stderr.write(`[bot-err] ${d}`));
    const check = () => {
      const req = http.get(`http://127.0.0.1:${LOCAL_PORT}/health`, (res) => {
        if (res.statusCode === 200) resolve();
        else setTimeout(check, 500);
      });
      req.on('error', () => setTimeout(check, 500));
    };
    setTimeout(check, 1000);
  });
}

app.whenReady().then(async () => {
  let url;

  // try Railway first
  if (RAILWAY_URL) {
    url = RAILWAY_URL;
  } else {
    // fallback: start local bot
    await startLocalBot();
    url = `http://127.0.0.1:${LOCAL_PORT}`;
  }

  const win = new BrowserWindow({
    width: 1280, height: 800,
    minWidth: 800, minHeight: 600,
    title: 'ZeroxAI Telegram',
    icon: join(ROOT, 'assets', 'icon-512.svg'),
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
    },
  });
  win.loadURL(url);
  win.setMenuBarVisibility(false);
});

app.on('window-all-closed', () => {
  if (botProcess) botProcess.kill();
  if (process.platform !== 'darwin') app.quit();
});
app.on('activate', () => {});
app.on('before-quit', () => {
  if (botProcess) botProcess.kill();
});
