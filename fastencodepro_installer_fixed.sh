#!/usr/bin/env bash
#
# FastEncode Pro - Complete Universal Installer
# Handles live environments, checks if packages actually installed
#

set -e

echo "============================================================"
echo "   FastEncode Pro - Universal Installation Script"
echo "============================================================"
echo ""

# Define paths
APP_DIR="$HOME/.local/bin/FastEncodePro"
DESKTOP_DIR="$HOME/.local/share/applications"
SCRIPT_URL="https://raw.githubusercontent.com/cpgplays/FastEncodePro/main/FastEncodePro.py"
ICON_URL="https://raw.githubusercontent.com/cpgplays/FastEncodePro/main/icon.png"

# ============================================================
# STEP 1: INSTALL SYSTEM PACKAGES
# ============================================================
echo "STEP 1: Installing System Dependencies"
echo "============================================================"
echo ""

# Detect the package manager
if command -v pacman &> /dev/null; then
    DISTRO="Arch-based"
    echo "✅ Detected: Arch Linux (pacman)"
    echo ""
    echo "📦 Required packages:"
    echo "   - python (Python interpreter)"
    echo "   - python-pip (Package installer)"
    echo "   - python-pyqt6 (GUI framework)"
    echo "   - python-scipy (Scientific computing)"
    echo "   - python-mpv (Video player library)"
    echo "   - python-pyopengl (OpenGL bindings)"
    echo "   - ffmpeg (Video encoder)"
    echo ""
    
    read -p "Install these packages? [Y/n] " -n 1 -r
    echo ""
    
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        echo "🔄 Syncing package database..."
        sudo pacman -Sy
        
        echo ""
        echo "📦 Installing packages..."
        
        # Install packages one by one to see which fail
        FAILED_PACKAGES=""
        for pkg in python python-pip python-pyqt6 python-scipy python-mpv python-pyopengl ffmpeg; do
            echo -n "  Installing $pkg... "
            if sudo pacman -S --needed --noconfirm $pkg &>/dev/null; then
                echo "✅"
            else
                echo "❌ FAILED"
                FAILED_PACKAGES="$FAILED_PACKAGES $pkg"
            fi
        done
        
        if [ -z "$FAILED_PACKAGES" ]; then
            echo ""
            echo "✅ All system packages installed successfully!"
        else
            echo ""
            echo "⚠️  These packages failed to install:$FAILED_PACKAGES"
            echo "    Continuing anyway - will try pip later..."
        fi
    fi

elif command -v apt-get &> /dev/null; then
    DISTRO="Debian/Ubuntu-based"
    echo "✅ Detected: Debian/Ubuntu"
    echo ""
    
    read -p "Install system packages? [Y/n] " -n 1 -r
    echo ""
    
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        echo "🔄 Updating package database..."
        sudo apt-get update
        
        echo ""
        echo "📦 Installing packages..."
        sudo apt-get install -y python3 python3-pip python3-pyqt6 python3-scipy python3-mpv python3-opengl ffmpeg
        echo "✅ System packages installed!"
    fi

elif command -v dnf &> /dev/null; then
    DISTRO="Fedora/RHEL-based"
    echo "✅ Detected: Fedora/RHEL"
    
    read -p "Install system packages? [Y/n] " -n 1 -r
    echo ""
    
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        sudo dnf install -y python3 python3-pip python3-PyQt6 python3-scipy python3-mpv python3-pyopengl ffmpeg
        echo "✅ System packages installed!"
    fi

else
    echo "⚠️  Unknown package manager - manual installation required"
    DISTRO="Unknown"
fi

echo ""
echo "============================================================"
echo "STEP 2: Verifying Python Environment"
echo "============================================================"
echo ""

# Check if Python and pip are actually available now
if ! command -v python3 &> /dev/null; then
    echo "❌ ERROR: Python 3 is not installed!"
    echo "   Please install Python manually and run this script again."
    exit 1
fi

echo "✅ Python 3 found: $(python3 --version)"

if ! command -v pip &> /dev/null && ! command -v pip3 &> /dev/null; then
    echo "❌ ERROR: pip is not installed!"
    echo "   Please install pip manually and run this script again."
    echo ""
    echo "   On Arch: sudo pacman -S python-pip"
    echo "   On Debian/Ubuntu: sudo apt install python3-pip"
    exit 1
fi

echo "✅ pip found: $(pip3 --version 2>/dev/null || pip --version)"

echo ""
echo "============================================================"
echo "STEP 3: Downloading FastEncodePro from GitHub"
echo "============================================================"
echo ""

mkdir -p "$APP_DIR"
mkdir -p "$DESKTOP_DIR"

echo "⬇️  Downloading FastEncodePro.py..."
if curl -s -f -L -H 'Cache-Control: no-cache' "$SCRIPT_URL" -o "$APP_DIR/FastEncodePro.py"; then
    chmod +x "$APP_DIR/FastEncodePro.py"
    echo "✅ Downloaded successfully"
else
    echo "❌ ERROR: Could not download from GitHub"
    echo "   Check your internet connection"
    exit 1
fi

echo "⬇️  Downloading icon..."
curl -s -f -L "$ICON_URL" -o "$APP_DIR/icon.png" 2>/dev/null || touch "$APP_DIR/icon.png"

echo ""
echo "============================================================"
echo "STEP 4: Creating Desktop Entry"
echo "============================================================"
echo ""

cat <<EOF > "$DESKTOP_DIR/fastencodepro.desktop"
[Desktop Entry]
Type=Application
Name=FastEncode Pro
Comment=GPU-Accelerated Video Editor
Exec=/usr/bin/python3 "$APP_DIR/FastEncodePro.py"
Icon=$APP_DIR/icon.png
Terminal=false
Categories=AudioVideo;Video;VideoEditing;
EOF

chmod +x "$DESKTOP_DIR/fastencodepro.desktop"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
echo "✅ Desktop entry created"

echo ""
echo "============================================================"
echo "STEP 5: Checking Python Modules"
echo "============================================================"
echo ""

# Check which modules are actually available
echo "Checking installed modules..."
echo ""

python3 << 'PYTHON_EOF'
import sys

modules = {
    'PyQt6': 'PyQt6',
    'mpv': 'python-mpv',
    'scipy': 'scipy',
    'numpy': 'numpy',
    'OpenGL': 'PyOpenGL',
}

installed = []
missing = []

for import_name, pip_name in modules.items():
    try:
        __import__(import_name)
        print(f"  ✅ {import_name:10s} installed")
        installed.append(import_name)
    except ImportError:
        print(f"  ❌ {import_name:10s} MISSING (pip package: {pip_name})")
        missing.append(pip_name)

# Save for later
with open('/tmp/fep_missing.txt', 'w') as f:
    f.write(' '.join(missing))

with open('/tmp/fep_stats.txt', 'w') as f:
    f.write(f"{len(installed)} {len(missing)}")
PYTHON_EOF

# Read stats
STATS=$(cat /tmp/fep_stats.txt)
INSTALLED_COUNT=$(echo $STATS | cut -d' ' -f1)
MISSING_COUNT=$(echo $STATS | cut -d' ' -f2)
MISSING=$(cat /tmp/fep_missing.txt)

echo ""
echo "📊 Status: $INSTALLED_COUNT installed, $MISSING_COUNT missing"

if [ "$MISSING_COUNT" -eq 0 ]; then
    echo ""
    echo "✅ All Python modules are installed!"
else
    echo ""
    echo "⚠️  Missing modules: $MISSING"
    echo ""
    read -p "Install missing modules via pip? [Y/n] " -n 1 -r
    echo ""
    
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        echo "📦 Installing via pip..."
        echo ""
        
        # Try both pip3 and pip
        PIP_CMD="pip3"
        command -v pip3 &>/dev/null || PIP_CMD="pip"
        
        # Try install with different flags
        if $PIP_CMD install --user $MISSING --break-system-packages; then
            echo "✅ Installed successfully!"
        elif $PIP_CMD install --user $MISSING; then
            echo "✅ Installed successfully!"
        else
            echo "⚠️  Some packages failed to install"
            echo "   Try manually: $PIP_CMD install --user $MISSING"
        fi
        
        echo ""
        echo "🔄 Verifying..."
        echo ""
        
        # Verify again
        python3 << 'PYTHON_VERIFY'
modules = ['PyQt6', 'mpv', 'scipy', 'numpy', 'OpenGL']
for mod in modules:
    try:
        __import__(mod)
        print(f"  ✅ {mod:10s} verified")
    except ImportError:
        print(f"  ❌ {mod:10s} still missing")
PYTHON_VERIFY
    fi
fi

echo ""
echo "============================================================"
echo "🎉 INSTALLATION COMPLETE!"
echo "============================================================"
echo ""
echo "Launch from application menu or run:"
echo "  python3 $APP_DIR/FastEncodePro.py"
echo ""
