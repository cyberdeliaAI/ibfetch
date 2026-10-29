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
    if (url.protocol !== "https:" || url.username || url.password || url.port) return null;
    if (url.hostname === "oauth.ibroadcast.com" && /^\/device(?:\/|$)/.test(url.pathname)) return url.href;
    if (url.search || url.hash) return null;
    if (url.hostname === "www.ibroadcast.com" && url.pathname === "/") return url.href;
    // Fixed project destinations only; never trust a URL supplied by release metadata.
    if (url.hostname === "github.com" && /^\/cyberdeliaAI\/ibfetch\/(?:releases(?:\/latest|\/tag\/v[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})?|blob\/main\/LICENSE|actions\/workflows\/tests\.yml)$/.test(url.pathname)) return url.href;
    return null;
  } catch { return null; }
}

module.exports = {applicationUrl, externalUrl};
