"use strict";
const {test}=require("node:test"),assert=require("node:assert/strict");
const {checkFuses}=require("../verify-fuses.cjs");
test("packaged fuse verification rejects every unsafe Node entry point and an unpacked-app fallback",async()=>{
  const api=await import("@electron/fuses"),{FuseVersion,FuseV1Options:o,FuseState:s}=api;
  const good={version:FuseVersion.V1,[o.RunAsNode]:s.DISABLE,[o.EnableNodeOptionsEnvironmentVariable]:s.DISABLE,
    [o.EnableNodeCliInspectArguments]:s.DISABLE,[o.OnlyLoadAppFromAsar]:s.ENABLE};
  assert.equal(checkFuses(good,api).only_asar,true);
  for(const key of [o.RunAsNode,o.EnableNodeOptionsEnvironmentVariable,o.EnableNodeCliInspectArguments])
    assert.throws(()=>checkFuses({...good,[key]:s.ENABLE},api));
  assert.throws(()=>checkFuses({...good,[o.OnlyLoadAppFromAsar]:s.DISABLE},api));
  assert.throws(()=>checkFuses({...good,version:"future"},api));
});
