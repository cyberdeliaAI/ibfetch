"use strict";

function applicationUrl(value, origin) {
  try {
    const url = new URL(value);
    return url.origin === origin && url.pathname === "/" && !url.username && !url.password;
  } catch { return false; }
}

function externalUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password) return null;
    // Sign-in stays at iBroadcast; no arbitrary navigation or custom protocols.
    if (url.hostname !== "oauth.ibroadcast.com" || !url.pathname.startsWith("/device")) return null;
    return url.href;
  } catch { return null; }
}

module.exports = {applicationUrl, externalUrl};
