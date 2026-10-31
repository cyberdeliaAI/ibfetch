"use strict";
// Only the trusted app page gets these three sign-in operations. No URLs or tokens cross IPC.
const {contextBridge, ipcRenderer} = require("electron");
contextBridge.exposeInMainWorld("ibfetchSignIn", Object.freeze({
  open: requestId => ipcRenderer.invoke("signin:open", requestId),
  close: requestId => ipcRenderer.send("signin:close", requestId),
  onClosed: callback => {
    if (typeof callback === "function") ipcRenderer.on("signin:closed", (_event, message) => callback(message));
  },
}));
