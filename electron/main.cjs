"use strict";

const {app, BrowserWindow, Menu, dialog, shell, session, screen} = require("electron");
const path = require("node:path");
const {Backend, backendCommand} = require("./backend.cjs");
const {applicationUrl, externalUrl} = require("./security.cjs");
const info = require("./package.json");
const {windowSize, rememberWindowSize} = require("./window-size.cjs");
const {installSignIn, signInApi} = require("./sign-in.cjs");
const {DesktopUpdates, installation, installUpdateBridge} = require("./updates.cjs");

app.setName("ibfetch");
if (process.env.IBFETCH_ELECTRON_PROFILE) app.setPath("userData", path.resolve(process.env.IBFETCH_ELECTRON_PROFILE));
app.enableSandbox();
let window, backend, origin, signIn, desktopUpdates, backendAddress, finished = false, stopping;
let updateShutdownRequested = false;

async function updateCanRestart() {
  if (stopping || !backend?.child || backend.child.exitCode !== null || backend.child.signalCode !== null
      || !window || window.isDestroyed()) return false;
  const state = await signInApi(backendAddress, "/api/state");
  if (!state || typeof state.signed_in !== "boolean" || !("transfer" in state)) return false;
  if (state.login || ["ready", "planning", "running"].includes(state.transfer?.state)) return false;
  const pending = await window.webContents.executeJavaScript("Boolean(queue.size || downloadStarting || signingOut || signInFlow.opening)");
  return !pending && !stopping && backend.child.exitCode === null && backend.child.signalCode === null;
}

function finishQuit() {
  stopping ||= (async () => {
    const result = await backend?.stop();
    finished = true;
    if (updateShutdownRequested && desktopUpdates?.installing) {
      if (result?.code === 0 && !result.signal && !result.forced) {
        try { if (desktopUpdates.finish()) return; } catch { /* show a bounded message below */ }
      }
      desktopUpdates.defer("install");
      dialog.showErrorBox("Update ibfetch", "The update could not be installed safely. Start ibfetch again or use the official release download.");
    }
    app.quit();
  })();
  return stopping;
}

function openExternal(value) {
  const safe = externalUrl(value);
  if (safe) shell.openExternal(safe).catch(() => dialog.showErrorBox("Could not open link", "Open the link in your browser."));
}

function createWindow(url) {
  const ses = session.fromPartition("persist:ibfetch");
  ses.setPermissionCheckHandler(() => false);
  ses.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  ses.setDevicePermissionHandler(() => false);
  ses.on("will-download", event => event.preventDefault());
  // The renderer only needs its own loopback backend. Approved links open in the system browser.
  ses.webRequest.onBeforeRequest((details, callback) => {
    let allowed = false;
    try { allowed = new URL(details.url).origin === origin; } catch { /* deny */ }
    callback({cancel: !allowed});
  });
  const sizeFile = path.join(app.getPath("userData"), "main-window.json");
  const size = windowSize(sizeFile, {width: 1320, height: 850, minWidth: 420, minHeight: 600},
                          screen.getPrimaryDisplay().workArea);
  window = new BrowserWindow({title: "ibfetch", ...size, show: false, backgroundColor: "#121915", webPreferences: {
      preload: path.join(__dirname, "sign-in-preload.cjs"),
      partition: "persist:ibfetch", nodeIntegration: false, contextIsolation: true,
      sandbox: true, webSecurity: true, webviewTag: false, devTools: !app.isPackaged,
      allowRunningInsecureContent: false,
    }});
  rememberWindowSize(window, sizeFile);
  signIn = installSignIn(window, url);
  const contents = window.webContents;
  contents.setWindowOpenHandler(({url: value}) => { openExternal(value); return {action: "deny"}; });
  contents.on("will-navigate", (event, value) => {
    if (!applicationUrl(value, origin)) { event.preventDefault(); openExternal(value); }
  });
  contents.on("will-redirect", (event, value) => { if (!applicationUrl(value, origin)) event.preventDefault(); });
  contents.on("will-attach-webview", event => event.preventDefault());
  contents.on("will-prevent-unload", event => {
    if (desktopUpdates?.installing) { desktopUpdates.defer("busy"); updateShutdownRequested = false; }
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
  if (desktopUpdates?.installing && !updateShutdownRequested) desktopUpdates.defer();
  event.preventDefault();
  if (window && !window.isDestroyed()) { window.close(); return; }
  finishQuit();
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
    origin = ready.origin; backendAddress = ready.url;
    const updateInstallation = installation({packaged: app.isPackaged, platform: process.platform, arch: process.arch,
      execPath: process.execPath, resourcesPath: process.resourcesPath});
    desktopUpdates = new DesktopUpdates({...updateInstallation, current: info.version, platform: process.platform, arch: process.arch,
      file: path.join(app.getPath("userData"), "desktop-updates.json"),
      createUpdater: () => require("./native-updater.cjs").createNativeUpdater(process.platform),
      onChange: value => { if (window && !window.isDestroyed()) window.webContents.send("updates:status", value); },
      onInstallError: () => {
        finished = true;
        dialog.showErrorBox("Update ibfetch", "The update could not be installed. Start ibfetch again or use the official release download.");
        app.quit();
      },
      onConfirm: async () => {
        if (!await updateCanRestart()) {
          desktopUpdates.defer("busy");
          if (window && !window.isDestroyed()) await dialog.showMessageBox(window, {type: "info", title: "Update ibfetch",
            message: "Finish or stop the download and empty the download list before installing the update.", buttons: ["Keep ibfetch open"]});
          return false;
        }
        const choice = await dialog.showMessageBox(window, {type: "question", title: "Update ibfetch",
          message: "Restart ibfetch and install the downloaded update?", detail: "Your saved settings, sign-in and music are kept.",
          buttons: ["Later", "Restart and install"], defaultId: 0, cancelId: 0});
        if (choice.response !== 1) return false;
        if (!await updateCanRestart()) { desktopUpdates.defer("busy"); return false; }
        return true;
      },
      onInstall: () => { updateShutdownRequested = true; app.quit(); },
    });
    installUpdateBridge({ipcMain: require("electron").ipcMain, window: () => window, origin: () => origin, applicationUrl, updates: desktopUpdates});
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      ...(process.platform === "darwin" ? [{label: "ibfetch", submenu: [{role: "about"}, {role: "quit"}]}] : []),
      {role: "editMenu"}, {label: "View", submenu: [{role: "resetZoom"}, {role: "zoomIn"}, {role: "zoomOut"}, {role: "togglefullscreen"}]},
    ]));
    createWindow(ready.url);
    if (process.env.IBFETCH_SIGN_IN_SMOKE_REPORT) require("./sign-in-smoke.cjs").check({app, window, backend, signIn});
    if (process.env.IBFETCH_SMOKE_REPORT) require("./smoke.cjs").check({app, window, backend, desktopUpdates});
  }).catch(() => { dialog.showErrorBox("Could not start ibfetch", "The bundled local server could not start. Restart the app."); app.quit(); });
}
