"use strict";
// Only used by the explicit local-provider test runner, never during a normal sign-in.
const assert=require("node:assert/strict"), fs=require("node:fs");
const {session}=require("electron"), {SIGN_IN_PARTITION}=require("./sign-in.cjs");
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
    stage="open";await run("beginSignIn()");await until(()=>Boolean(signIn.window));
    let view=signIn.window;await until(()=>!view.webContents.isLoading());
    const prefs=view.webContents.getLastWebPreferences();
    assert.equal(prefs.preload,undefined);assert.equal(prefs.nodeIntegration,false);
    assert.equal(prefs.sandbox,true);assert.equal(prefs.contextIsolation,true);
    assert.equal(await view.webContents.executeJavaScript('typeof window.ibfetchSignIn'),"undefined");
    assert.equal(new URL(view.webContents.getURL()).origin,"https://oauth.ibroadcast.com");
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
    stage="layouts";const layouts=[];
    for(const width of [420,760,1320]) {
      window.setSize(width,850);await sleep(120);
      const result=await run('({width:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth})');
      assert.equal(result.overflow,false);layouts.push(result);
    }
    done({ok:true,providerWindow:true,noRemoteBridge:true,sandbox:true,cancelled:true,deniedAndRetried:true,
      browserFallback:true,approvedAndReturned:true,layouts});
  } catch(error) {done({ok:false,stage,error:error.message});}
  finally {if(intercepted)session.fromPartition(SIGN_IN_PARTITION).protocol.unhandle("https");}
}
module.exports={check};
