#!/bin/bash

# --- FastEncode Pro: Universal Web Installer ---
# This pulls the absolute latest code from the main branch.

# 1. Define Paths & URLs
APP_DIR="$HOME/.local/bin/FastEncodePro"
DESKTOP_DIR="$HOME/.local/share/applications"

# This URL always points to the latest commit of this file
SCRIPT_URL="https://raw.githubusercontent.com/cpgplays/FastEncodePro/main/FastEncodePro.py"
ICON_URL="https://raw.githubusercontent.com/cpgplays/FastEncodePro/main/icon.png"

# 2. Preparation
echo "⚙️  Preparing installation..."
mkdir -p "$APP_DIR"
mkdir -p "$DESKTOP_DIR"

# 3. Download the Latest Software
echo "⬇️  Downloading latest version from GitHub..."
# We use curl with -H 'Cache-Control: no-cache' to ensure we don't get an old version
if curl -s -f -L -H 'Cache-Control: no-cache' "$SCRIPT_URL" -o "$APP_DIR/FastEncodePro.py"; then
    chmod +x "$APP_DIR/FastEncodePro.py"
    echo "✅ Software updated successfully."
else
    echo "❌ ERROR: Could not download from GitHub."
    echo "   Please check that the file in your repo is named 'FastEncodePro.py' (case sensitive!)"
    exit 1
fi

# 4. Download the Icon
echo "⬇️  Checking for icon..."
curl -s -f -L "$ICON_URL" -o "$APP_DIR/icon.png" || touch "$APP_DIR/icon.png"

# 5. Register with Window Manager (Hyprland/Wayland)
echo "📝 Updating System Registry..."
cat <<EOF > "$DESKTOP_DIR/fastencodepro.desktop"
[Desktop Entry]
Type=Application
Name=FastEncode Pro
Comment=Accessible Video Editor
Exec=/usr/bin/python3 "$APP_DIR/FastEncodePro.py"
Icon=$APP_DIR/icon.png
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=FastEncodePro
EOF

# 6. Refresh Icon Cache
update-desktop-database "$DESKTOP_DIR" 2>/dev/null

echo "------------------------------------------------"
echo "🎉 SUCCESS! You have the latest version."
echo "   Launch 'FastEncode Pro' from your menu."
echo "------------------------------------------------"