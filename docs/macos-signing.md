# Signing and notarizing macOS builds

The standalone app uses the bundle identifier `nl.cyberdelia.ibfetch`.
It is packaged as `ibfetch.app` and uses the ibfetch profile. The Developer
ID Application certificate and Apple notarization account belong to your team.
The iBroadcast OAuth registration configures sign-in separately.

## GitHub Actions configuration

In this repository, open **Settings → Secrets and variables → Actions**.

The native app includes ibfetch's public OAuth client ID. The optional
`IBROADCAST_CLIENT_ID` repository variable overrides it for custom builds.

Under **Secrets**, set these repository secrets:

| Secret | Value |
| --- | --- |
| `CSC_LINK` | Base64 of the Developer ID Application `.p12` export, including the private key |
| `CSC_KEY_PASSWORD` | Password for the `.p12` export |
| `APPLE_ID` | Email address of the Apple Account used for notarization |
| `APPLE_APP_SPECIFIC_PASSWORD` | App-specific password for that Apple Account |
| `APPLE_TEAM_ID` | Apple Developer Team ID for the certificate and notarization account |

Open **Actions → Rust builds → Run workflow** on `main` and enable **Sign and
notarize both Mac artifacts**. The Mac jobs import the certificate into a
throwaway keychain, sign with hardened runtime, submit to Apple, staple the
ticket, and verify the signature and ticket again after extracting each ZIP.
Download the two Mac artifacts from the finished run. The workflow produces
artifacts only, so a GitHub release is a separate step.

Ordinary pushes and pull requests build locally signed Mac test artifacts.
The Apple secrets are used only for the explicit signing run on `main`.

See [Apple's notarization documentation](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
and [GitHub's Actions secrets guide](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).
