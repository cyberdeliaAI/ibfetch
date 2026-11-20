"use strict";
// Only used by the explicit local-provider test runner, never during a normal sign-in.
const assert=require("node:assert/strict"), fs=require("node:fs"), path=require("node:path");
const {session,screen}=require("electron"), {SIGN_IN_PARTITION}=require("./sign-in.cjs");
async function check({app,window,signIn}) {
  const base=process.env.IBFETCH_BASE, report=process.env.IBFETCH_SIGN_IN_SMOKE_REPORT;
  if(!process.env.IBFETCH_HOME || !process.env.IBFETCH_ELECTRON_PROFILE || !base || new URL(base).hostname!=="127.0.0.1")
    throw Error("Sign-in smoke checks require a disposable home/profile and loopback provider.");
  const run=code=>window.webContents.executeJavaScript(code), sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const until=async predicate=>{for(let i=0;i<200;i++){if(await predicate())return;await sleep(100);}throw Error("Sign-in condition timed out");};
  let stage="startup", intercepted=false, finished=false;
  const done=result=>{if(finished)return;finished=true;clearTimeout(timer);fs.writeFileSync(report,JSON.stringify(result));window.destroy();app.quit();};
  const timer=setTimeout(()=>done({ok:false,stage}),90000);
  try {
    if(window.webContents.isLoading()) await new Promise(resolve=>window.webContents.once("did-finish-load",resolve));
    await until(()=>run('Boolean(S) && typeof window.ibfetchSignIn?.open === "function"'));
    session.fromPartition(SIGN_IN_PARTITION).protocol.handle("https", request=>{
      assert.equal(new URL(request.url).origin,"https://oauth.ibroadcast.com");
      return new Response('<!doctype html><meta charset="utf-8"><title>Controlled iBroadcast sign-in</title><h1>iBroadcast sign-in test</h1><p>No real account is used.</p>',
        {headers:{"Content-Type":"text/html","Content-Security-Policy":"default-src 'none'"}});
    });intercepted=true;
    const restart=process.env.IBFETCH_SIGN_IN_PHASE==="restart";
    if(restart) {
      const area=screen.getPrimaryDisplay().workArea, bounds=window.getBounds();
      assert.equal(bounds.width,Math.min(1100,area.width));assert.equal(bounds.height,Math.min(760,area.height));
      await run('(async()=>{confirm=()=>true;await signOut();await poll();})()');
    }
    stage="open";await run("beginSignIn()");await until(()=>Boolean(signIn.window));
    let view=signIn.window;await until(()=>!view.webContents.isLoading());
    const prefs=view.webContents.getLastWebPreferences();
    assert.equal(prefs.preload,undefined);assert.equal(prefs.nodeIntegration,false);
    assert.equal(prefs.sandbox,true);assert.equal(prefs.contextIsolation,true);
    assert.equal(await view.webContents.executeJavaScript('typeof window.ibfetchSignIn'),"undefined");
    assert.equal(new URL(view.webContents.getURL()).origin,"https://oauth.ibroadcast.com");
    if(restart) {
      const area=screen.getDisplayMatching(window.getBounds()).workArea;
      assert.equal(view.getBounds().width,Math.min(502,area.width));
      assert.equal(view.getBounds().height,Math.min(650,area.height));
      view.close();await until(()=>run("!S.login && !signInFlow.requestId"));
      done({ok:true,phase:"restart",mainSizeRestored:true,signInSizeRestored:true});return;
    }
    view.setSize(502,650);
    stage="close";view.close();await until(()=>run("!S.login && !signInFlow.requestId"));
    stage="deny";await run("beginSignIn()");await until(()=>Boolean(signIn.window));
    await fetch(base+"/_signin/outcome",{method:"POST",body:JSON.stringify("deny")});
    await until(()=>run('S.login?.state === "error"'));
    assert.equal(signIn.window,null);assert.equal(await run("S.signed_in"),false);
    stage="fallback";await run("beginSignIn()");await until(()=>Boolean(signIn.window));view=signIn.window;
    await until(()=>!view.webContents.isLoading());
    await view.webContents.executeJavaScript("location.href='https://example.invalid/'").catch(()=>{});
    await until(()=>!signIn.window);
    await until(()=>run('document.getElementById("signinFallback").open'));
    assert.equal(await run('S.login.state'),"waiting");
    stage="approve";await run("reopenSignIn()");await until(()=>Boolean(signIn.window));
    await fetch(base+"/_signin/outcome",{method:"POST",body:JSON.stringify("approve")});
    await until(()=>run('S.signed_in && S.library.state === "ready"'));
    assert.equal(signIn.window,null);
    stage="group-and-sort";
    await until(()=>run('Boolean(meta?.artists?.length)'));
    await run(`document.querySelector('#letters [data-letter="#"]').click();document.querySelector('#artistList [data-initial="#"]').click();`);
    await until(()=>run('results.length===2'));
    assert.deepEqual(await run('results.map(a=>a.artist).sort()'),["(First compilation)","10 First"]);
    await run(`document.querySelector('#results [data-sort="title"]').click()`);
    assert.deepEqual(await run('results.map(a=>a.title)'),["Apple compilation","Zed compilation"]);
    await run('document.querySelector(".row input").click()');
    const selected=await run('[...checked][0]');
    await run(`document.querySelector('#results [data-sort="title"]').click()`);
    assert.deepEqual(await run('[...checked]'),[selected]);
    assert.equal(await run(`document.querySelector('[aria-sort="descending"] button').dataset.sort`),"title");
    await run('document.getElementById("addAllBtn").click()');
    assert.equal(await run('queue.size'),2);
    if(process.env.IBFETCH_SIGN_IN_SCREENSHOT) {
      await sleep(250);
      fs.writeFileSync(process.env.IBFETCH_SIGN_IN_SCREENSHOT,(await window.webContents.capturePage()).toPNG());
      window.setSize(760,850);
      await run('document.documentElement.dataset.appearance="light";document.documentElement.dataset.palette="amber";showTheme()');
      await sleep(250);
      const light=path.join(path.dirname(process.env.IBFETCH_SIGN_IN_SCREENSHOT),path.basename(process.env.IBFETCH_SIGN_IN_SCREENSHOT,".png")+"-light.png");
      fs.writeFileSync(light,(await window.webContents.capturePage()).toPNG());
      window.setSize(1320,850);
      await run('document.documentElement.dataset.appearance="dark";document.documentElement.dataset.palette="forest";showTheme()');
    }
    stage="account-switch";
    const providerSession=session.fromPartition(SIGN_IN_PARTITION), mainSession=window.webContents.session;
    await providerSession.cookies.set({url:"https://oauth.ibroadcast.com",name:"synthetic-account-cookie",value:"first"});
    await mainSession.cookies.set({url:new URL(window.webContents.getURL()).origin,name:"synthetic-main-cookie",value:"keep"});
    await run('confirm=()=>true;signOut()');
    assert.equal(await run('S.signed_in'),false);
    assert.equal(await run('queue.size+results.length'),0);
    assert.deepEqual(await providerSession.cookies.get({name:"synthetic-account-cookie"}),[]);
    assert.equal((await mainSession.cookies.get({name:"synthetic-main-cookie"})).length,1);
    assert.equal(await run('document.getElementById("fArtist").value'),"");
    assert.equal(await run('document.getElementById("results").textContent.includes("compilation")'),false);
    await fetch(base+"/_signin/account",{method:"POST",body:JSON.stringify("second")});
    await run('beginSignIn()');await until(()=>Boolean(signIn.window));
    await fetch(base+"/_signin/outcome",{method:"POST",body:JSON.stringify("approve")});
    await until(()=>run('S.user?.name==="second account" && S.library.state==="ready" && meta?.artists?.[0]?.[0]==="# Second account"'));
    await run(`document.querySelector('#letters [data-letter="#"]').click();document.querySelector('#artistList [data-initial="#"]').click();`);
    await until(()=>run('results.length===1 && results[0].title==="Second account album"'));
    assert.equal(await run('queue.size'),0);
    const folder=await run('document.getElementById("outDir").value');
    assert.ok(folder.length>0);
    stage="layouts";const layouts=[];
    for(const width of [420,760,1320]) {
      window.setSize(width,850);await sleep(120);
      const result=await run('({width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth})');
      assert.equal(result.overflow,false);layouts.push(result);
    }
    window.setSize(1100,760);await sleep(350);
    done({ok:true,phase:"initial",windowSizesSaved:true,providerWindow:true,noRemoteBridge:true,sandbox:true,cancelled:true,deniedAndRetried:true,
      browserFallback:true,approvedAndReturned:true,twoAccounts:true,providerCookiesCleared:true,mainCookiesPreserved:true,
      hashArtistGroup:true,albumSorting:true,selectionPreserved:true,layouts});
  } catch(error) {done({ok:false,stage,error:error.message});}
  finally {if(intercepted)session.fromPartition(SIGN_IN_PARTITION).protocol.unhandle("https");}
}
module.exports={check};
