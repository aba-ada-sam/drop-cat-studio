/**
 * Drop Cat Go Studio — Electron main process.
 * Opens a transparent acrylic window on Windows 11 and loads the local server.
 */
const { app, BrowserWindow, shell, nativeTheme } = require('electron');
const path = require('path');
const fs   = require('fs');
const http = require('http');

const PORT_FILE = path.join(__dirname, '..', '.dcs-port');

nativeTheme.themeSource = 'dark';

// ── Port discovery ────────────────────────────────────────────────────────────

function readPort() {
  try {
    const data = JSON.parse(fs.readFileSync(PORT_FILE, 'utf8'));
    return data.port || 7860;
  } catch {
    return 7860;
  }
}

// Poll .dcs-port until it appears (server writes it on startup)
async function waitForPortFile(maxMs = 60000) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    if (fs.existsSync(PORT_FILE)) return readPort();
    await sleep(300);
  }
  return readPort();
}

// Poll the server /api/system until it responds
async function waitForServer(port, maxMs = 30000) {
  const deadline = Date.now() + maxMs;
  while (Date.now() < deadline) {
    try {
      await httpGet(`http://127.0.0.1:${port}/api/system`, 1500);
      return true;
    } catch {
      await sleep(500);
    }
  }
  return false;
}

function httpGet(url, timeoutMs) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, res => { res.resume(); resolve(); });
    req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('timeout')); });
    req.on('error', reject);
  });
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Window ────────────────────────────────────────────────────────────────────

async function createWindow() {
  const port = await waitForPortFile();
  await waitForServer(port);

  const win = new BrowserWindow({
    width:    1400,
    height:   900,
    minWidth: 960,
    minHeight: 600,

    // Windows 11 acrylic — client area becomes a blurred-desktop material
    backgroundColor: '#00000000',
    backgroundMaterial: 'acrylic',

    // Keep the native title bar (standard Windows controls)
    titleBarStyle: 'default',

    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },

    icon: path.join(__dirname, '..', 'static', 'logo-512.png'),
    title: 'Drop Cat Go Studio',
    show: false,
  });

  // Show once DOM is ready to avoid white flash
  win.once('ready-to-show', () => win.show());

  // External links open in the default browser, not inside the app
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(`http://127.0.0.1:${port}`)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  win.loadURL(`http://127.0.0.1:${port}`);
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
