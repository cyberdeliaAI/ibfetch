"use strict";

// Explicit isolated fixture. Real preload/IPC and pointer input; fake packages,
// no update network requests and no installer execution.
const assert = require("node:assert/strict");
const {EventEmitter} = require("node:events");
const {packageName} = require("./updates.cjs");

async function checkUpdates({window, updates}) {
  for (const name of ["MacUpdater","NsisUpdater","DebUpdater"]) assert.equal(typeof require("electron-updater")[name],"function");
  const transport = require("./native-updater.cjs").createNativeUpdater(process.platform);
  assert.equal(transport.autoDownload,false);assert.equal(transport.autoInstallOnAppQuit,false);
  assert.notEqual(transport.netSession,window.webContents.session);
  const owner=window.webContents,bounds=window.getBounds();
  const run=code=>owner.executeJavaScript(code), frame=()=>run("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))");
  const until=async code=>{
    for(let i=0;i<150;i++){if(await run(code))return;await new Promise(resolve=>setTimeout(resolve,20));}
    throw Error("Native update UI did not settle: "+code);
  };
  const click=async selector=>{
    await run(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center',behavior:'instant'})`);await frame();
    const point=await run(`(()=>{const e=document.querySelector(${JSON.stringify(selector)}),r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return {x,y,hit:e.contains(document.elementFromPoint(x,y))};})()`);
    assert.equal(point.hit,true,"Update control must receive pointer input");
    for(const type of ["mouseMove","mouseDown","mouseUp"]) owner.sendInputEvent({type,x:Math.round(point.x),y:Math.round(point.y),...(type==="mouseMove"?{}:{button:"left",clickCount:1})});
    await frame();
  };
  await until("Boolean(S)");
  const saved={supported:updates.supported,reason:updates.reason,createUpdater:updates.createUpdater,onConfirm:updates.onConfirm,
    onInstall:updates.onInstall,updater:updates.updater,state:updates.state,version:updates.version,progress:updates.progress,
    error:updates.error,ready:updates.ready,installing:updates.installing,approved:updates.approved,prefs:updates.prefs};
  const parts=updates.current.split('.').map(Number);parts[2]++;const next=parts.join('.');
  let active,complete,downloads=0,installRequests=0;
  const cases=[];
  await run("globalThis.updateSmokeBefore={stopped,updates:S.updates,releaseData,desktopData:desktopUpdate.data,explicit:desktopUpdate.explicit,theme:document.documentElement.dataset.theme,queue:[...queue],checked:[...checked],history:history.length};stopped=true;statePollSerial++;void 0;");
  try {
    updates.supported=true;updates.reason="";
    updates.createUpdater=()=>{
      const native=active=new EventEmitter();native.setFeedURL=()=>{};
      native.checkForUpdates=async()=>({updateInfo:{version:next,files:[{url:packageName(next,updates.platform,updates.arch),size:20,sha512:Buffer.alloc(64,12).toString('base64')}]}});
      native.downloadUpdate=()=>{downloads++;return new Promise(resolve=>{complete=resolve;});};
      native.quitAndInstall=()=>{throw Error("An isolated fixture must never execute an installer");};
      return native;
    };
    updates.onConfirm=async()=>true;
    updates.onInstall=()=>{installRequests++;updates.defer("busy");};
    for(const width of [420,760,768,1280])for(const theme of ["light","dark"]) {
      window.setContentSize(width,800);window.show();window.focus();
      updates.updater=null;updates.state="idle";updates.version="";updates.ready=false;updates.busy=null;updates.installing=false;updates.error="";
      await run(`if(document.getElementById('updateDialog').open)closeUpdateDialog();document.documentElement.dataset.theme=${JSON.stringify(theme)};
        desktopUpdate.offerKey='';desktopUpdate.explicit=true;queue.set(900,{id:900});checked.add(901);
        S.updates={current:${JSON.stringify(updates.current)},latest:${JSON.stringify(next)},available:true,automatic:false,checking:false,
          name:'Synthetic update',notes:'## Changes\\n- Safe synthetic notes\\n<img src="https://invalid.example/never-load">'};renderUpdates(S.updates);`);
      await until("desktopUpdate.data?.state==='available'");
      await click('#updateBtn');
      assert.equal(await run("document.querySelector('#releaseNotes img')===null"),true);
      assert.equal(await run("document.getElementById('desktopInstall').hidden"),true);
      assert.equal(await run("document.getElementById('desktopAuto').checked"),false);
      await click('#desktopDownload');await until("desktopUpdate.data?.state==='downloading'");
      active.emit('download-progress',{percent:51});await frame();
      assert.equal(await run("document.getElementById('desktopUpdateStatus').textContent.includes('51')"),true);
      complete();await until("desktopUpdate.data?.state==='ready'");
      assert.equal(await run("document.getElementById('desktopInstall').disabled"),true);
      assert.equal(await run("queue.has(900)&&checked.has(901)&&history.length===updateSmokeBefore.history"),true);
      await click('#updateSettings');
      assert.equal(await run("!document.getElementById('updateDialog').open&&!document.getElementById('updatePanel').hidden&&document.activeElement.id==='checkUpdates'"),true);
      assert.equal(await run("queue.has(900)&&checked.has(901)"),true);
      await click('#updateDetails');
      assert.equal(await run("document.getElementById('updateDialog').open"),true);
      await run("queue.clear();refreshNativeUpdateControls()");
      await click('#desktopInstall');await until("desktopUpdate.data?.error==='busy'");
      cases.push({width,theme,pointer:true,progress:true,installationDeferred:true,selectionsPreserved:true,settingsReachable:true});
      await click('#updateLater');
    }
    assert.equal(downloads,8);assert.equal(installRequests,8);
    return {ok:true,cases,providerRequests:0,installerExecutions:0,realAccountWrites:0};
  } finally {
    Object.assign(updates,saved);updates.publish();window.setBounds(bounds);
    await run("if(document.getElementById('updateDialog').open)closeUpdateDialog();stopped=updateSmokeBefore.stopped;S.updates=updateSmokeBefore.updates;releaseData=updateSmokeBefore.releaseData;desktopUpdate.data=updateSmokeBefore.desktopData;desktopUpdate.explicit=updateSmokeBefore.explicit;document.documentElement.dataset.theme=updateSmokeBefore.theme;queue.clear();for(const [id,item] of updateSmokeBefore.queue)queue.set(id,item);checked=new Set(updateSmokeBefore.checked);if(S.updates)renderUpdates(S.updates);delete globalThis.updateSmokeBefore;");
  }
}
module.exports={checkUpdates};
