#!/bin/bash

# --- FastEncode Pro v0.06 Installer ---

# 1. Define Paths
APP_DIR="$HOME/.local/bin/FastEncodePro"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_URL="https://raw.githubusercontent.com/cpgplays/FastEncodePro/main/icon.png"

# 2. Clean up
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
mkdir -p "$DESKTOP_DIR"

echo "📂 Installation folders ready."

# 3. Find and Move the Python Script (Handling spaces in the name)
echo "🔍 Looking for v0.06 script..."

# We try the specific name you gave, with and without .py extension
if [ -f "$HOME/Downloads/FastEncode Pro - Accessibility Edition v0.06.py" ]; then
    SOURCE="$HOME/Downloads/FastEncode Pro - Accessibility Edition v0.06.py"
elif [ -f "$HOME/Downloads/FastEncode Pro - Accessibility Edition v0.06" ]; then
    SOURCE="$HOME/Downloads/FastEncode Pro - Accessibility Edition v0.06"
# Fallback: Find any file starting with "FastEncode Pro" in Downloads
elif compgen -G "$HOME/Downloads/FastEncode Pro*.py" > /dev/null; then
    SOURCE=$(ls "$HOME/Downloads/FastEncode Pro"*.py | head -n 1)
else
    echo "❌ ERROR: Could not find the script."
    echo "   Please make sure the file in Downloads is named:"
    echo "   'FastEncode Pro - Accessibility Edition v0.06.py'"
    exit 1
fi

echo "📦 Found: $(basename "$SOURCE")"
cp "$SOURCE" "$APP_DIR/FastEncodePro.py"
chmod +x "$APP_DIR/FastEncodePro.py"

# 4. Download Icon from GitHub
echo "⬇️  Fetching Icon from GitHub..."
if curl -s -f -L "$ICON_URL" -o "$APP_DIR/icon.png"; then
    echo "✅ Icon downloaded successfully."
else
    echo "⚠️  WARNING: Could not find icon.png on GitHub yet."
    echo "   (Once you upload 'icon.png' to your repo, run this script again!)"
    # Create an empty file so the desktop entry doesn't crash
    touch "$APP_DIR/icon.png" 
fi

# 5. Create Desktop Entry
echo "📝 Registering Desktop Shortcut..."
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

# 6. Refresh System
update-desktop-database "$DESKTOP_DIR" 2>/dev/null

echo "------------------------------------------------"
echo "🎉 INSTALLATION COMPLETE"
echo "   Version: v0.06"
echo "   Icon Source: GitHub"
echo "   Status: Ready to launch!"
echo "------------------------------------------------"