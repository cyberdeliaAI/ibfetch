"use strict";
const fs = require("node:fs");

async function check({app, window, backend}) {
  try {
    await new Promise(resolve => {
      if (!window.webContents.isLoading()) resolve();
      else window.webContents.once("did-finish-load", resolve);
    });
    const preferences = window.webContents.getLastWebPreferences();
    const html = await window.webContents.executeJavaScript("document.documentElement.outerHTML");
    const appearance = await window.webContents.executeJavaScript("({...document.documentElement.dataset})");
    const branding = await window.webContents.executeJavaScript("(() => { const imgs = [...document.querySelectorAll('.powered img, .brand img')]; return imgs.length === 3 && imgs.every(img => img.complete && img.naturalWidth > 0); })()");
    if (!html.includes("Sign in to iBroadcast") || !branding || preferences.nodeIntegration || !preferences.sandbox || !preferences.contextIsolation || appearance.palette !== "ocean" || appearance.theme !== "dark") throw new Error("Smoke check failed");
    fs.writeFileSync(process.env.IBFETCH_SMOKE_REPORT, JSON.stringify({ok: true, version: app.getVersion(),
      isolated: true, palette: appearance.palette, theme: appearance.theme, backendPid: backend.child.pid}));
    window.destroy();
    app.quit();
  } catch {
    fs.writeFileSync(process.env.IBFETCH_SMOKE_REPORT, JSON.stringify({ok: false}));
    window.destroy(); app.quit();
  }
}
module.exports = {check};
