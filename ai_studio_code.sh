#!/usr/bin/env bash
#
# FastEncode Pro - Complete Universal Installer
# 1. Auto-detects Linux Distro and installs dependencies
# 2. Downloads latest version from GitHub
# 3. Creates desktop entry and icon
#

set -e  # Exit on error

echo "============================================================"
echo "   FastEncode Pro - Universal Installation Script"
echo "============================================================"
echo ""
echo "This installer will:"
echo "  1. Detect your Linux distribution and install dependencies"
echo "  2. Download the latest version from GitHub"
echo "  3. Set up desktop integration"
echo ""

# Define paths
APP_DIR="$HOME/.local/bin/FastEncodePro"
DESKTOP_DIR="$HOME/.local/share/applications"
SCRIPT_URL="https://raw.githubusercontent.com/cpgplays/FastEncodePro/main/FastEncodePro.py"
ICON_URL="https://raw.githubusercontent.com/cpgplays/FastEncodePro/main/icon.png"

# ============================================================
# STEP 1: DISTRO DETECTION AND DEPENDENCIES
# ============================================================
echo "STEP 1: Checking Dependencies"
echo "============================================================"
echo ""

# Detect the package manager
if command -v apt-get &> /dev/null; then
    DISTRO="Debian/Ubuntu-based"
    UPDATE_CMD="sudo apt-get update"
    INSTALL_CMD="sudo apt-get install -y"
    PACKAGES="python3 python3-pyqt6 python3-scipy python3-mpv ffmpeg python3-opengl"

elif command -v pacman &> /dev/null; then
    DISTRO="Arch-based"
    UPDATE_CMD=""
    INSTALL_CMD="sudo pacman -S --needed --noconfirm"
    PACKAGES="python python-pyqt6 python-scipy python-mpv ffmpeg python-pyopengl"

elif command -v dnf &> /dev/null; then
    DISTRO="Fedora/RHEL-based"
    UPDATE_CMD=""
    INSTALL_CMD="sudo dnf install -y"
    PACKAGES="python3 python3-PyQt6 python3-scipy python3-mpv ffmpeg python3-pyopengl"

elif command -v zypper &> /dev/null; then
    DISTRO="openSUSE-based"
    UPDATE_CMD=""
    INSTALL_CMD="sudo zypper install -y"
    PACKAGES="python3 python3-qt6 python3-scipy python3-mpv ffmpeg python3-PyOpenGL"

else
    DISTRO="Unknown"
fi

if [ "$DISTRO" = "Unknown" ]; then
    echo "⚠️  Unsupported or unknown package manager."
    echo "    Auto-install is not available for this distribution."
    echo ""
    echo "    Please install these packages manually via your package manager:"
    echo "    - Python 3.10+"
    echo "    - PyQt6"
    echo "    - python-mpv"
    echo "    - python-scipy"
    echo "    - ffmpeg"
    echo "    - python-pyopengl (optional)"
    echo ""
    read -p "Press [Enter] to skip dependency installation and continue anyway..."
else
    echo "✅ Detected OS: $DISTRO"
    echo ""
    echo "The following packages will be installed/checked:"
    echo "📦 $PACKAGES"
    echo ""
    read -p "Install missing packages? [Y/n] " -n 1 -r
    echo ""
    
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        echo "Installing packages..."
        
        # Run update command if required (like apt-get update)
        if [ -n "$UPDATE_CMD" ]; then
            echo "Running system package update..."
            $UPDATE_CMD || true
        fi
        
        # Run the installation
        if $INSTALL_CMD $PACKAGES; then
            echo ""
            echo "✅ Dependencies installed successfully!"
        else
            echo ""
            echo "⚠️  Some dependencies failed to install."
            echo "    (Some distros might not have 'python3-mpv' in their default repos)."
            echo "    We will continue, but check the verification step at the end!"
        fi
    else
        echo "⚠️  Skipping dependency installation."
    fi
fi

echo ""
echo "============================================================"
echo "STEP 2: Downloading Latest Version from GitHub"
echo "============================================================"
echo ""

# Create directories
mkdir -p "$APP_DIR"
mkdir -p "$DESKTOP_DIR"

# Download the latest software
echo "⬇️  Downloading FastEncodePro.py from GitHub..."
if curl -s -f -L -H 'Cache-Control: no-cache' "$SCRIPT_URL" -o "$APP_DIR/FastEncodePro.py"; then
    chmod +x "$APP_DIR/FastEncodePro.py"
    echo "✅ Software downloaded successfully"
else
    echo "❌ ERROR: Could not download from GitHub"
    echo "   URL: $SCRIPT_URL"
    echo "   Please check your internet connection and try again."
    exit 1
fi

echo ""

# Download the icon
echo "⬇️  Downloading icon..."
if curl -s -f -L "$ICON_URL" -o "$APP_DIR/icon.png"; then
    echo "✅ Icon downloaded successfully"
else
    echo "⚠️  Could not download icon (using placeholder)"
    touch "$APP_DIR/icon.png"
fi

echo ""
echo "============================================================"
echo "STEP 3: Setting Up Desktop Integration"
echo "============================================================"
echo ""

# Create desktop entry
echo "📝 Creating desktop entry..."
cat <<EOF > "$DESKTOP_DIR/fastencodepro.desktop"
[Desktop Entry]
Type=Application
Name=FastEncode Pro
Comment=GPU-Accelerated Video Editor with Embedded MPV Player
Exec=/usr/bin/python3 "$APP_DIR/FastEncodePro.py"
Icon=$APP_DIR/icon.png
Terminal=false
Categories=AudioVideo;Video;VideoEditing;
Keywords=video;editor;encoder;mpv;gpu;nvenc;prores;
StartupWMClass=FastEncodePro
StartupNotify=true
EOF

chmod +x "$DESKTOP_DIR/fastencodepro.desktop"
echo "✅ Desktop entry created"

echo ""

# Refresh desktop database
echo "🔄 Refreshing desktop database..."
if command -v update-desktop-database &> /dev/null; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    echo "✅ Desktop database updated"
else
    echo "⚠️  update-desktop-database not found (non-fatal)"
fi

echo ""
echo "============================================================"
echo "STEP 4: Verifying Installation"
echo "============================================================"
echo ""

# Verify Python modules
echo "Checking Python modules..."
python3 << 'PYTHON_EOF'
import sys

modules_to_check = {
    'PyQt6': 'PyQt6 GUI framework',
    'mpv': 'python-mpv for video playback',
    'scipy': 'SciPy library'
}

optional_modules = {
    'OpenGL': 'PyOpenGL (optional)',
}

print()
print("Required modules:")
all_ok = True
for module, desc in modules_to_check.items():
    try:
        __import__(module)
        print(f"  ✅ {module}")
    except ImportError:
        print(f"  ❌ {module} - MISSING")
        all_ok = False

print()
print("Optional modules:")
for module, desc in optional_modules.items():
    try:
        __import__(module)
        print(f"  ✅ {module}")
    except ImportError:
        print(f"  ⚠️  {module} - not installed (recommended)")

print()

if not all_ok:
    print("❌ Some required modules are missing!")
    print("   If your Linux distro failed to install 'python-mpv' or 'scipy', you can try running:")
    print("   pip install mpv scipy --break-system-packages")
    sys.exit(1)
else:
    print("✅ All required modules verified!")

PYTHON_EOF

if [ $? -ne 0 ]; then
    echo ""
    echo "⚠️  Module verification failed!"
    echo "    FastEncode Pro may not launch correctly."
    echo ""
fi

echo ""
echo "============================================================"
echo "🎉 INSTALLATION COMPLETE!"
echo "============================================================"
echo ""
echo "FastEncode Pro has been installed to:"
echo "    $APP_DIR/FastEncodePro.py"
echo ""
echo "You can now:"
echo "  1. Launch from your application menu: 'FastEncode Pro'"
echo "  2. Or run from terminal: python3 $APP_DIR/FastEncodePro.py"
echo ""
echo "Features:"
echo "  ✅ Embedded MPV player (Wayland native, no separate window)"
echo "  ✅ GPU-accelerated encoding (NVENC, ProRes, AV1)"
echo "  ✅ 5 advanced filters (denoise, deflicker, exposure, temporal, sharpness)"
echo "  ✅ Timeline editor with multi-track audio mixing"
echo "  ✅ Batch processing"
echo "  ✅ Full accessibility support (switch control, dwell clicking)"
echo ""
echo "To update in the future, just run this installer again!"
echo ""
echo "============================================================"