#!/bin/sh
# Build a native standalone app. The developer identity is optional for local QA.
set -eu
cd "$(dirname "$0")/.."
export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
cargo build --locked --release
profile="${CARGO_TARGET_DIR:-target}"
if [ -n "${CARGO_BUILD_TARGET:-}" ]; then profile="$profile/$CARGO_BUILD_TARGET"; fi
profile="$profile/release"
app="$profile/ibfetch.app"
stage=$(mktemp -d "$profile/.ibfetch-bundle.XXXXXXXX")
trap 'rm -rf "$stage"' EXIT
bundle="$stage/ibfetch.app"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources" "$stage/AppIcon.iconset"
install -m 755 "$profile/ibfetch" "$bundle/Contents/MacOS/ibfetch"
install -m 644 LICENSE "$bundle/Contents/Resources/LICENSE"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" assets/ibfetch-icon.png --out "$stage/AppIcon.iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" assets/ibfetch-icon.png --out "$stage/AppIcon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$stage/AppIcon.iconset" -o "$bundle/Contents/Resources/AppIcon.icns"
version=$(sed -n 's/^version = "\([0-9][0-9.]*\).*"$/\1/p' Cargo.toml | head -n 1)
app_id=$(sed -n 's/^pub const MAC_BUNDLE_ID: &str = "\([^"]*\)";$/\1/p' src/lib.rs)
test -n "$version" && test -n "$app_id"
cat > "$bundle/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>ibfetch</string>
  <key>CFBundleIdentifier</key><string>$app_id</string>
  <key>CFBundleExecutable</key><string>ibfetch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>AppIcon.icns</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.music</string>
  <key>LSMinimumSystemVersion</key><string>$MACOSX_DEPLOYMENT_TARGET</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$bundle"
else
  codesign --force --sign - "$bundle"
fi
codesign --verify --deep --strict "$bundle"
rm -rf "$app"
mv "$bundle" "$app"
"$app/Contents/MacOS/ibfetch" --version
printf 'Built %s\n' "$app"
