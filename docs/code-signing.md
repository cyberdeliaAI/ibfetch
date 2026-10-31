# macOS Developer ID signing and notarization

This recipe is explicit build support. Existing 1.4.0 release assets are unchanged.
A local Developer ID certificate alone does not establish notarization or give
GitHub runners access to the certificate's private key.

Configure the following GitHub Actions secrets in the **ibfetch** repository:

| Secret | Contents |
|---|---|
| `CSC_LINK` | Base64 of a password-protected Developer ID Application .p12 with its private key |
| `CSC_KEY_PASSWORD` | The .p12 export password |
| `APPLE_ID` | Apple Developer account email |
| `APPLE_APP_SPECIFIC_PASSWORD` | Apple app-specific password, never the account password |
| `APPLE_TEAM_ID` | Ten-character team identifier |

The same Apple team/certificate can sign both Tagcast and ibfetch. GitHub secrets
are scoped to their repository: creating them in Tagcast does not copy them here.
Never put credentials in files, source, workflow literals or logs. GitHub will not
return an existing secret's plaintext value; configure it again using the original
credentials, or an appropriately scoped organization secret.

Run **Desktop builds** manually and select **sign_macos**. This produces artifacts
only; it does not create a release, publish assets or replace historical downloads.
Ordinary pushes use ad-hoc integrity signatures without signing credentials. The
local equivalent is `python tools/build_desktop.py --sign-macos` with the five
variables supplied securely. Missing settings fail before install/freezing work.

Signing credentials go only to the Electron pack/sign/notarize child. Dependency
installation, Python freezing, smoke tests and backend processes strip Apple/CSC
variables. Electron's signer seals nested Python/native code and the outer app with
a distribution Developer ID signature and hardened runtime. Only the V8 JIT
entitlement is added; library validation and renderer sandboxing stay enabled.

The build requires accepted Apple notarization, a stapled ticket, matching team,
hardened runtime and successful Gatekeeper assessment on both the app and its
extracted final archive. Failure stops the build without unsigned fallback.
Checksums are written only after successful verification. Boolean proof is saved
in `build/reports/macos-signing.json`; it contains no credentials. The normal
isolated native application and sign-in checks remain required.

Notarization can take longer than ordinary builds. A timeout is not approval.
Windows signing is separate; MIT licensing does not imply an Apple signature.

References: [Apple notarization](https://developer.apple.com/documentation/technotes/tn3147-migrating-to-the-latest-notarization-tool),
[electron-builder signing](https://www.electron.build/v26/docs/features/code-signing/).
