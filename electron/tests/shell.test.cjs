"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const {applicationUrl, externalUrl} = require("../security.cjs");
const {backendCommand, readyMessage, Backend} = require("../backend.cjs");
const info = require("../package.json");

test("Python, Electron and its lockfile have the same version", () => {
  const source=fs.readFileSync(path.join(__dirname,"../../ibfetch.py"),"utf8");
  assert.equal(source.match(/^VERSION = "([^"]+)"/m)[1],info.version);
  const lock=JSON.parse(fs.readFileSync(path.join(__dirname,"../package-lock.json"),"utf8"));
  assert.equal(lock.version,info.version);
  assert.equal(lock.packages[""].version,info.version);
});

test("only the matching private loopback readiness message is accepted", () => {
  const value = {event:"ready", app:"ibfetch", protocol:1, version:info.version,
                 url:"http://127.0.0.1:12345/?t=abcdefghijklmnopqrstuvwxyz"};
  assert.equal(readyMessage(value, info.version).origin, "http://127.0.0.1:12345");
  for (const bad of ["http://evil.invalid:1/?t=abcdefghijklmnopqrstuvwxyz", "file:///tmp/test", "http://127.0.0.1:1/", "http://127.0.0.1:1/?t=abcdefghijklmnopqrstuvwxyz&extra=1"])
    assert.throws(() => readyMessage({...value,url:bad}, info.version));
  assert.throws(() => readyMessage({...value, version:"other"}, info.version));
});

test("external links are restricted to HTTPS approval, branding and fixed ibfetch pages", () => {
  assert.ok(externalUrl("https://oauth.ibroadcast.com/device?code=test"));
  for (const value of ["javascript:alert(1)", "file:///tmp/a", "https://example.com/", "https://name:secret@oauth.ibroadcast.com/device", "http://oauth.ibroadcast.com/device"])
    assert.equal(externalUrl(value), null);
  for (const value of ["https://www.ibroadcast.com/", "https://github.com/cyberdeliaAI/ibfetch/releases/latest",
    "https://github.com/cyberdeliaAI/ibfetch/releases/tag/v1.5.0", "https://github.com/cyberdeliaAI/ibfetch/blob/main/LICENSE"])
    assert.equal(externalUrl(value), value);
  for (const value of ["https://oauth.ibroadcast.com/device-evil", "https://github.com/evil/ibfetch/releases/latest",
    "https://github.com/cyberdeliaAI/ibfetch/releases/latest?redirect=evil", "https://github.com/cyberdeliaAI/ibfetch/releases/tag/v1.5.0/extra",
    "https://www.ibroadcast.com:444/", "https://www.ibroadcast.com/other", "https://github.com/cyberdeliaAI/ibfetch/settings"])
    assert.equal(externalUrl(value), null);
});

test("renderer navigation is restricted to its own application root", () => {
  assert.ok(applicationUrl("http://127.0.0.1:12345/?t=token", "http://127.0.0.1:12345"));
  assert.equal(applicationUrl("http://127.0.0.1:12345/api/state", "http://127.0.0.1:12345"), false);
  assert.equal(applicationUrl("http://127.0.0.1:12346/", "http://127.0.0.1:12345"), false);
});

test("packaged applications always use the bundled backend", () => {
  const command = backendCommand({packaged:true,resourcesPath:"/resources",python:"untrusted",platform:"win32"});
  assert.ok(command.file.endsWith("ibfetch-server.exe"));
  assert.deepEqual(command.args,["--desktop-host"]);
});

test("the shell exposes only sign-in operations with no Node access or webviews", () => {
  const source=fs.readFileSync(path.join(__dirname,"../main.cjs"),"utf8");
  for(const setting of ["nodeIntegration: false", "contextIsolation: true", "sandbox: true", "webSecurity: true", "webviewTag: false"])
    assert.ok(source.includes(setting));
  assert.ok(source.includes('preload: path.join(__dirname, "sign-in-preload.cjs")'));
  const bridge=fs.readFileSync(path.join(__dirname,"../sign-in-preload.cjs"),"utf8");
  assert.ok(bridge.includes('exposeInMainWorld("ibfetchSignIn"'));
  assert.ok(!bridge.includes("sendSync") && !bridge.includes("access_token") && !bridge.includes("shell"));
});

test("a real source backend starts with a temporary home and exits on parent EOF", async () => {
  const os=require("node:os");
  const home=fs.mkdtempSync(path.join(os.tmpdir(),"ibfetch-host-test-"));
  fs.writeFileSync(path.join(home,"app-settings.json"),JSON.stringify({auto_updates:false}));
  const sourceRoot=path.resolve(__dirname,"../..");
  const backend=new Backend(backendCommand({packaged:false,sourceRoot,python:process.env.IBFETCH_PYTHON}),
    {version:info.version,cwd:sourceRoot,env:{...process.env,IBFETCH_HOME:home},timeout:90000});
  try {
    const ready=await backend.start();
    assert.ok(ready.origin.startsWith("http://127.0.0.1:"));
    const page=await fetch(ready.url);
    assert.equal(page.status,200);
    assert.ok((await page.text()).includes("<title>ibfetch</title>"));
    assert.ok(!page.headers.get("content-security-policy").includes("script-src 'unsafe-inline'"));
  } finally {
    await backend.stop();
    fs.rmSync(home,{recursive:true,force:true});
  }
});

test("closing terminates only its owned backend when a worker is stuck in a socket read", async () => {
  const os=require("node:os");
  const home=fs.mkdtempSync(path.join(os.tmpdir(),"ibfetch-stuck-host-"));
  fs.writeFileSync(path.join(home,"app-settings.json"),JSON.stringify({auto_updates:false}));
  const sourceRoot=path.resolve(__dirname,"../..");
  const python=backendCommand({packaged:false,sourceRoot,python:process.env.IBFETCH_PYTHON}).file;
  // A genuine Python executor worker blocks process exit after parent EOF, just
  // like a pending download read. The sockets and settings are entirely local.
  const code=`import concurrent.futures, os, socket\nfrom pathlib import Path\nimport ibfetch\na,b=socket.socketpair()\npool=concurrent.futures.ThreadPoolExecutor(1)\npool.submit(a.recv,1)\nibfetch.cmd_desktop(Path(os.environ['IBFETCH_HOME']))`;
  const backend=new Backend({file:python,args:["-B","-c",code]},
    {version:info.version,cwd:sourceRoot,env:{...process.env,IBFETCH_HOME:home},timeout:90000,stopTimeout:150});
  try {
    await backend.start();
    const started=Date.now();
    const stopped=backend.stop();
    assert.equal(backend.stop(),stopped);
    await stopped;
    assert.ok(Date.now()-started<5000,"shutdown must not wait for a network timeout");
    assert.ok(backend.child.exitCode!==null || backend.child.signalCode!==null);
    assert.equal(process.kill(process.pid,0),true,"the parent remains alive");
  } finally {
    await backend.stop();
    fs.rmSync(home,{recursive:true,force:true});
  }
});

test("the backend process never inherits Apple signing credentials", async () => {
  const os=require("node:os"), home=fs.mkdtempSync(path.join(os.tmpdir(),"ibfetch-signing-env-"));
  const sourceRoot=path.resolve(__dirname,"../.."), version=info.version;
  const code=`import os,json,sys\nassert not any(k.startswith(('CSC_','WIN_CSC_','APPLE_')) for k in os.environ)\nprint(json.dumps(dict(event='ready',app='ibfetch',protocol=1,version=${JSON.stringify(version)},url='http://127.0.0.1:12345/?t=abcdefghijklmnopqrstuvwxyz')),flush=True)\nsys.stdin.buffer.read(1)`;
  const python=backendCommand({packaged:false,sourceRoot,python:process.env.IBFETCH_PYTHON}).file;
  const backend=new Backend({file:python,args:["-B","-c",code]},{version,cwd:sourceRoot,
    env:{...process.env,APPLE_ID:"synthetic",APPLE_APP_SPECIFIC_PASSWORD:"synthetic",CSC_LINK:"synthetic",WIN_CSC_LINK:"synthetic"}});
  try { await backend.start(); } finally { await backend.stop();fs.rmSync(home,{recursive:true,force:true}); }
});
