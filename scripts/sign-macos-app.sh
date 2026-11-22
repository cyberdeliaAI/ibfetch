#!/usr/bin/env bash
# Sign and notarize a built app on a macOS release runner.
set -euo pipefail
umask 077

app=${1:?Pass the path to the built macOS app}
test -d "$app"
: "${CSC_LINK:?Set CSC_LINK to the base64-encoded Developer ID PKCS#12}"
: "${CSC_KEY_PASSWORD:?Set CSC_KEY_PASSWORD to the PKCS#12 password}"
: "${APPLE_ID:?Set APPLE_ID to the notarization Apple Account}"
: "${APPLE_APP_SPECIFIC_PASSWORD:?Set APPLE_APP_SPECIFIC_PASSWORD}"
: "${APPLE_TEAM_ID:?Set APPLE_TEAM_ID for the Developer ID certificate}"

work=$(mktemp -d)
keychain="$work/signing.keychain-db"
keychains=()
registered=0
cleanup() {
    if [[ "$registered" == 1 ]]; then
        security list-keychains -d user -s ${keychains[@]+"${keychains[@]}"} || true
    fi
    security delete-keychain "$keychain" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
password=$(openssl rand -hex 32)
printf '%s' "${CSC_LINK#*;base64,}" | /usr/bin/base64 -D > "$work/certificate.p12"
security create-keychain -p "$password" "$keychain"
security unlock-keychain -p "$password" "$keychain"
security set-keychain-settings -lut 1800 "$keychain"
security import "$work/certificate.p12" -k "$keychain" -P "$CSC_KEY_PASSWORD" -T /usr/bin/codesign
rm "$work/certificate.p12"
security set-key-partition-list -S apple-tool:,apple:,codesign: -k "$password" "$keychain" >/dev/null

# Register the signing keychain for Security.framework and install the Apple
# Developer ID intermediates, without changing the system trust settings.
while read -r listed; do
    listed=${listed#\"}
    keychains+=("${listed%\"}")
done < <(security list-keychains -d user)
security list-keychains -d user -s "$keychain" ${keychains[@]+"${keychains[@]}"}
registered=1
for issuer in DeveloperIDCA DeveloperIDG2CA; do
    curl --proto '=https' --fail --silent --show-error --retry 2 --max-time 30 \
        "https://www.apple.com/certificateauthority/$issuer.cer" -o "$work/$issuer.cer"
    fingerprint=$(openssl x509 -inform DER -in "$work/$issuer.cer" -noout -fingerprint -sha256)
    fingerprint=${fingerprint#*=}
    if ! grep -Fq "${fingerprint//:/}" <<< "$(security find-certificate -a -Z "$keychain")"; then
        security import "$work/$issuer.cer" -k "$keychain" -t cert
    fi
done

if ! identity=$(security find-identity -v -p codesigning "$keychain" | awk -v team="$APPLE_TEAM_ID" '
    /"Developer ID Application:/ && index($0, "(" team ")") { print $2; count++ }
    END { if (count != 1) exit 1 }
'); then
    echo '::error::Expected one valid Developer ID Application identity for APPLE_TEAM_ID.'
    security find-identity -p codesigning "$keychain"
    exit 1
fi
echo 'Signing with the Developer ID Application identity from the temporary keychain.'
codesign --force --options runtime --timestamp --sign "$identity" "$app"
codesign --verify --deep --strict "$app"

ditto -c -k --keepParent "$app" "$work/app.zip"
if ! xcrun notarytool submit "$work/app.zip" \
    --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_SPECIFIC_PASSWORD" \
    --wait --timeout 12m --output-format json > "$work/notarization.json" ||
    ! jq -e '.status == "Accepted"' "$work/notarization.json" >/dev/null
then
    cat "$work/notarization.json" >&2
    exit 1
fi
xcrun stapler staple "$app"
xcrun stapler validate "$app"
spctl --assess --type execute "$app"
