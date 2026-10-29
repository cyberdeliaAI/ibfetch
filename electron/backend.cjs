"use strict";

const {spawn} = require("node:child_process");
const path = require("node:path");

function backendCommand({packaged, resourcesPath, sourceRoot, python, platform = process.platform}) {
  if (packaged) return {file: path.join(resourcesPath, "backend", platform === "win32" ? "ibfetch-server.exe" : "ibfetch-server"), args: ["--desktop-host"]};
  return {file: python || (platform === "win32" ? "python" : "python3"),
          args: ["-B", path.join(sourceRoot, "ibfetch.py"), "--desktop-host"]};
}

function readyMessage(value, version) {
  if (value?.event !== "ready" || value.app !== "ibfetch" || value.protocol !== 1 || value.version !== version) throw new Error("The local server does not match this ibfetch version.");
  const url = new URL(value.url);
  if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port || url.username || url.password || url.pathname !== "/" || url.hash || !/^[A-Za-z0-9_-]{24,80}$/.test(url.searchParams.get("t") || "") || [...url.searchParams].length !== 1) throw new Error("Invalid ibfetch desktop address.");
  return {url: url.href, origin: url.origin};
}

class Backend {
  constructor(command, {version, cwd, env = process.env, onExit = () => {}, timeout = 30000, stopTimeout = 5000}) {
    Object.assign(this, {command, version, cwd, env, onExit, timeout, stopTimeout, stopping: false});
  }

  start() {
    return new Promise((resolve, reject) => {
      this.child = spawn(this.command.file, this.command.args, {cwd: this.cwd,
        env: {...this.env, PYTHONUNBUFFERED: "1", PYTHONDONTWRITEBYTECODE: "1"},
        windowsHide: true, stdio: ["pipe", "pipe", "pipe"]});
      this.child.stderr.resume(); // Never display account data or private URLs from stderr.
      this.child.stdin.on("error", () => {});
      let settled = false, buffer = "";
      const fail = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        this.stop();
        reject(new Error("ibfetch's local server could not start. Restart the app."));
      };
      const timer = setTimeout(fail, this.timeout);
      this.exited = new Promise(done => {
        this.child.once("error", () => { fail(); done(); });
        this.child.once("exit", () => {
          fail(); done();
          if (!this.stopping) this.onExit();
        });
      });
      this.child.stdout.setEncoding("utf8");
      this.child.stdout.on("data", chunk => {
        if (settled) return;
        buffer += chunk;
        if (buffer.length > 8192) { fail(); return; }
        const end = buffer.indexOf("\n");
        if (end < 0) return;
        try {
          const ready = readyMessage(JSON.parse(buffer.slice(0, end)), this.version);
          settled = true; clearTimeout(timer); resolve(ready);
        } catch { fail(); }
      });
    });
  }

  stop() {
    if (this.stopPromise) return this.stopPromise;
    this.stopping = true;
    if (this.child?.stdin.writable) this.child.stdin.end();
    // EOF allows graceful cancellation. A stuck read must not retain the app's
    // single-instance lock for the full network timeout. Only terminate our child.
    const timer = setTimeout(() => this.child?.kill("SIGKILL"), this.stopTimeout);
    this.stopPromise = (this.exited || Promise.resolve()).finally(() => clearTimeout(timer));
    return this.stopPromise;
  }
}

module.exports = {Backend, backendCommand, readyMessage};
