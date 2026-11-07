"use strict";

function checkFuses(wire, {FuseVersion, FuseV1Options: options, FuseState: state}) {
  if (wire.version !== FuseVersion.V1) throw Error("Unsupported Electron fuse wire");
  for (const option of [options.RunAsNode, options.EnableNodeOptionsEnvironmentVariable, options.EnableNodeCliInspectArguments])
    if (wire[option] !== state.DISABLE) throw Error("Electron has an unsafe Node startup fuse");
  if (wire[options.OnlyLoadAppFromAsar] !== state.ENABLE) throw Error("Electron must load only the packaged ASAR");
  return {run_as_node:false,node_options:false,node_inspect:false,only_asar:true};
}
async function verifyFuses(executable) {
  const api=await import("@electron/fuses");
  return checkFuses(await api.getCurrentFuseWire(executable),api);
}
if (require.main === module) verifyFuses(process.argv[2]).then(result=>process.stdout.write(JSON.stringify(result)+"\n"))
  .catch(()=>{process.stderr.write("Packaged Electron fuse verification failed\n");process.exitCode=1;});
module.exports={checkFuses,verifyFuses};
