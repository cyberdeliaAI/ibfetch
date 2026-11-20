"use strict";
// Only the trusted app page gets these sign-in operations. No URLs or tokens cross IPC.
const {contextBridge, ipcRenderer} = require("electron");
contextBridge.exposeInMainWorld("ibfetchSignIn", Object.freeze({
  open: requestId => ipcRenderer.invoke("signin:open", requestId),
  close: requestId => ipcRenderer.send("signin:close", requestId),
  reset: () => ipcRenderer.invoke("signin:reset"),
  onClosed: callback => {
    if (typeof callback === "function") ipcRenderer.on("signin:closed", (_event, message) => callback(message));
  },
}));

// Fixed update actions only: no URL, local path, command or credentials cross IPC.
const listenUpdate = callback => {
  if (typeof callback === "function") ipcRenderer.on("updates:status", (_event, value) => callback(value));
};
contextBridge.exposeInMainWorld("ibfetchUpdates", Object.freeze({
  status: () => ipcRenderer.invoke("updates:status"),
  offer: (version, automatic) => ipcRenderer.invoke("updates:offer", {version, automatic}),
  download: version => ipcRenderer.invoke("updates:download", version),
  install: version => ipcRenderer.invoke("updates:install", version),
  skip: version => ipcRenderer.invoke("updates:skip", version),
  configure: autoDownload => ipcRenderer.invoke("updates:configure", autoDownload),
  onStatus: listenUpdate,
}));
