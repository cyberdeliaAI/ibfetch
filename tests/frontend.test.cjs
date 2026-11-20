"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const python = fs.readFileSync(path.join(__dirname, "..", "ibfetch.py"), "utf8");
const scripts = [...python.split('PAGE = r"""')[1].matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const script = scripts[scripts.length - 1];

function harness({events = false, appearance = "auto", palette = "forest", dark = false, window = {open:()=>null}} = {}) {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value: "", textContent: "", innerHTML: "", hidden: false, disabled: false,
      checked: false, options: [], style: {}, listeners: {}, open: false,
      showModal() { this.open = true; }, close() { this.open = false; this.listeners.close?.(); },
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
    document, window: {addEventListener(){},...window}, URL, matchMedia: () => media,
    setTimeout() { return 1; }, clearTimeout() {}, confirm() { return true; },
    fetch() { throw new Error("Unexpected network request"); }, console,
  });
  const code = events ? script.replace(/renderQueue\(\);\s*loop\(\);\s*$/, "")
                      : script.split("/* events */")[0];
  vm.runInContext(scripts[0], context);
  vm.runInContext(code, context);
  const run = (source) => vm.runInContext(source, context);
  run(`globalThis.originalRenderResults = renderResults; globalThis.originalRenderResultsEmpty = renderResultsEmpty;`);
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

const pendingLogin = id => ({state:"waiting",request_id:id,code:"TEST-CODE",
  url:"https://oauth.ibroadcast.com/device?user_code=TEST-CODE",expires_at:Date.now()/1000+600});
function popup() {
  return {closed:false,document:{body:{}},location:{replace(url){this.url=url;}},
    close(){this.closed=true;},focus(){this.focused=true;}};
}
test("browser sign-in reserves a popup during the click and shows the provider after the code arrives", async () => {
  const child=popup(), calls=[];
  const h=harness({window:{open(){calls.push("popup");return child;}}});
  h.run(`S={signed_in:false,login:null}; poll=()=>{};
    api=()=>new Promise(resolve=>{globalThis.finishStart=resolve;});`);
  const started=h.run("beginSignIn()");
  assert.deepEqual(calls,["popup"]);
  assert.equal(child.opener,null);
  assert.equal(h.node("signinBtn").disabled,true);
  h.run(`finishStart({login:${JSON.stringify(pendingLogin("a".repeat(24)))}});`);
  await started;
  assert.equal(child.location.url,pendingLogin().url);
  assert.equal(h.node("signinFallback").open,false);
  assert.equal(h.node("signinCode").textContent,"TEST-CODE");
});
test("blocked popups and malformed provider links retain a safe browser/code fallback", async () => {
  const h=harness();
  h.run(`S={signed_in:false,login:null};poll=()=>{};
    api=async()=>({login:${JSON.stringify(pendingLogin("a".repeat(24)))}});`);
  await h.run("beginSignIn()");
  assert.equal(h.node("signinFallback").open,true);
  assert.equal(h.node("signinLink").href,pendingLogin().url);
  assert.equal(h.run('signInVerificationUrl({url:"javascript:alert(1)",plain_url:"https://evil.test"})'),"https://oauth.ibroadcast.com/device");
});
test("desktop sign-in sends only an opaque attempt ID and completion closes its window", async () => {
  const opened=[],closed=[];
  const h=harness({window:{ibfetchSignIn:{open:async id=>{opened.push(id);return true;},close:id=>closed.push(id)}}});
  h.run(`S={signed_in:false,login:null};poll=()=>{};
    api=async()=>({login:${JSON.stringify(pendingLogin("a".repeat(24)))}});`);
  await h.run("beginSignIn()");
  assert.deepEqual(opened,["a".repeat(24)]);
  h.run("S.signed_in=true;S.login=null;renderLogin();");
  assert.deepEqual(closed,["a".repeat(24)]);
});
test("cancelling an in-flight start cancels only its late attempt, never a newer login", async () => {
  const child=popup();
  const h=harness({window:{open:()=>child}});
  h.run(`S={signed_in:false,login:null};poll=()=>{};
    api=()=>new Promise(resolve=>{globalThis.finishStart=resolve;});`);
  const started=h.run("beginSignIn()");
  await h.run("cancelSignIn()");
  h.run(`globalThis.cancellations=[];api=async(path,body)=>{cancellations.push(body.request_id);};
    finishStart({login:${JSON.stringify(pendingLogin("a".repeat(24)))}});`);
  await started;
  assert.equal(child.closed,true);
  assert.deepEqual(JSON.parse(h.run("JSON.stringify(cancellations)")),["a".repeat(24)]);
  assert.equal(h.run("S.login"),null);
});
test("stale polling cannot restore a cancelled sign-in or reopen its popup", async () => {
  const h=harness();
  h.run(`S={signed_in:false,login:${JSON.stringify(pendingLogin("a".repeat(24)))}};render=()=>{};
    api=()=>new Promise(resolve=>{globalThis.oldState=resolve;});`);
  const old=h.run("poll()");
  h.run("api=async(path)=>path==='/api/state'?{signed_in:false,login:null}:{};");
  await h.run("cancelSignIn()");
  h.run(`oldState({signed_in:false,login:${JSON.stringify(pendingLogin("a".repeat(24)))}});`);
  await old;
  assert.equal(h.run("S.login"),null);
});
test("provider-window failures offer browser fallback and ignore stale close events", async () => {
  let callback;
  const h=harness({window:{ibfetchSignIn:{open:async()=>true,close(){},onClosed(fn){callback=fn;}}}});
  h.run(`S={signed_in:false,login:${JSON.stringify(pendingLogin("a".repeat(24)))}};
    signInFlow.requestId="${"a".repeat(24)}";`);
  callback({requestId:"b".repeat(24),reason:"closed"});
  assert.equal(h.run("S.login.state"),"waiting");
  callback({requestId:"a".repeat(24),reason:"load-failed"});
  assert.equal(h.node("signinFallback").open,true);
  assert.match(h.node("signinStatus").textContent,/could not load/);
  assert.equal(h.run("S.login.state"),"waiting");
});


test("quitting closes the sign-in popup and discards a late state response", async () => {
  const child=popup(), h=harness({window:{open:()=>child}});
  h.run(`S={signed_in:false,login:null}; signInFlow.popup=null;
    api=()=>new Promise(resolve=>{globalThis.finishState=resolve;});`);
  const pending=h.run("poll()");
  h.run("signInFlow.popup = window.open(); stopped=true;showBye();");
  h.run(`finishState({signed_in:false,login:${JSON.stringify(pendingLogin("a".repeat(24)))}});`);
  await pending;
  assert.equal(child.closed,true);
  assert.equal(h.node("signin").hidden,true);
  assert.equal(h.node("bye").hidden,false);
  assert.equal(h.run("S.login"),null);
});


test("the # group selects every matching artist, including those beyond the sidebar cap", async () => {
  const h=harness({events:true});
  h.run(`S={library:{state:"ready"}}; letter="#";
    meta={artists:Array.from({length:605},(_,i)=>[i+" Artist",1])};
    renderArtists(); api=async(path,body)=>{globalThis.sent={path,body};return {albums:[]};};`);
  assert.ok(h.node("artistList").innerHTML.includes('data-initial="#"'));
  assert.ok(h.node("artistList").innerHTML.includes('Every artist on #'));
  assert.ok(h.node("artistList").innerHTML.includes('105 more'));
  h.node("fGenre").value="Jazz";
  h.node("artistList").listeners.click({target:{closest:()=>({dataset:{initial:"#"}})}});
  assert.equal(h.run('sent.body.artist_initial'),"#");
  assert.equal(h.run('sent.body.artist'),"");
  assert.equal(h.run('sent.body.genre'),"Jazz");
  assert.equal(h.node("fArtist").placeholder,"Every artist on #");
  h.node("clearBtn").listeners.click();assert.equal(h.run('activeInitial'),null);
});

test("typing an artist replaces a group without changing literal wildcard semantics", () => {
  const h=harness({events:true});
  h.run('activeInitial="#";');h.node("fArtist").value="#*";
  h.node("fArtist").listeners.input();
  assert.equal(h.run('filters().artist_initial'),null);
  assert.equal(h.run('filters().artist'),"#*");
  for(const [name,expected] of [["10cc","#"],["# Hash","#"],["(Moods)","#"],["日本","#"],["ß Band","#"],["Élan","E"],["Zulu","Z"]])
    assert.equal(h.run(`letterOf(${JSON.stringify(name)})`),expected);
  h.node("fSkipThe").checked=true;
  assert.equal(h.run('letterOf("The 2 Bears")'),"#");
});

test("album sorting toggles natural order while preserving selected albums and the queue", () => {
  const h=harness({events:true});
  h.run(`results=[{id:1,title:"Album 10",artist:"Zulu",year:2000,tracks:[1],formats:"FLAC",length:1},
    {id:2,title:"Album 2",artist:"Artist",year:1990,tracks:[2],formats:"FLAC",length:2}];
    checked.add(2);queue.set(2,results[1]);expanded.set(1,true);lastClick=1;sortResults("title");`);
  assert.deepEqual(Array.from(h.run('results.map(a=>a.id)')),[2,1]);
  assert.equal(h.run('checked.has(2) && queue.has(2)'),true);
  assert.equal(h.run('expanded.size'),0);assert.equal(h.run('lastClick'),-1);
  h.run('originalRenderResults()');
  assert.ok(h.node('results').innerHTML.includes('aria-sort="ascending"'));
  assert.ok(h.node('results').innerHTML.includes('data-sort="title"'));
  h.node('results').listeners.click({target:{closest:s=>s==='button[data-sort]'?{dataset:{sort:'title'}}:null}});
  assert.deepEqual(Array.from(h.run('results.map(a=>a.id)')),[1,2]);
  assert.equal(h.run('checked.has(2) && queue.has(2)'),true);
});

test("chosen album sorting is applied to new searches and numeric columns sort as numbers", async () => {
  const h=harness();
  h.run(`S={library:{state:"ready"}}; sortResults("title"); filters=()=>({artist:"Artist"});
    api=async()=>({albums:[{id:1,title:"Z",year:2000,tracks:[1,2]}, {id:2,title:"A",year:1999,tracks:[3]}]});`);
  await h.run('search()');assert.deepEqual(Array.from(h.run('results.map(a=>a.id)')),[2,1]);
  h.run('sortResults("tracks")');assert.deepEqual(Array.from(h.run('results.map(a=>a.id)')),[2,1]);
  h.run('sortResults("year")');assert.deepEqual(Array.from(h.run('results.map(a=>a.id)')),[2,1]);
});

test("sign-out removes old account rows and filters before waiting for provider cookie reset", async () => {
  let release,resetCalls=0;
  const h=harness({window:{ibfetchSignIn:{reset:()=>{resetCalls++;return new Promise(resolve=>{release=resolve;});}}}});
  h.run(`S={session:1,signed_in:true,library:{state:"ready",version:1},user:{name:"First",premium:true}};
    sessionVersion=1; meta={artists:[["First Artist",1]]}; letter="#"; activeInitial="#";
    results=[{id:1,title:"Old account album"}];checked.add(1);queue.set(1,results[0]);expanded.set(1,true);
    renderResultsEmpty=originalRenderResultsEmpty;api=async()=>({});poll=async()=>{};`);
  h.node('results').innerHTML='Old account album';h.node('fArtist').value='First Artist';
  h.node('artistFind').value='First';h.node('fTag').value='Old tag';h.node('outDir').value='/keep/output';
  const pending=h.run('signOut()');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(resetCalls,1);assert.equal(h.run('signingOut'),true);
  assert.equal(h.run('S.signed_in'),false);assert.equal(h.run('queue.size + results.length + checked.size'),0);
  assert.equal(h.run('activeInitial'),null);assert.equal(h.node('fArtist').value,'');assert.equal(h.node('fTag').value,'');
  assert.equal(h.node('artistFind').value,'');assert.equal(h.node('outDir').value,'/keep/output');
  assert.ok(!h.node('results').innerHTML.includes('Old account album'));
  await h.run('beginSignIn()');assert.equal(h.run('signInFlow.opening'),false);
  release(true);await pending;assert.equal(h.run('signingOut'),false);assert.equal(h.node('signinBtn').disabled,false);
});

test("a ready second account cannot display first-account rows while its metadata arrives", () => {
  const h=harness();
  h.run(`S={session:2,signed_in:true,library:{state:"ready",version:2},user:{name:"Second",premium:true}};
    sessionVersion=1;loadMeta=()=>{};renderResultsEmpty=originalRenderResultsEmpty;
    results=[{id:1,title:"First album"}];queue.set(1,results[0]);`);
  h.node('results').innerHTML='First album';h.node('fArtist').value='First Artist';h.run('render()');
  assert.equal(h.node('fArtist').value,'');assert.equal(h.run('results.length'),0);
  assert.equal(h.run('queue.size'),0);assert.ok(!h.node('results').innerHTML.includes('First album'));
});

test("a refused backend logout preserves the connected account and queue", async () => {
  let resets=0;
  const h=harness({window:{ibfetchSignIn:{reset:async()=>{resets++;return true;}}}});
  h.run(`S={session:1,signed_in:true,library:{state:"ready"}};queue.set(1,{id:1});
    api=async()=>{throw Error("Stop the download before signing out");};poll=()=>{};`);
  await h.run('signOut()');
  assert.equal(h.run('S.signed_in'),true);assert.equal(h.run('queue.size'),1);assert.equal(resets,0);
});

test("quitting while sign-out is pending cannot restore the sign-in screen", async () => {
  const h=harness();
  h.run(`S={session:1,signed_in:true,library:{state:"ready"}};
    api=()=>new Promise(resolve=>{globalThis.finishSignOut=resolve;});
    render=()=>{throw Error("must not render after quit");};poll=()=>{throw Error("must not poll after quit");};`);
  const pending=h.run('signOut()');h.run('stopped=true;finishSignOut({});');await pending;
  assert.equal(h.run('signingOut'),false);
});

test("browser sign-out discards an old account's in-flight search without a native bridge", async () => {
  const h=harness();
  h.run(`S={session:1,signed_in:true,library:{state:"ready",version:1}};
    renderResultsEmpty=originalRenderResultsEmpty;
    api=()=>new Promise(resolve=>{globalThis.finishOldSearch=resolve;});`);
  h.node('fArtist').value='First Artist';const search=h.run('search()');
  h.run('api=async()=>({});poll=()=>{};');await h.run('signOut()');
  h.run('finishOldSearch({albums:[{id:1,title:"Old account album"}]});');await search;
  assert.equal(h.run('S.signed_in'),false);assert.equal(h.run('results.length'),0);
  assert.ok(!h.node('results').innerHTML.includes('Old account album'));
});

test("update details format bounded notes without loading provider HTML or images", () => {
  const h=harness();
  const notes='# New release\n\n**Changes** and `code`\n- First\n- Second\n\n| Item | Status |\n| -- | -- |\n| Download | Ready |\n\n<img src="https://evil.invalid/never" onerror="bad()">\n![hidden](https://evil.invalid/image.png)\n[Bad](javascript:bad())\n[User](https://user:pass@example.com/)';
  const html=h.run(`renderReleaseNotes(${JSON.stringify(notes)})`);
  assert.match(html,/<h3>New release<\/h3>/);assert.match(html,/<strong>Changes<\/strong>/);
  assert.match(html,/<code>code<\/code>/);assert.match(html,/<ul>/);assert.match(html,/<table>/);
  assert.doesNotMatch(html,/<img|<script|href="javascript:|href="https:\/\/user:/);
  assert.match(html,/&lt;img/);
  const bounded=h.run('renderReleaseNotes("x".repeat(50000)+"MUST_NOT_APPEAR")');assert.ok(!bounded.includes('MUST_NOT_APPEAR'));
  assert.equal(h.run('renderReleaseNotes(null)'), '');
  assert.equal(h.run('renderReleaseNotes("[".repeat(50000))'),'<p>'+'['.repeat(50000)+'</p>');
});

test("native progress keeps update notes and selections stable and never enables install before download", () => {
  const h=harness({events:true});
  h.run(`queue.set(1,{id:1}); checked.add(7); renderUpdates({current:"1.7.0",latest:"1.8.0",available:true,automatic:false,notes:"## Changes\\nSafe text"});
    applyDesktopUpdate({revision:1,supported:true,version:"1.8.0",state:"available",autoDownload:false});openUpdateDialog();`);
  assert.equal(h.node('updateDialog').open,true);assert.equal(h.node('desktopDownload').hidden,false);
  assert.equal(h.node('desktopInstall').hidden,true);
  const notes=h.node('releaseNotes').innerHTML;h.node('releaseNotes').scrollTop=42;
  h.run('applyDesktopUpdate({revision:2,supported:true,version:"1.8.0",state:"downloading",progress:47,autoDownload:false});');
  assert.match(h.node('desktopUpdateStatus').textContent,/47/);assert.equal(h.node('desktopProgress').value,47);
  assert.equal(h.node('releaseNotes').innerHTML,notes);assert.equal(h.node('releaseNotes').scrollTop,42);
  h.run('applyDesktopUpdate({revision:1,supported:true,version:"1.8.0",state:"available"});');
  assert.equal(h.node('desktopProgress').value,47);
  h.run('applyDesktopUpdate({revision:3,supported:true,version:"1.8.0",state:"ready",autoDownload:false});');
  assert.equal(h.node('desktopInstall').hidden,false);assert.equal(h.node('desktopInstall').disabled,true);
  h.run('queue.clear();refreshNativeUpdateControls();');assert.equal(h.node('desktopInstall').disabled,false);
  assert.equal(h.run('checked.has(7)'),true);
  h.node('updateLater').listeners.click();assert.equal(h.node('updateDialog').open,false);
});

test("manual checks do not implicitly allow automatic downloading on following polls", async () => {
  const offers=[];
  const h=harness({window:{ibfetchUpdates:{offer:async(version,automatic)=>{offers.push({version,automatic});return {revision:1,supported:false,version};}}}});
  h.run('S={updates:{current:"1.7.0",latest:"1.8.0",available:true,automatic:true,checking:false,checked_at:1}};api=async()=>({...S.updates,checking:true});');
  await h.run('checkUpdates()');
  h.run('renderUpdates({...S.updates,checking:false,checked_at:2});');await new Promise(resolve=>setImmediate(resolve));
  h.run('renderUpdates({...S.updates,checking:false,checked_at:2});');await new Promise(resolve=>setImmediate(resolve));
  assert.ok(offers.length>0&&offers.every(o=>!o.automatic));
  h.run('renderUpdates({...S.updates,checking:false,checked_at:3});');await new Promise(resolve=>setImmediate(resolve));
  assert.equal(offers.at(-1).automatic,true);
});

test("Homebrew and unsupported copies show manual instructions without installer buttons", () => {
  const h=harness();h.run('renderUpdates({current:"1.7.0",latest:"1.8.0",available:true,automatic:false});applyDesktopUpdate({revision:1,supported:false,reason:"homebrew",version:"1.8.0"});openUpdateDialog();');
  assert.match(h.node('desktopUpdateStatus').textContent,/brew upgrade --cask cyberdeliaAI\/apps\/ibfetch/);
  assert.equal(h.node('desktopInstall').hidden,true);assert.equal(h.node('desktopDownload').hidden,true);
  assert.equal(h.node('desktopAutoLabel').hidden,true);
});

test("skip hides a notification until an explicit check while keeping the download list", async () => {
  const h=harness({window:{ibfetchUpdates:{skip:async version=>({revision:2,supported:true,version,state:'available',skipped:version})}}});
  h.run('queue.set(1,{id:1});renderUpdates({current:"1.7.0",latest:"1.8.0",available:true,automatic:false});applyDesktopUpdate({revision:1,supported:true,version:"1.8.0",state:"available"});openUpdateDialog();');
  await h.run('nativeUpdateAction("skip")');
  assert.equal(h.node('updateDialog').open,false);assert.equal(h.node('updateBtn').textContent,'ibfetch 1.7.0');
  h.run('desktopUpdate.explicit=true;renderUpdates(releaseData);');assert.match(h.node('updateBtn').textContent,/Update available/);
  assert.equal(h.run('queue.size'),1);
});

test("available updates keep manual checks and opt-out reachable for browser, Homebrew and native copies", async () => {
  for (const native of [null,{revision:1,supported:false,reason:'homebrew',version:'1.8.0'},
    {revision:1,supported:true,state:'available',version:'1.8.0'}]) {
    const h=harness({events:true});
    h.run(`S={updates:{current:'1.7.0',latest:'1.8.0',available:true,automatic:true,checking:false}};
      queue.set(1,{id:1});checked.add(2);renderUpdates(S.updates);
      api=async(path,body)=>{globalThis.lastUpdateAction=path;return {...S.updates,automatic:body.automatic??S.updates.automatic};};`);
    if(native)h.run(`applyDesktopUpdate(${JSON.stringify(native)});`);
    h.node('updateBtn').listeners.click();assert.equal(h.node('updateDialog').open,true);
    let stopped=false;h.node('updateSettings').listeners.click({stopPropagation(){stopped=true;}});
    assert.equal(stopped,true,'the outside-click handler must not close settings immediately');
    assert.equal(h.node('updateDialog').open,false);assert.equal(h.node('updatePanel').hidden,false);
    assert.equal(h.node('checkUpdates').focused,true);
    const focus=h.node('updateBtn').focus;
    h.node('updateBtn').focus=()=>{throw Error('a queued dialog close must not steal focus from settings');};
    h.node('updateDialog').listeners.close();h.node('updateBtn').focus=focus;
    h.node('autoUpdates').checked=false;await h.node('autoUpdates').listeners.change();
    assert.equal(h.run('S.updates.automatic'),false);assert.equal(h.run('lastUpdateAction'),'/api/updates/settings');
    await h.node('checkUpdates').listeners.click();assert.equal(h.run('lastUpdateAction'),'/api/updates/check');
    assert.equal(h.run('queue.has(1)&&checked.has(2)'),true);
    h.node('updateDetails').listeners.click();assert.equal(h.node('updateDialog').open,true);
  }
});
