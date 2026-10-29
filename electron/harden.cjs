"use strict";
const path = require("node:path");
const {flipFuses, FuseVersion, FuseV1Options} = require("@electron/fuses");

module.exports = async context => {
  const name = context.packager.appInfo.productFilename;
  const executable = context.electronPlatformName === "darwin"
    ? path.join(context.appOutDir, name + ".app")
    : path.join(context.appOutDir, name + (context.electronPlatformName === "win32" ? ".exe" : ""));
  await flipFuses(executable, {
    version: FuseVersion.V1,
    [FuseV1Options.RunAsNode]: false,
    [FuseV1Options.EnableNodeOptionsEnvironmentVariable]: false,
    [FuseV1Options.EnableNodeCliInspectArguments]: false,
    [FuseV1Options.OnlyLoadAppFromAsar]: true,
  });
};
