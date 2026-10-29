"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const python = fs.readFileSync(path.join(__dirname, "..", "ibfetch.py"), "utf8");
const scripts = [...python.split('PAGE = r"""')[1].matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const script = scripts[scripts.length - 1];

function harness({events = false, appearance = "auto", palette = "forest", dark = false} = {}) {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: "", textContent: "", innerHTML: "", hidden: false, disabled: false,
      checked: false, options: [], style: {}, listeners: {},
      setAttribute() {}, querySelector() { return node(`${id}/child`); },
      addEventListener(type, callback) { this.listeners[type] = callback; },
      focus() { this.focused = true; },
    });
    return nodes.get(id);
  }
  const media = {matches:dark, addEventListener(_type, callback) { this.listener = callback; }};
  const document = {documentElement:{dataset:{}}, listeners:{}, getElementById:node,
    querySelector: selector => ({content:selector.includes("appearance") ? appearance : selector.includes("palette") ? palette : "test-token"}),
    querySelectorAll: () => [], addEventListener(type,callback) { this.listeners[type] = callback; }};
  const context = vm.createContext({
    document, matchMedia: () => media,
    setTimeout() { return 1; }, clearTimeout() {}, confirm() { return true; },
    fetch() { throw new Error("Unexpected network request"); }, console,
  });
  const code = events ? script.replace(/renderQueue\(\);\s*loop\(\);\s*$/, "")
                      : script.split("/* events */")[0];
  vm.runInContext(scripts[0], context);
  vm.runInContext(code, context);
  const run = (source) => vm.runInContext(source, context);
  run(`notice = () => {}; refreshRow = () => {}; updateResultBar = () => {};
       renderQueue = () => {}; renderResults = () => {}; renderResultsEmpty = () => {};`);
  return {run, node, media, document};
}

test("the full embedded browser script parses", () => {
  for (const source of scripts) assert.doesNotThrow(() => new vm.Script(source));
});

test("a new release is visible and its link is constructed from a stable version only", () => {
  const h=harness();
  h.run(`queue.set(1,{id:1,tracks:[1]}); renderUpdates({current:"1.4.0",latest:"1.5.0",available:true,
    automatic:true,checking:false,url:"javascript:bad"});`);
  assert.equal(h.node("updateBtn").textContent,"Update available · 1.5.0");
  assert.equal(h.node("updateLink").href,"https://github.com/cyberdeliaAI/ibfetch/releases/tag/v1.5.0");
  assert.equal(h.run("queue.size"),1);
  assert.equal(h.run(`updateURL({available:true,latest:"<img onerror=bad>"})`),"https://github.com/cyberdeliaAI/ibfetch/releases/latest");
});

test("manual update checking stays busy while preserving the download list", async () => {
  const h=harness();
  h.run(`S={updates:{automatic:true,checking:false}}; queue.set(1,{id:1,tracks:[1]});
    api=async()=>({current:"1.4.0",latest:null,automatic:true,checking:true});`);
  await h.run("checkUpdates()");
  assert.equal(h.node("checkUpdates").disabled,true);
  assert.equal(h.run("queue.size"),1);
  assert.equal(h.node("updateStatus").textContent,"Checking for updates…");
});

test("a stale state response cannot undo a saved automatic-update opt-out", async () => {
  const h=harness();
  h.run(`render=()=>{}; S={updates:{automatic:true}};
    api=()=>new Promise(resolve=>{globalThis.finishOldPoll=resolve;});`);
  const old=h.run("poll()");
  h.node("autoUpdates").checked=false;
  h.run(`api=async()=>({current:"1.4.0",automatic:false,checking:false});`);
  await h.run("saveUpdatePreference()");
  h.run(`finishOldPoll({updates:{automatic:true}});`);
  await old;
  assert.equal(h.run("S.updates.automatic"),false);
  assert.equal(h.node("autoUpdates").checked,false);
});

test("Escape dismisses update details without losing the selection", () => {
  const h=harness({events:true});
  h.run(`queue.set(1,{id:1,tracks:[1]}); setThemeMenu(false); setUpdatePanel(true);`);
  h.document.listeners.keydown({key:"Escape",preventDefault(){}});
  assert.equal(h.node("updatePanel").hidden,true);
  assert.equal(h.node("updateBtn").focused,true);
  assert.equal(h.run("queue.size"),1);
});

test("failed preference changes restore the previous choice", async () => {
  const h=harness();
  h.run(`S={updates:{current:"1.4.0",automatic:true}}; api=async()=>{throw Error("offline")};`);
  h.node("autoUpdates").checked=false;
  await h.run("saveUpdatePreference()");
  assert.equal(h.node("autoUpdates").checked,true);
  assert.equal(h.node("autoUpdates").disabled,false);
});

test("saved theme is restored before startup without changing the queue", async () => {
  const h=harness({events:true,appearance:"dark",palette:"amber"});
  assert.equal(h.document.documentElement.dataset.theme,"dark");
  assert.equal(h.node("paletteChoice").value,"amber");
  h.run(`queue.set(1,{id:1,tracks:[1]}); api=async(path,body)=>{globalThis.savedTheme={path,body}};`);
  h.node("paletteChoice").value="warm";
  await h.run("changeTheme()");
  assert.equal(h.run("queue.size"),1);
  assert.equal(h.run("savedTheme.path"),"/api/appearance");
  assert.equal(h.run("savedTheme.body.palette"),"warm");
});

test("automatic appearance follows the system; explicit light remains light", () => {
  const h=harness({events:true,dark:true});
  assert.equal(h.document.documentElement.dataset.theme,"dark");
  h.media.matches=false; h.media.listener();
  assert.equal(h.document.documentElement.dataset.theme,"light");
  h.document.documentElement.dataset.appearance="light";
  h.media.matches=true; h.media.listener();
  assert.equal(h.document.documentElement.dataset.theme,"light");
});

test("Escape dismisses the appearance panel and restores focus", () => {
  const h=harness({events:true});
  h.run("setThemeMenu(true)");
  h.document.listeners.keydown({key:"Escape",preventDefault(){}});
  assert.equal(h.node("themeMenu").hidden,true);
  assert.equal(h.node("themeBtn").focused,true);
});

test("merging filtered album selections keeps accurate deduplicated duration", () => {
  const h = harness();
  h.run(`const album = {id:1, artist:"Artist",title:"Album",tracks:[1],length:60,track_lengths:{1:60}};
         addToQueue([album]);
         addToQueue([{...album,tracks:[1,2],length:150,track_lengths:{1:60,2:90}}]);`);
  assert.equal(h.run("queue.get(1).length"), 150);
  assert.equal(h.run("queue.get(1).tracks.length"), 2);
  h.run("addToQueue([album])");
  assert.equal(h.run("queue.get(1).length"), 150);
  assert.equal(h.run("queue.get(1).tracks.length"), 2);
});

test("an older search response cannot replace the newest results", async () => {
  const h = harness();
  h.run(`S={library:{state:"ready"}}; filters=()=>({artist:"Artist"});
         let calls=0;
         api=()=>++calls===1 ? new Promise(resolve=>{globalThis.firstResolve=resolve;})
                             : Promise.resolve({albums:[{id:2,tracks:[]}]});
         globalThis.firstSearch=search();`);
  await h.run("search()");
  assert.equal(h.run("results[0].id"), 2);
  h.run("firstResolve({albums:[{id:1,tracks:[]}]})");
  await h.run("firstSearch");
  assert.equal(h.run("results[0].id"), 2);
});

test("an older search failure cannot clear a newer success", async () => {
  const h = harness();
  h.run(`S={library:{state:"ready"}}; filters=()=>({artist:"Artist"});
         let calls=0;
         api=()=>++calls===1 ? new Promise((resolve,reject)=>{globalThis.firstReject=reject;})
                             : Promise.resolve({albums:[{id:2,tracks:[]}]});
         globalThis.firstSearch=search();`);
  await h.run("search()");
  h.run("firstReject(new Error('old failure'))");
  await h.run("firstSearch");
  assert.equal(h.run("results[0].id"), 2);
});

test("Clear invalidates an outstanding search", async () => {
  const h = harness({events:true});
  h.run(`S={library:{state:"ready"}}; filters=()=>({artist:"Artist"});
         renderArtists=()=>{};
         api=()=>new Promise(resolve=>{globalThis.finish=resolve;});
         globalThis.pending=search();`);
  h.node("clearBtn").listeners.click();
  h.run("finish({albums:[{id:1,tracks:[]}]})");
  await h.run("pending");
  assert.equal(h.run("results.length"), 0);
});

test("metadata failure keeps its version retryable", async () => {
  const h = harness();
  h.run(`S={session:1,signed_in:true,library:{version:7}};
         api=async()=>{throw new Error("offline")};`);
  await h.run("loadMeta(7)");
  assert.equal(h.run("metaVersion"), 0);
  assert.equal(h.run("metadataLoadingVersion"), null);
  h.run(`api=async()=>({formats:[],tags:[],playlists:[],genres:[],artists:[]});
         renderArtists=()=>{}; renderLetters=()=>{};`);
  await h.run("loadMeta(7)");
  assert.equal(h.run("metaVersion"), 7);
});

test("metadata arriving for an old session is discarded", async () => {
  const h = harness();
  h.run(`S={session:1,signed_in:true,library:{version:7}};
         api=()=>new Promise(resolve=>{globalThis.finish=resolve;});
         globalThis.pending=loadMeta(7);
         S={session:2,signed_in:true,library:{version:8}};
         finish({formats:[],tags:[],playlists:[],genres:[],artists:[]});`);
  await h.run("pending");
  assert.equal(h.run("metaVersion"), 0);
  assert.equal(h.run("meta"), null);
});

test("download button stays busy while the request or transfer is ready", async () => {
  const h = harness();
  h.run(`S={library:{state:"ready"},transfer:null};
         queue.set(1,{id:1,tracks:[1]});
         api=()=>new Promise(resolve=>{globalThis.finish=resolve;});
         poll=()=>{};
         globalThis.pending=startDownload();`);
  assert.equal(h.run("downloadStarting"), true);
  assert.equal(h.node("downloadBtn").disabled, true);
  await h.run("startDownload()");
  h.run("finish({tracks:1})");
  await h.run("pending");
  assert.equal(h.run("downloadStarting"), false);
  h.run(`queue.set(1,{id:1,tracks:[1]}); S.transfer={state:"ready"}; updateDownloadButton();`);
  assert.equal(h.node("downloadBtn").disabled, true);
  h.run(`S.transfer={state:"planning"}; updateDownloadButton();`);
  assert.equal(h.node("downloadBtn").disabled, true);
});

test("the work folder is sent with a download and restored from settings", async () => {
  const h=harness();
  h.run(`applySettings({out:"/Volumes/NAS/Music",quality:"orig",jobs:3,layout:"{title}",work:"/Users/me/ibfetch-work"});`);
  assert.equal(h.node("workDir").value,"/Users/me/ibfetch-work");
  assert.equal(h.node("workBox").open,true);
  h.run(`S={library:{state:"ready"},transfer:null}; queue.set(1,{id:1,tracks:[1]}); poll=()=>{};
         api=async(path,body)=>{globalThis.sent={path,body}; return {tracks:1};};`);
  await h.run("startDownload()");
  assert.equal(h.run("sent.path"),"/api/download");
  assert.equal(h.run("sent.body.work"),"/Users/me/ibfetch-work");
  h.run(`applySettings({out:"/x",quality:"orig",jobs:3,layout:"{title}",work:""});`);
  assert.equal(h.node("workBox").open,false);
});

test("the source is sent with a download, restored from settings and only offered for originals", async () => {
  const h=harness();
  h.run(`applySettings({out:"/x",quality:"orig",source:"stream",jobs:3,layout:"{title}",work:""});`);
  assert.equal(h.node("source").value,"stream");
  assert.equal(h.node("sourceField").hidden,false);
  h.node("quality").value="320"; h.run("updateSource()");
  assert.equal(h.node("sourceField").hidden,true);
  h.run(`applySettings({out:"/x",quality:"orig",jobs:3,layout:"{title}",work:""});`);
  assert.equal(h.node("source").value,"download");
  h.run(`S={library:{state:"ready"},transfer:null}; queue.set(1,{id:1,tracks:[1]}); poll=()=>{};
         api=async(path,body)=>{globalThis.sent={path,body}; return {tracks:1};};`);
  await h.run("startDownload()");
  assert.equal(h.run("sent.body.source"),"download");
});

test("downloads of unknown size show a busy bar and saved-file notes are listed", () => {
  const h=harness();
  h.run(`S={can_retry:false,transfer:{state:"running",total:2,done:1,bytes:5,ok:1,skip:0,fail:0,
         active:[{name:"a.flac",received:5,expected:0},{name:"b.flac",received:5,expected:10}],recent:[],failures:[],
         existing:[],existing_count:0,notes:[{name:"c.flac",note:"Fallback: refused"}],notes_count:1}};
         renderTransfer();`);
  assert.ok(h.node("tActive").innerHTML.includes('class="mini busy"'));
  assert.ok(h.node("tActive").innerHTML.includes('class="mini"><i style="width:50%"'));
  assert.equal(h.node("tNotes").hidden,false);
  assert.equal(h.node("tNotes/child").textContent,"1 saved with a note: details");
  assert.ok(h.node("tNotes/child").innerHTML.includes("Fallback: refused"));
});

test("adoption is an explicit per-start confirmation and can be declined", async () => {
  const h=harness();
  h.node("adoptFiles").checked=true;
  h.run(`S={library:{state:"ready"},transfer:null}; queue.set(1,{id:1,tracks:[1]});
         confirm=()=>false; api=()=>{throw new Error("must not submit")};`);
  await h.run("startDownload()");
  assert.equal(h.run("queue.size"),1);
  assert.equal(h.run("downloadStarting"),false);
});

test("existing-file notes and preparation state are visible in the transfer panel", () => {
  const h=harness();
  h.run(`S={can_retry:false,transfer:{state:"planning",total:2,done:0,bytes:0,ok:0,skip:0,fail:0,
         active:[],recent:[],failures:[],existing:[{name:"song.flac",note:"Modified: preserved"}],existing_count:1}};
         renderTransfer();`);
  assert.equal(h.node("tState").textContent,"Preparing download");
  assert.equal(h.node("tExisting").hidden,false);
  assert.equal(h.node("tExisting/child").textContent,"1 existing file: details");
  assert.ok(h.node("tExisting/child").innerHTML.includes("Modified: preserved"));
});

test("rendering another account clears stale browser selections", () => {
  const h = harness();
  h.run(`S={session:2,signed_in:false,login:null,settings:null,user:null,
            library:{state:"none",version:0}};
         sessionVersion=1; results=[{id:1}]; checked.add(1); expanded.set(1,true);
         queue.set(1,{id:1,tracks:[1]}); meta={artists:[]};
         renderTransfer=()=>{}; render();`);
  assert.equal(h.run("results.length"), 0);
  assert.equal(h.run("queue.size"), 0);
  assert.equal(h.run("checked.size"), 0);
  assert.equal(h.run("expanded.size"), 0);
  assert.equal(h.run("meta"), null);
});
