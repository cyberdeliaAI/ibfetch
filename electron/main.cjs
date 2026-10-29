"use strict";

const {app, BrowserWindow, Menu, dialog, shell, session} = require("electron");
const path = require("node:path");
const {Backend, backendCommand} = require("./backend.cjs");
const {applicationUrl, externalUrl} = require("./security.cjs");
const info = require("./package.json");

app.setName("ibfetch");
if (process.env.IBFETCH_ELECTRON_PROFILE) app.setPath("userData", path.resolve(process.env.IBFETCH_ELECTRON_PROFILE));
app.enableSandbox();
let window, backend, origin, finished = false, stopping;

function openExternal(value) {
  const safe = externalUrl(value);
  if (safe) shell.openExternal(safe).catch(() => dialog.showErrorBox("Could not open iBroadcast", "Open iBroadcast's approval page in your browser."));
}

function createWindow(url) {
  const ses = session.fromPartition("persist:ibfetch");
  ses.setPermissionCheckHandler(() => false);
  ses.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  ses.setDevicePermissionHandler(() => false);
  ses.on("will-download", event => event.preventDefault());
  // The renderer only needs its own loopback backend. OAuth opens in the system browser.
  ses.webRequest.onBeforeRequest((details, callback) => {
    let allowed = false;
    try { allowed = new URL(details.url).origin === origin; } catch { /* deny */ }
    callback({cancel: !allowed});
  });
  window = new BrowserWindow({title: "ibfetch", width: 1320, height: 850, minWidth: 420,
    minHeight: 600, show: false, backgroundColor: "#121915", webPreferences: {
      partition: "persist:ibfetch", nodeIntegration: false, contextIsolation: true,
      sandbox: true, webSecurity: true, webviewTag: false, devTools: !app.isPackaged,
      allowRunningInsecureContent: false,
    }});
  const contents = window.webContents;
  contents.setWindowOpenHandler(({url: value}) => { openExternal(value); return {action: "deny"}; });
  contents.on("will-navigate", (event, value) => {
    if (!applicationUrl(value, origin)) { event.preventDefault(); openExternal(value); }
  });
  contents.on("will-redirect", (event, value) => { if (!applicationUrl(value, origin)) event.preventDefault(); });
  contents.on("will-attach-webview", event => event.preventDefault());
  contents.on("will-prevent-unload", event => {
    const choice = dialog.showMessageBoxSync(window, {type: "question", title: "Close ibfetch?",
      message: "A download or unsent download list is active.",
      detail: "Closing stops downloads and discards the unsent list. Matching partial downloads can be resumed later.",
      buttons: ["Keep open", "Close ibfetch"], defaultId: 0, cancelId: 0});
    if (choice === 1) event.preventDefault();
  });
  contents.on("render-process-gone", () => { window?.destroy(); app.quit(); });
  window.once("ready-to-show", () => window.show());
  window.on("closed", () => { window = null; app.quit(); });
  window.loadURL(url).catch(() => { dialog.showErrorBox("Could not open ibfetch", "Restart the desktop app."); window?.destroy(); app.quit(); });
}

app.on("before-quit", event => {
  if (finished) return;
  event.preventDefault();
  if (window && !window.isDestroyed()) { window.close(); return; }
  stopping ||= (async () => { await backend?.stop(); finished = true; app.quit(); })();
});
app.on("window-all-closed", () => app.quit());

if (!app.requestSingleInstanceLock()) { finished = true; app.quit(); }
else {
  app.on("second-instance", () => { if (window) { if (window.isMinimized()) window.restore(); window.show(); window.focus(); } });
  app.whenReady().then(async () => {
    const sourceRoot = path.resolve(__dirname, "..");
    backend = new Backend(backendCommand({packaged: app.isPackaged, resourcesPath: process.resourcesPath,
      sourceRoot, python: process.env.IBFETCH_PYTHON}), {version: info.version,
      cwd: app.isPackaged ? process.resourcesPath : sourceRoot,
      onExit: () => { window?.destroy(); app.quit(); }});
    const ready = await backend.start();
    if (stopping) return;
    origin = ready.origin;
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      ...(process.platform === "darwin" ? [{label: "ibfetch", submenu: [{role: "about"}, {role: "quit"}]}] : []),
      {role: "editMenu"}, {label: "View", submenu: [{role: "resetZoom"}, {role: "zoomIn"}, {role: "zoomOut"}, {role: "togglefullscreen"}]},
    ]));
    createWindow(ready.url);
    if (process.env.IBFETCH_SMOKE_REPORT) require("./smoke.cjs").check({app, window, backend});
  }).catch(() => { dialog.showErrorBox("Could not start ibfetch", "The bundled local server could not start. Restart the app."); app.quit(); });
}
