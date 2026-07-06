const { app, BrowserWindow } = require('electron');
const { spawn } = require('node:child_process');
const { join, dirname } = require('node:path');
const { existsSync, readFileSync } = require('node:fs');
const http = require('node:http');

const ROOT = join(__dirname, '..');
const BOT_DIR = existsSync(join(ROOT, 'ZeroxAiApp', 'telegram_bot.py'))
  ? join(ROOT, 'ZeroxAiApp')
  : join(process.resourcesPath, 'ZeroxAiApp');
const PORT = 8080;

function loadDotEnv() {
  const envPath = join(BOT_DIR, '.env');
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
}

loadDotEnv();

let botProcess = null;

function startBot() {
  return new Promise((resolve) => {
    botProcess = spawn(process.platform === 'win32' ? 'python' : 'python3', ['-u', 'telegram_bot.py'], {
      cwd: BOT_DIR,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });
    botProcess.stdout.on('data', (d) => process.stdout.write(`[bot] ${d}`));
    botProcess.stderr.on('data', (d) => process.stderr.write(`[bot-err] ${d}`));
    botProcess.on('exit', (code) => console.log(`Bot exited with code ${code}`));

    // wait for server to be ready
    const check = () => {
      const req = http.get(`http://127.0.0.1:${PORT}/health`, (res) => {
        if (res.statusCode === 200) resolve();
        else setTimeout(check, 500);
      });
      req.on('error', () => setTimeout(check, 500));
    };
    setTimeout(check, 1000);
  });
}

app.whenReady().then(async () => {
  await startBot();
  createWindow();
});

function createWindow() {
  const win = new BrowserWindow({
    width: 1280, height: 800,
    minWidth: 800, minHeight: 600,
    title: 'ZeroxAI Telegram',
    icon: join(ROOT, 'assets', 'icon-512.svg'),
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
    },
  });
  win.loadURL(`http://127.0.0.1:${PORT}`);
  // remove menu bar for cleaner look
  win.setMenuBarVisibility(false);
}

app.on('window-all-closed', () => {
  if (botProcess) botProcess.kill();
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on('before-quit', () => {
  if (botProcess) botProcess.kill();
});
