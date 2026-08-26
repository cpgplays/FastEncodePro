#!/usr/bin/env python3
"""
FastEncode Pro - Timeline Edition v0.9.3
GPU-Accelerated Video Editor with Native Wayland MPV Support

v0.9.3 Features:
- Master Canvas Compositor Engine: True NLE rendering via filter_complex.
- Zero System RAM bottleneck; 100% frame-accurate Timeline rendering.
- Added Automatic Audio Sync detection.
- Fixed Wayland ghost-window bugs during audio sync analysis.
- Fixed PyQt6 thread-safety crashes for timeline waveforms.
- Multi-clip export: video overlay uses timeline-aligned PTS (fixes black after first clip).
- Timeline export audio: amix uses longest input (fixes silence after first clip).
- Timeline EDL preview: native audio per segment (no first-clip-only lavfi on multi-file EDL).
- Windows / PyInstaller: DLL search path bootstrap before ``import mpv``; import errors include pip + EXE hints.
- MPV preview: Qt-thread-safe callbacks; deferred seek/lavfi after ``file-loaded`` (fixes EDL/timeline black screen).
- Ship preview in EXE: ``pip install -r requirements-FastEncodePro.txt`` when building; PyInstaller ``--hidden-import=mpv`` + ``libmpv-2.dll``.
- THE FIX: Timeline scrubbers now track in real-time, empty clicks load the full EDL track sequence properly!
"""

import locale
import os
locale.setlocale(locale.LC_NUMERIC, 'C')
os.environ['LC_NUMERIC'] = 'C'
print("âœ… Locale set to C for MPV")

import sys
import shutil
import urllib.request
import urllib.error
import subprocess
import tempfile
import json
import time
import math
from pathlib import Path


def _bootstrap_mpv_runtime_path():
    """PyInstaller / frozen EXE: register folders where ``libmpv-2.dll`` is unpacked before ``import mpv``."""
    if not getattr(sys, 'frozen', False):
        return
    bases = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        bases.append(os.path.abspath(meipass))
    exe = getattr(sys, 'executable', '') or ''
    exe_dir = os.path.abspath(os.path.dirname(exe)) if exe else ''
    if exe_dir and exe_dir not in bases:
        bases.append(exe_dir)
    prepend = []
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        try:
            if hasattr(os, 'add_dll_directory'):
                os.add_dll_directory(base)
        except (OSError, ValueError, FileNotFoundError, AttributeError):
            pass
        prepend.append(base)
    if prepend:
        os.environ['PATH'] = os.pathsep.join(prepend) + os.pathsep + os.environ.get('PATH', '')


_bootstrap_mpv_runtime_path()

PYINSTALLER_HIDDEN_IMPORTS_MPV = ('mpv',)

MPV_AVAILABLE = False
try:
    import mpv  # noqa: F401
    MPV_AVAILABLE = True
    print("âœ… python-mpv available")
except (ImportError, OSError) as _mpv_err:
    print(f"âš ï¸  python-mpv / libmpv unavailable: {_mpv_err}")
    print("   Dev: pip install mpv   |   EXE: hidden-import=mpv + bundle libmpv-2.dll next to the app / in _MEIPASS.")
except Exception as _mpv_err:
    print(f"âš ï¸  python-mpv failed to load: {_mpv_err}")

try:
    import sounddevice as sd
    import scipy.io.wavfile as _scipy_wav
    SOUNDDEVICE_AVAILABLE = True
    print("âœ… sounddevice + scipy available")
except (ImportError, OSError):
    sd = None
    _scipy_wav = None
    SOUNDDEVICE_AVAILABLE = False
    print("âš ï¸  sounddevice or scipy unavailable - voiceover recording disabled")

from PyQt6.QtWidgets import *
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSettings, QUrl, QPointF, QTimer, QEvent, QPoint, QRectF, QObject, QSize
from PyQt6.QtGui import (
    QFont, QPalette, QColor, QPainter, QBrush, QPen, QCursor, QAction, QPainterPath,
    QMouseEvent, QImage, QPixmap, QConicalGradient, QRadialGradient,
)


__version__ = "0.9.4h"
GITHUB_REPO = "TylerDavies/FastEncodePro"  # Change to your actual GitHub repo

class UpdateManager:
    """Handles FFmpeg and App auto-updates for Windows and Linux"""
    FFMPEG_WIN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    FFMPEG_WIN_BTBN_API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
    FFMPEG_LINUX_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    
    @staticmethod
    def get_ffmpeg_path():
        import shutil
        path = shutil.which('ffmpeg')
        if path:
            return path
        # Check common locations
        for p in ['./ffmpeg.exe', './bin/ffmpeg.exe', 'C:/ffmpeg/bin/ffmpeg.exe', '/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg']:
            if os.path.exists(p):
                return p
        return path
    
    @staticmethod
    def get_ffmpeg_version():
        try:
            result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            first_line = (result.stdout or '').splitlines()[0] if result.stdout else 'Unknown'
            return first_line
        except Exception as e:
            return f"Error: {e}"
    
    @staticmethod
    def update_ffmpeg_windows(parent=None, log_callback=None):
        """Windows: Download latest Gyan build and replace ffmpeg.exe/ffprobe.exe"""
        def log(msg):
            if log_callback:
                log_callback(msg)
            print(msg)
        try:
            log("Checking FFmpeg latest build...")
            ffmpeg_path = UpdateManager.get_ffmpeg_path()
            if not ffmpeg_path:
                # Default to local dir
                ffmpeg_path = os.path.join(os.getcwd(), "ffmpeg.exe")
                log(f"No existing FFmpeg found, will install to {ffmpeg_path}")
            else:
                log(f"Current FFmpeg: {ffmpeg_path}")
                log(f"Current version: {UpdateManager.get_ffmpeg_version()}")
            
            # Download latest essentials build
            log(f"Downloading latest FFmpeg from gyan.dev...")
            import tempfile, zipfile
            tmp_dir = tempfile.mkdtemp()
            zip_path = os.path.join(tmp_dir, "ffmpeg.zip")
            
            # Use urllib with user-agent
            req = urllib.request.Request(UpdateManager.FFMPEG_WIN_URL, headers={'User-Agent': 'FastEncodePro-Updater'})
            with urllib.request.urlopen(req, timeout=30) as r, open(zip_path, 'wb') as f:
                total = int(r.headers.get('Content-Length', 0))
                downloaded = 0
                chunk = 1024*64
                while True:
                    data = r.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    downloaded += len(data)
                    if total > 0 and parent:
                        pct = int(downloaded/total*100)
                        if hasattr(parent, 'status_label'):
                            parent.status_label.setText(f"Downloading FFmpeg: {pct}%")
            
            log(f"Extracting...")
            with zipfile.ZipFile(zip_path, 'r') as z:
                # Find ffmpeg.exe inside
                for name in z.namelist():
                    if name.endswith('ffmpeg.exe') or name.endswith('ffprobe.exe'):
                        # Extract to tmp
                        z.extract(name, tmp_dir)
                        src = os.path.join(tmp_dir, name)
                        # Find destination dir
                        dest_dir = os.path.dirname(ffmpeg_path) if os.path.dirname(ffmpeg_path) else os.getcwd()
                        dest_name = os.path.basename(name)
                        dest = os.path.join(dest_dir, dest_name)
                        # Backup old
                        if os.path.exists(dest):
                            backup = dest + ".bak"
                            try:
                                if os.path.exists(backup):
                                    os.remove(backup)
                                os.rename(dest, backup)
                                log(f"Backed up {dest} to {backup}")
                            except Exception as e:
                                log(f"Backup failed: {e}")
                        try:
                            import shutil as _sh
                            _sh.copy2(src, dest)
                            log(f"Updated {dest}")
                        except Exception as e:
                            # If file in use, try to copy to same dir as app
                            alt_dest = os.path.join(os.getcwd(), dest_name)
                            try:
                                _sh.copy2(src, alt_dest)
                                log(f"FFmpeg in use, installed to {alt_dest} - add to PATH")
                            except Exception as e2:
                                log(f"Failed to copy: {e2}")
                                return False, str(e2)
            
            log(f"FFmpeg update complete! New version: {UpdateManager.get_ffmpeg_version()}")
            return True, "FFmpeg updated successfully"
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            log(f"FFmpeg update failed: {e}\n{tb}")
            return False, str(e)
    
    @staticmethod
    def update_ffmpeg_linux(log_callback=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            print(msg)
        try:
            log("Linux FFmpeg update - checking package manager...")
            # Try apt, dnf, pacman
            if shutil.which('apt'):
                log("Found apt, running sudo apt update && sudo apt install -y ffmpeg...")
                # Can't run sudo automatically, give instructions
                return False, "On Linux, run: sudo apt update && sudo apt install -y ffmpeg\nOr download static build from johnvansickle.com/ffmpeg/"
            elif shutil.which('dnf'):
                return False, "Run: sudo dnf install -y ffmpeg"
            else:
                log(f"Downloading static build from {UpdateManager.FFMPEG_LINUX_URL}")
                # Download logic similar to Windows but tar.xz
                import tempfile, tarfile
                tmp_dir = tempfile.mkdtemp()
                tar_path = os.path.join(tmp_dir, "ffmpeg.tar.xz")
                req = urllib.request.Request(UpdateManager.FFMPEG_LINUX_URL, headers={'User-Agent': 'FastEncodePro-Updater'})
                with urllib.request.urlopen(req, timeout=30) as r, open(tar_path, 'wb') as f:
                    f.write(r.read())
                with tarfile.open(tar_path, 'r:xz') as tar:
                    for member in tar.getmembers():
                        if member.name.endswith('/ffmpeg') or member.name.endswith('/ffprobe'):
                            tar.extract(member, tmp_dir)
                            src = os.path.join(tmp_dir, member.name)
                            dest = f"/usr/local/bin/{os.path.basename(member.name)}"
                            try:
                                import shutil as _sh
                                _sh.copy2(src, dest)
                                log(f"Updated {dest}")
                            except PermissionError:
                                return False, f"Need sudo to copy to {dest}. Run: sudo cp {src} {dest}"
                return True, "FFmpeg updated"
        except Exception as e:
            import traceback
            return False, f"{e}\n{traceback.format_exc()}"
    
    @staticmethod
    def check_app_update(current_version):
        """Check GitHub releases for newer version"""
        try:
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(api_url, headers={'User-Agent': 'FastEncodePro-Updater', 'Accept': 'application/vnd.github.v3+json'})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
                latest_tag = data.get('tag_name', '').lstrip('v')
                latest_version = latest_tag
                html_url = data.get('html_url', '')
                assets = data.get('assets', [])
                # Compare versions
                def parse_ver(v):
                    try:
                        return [int(x) for x in re.split(r'[\.\-]', v) if x.isdigit()]
                    except:
                        return [0]
                cur = parse_ver(current_version)
                lat = parse_ver(latest_version)
                is_newer = lat > cur
                return {
                    'current': current_version,
                    'latest': latest_version,
                    'is_newer': is_newer,
                    'url': html_url,
                    'assets': assets,
                    'body': data.get('body', ''),
                    'raw': data
                }
        except Exception as e:
            return {'error': str(e), 'is_newer': False}
    
    @staticmethod
    def download_and_install_update(asset_url, dest_path, log_callback=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            print(msg)
        try:
            log(f"Downloading update from {asset_url}...")
            import tempfile
            tmp_dir = tempfile.mkdtemp()
            file_name = os.path.basename(asset_url.split('?')[0]) or "update"
            if not file_name.endswith(('.exe', '.zip', '.py', '.AppImage')):
                file_name = "FastEncodePro_Update.exe" if os.name == 'nt' else "FastEncodePro_Update.py"
            tmp_file = os.path.join(tmp_dir, file_name)
            req = urllib.request.Request(asset_url, headers={'User-Agent': 'FastEncodePro-Updater'})
            with urllib.request.urlopen(req, timeout=60) as r, open(tmp_file, 'wb') as f:
                f.write(r.read())
            log(f"Downloaded to {tmp_file}")
            # If it's an installer exe on Windows, run it
            if tmp_file.endswith('.exe') and os.name == 'nt':
                log("Launching installer...")
                # For INNO installer, run it
                subprocess.Popen([tmp_file, '/SILENT'], creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                return True, f"Installer launched: {tmp_file}"
            elif tmp_file.endswith('.py'):
                # Replace current .py file
                current_file = os.path.abspath(__file__)
                backup = current_file + ".bak"
                import shutil as _sh
                if os.path.exists(backup):
                    os.remove(backup)
                _sh.copy2(current_file, backup)
                _sh.copy2(tmp_file, current_file)
                log(f"Updated {current_file}, backup at {backup}")
                return True, "App updated, restart required"
            elif tmp_file.endswith('.zip'):
                import zipfile
                extract_dir = os.path.join(tmp_dir, "extracted")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(tmp_file, 'r') as z:
                    z.extractall(extract_dir)
                log(f"Extracted to {extract_dir}, please copy files manually")
                # Try to open folder
                if os.name == 'nt':
                    os.startfile(extract_dir)
                else:
                    subprocess.Popen(['xdg-open', extract_dir])
                return True, f"Extracted to {extract_dir}"
            else:
                return False, f"Unknown asset type: {file_name}"
        except Exception as e:
            import traceback
            return False, f"{e}\n{traceback.format_exc()}"

__author__ = "cpgplays"

# --- HELPER FUNCTIONS ---

def get_audio_stream_count_static(filepath):
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index', '-of', 'csv=p=0', filepath]
        out = subprocess.check_output(cmd, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0).decode().strip()
        if not out: return 0
        return len(out.splitlines())
    except:
        return 1


def get_video_fps_static(filepath):
    try:
        cmd = [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=avg_frame_rate,r_frame_rate',
            '-of', 'json', filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        data = json.loads(result.stdout or '{}')
        stream = data.get('streams', [{}])[0]
        for key in ('avg_frame_rate', 'r_frame_rate'):
            value = stream.get(key, '0/0')
            if '/' in value:
                num, den = value.split('/', 1)
                num = float(num)
                den = float(den)
                if den and num:
                    return num / den
            else:
                fps = float(value)
                if fps > 0:
                    return fps
    except Exception:
        pass
    return 60.0


def get_export_target_labels():
    return [
        "Master / color grading (NVENC P7)",
        "YouTube - long form (NVENC P5)",
        "YouTube Shorts (NVENC P5)",
        "TikTok / Reels / Shorts vertical (NVENC P5)",
        "Instagram - feed / square (NVENC P5)",
        "X (Twitter) / General social (NVENC P5)",
    ]


def get_nvenc_preset_for_target(export_target_index):
    return "p7" if export_target_index == 0 else "p5"


def get_export_extension_for_codec(codec):
    return ".mov" if codec == "prores_ks" else ".mp4"


def should_enable_faststart(settings):
    # DISABLED v0.9.4: +faststart causes 10 min 117 Mbps random copy on 1000+ MB/s SSDs
    return False


def append_faststart_args(cmd, settings, log_callback=None):
    # No-op: faststart removed to unlock full SSD write speed (was 4KB random copy)
    if callable(log_callback) and settings.get('rate_control', 'cbr') not in ('cqp', 'lossless'):
        log_callback("Faststart disabled (v0.9.4): unlocked SSD speed, finalizing now 2 sec not 10 min.")
    return


def append_output_file_args(cmd, output_path, settings=None, log_callback=None):
    # FIX v0.9.4: Removed -flush_packets 0 and +faststart - direct sequential write
    settings = settings or {}
    cmd.append(output_path)


def build_nvenc_cbr_args(settings, fps_value=None, is_zero_copy=False):
    bitrate_kbps = int(settings.get('bitrate_mbps', 100) * 1000)
    codec = settings.get('video_codec', '')
    pixel_format = settings.get('pixel_format', 1)  # Default 10-bit
    
    if codec == 'h264_nvenc':
        pixel_format = 0  # H.264 only supports 8-bit
        
    pix_fmt = 'yuv420p' if pixel_format == 0 else 'p010le'
    export_target_index = settings.get('export_target_index', 0)
    preset = get_nvenc_preset_for_target(export_target_index)
    gop = str(int((fps_value or 30) * 2))
    rate_control = settings.get('rate_control', 'cbr')

    base = ['-preset', preset, '-tune', 'hq', '-g', gop]
    
    if codec == 'hevc_nvenc':
        base.extend(['-profile:v', 'main10' if pixel_format == 1 else 'main'])
        # Add high tier to support high bandwidth of 5K/8K
        base.extend(['-tier', 'high'])
        # FIX for 5K60 10-bit CBR >300 Mbps: Force Level 6.2 High Tier (up to 800 Mbps)
        # Without this, auto-level picks 5.1/6.0 which caps at 160/240 Mbps and fails with
        # InitializeEncoder failed: invalid param (8) when you request 500000k
        # Level 6.2 is required for any CBR >240 Mbps or for P7 Master exports
        if bitrate_kbps > 60000 or export_target_index == 0 or rate_control in ('cbr', 'vbr', 'lossless'):
            base.extend(['-level', '6.2'])
        # Fix multipass conflict with P1-P7 presets: P7 does not support old 2-pass CBR_HQ
        # Use single-pass or qres to avoid "Preset P1 to P7 not supported with older 2 Pass RC Modes"
        base.extend(['-multipass', 'disabled'])
    elif codec == 'h264_nvenc':
        base.extend(['-profile:v', 'high'])
        # For H.264 at 5K high bitrate, also force high level
        if bitrate_kbps > 60000:
            base.extend(['-level', '6.2'])
        
    if not is_zero_copy:
        base.extend(['-pix_fmt', pix_fmt])
        
    if codec != 'av1_nvenc':
        if rate_control == 'lossless':
            base.extend(['-bf', '0'])
        else:
            # FIX: At 5K60 10-bit, bf 3 + b_ref_mode middle can exceed hardware limits on many GPUs
            # For high bitrate Master (P7) or >300 Mbps, use fewer B-frames
            if bitrate_kbps > 300000 or (export_target_index == 0 and codec == 'hevc_nvenc'):
                base.extend(['-bf', '2', '-b_ref_mode', 'middle'])
            else:
                base.extend(['-bf', '3', '-b_ref_mode', 'middle'])

    if rate_control == 'cbr':
        # FIX: CBR bufsize must equal bitrate, not 2x. 2x creates 1 Gbps VBV for 500M which
        # NVENC rejects as invalid param. Your crash log showed -bufsize 1000000k for 500000k.
        return base + ['-rc', 'cbr', '-b:v', f'{bitrate_kbps}k', '-maxrate', f'{bitrate_kbps}k', '-bufsize', f'{bitrate_kbps}k']
    elif rate_control == 'vbr':
        return base + ['-rc', 'vbr', '-b:v', f'{bitrate_kbps}k', '-maxrate', f'{int(bitrate_kbps * 2)}k', '-bufsize', f'{int(bitrate_kbps * 2)}k']
    elif rate_control == 'abr':
        return base + ['-rc', 'vbr', '-b:v', f'{bitrate_kbps}k']
    elif rate_control == 'cqp':
        try:
            cq_value = int(settings.get('cq_value', 18))
        except (TypeError, ValueError):
            cq_value = 18
        cq_value = max(0, min(51, cq_value))
        return base + ['-rc', 'constqp', '-qp', str(cq_value)]
    elif rate_control == 'lossless':
        # FIX: true lossless qp 0 at 5K generates 2000+ Mbps. Keep level 6.2 already set above.
        return base + ['-rc', 'constqp', '-qp', '0']
    else:
        return base + ['-rc', 'cbr', '-b:v', f'{bitrate_kbps}k', '-maxrate', f'{bitrate_kbps}k', '-bufsize', f'{bitrate_kbps}k']


def get_cuvid_decoder_for_codec(codec_name):
    codec_name = (codec_name or "").lower()
    mapping = {
        "h264": "h264_cuvid",
        "hevc": "hevc_cuvid",
        "h265": "hevc_cuvid",
        "av1": "av1_cuvid",
        "vp9": "vp9_cuvid",
        "mpeg2video": "mpeg2_cuvid",
        "mpeg4": "mpeg4_cuvid",
    }
    return mapping.get(codec_name)


def build_hw_decode_input_args(file_path, codec_name, use_gpu_decode, target_fps=None):
    if not use_gpu_decode:
        return ['-i', file_path]
    # Drop -hwaccel_output_format cuda so frames are copied to system RAM for robust CPU filtering.
    # This avoids pad_cuda/overlay_cuda crashes/memory leaks entirely while still using hardware decoding.
    args = ['-hwaccel', 'cuda']
    # Re-enable -r target_fps to enforce Constant Frame Rate, preventing PTS stutter in the overlay
    if target_fps:
        args.extend(['-r', str(target_fps)])
    args.extend(['-i', file_path])
    return args


def has_optional_video_filters(settings):
    if settings.get('color_bw_mode', False): return True
    if settings.get('cinema_scope', False): return True
    if abs(settings.get('color_brightness', 0)) > 0.001: return True
    if abs(settings.get('color_contrast', 0)) > 0.001: return True
    if abs(settings.get('color_saturation', 0)) > 0.001: return True
    if abs(settings.get('color_gamma', 0)) > 0.001: return True
    if any(abs(settings.get(k, 0)) > 0.001 for k in ('lift_x', 'lift_y', 'gamma_x', 'gamma_y', 'gain_x', 'gain_y')): return True
    return any(settings.get(key, 0) > 0 for key in ('denoise_level', 'deflicker_level', 'exposure_level', 'temporal_level', 'sharpness_level'))


def _map_wheel_xy_to_rgb(x, y):
    r = x
    g = -0.5 * x + 0.866 * y
    b = -0.5 * x - 0.866 * y
    return r, g, b


def build_video_filters_from_settings(settings):
    """Shared FFmpeg filter chain for export and live MPV preview."""
    filters = []

    denoise = settings.get('denoise_level', 0)
    if denoise > 0:
        # FIX v0.9.4f: MULTI-THREADING - hqdn3d is single-threaded, user wants CPU/GPU sharing
        # Code 4294967274 = -22 Invalid arg = my previous scale_cuda inside bg6 was wrong
        # Simple fix: use threads=0 for hqdn3d which enables multi-threading where possible, or nlmeans which is threaded
        is_10bit = settings.get('pixel_format', 1) == 1 and settings.get('video_codec', '') != 'h264_nvenc'
        
        # Multi-threaded options - hqdn3d with threads=0 uses all cores for spatial part
        # nlmeans is threaded, atadenoise is threaded
        vals = ['', 
                'hqdn3d=1.5:1.5:6:6:threads=0', 
                'hqdn3d=2:2:8:8:threads=0', 
                'hqdn3d=3:3:10:10:threads=0',
                'hqdn3d=4:4:12:12:threads=0', 
                'hqdn3d=6:6:15:15:threads=0', 
                'hqdn3d=8:8:18:18:threads=0']
        
        if denoise < len(vals) and vals[denoise]:
            if is_10bit:
                # 10-bit needs nv12 for hqdn3d
                filters.append(f"format=nv12,{vals[denoise]},format=p010le")
            else:
                filters.append(vals[denoise])

    deflicker = settings.get('deflicker_level', 0)
    if deflicker > 0:
        vals = ['', 'deflicker=mode=pm:size=5', 'deflicker=mode=pm:size=10', 'deflicker=mode=pm:size=15',
                'deflicker=mode=am:size=20', 'deflicker=mode=am:size=30']
        if deflicker < len(vals):
            filters.append(vals[deflicker])

    exposure = settings.get('exposure_level', 0)
    if exposure > 0:
        exp_map = {1: 'eq=brightness=0.05', 2: 'eq=brightness=0.1', 3: 'eq=brightness=0.15',
                   4: 'eq=brightness=0.2', 5: 'eq=brightness=-0.05', 6: 'eq=brightness=-0.1'}
        if exposure in exp_map:
            filters.append(exp_map[exposure])

    temporal = settings.get('temporal_level', 0)
    if temporal > 0:
        vals = ['', 'tmix=frames=3:weights="1 1 1"', 'tmix=frames=5:weights="1 1 2 1 1"',
                'tmix=frames=7:weights="1 1 2 2 2 1 1"']
        if temporal < len(vals):
            filters.append(vals[temporal])

    sharpness = settings.get('sharpness_level', 0)
    if sharpness > 0:
        vals = ['', 'unsharp=3:3:0.3:3:3:0', 'unsharp=5:5:0.5:5:5:0', 'unsharp=5:5:0.8:5:5:0.4']
        if sharpness < len(vals):
            filters.append(vals[sharpness])

    rs, gs, bs = _map_wheel_xy_to_rgb(settings.get('lift_x', 0), settings.get('lift_y', 0))
    rm, gm, bm = _map_wheel_xy_to_rgb(settings.get('gamma_x', 0), settings.get('gamma_y', 0))
    rh, gh, bh = _map_wheel_xy_to_rgb(settings.get('gain_x', 0), settings.get('gain_y', 0))
    if any(v != 0 for v in (rs, gs, bs, rm, gm, bm, rh, gh, bh)):
        filters.append(
            f"colorbalance=rs={rs:.2f}:gs={gs:.2f}:bs={bs:.2f}:rm={rm:.2f}:gm={gm:.2f}:bm={bm:.2f}"
            f":rh={rh:.2f}:gh={gh:.2f}:bh={bh:.2f}"
        )

    b = settings.get('color_brightness', 0) / 100.0
    c = (settings.get('color_contrast', 0) / 100.0) + 1.0
    s = (settings.get('color_saturation', 0) / 100.0) + 1.0
    g = (settings.get('color_gamma', 0) / 100.0) + 1.0
    if b != 0.0 or c != 1.0 or s != 1.0 or g != 1.0:
        filters.append(f'eq=brightness={b:.2f}:contrast={c:.2f}:saturation={s:.2f}:gamma={g:.2f}')

    if settings.get('color_bw_mode', False):
        filters.append('hue=s=0')

    if settings.get('cinema_scope', False):
        # 2.35:1 letterbox bars (~12.1% top/bottom on 16:9)
        filters.append(
            'drawbox=x=0:y=0:w=iw:h=ih*0.121:color=black:t=fill,'
            'drawbox=x=0:y=ih*0.879:w=iw:h=ih*0.121:color=black:t=fill'
        )

    return filters


def analyze_timeline_auto_balance(timeline):
    """Analyze timeline clips and compute lift/gamma/gain + eq corrections."""
    if not timeline.clips:
        return None

    samples = []
    sorted_clips = sorted(timeline.clips, key=lambda c: c.start_time)[:3]
    for clip in sorted_clips:
        if not os.path.exists(clip.file_path):
            continue
        seek = clip.in_point + max(0.1, clip.get_trimmed_duration() * 0.5)
        cmd = [
            'ffmpeg', '-v', 'error', '-ss', f'{seek:.3f}', '-i', clip.file_path,
            '-vframes', '1', '-vf', 'scale=32:32', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1',
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            if result.returncode != 0 or len(result.stdout) < 32 * 32 * 3:
                continue
            data = result.stdout
            n = 32 * 32
            avg_r = sum(data[i * 3] for i in range(n)) / n
            avg_g = sum(data[i * 3 + 1] for i in range(n)) / n
            avg_b = sum(data[i * 3 + 2] for i in range(n)) / n
            samples.append((avg_r, avg_g, avg_b))
        except Exception:
            continue

    if not samples:
        return None

    avg_r = sum(s[0] for s in samples) / len(samples)
    avg_g = sum(s[1] for s in samples) / len(samples)
    avg_b = sum(s[2] for s in samples) / len(samples)
    r_dev = (avg_r - 128) / 128.0
    g_dev = (avg_g - 128) / 128.0
    b_dev = (avg_b - 128) / 128.0
    luma = 0.299 * avg_r + 0.587 * avg_g + 0.114 * avg_b
    chroma_spread = max(avg_r, avg_g, avg_b) - min(avg_r, avg_g, avg_b)

    return {
        'lift_x': max(-0.5, min(0.5, -r_dev * 0.3)),
        'lift_y': max(-0.5, min(0.5, (g_dev - b_dev) * 0.2)),
        'gamma_x': max(-0.5, min(0.5, -r_dev * 0.15)),
        'gamma_y': max(-0.5, min(0.5, (g_dev - b_dev) * 0.1)),
        'gain_x': max(-0.5, min(0.5, -r_dev * 0.1)),
        'gain_y': max(-0.5, min(0.5, (g_dev - b_dev) * 0.05)),
        'color_brightness': int(max(-30, min(30, (128 - luma) / 128 * 40))),
        'color_contrast': 10 if luma < 110 or luma > 145 else 5,
        'color_saturation': 15 if chroma_spread < 40 else 5,
    }


def detect_hardware_capabilities():
    caps = {
        'nvidia_smi': False,
        'gpu_name': 'Unknown GPU',
        'nvidia': False,
        'amd': False,
        'intel': False,
        'nvenc_h264': False,
        'nvenc_hevc': False,
        'nvenc_av1': False,
        'amf_h264': False,
        'amf_hevc': False,
        'amf_av1': False,
        'qsv_h264': False,
        'qsv_hevc': False,
        'qsv_av1': False,
        'vaapi_h264': False,
        'vaapi_hevc': False,
        'vaapi_av1': False,
        'nvdec': False,
        'encoders': [],
    }

    try:
        if shutil.which('nvidia-smi'):
            caps['nvidia_smi'] = True
            smi = subprocess.run(
                ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
                capture_output=True, text=True, timeout=2, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            name = (smi.stdout or '').strip().splitlines()
            if name and name[0]:
                caps['gpu_name'] = name[0]
    except Exception:
        pass

    try:
        enc = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        enc_text = (enc.stdout or '') + (enc.stderr or '')
        # NVIDIA NVENC
        caps['nvenc_h264'] = 'h264_nvenc' in enc_text
        caps['nvenc_hevc'] = 'hevc_nvenc' in enc_text
        caps['nvenc_av1'] = 'av1_nvenc' in enc_text
        # AMD AMF
        caps['amf_h264'] = 'h264_amf' in enc_text
        caps['amf_hevc'] = 'hevc_amf' in enc_text
        caps['amf_av1'] = 'av1_amf' in enc_text
        # Intel QSV
        caps['qsv_h264'] = 'h264_qsv' in enc_text
        caps['qsv_hevc'] = 'hevc_qsv' in enc_text
        caps['qsv_av1'] = 'av1_qsv' in enc_text
        # VAAPI (Linux)
        caps['vaapi_h264'] = 'h264_vaapi' in enc_text
        caps['vaapi_hevc'] = 'hevc_vaapi' in enc_text
        caps['vaapi_av1'] = 'av1_vaapi' in enc_text

        # Vendor flags
        caps['nvidia'] = caps['nvenc_h264'] or caps['nvenc_hevc'] or caps['nvenc_av1'] or caps['nvidia_smi']
        caps['amd'] = caps['amf_h264'] or caps['amf_hevc'] or caps['amf_av1'] or caps['vaapi_h264']
        caps['intel'] = caps['qsv_h264'] or caps['qsv_hevc'] or caps['qsv_av1']

        # Build list of available encoders for UI
        for key in ['nvenc_h264','nvenc_hevc','nvenc_av1','amf_h264','amf_hevc','amf_av1','qsv_h264','qsv_hevc','qsv_av1','vaapi_h264','vaapi_hevc','vaapi_av1']:
            if caps.get(key):
                caps['encoders'].append(key)
    except Exception:
        pass

    try:
        dec = subprocess.run(['ffmpeg', '-hide_banner', '-decoders'], capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        dec_text = (dec.stdout or '') + (dec.stderr or '')
        caps['nvdec'] = ('h264_cuvid' in dec_text) or ('hevc_cuvid' in dec_text) or ('av1_cuvid' in dec_text)
    except Exception:
        pass

    return caps


def get_codec_display_list(caps):
    """Build dynamic codec list based on detected hardware - for INNO installer consistency"""
    options = []
    # Always available - CPU
    options.append(("ProRes (CPU - Universal)", "prores_ks"))
    # NVIDIA
    if caps.get('nvenc_h264'):
        options.append(("H.264 (NVENC - NVIDIA)", "h264_nvenc"))
    if caps.get('nvenc_hevc'):
        options.append(("H.265/HEVC (NVENC - NVIDIA)", "hevc_nvenc"))
    if caps.get('nvenc_av1'):
        options.append(("AV1 (NVENC - NVIDIA)", "av1_nvenc"))
    # AMD AMF - Windows
    if caps.get('amf_h264'):
        options.append(("H.264 (AMF - AMD)", "h264_amf"))
    if caps.get('amf_hevc'):
        options.append(("H.265/HEVC (AMF - AMD)", "hevc_amf"))
    if caps.get('amf_av1'):
        options.append(("AV1 (AMF - AMD)", "av1_amf"))
    # Intel QSV
    if caps.get('qsv_h264'):
        options.append(("H.264 (QSV - Intel)", "h264_qsv"))
    if caps.get('qsv_hevc'):
        options.append(("H.265/HEVC (QSV - Intel)", "hevc_qsv"))
    if caps.get('qsv_av1'):
        options.append(("AV1 (QSV - Intel)", "av1_qsv"))
    # VAAPI - Linux fallback for AMD/Intel
    if caps.get('vaapi_h264') and not caps.get('amf_h264') and not caps.get('qsv_h264'):
        options.append(("H.264 (VAAPI - Linux)", "h264_vaapi"))
    if caps.get('vaapi_hevc') and not caps.get('amf_hevc') and not caps.get('qsv_hevc'):
        options.append(("H.265/HEVC (VAAPI - Linux)", "hevc_vaapi"))

    # Fallback if no HW encoders found - show all NVENC as disabled hint
    if len(options) == 1:
        options.append(("H.264 (NVENC - Not Detected)", "h264_nvenc"))
        options.append(("H.265/HEVC (NVENC - Not Detected)", "hevc_nvenc"))
        options.append(("AV1 (NVENC - Not Detected)", "av1_nvenc"))

    return options


# Validation matrix for next version - which rate controls work with which codec
VALID_RC_FOR_CODEC = {
    'prores_ks': ['cbr', 'vbr', 'cqp'],  # ProRes uses quality slider differently
    'h264_nvenc': ['cbr', 'vbr', 'abr', 'cqp'],
    'hevc_nvenc': ['cbr', 'vbr', 'abr', 'cqp', 'lossless'],
    'av1_nvenc': ['cbr', 'vbr', 'abr', 'cqp'],  # lossless not supported on AV1 NVENC
    'h264_amf': ['cbr', 'vbr', 'cqp'],
    'hevc_amf': ['cbr', 'vbr', 'cqp'],
    'av1_amf': ['cbr', 'vbr', 'cqp'],
    'h264_qsv': ['cbr', 'vbr', 'abr', 'cqp'],
    'hevc_qsv': ['cbr', 'vbr', 'abr', 'cqp', 'lossless'],
    'av1_qsv': ['cbr', 'vbr', 'cqp'],
    'h264_vaapi': ['cbr', 'vbr', 'cqp'],
    'hevc_vaapi': ['cbr', 'vbr', 'cqp'],
}

INVALID_RC_REASON = {
    ('av1_nvenc', 'lossless'): "AV1 NVENC does not support lossless QP 0 - use CQP 0-5 for near-lossless",
    ('av1_amf', 'lossless'): "AV1 AMF does not support lossless - use CQP",
    ('av1_qsv', 'lossless'): "AV1 QSV does not support lossless - use CQP",
    ('h264_nvenc', 'lossless'): "H.264 NVENC lossless is not stable - use HEVC for lossless archive",
    ('h264_amf', 'lossless'): "H.264 AMF does not support lossless",
}


# --- WAVEFORM GENERATOR ---

class WaveformWorker(QThread):
    finished = pyqtSignal(str, object)

    def __init__(self, file_path):
        super().__init__()
        self.file_path = file_path

    def run(self):
        try:
            temp_png = os.path.join(tempfile.gettempdir(), f"wave_{hash(self.file_path)}.png")

            cmd = [
                'ffmpeg', '-y', '-v', 'error',
                '-i', self.file_path,
                '-filter_complex', 'aformat=channel_layouts=mono,showwavespic=s=2000x100:colors=white|0x4ade80',
                '-frames:v', '1',
                temp_png
            ]

            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

            if os.path.exists(temp_png):
                image = QImage(temp_png)
                self.finished.emit(self.file_path, image)
                try:
                    os.remove(temp_png)
                except:
                    pass
        except Exception as e:
            print(f"Waveform gen error: {e}")

# --- ACCESSIBILITY CLASSES ---

class DwellClickOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(60, 60)
        self.progress = 0.0
        self.active = False

    def update_progress(self, value):
        self.progress = value
        self.update()

    def paintEvent(self, event):
        if not self.active or self.progress <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 100))
        painter.drawEllipse(5, 5, 50, 50)

        pen = QPen(QColor("#4ade80"))
        pen.setWidth(6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        span_angle = int(-self.progress * 360 * 16)
        painter.drawArc(10, 10, 40, 40, 90 * 16, span_angle)

class DwellClickFilter(QObject):
    click_triggered = pyqtSignal(QPoint)
    progress_update = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer()
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.check_dwell)
        self.enabled = False
        self.last_pos = QPoint(0, 0)
        self.dwell_start_time = 0
        self.dwell_duration = 1.2
        self.jitter_threshold = 10
        self.overlay = DwellClickOverlay()

    def set_enabled(self, enabled):
        self.enabled = enabled
        if enabled:
            self.timer.start()
            self.overlay.show()
        else:
            self.timer.stop()
            self.overlay.hide()

    def set_params(self, duration, threshold):
        self.dwell_duration = duration
        self.jitter_threshold = threshold

    def check_dwell(self):
        if not self.enabled: return
        current_pos = QCursor.pos()
        dist = (current_pos - self.last_pos).manhattanLength()
        if dist > self.jitter_threshold:
            self.last_pos = current_pos
            self.dwell_start_time = time.time()
            self.overlay.active = False
            self.overlay.update_progress(0)
            self.overlay.move(current_pos.x() - 30, current_pos.y() - 30)
        else:
            elapsed = time.time() - self.dwell_start_time
            progress = min(1.0, elapsed / self.dwell_duration)
            self.overlay.move(current_pos.x() - 30, current_pos.y() - 30)
            self.overlay.active = True
            self.overlay.update_progress(progress)
            if elapsed >= self.dwell_duration:
                self.dwell_start_time = time.time()
                self.overlay.update_progress(0)
                self.perform_click(current_pos)

    def perform_click(self, pos):
        self.overlay.hide()
        widget = QApplication.widgetAt(pos)
        if widget:
            local_pos = widget.mapFromGlobal(pos)
            QTest_click = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(local_pos), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(widget, QTest_click)
            QTest_release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(local_pos), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(widget, QTest_release)
        QTimer.singleShot(100, self.overlay.show)

# --- MPV VIDEO WIDGET ---

class MPVVideoWidget(QWidget):
    positionChanged = pyqtSignal(int)
    durationChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)

        self.mpv = None
        self.current_file = None
        self._is_paused = True
        self._duration_ms = 0
        self._position_ms = 0
        self._pending_audio_filter = None
        self._pending_video_filter = None
        self._file_loading = False
        self._pending_seek_ms = None

        self.position_timer = QTimer(self)
        self.position_timer.timeout.connect(self._update_position)
        self.position_timer.setInterval(100)

        self.setStyleSheet("background-color: #0f172a;")
        self.setMinimumSize(640, 360)

        if not MPV_AVAILABLE:
            layout = QVBoxLayout(self)
            error_label = QLabel(
                "âš ï¸ MPV preview unavailable"
                "â€¢ Development: pip install mpv"
                "â€¢ Linux: install python-mpv and mpv (distro packages)"
                "â€¢ Frozen EXE (Auto-py-to-exe): Advanced â†’ add --hidden-import=mpv"
                "  and Add Binary: libmpv-2.dll (plus any DLLs your MPV build needs)"
            )
            error_label.setStyleSheet("color: #ef4444; font-size: 12pt; font-weight: bold;")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            error_label.setWordWrap(True)
            layout.addWidget(error_label)
            return

        layout = QVBoxLayout(self)
        info_label = QLabel(
            "ðŸŽ¬ Video Preview"
            "Video plays in a separate MPV window"
            "(Native Wayland support - no XWayland needed)"
            "Use the playback controls below"
        )
        info_label.setStyleSheet("""
            QLabel {
                color: #60a5fa;
                font-size: 12pt;
                background-color: #1e293b;
                border: 2px solid #3b82f6;
                border-radius: 8px;
                padding: 20px;
            }
        """)
        info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(info_label)

        self._init_mpv()

    def _mpv_emit_duration_ms(self, ms):
        self._duration_ms = ms
        self.durationChanged.emit(ms)

    def _mpv_set_paused(self, value):
        self._is_paused = bool(value)

    def _mpv_apply_file_loaded_pending(self):
        """Runs on Qt GUI thread (libmpv callbacks are not thread-safe with Qt)."""
        if not self.mpv:
            self._file_loading = False
            return
        try:
            if self._pending_audio_filter is not None:
                try:
                    self.mpv.lavfi_complex = self._pending_audio_filter
                except Exception:
                    pass
                self._pending_audio_filter = None
            if self._pending_video_filter is not None:
                try:
                    self.mpv['vf'] = self._pending_video_filter
                except Exception:
                    pass
                self._pending_video_filter = None
            if self._pending_seek_ms is not None:
                try:
                    self.mpv.seek(self._pending_seek_ms / 1000.0, reference='absolute')
                    self._position_ms = self._pending_seek_ms
                except Exception:
                    pass
                self._pending_seek_ms = None
        finally:
            self._file_loading = False

    def _init_mpv(self):
        if not MPV_AVAILABLE:
            return
        try:
            import mpv
            self.mpv = mpv.MPV(
                vo='gpu',
                hwdec='auto-copy',
                keep_open='yes',
                idle='yes',
                hr_seek='yes',
                force_window='immediate',
                ontop='no',
                border='yes',
                title='FastEncodePro - Video Preview',
                geometry='640x360',
                osc='no',
                input_default_bindings='no',
                input_vo_keyboard='no',
                audio_client_name='FastEncodePro',
                audio_fallback_to_null='yes',
                cache='yes',
                demuxer_max_bytes='100MiB',
            )

            @self.mpv.property_observer('duration')
            def duration_observer(_name, value):
                if value and value > 0:
                    ms = int(value * 1000)
                    QTimer.singleShot(0, lambda m=ms: self._mpv_emit_duration_ms(m))

            @self.mpv.property_observer('time-pos')
            def position_observer(_name, value):
                if value is not None:
                    self._position_ms = int(value * 1000)

            @self.mpv.property_observer('pause')
            def pause_observer(_name, value):
                v = bool(value)
                QTimer.singleShot(0, lambda v=v: self._mpv_set_paused(v))

            @self.mpv.event_callback('file-loaded')
            def file_loaded_handler(event):
                QTimer.singleShot(0, self._mpv_apply_file_loaded_pending)

        except Exception:
            self.mpv = None

    def load_file(self, file_path, seek_ms=None):
        if not self.mpv:
            return False
        try:
            self._file_loading = True
            self.mpv.lavfi_complex = ""
            self._pending_audio_filter = None
            self._pending_video_filter = None
            self._pending_seek_ms = seek_ms
            self.current_file = file_path
            self.mpv.loadfile(file_path)
            self.mpv.pause = True
            self._is_paused = True
            return True
        except Exception:
            self._file_loading = False
            self._pending_seek_ms = None
            return False

    def play(self):
        if not self.mpv or not self.current_file:
            return
        try:
            self.mpv.pause = False
            self._is_paused = False
            self.position_timer.start()
        except Exception:
            pass

    def pause(self):
        if not self.mpv:
            return
        try:
            self.mpv.pause = True
            self._is_paused = True
            self.position_timer.stop()
        except Exception:
            pass

    def is_paused(self):
        return self._is_paused

    def seek(self, position_ms):
        if not self.mpv:
            return
        try:
            self.mpv.seek(position_ms / 1000.0, reference='absolute')
            self._position_ms = position_ms
        except Exception:
            pass

    def position(self):
        return self._position_ms

    def duration(self):
        return self._duration_ms

    def _update_position(self):
        self.positionChanged.emit(self._position_ms)

    def stop(self):
        if not self.mpv:
            return
        try:
            self.mpv.command('stop')
            self._is_paused = True
            self._position_ms = 0
            self.position_timer.stop()
        except Exception:
            pass

    def set_audio_complex_filter(self, filter_string):
        if not self.mpv:
            return
        self._pending_audio_filter = filter_string
        if self._file_loading:
            return
        try:
            self.mpv.lavfi_complex = filter_string
            self._pending_audio_filter = None
        except Exception:
            pass

    def set_video_filter(self, filter_string):
        if not self.mpv:
            return
        self._pending_video_filter = filter_string
        if self._file_loading:
            return
        try:
            self.mpv['vf'] = filter_string
            self._pending_video_filter = None
        except Exception:
            pass

    def shutdown(self):
        self.position_timer.stop()
        if self.mpv:
            try:
                self.mpv.terminate()
            except Exception:
                pass


class TimelineClip:
    def __init__(self, file_path, track, start_time, in_point=0, out_point=None, duration=None, volumes=None, normalization=None, sync_offset=None):
        self.file_path = file_path
        self.track = track
        self.start_time = start_time
        self.in_point = in_point
        self.name = Path(file_path).name
        self.full_duration = duration if duration is not None else self.get_video_duration()
        self.audio_streams = get_audio_stream_count_static(self.file_path)

        self.volumes = volumes if volumes else [0.0] * max(1, self.audio_streams)
        self.normalization = normalization if normalization else [False] * max(1, self.audio_streams)
        self.sync_offset = sync_offset if sync_offset is not None else 0

        self.waveform_pixmap = None
        self.transition_type = None
        self.transition_duration = 0

        if out_point is None or out_point <= 0:
            self.out_point = self.full_duration
        else:
            self.out_point = out_point
        if self.out_point <= self.in_point:
            self.out_point = self.full_duration

    def get_video_duration(self):
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.file_path], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return float(result.stdout.strip())
        except:
            return 60.0

    def get_trimmed_duration(self):
        return self.out_point - self.in_point

    def get_end_time(self):
        return self.start_time + self.get_trimmed_duration()

    def timeline_time_to_clip_time(self, timeline_time):
        if timeline_time < self.start_time or timeline_time > self.get_end_time():
            return None
        offset = timeline_time - self.start_time
        return self.in_point + offset

    def to_dict(self):
        return {
            "file_path": self.file_path,
            "track": self.track,
            "start_time": self.start_time,
            "in_point": self.in_point,
            "out_point": self.out_point,
            "duration": self.full_duration,
            "volumes": self.volumes,
            "normalization": self.normalization,
            "sync_offset": self.sync_offset
        }

    @staticmethod
    def from_dict(data):
        return TimelineClip(
            data["file_path"],
            data["track"],
            data["start_time"],
            data["in_point"],
            data["out_point"],
            data["duration"],
            data.get("volumes", [0.0]),
            data.get("normalization", [False]),
            data.get("sync_offset", 0)
        )


class TextClip:
    def __init__(self, text, start_time, duration):
        self.text = text
        self.start_time = start_time
        self.duration = duration
        self.font_color = "white"
        self.font_size = 48
        self.x = "(w-text_w)/2"
        self.y = "(h-text_h)-50"
    
    def get_end_time(self):
        return self.start_time + self.duration

class AudioClip:
    def __init__(self, file_path, start_time, duration):
        self.file_path = file_path
        self.start_time = start_time
        self.duration = duration
        
    def get_end_time(self):
        return self.start_time + self.duration

class ColorWheelWidget(QWidget):
    colorChanged = pyqtSignal(float, float)
    
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setMinimumSize(120, 140)
        self.title = title
        self.cursor_pos = QPointF(0, 0)
        self.is_dragging = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        w, h = self.width(), self.height() - 20
        cx, cy = w / 2, h / 2 + 10
        radius = min(cx, cy - 10) - 5
        
        # Draw background
        painter.setPen(Qt.PenStyle.NoPen)
        gradient = QConicalGradient(cx, cy, 0)
        for i in range(360):
            gradient.setColorAt(i / 360.0, QColor.fromHsv(i, 255, 180))
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        
        # Desaturate center
        rad_grad = QRadialGradient(cx, cy, radius)
        rad_grad.setColorAt(0.0, QColor(128, 128, 128, 255))
        rad_grad.setColorAt(1.0, QColor(128, 128, 128, 0))
        painter.setBrush(QBrush(rad_grad))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        
        # Draw cursor
        cursor_x = cx + self.cursor_pos.x() * radius
        cursor_y = cy + self.cursor_pos.y() * radius
        
        painter.setPen(QPen(Qt.GlobalColor.white, 2))
        painter.setBrush(Qt.GlobalColor.black)
        painter.drawEllipse(QPointF(cursor_x, cursor_y), 4, 4)
        
        # Draw Title
        painter.setPen(QPen(Qt.GlobalColor.white))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, w, 20), Qt.AlignmentFlag.AlignCenter, self.title)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.update_cursor(event.position())
            self.is_dragging = True

    def mouseMoveEvent(self, event):
        if self.is_dragging:
            self.update_cursor(event.position())

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = False

    def mouseDoubleClickEvent(self, event):
        self.cursor_pos = QPointF(0, 0)
        self.update()
        self.emit_color()

    def update_cursor(self, pos):
        w, h = self.width(), self.height() - 20
        cx, cy = w / 2, h / 2 + 10
        radius = min(cx, cy - 10) - 5
        
        dx = pos.x() - cx
        dy = pos.y() - cy
        dist = math.hypot(dx, dy)
        
        if dist > radius:
            dx = (dx / dist) * radius
            dy = (dy / dist) * radius
            
        self.cursor_pos = QPointF(dx / radius, dy / radius)
        self.update()
        self.emit_color()

    def emit_color(self):
        # Invert Y so up is positive
        self.colorChanged.emit(self.cursor_pos.x(), -self.cursor_pos.y())

    def reset(self, emit=True):
        self.cursor_pos = QPointF(0, 0)
        self.is_dragging = False
        self.update()
        if emit:
            self.emit_color()

class TimelineWidget(QWidget):
    clip_selected = pyqtSignal(object)
    playhead_moved = pyqtSignal(float)
    timeline_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.clips = []
        self.text_clips = []
        self.audio_clips = []
        self.selected_clip = None
        self.selected_text_clip = None
        self.selected_audio_clip = None
        self.dragging_clip = None
        self.drag_start_pos = None
        self.drag_offset = 0
        self.zoom_level = 10.0
        self.scroll_offset = 0
        self.setMinimumHeight(350)
        self.setMouseTracking(True)
        self.track_height = 100
        self.num_tracks = 4
        self.playhead_position = 0
        self.dragging_playhead = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.waveform_threads = []

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg_color = QColor("#111827")
        painter.fillRect(self.rect(), bg_color)
        ruler_height = 40
        painter.fillRect(0, 0, self.width(), ruler_height, QColor("#1f2937"))
        painter.setPen(QColor("#9ca3af"))
        font = QFont("Arial", 8)
        painter.setFont(font)

        visible_time_start = self.scroll_offset
        visible_time_end = self.scroll_offset + (self.width() / self.zoom_level)
        for sec in range(int(visible_time_start), int(visible_time_end) + 1, 5):
            x = (sec - self.scroll_offset) * self.zoom_level
            if 0 <= x < self.width():
                painter.drawLine(int(x), ruler_height - 10, int(x), ruler_height)
                painter.drawText(int(x) + 2, ruler_height - 15, f"{sec}s")
        for track in range(self.num_tracks):
            y = ruler_height + track * self.track_height
            track_color = QColor("#1f2937") if track % 2 == 0 else QColor("#374151")
            painter.fillRect(0, y, self.width(), self.track_height, track_color)
            painter.setPen(QColor("#4b5563"))
            painter.drawLine(0, y + self.track_height, self.width(), y + self.track_height)
        for clip in self.clips:
            self.draw_clip(painter, clip, ruler_height)
        # Draw text clips on track 2
        for tc in self.text_clips:
            x = self.time_to_x(tc.start_time)
            w = int(tc.duration * self.zoom_level)
            y = ruler_height + 2 * self.track_height + 5
            h = self.track_height - 10
            painter.setBrush(QBrush(QColor("#f59e0b")))
            painter.setPen(QPen(QColor("white"), 2))
            painter.drawRoundedRect(x, y, w, h, 5, 5)
            painter.setPen(QColor("white"))
            painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
            painter.drawText(x + 5, y + 20, tc.text[:30])
            painter.setFont(QFont("Arial", 7))
            painter.drawText(x + 5, y + h - 5, f"TEXT | {tc.duration:.1f}s")
        # Draw audio (voiceover) clips on track 2 (visible row)
        for ac in self.audio_clips:
            x = self.time_to_x(ac.start_time)
            w = max(int(ac.duration * self.zoom_level), 10)
            y = ruler_height + 2 * self.track_height + 5
            h = self.track_height - 10
            painter.setBrush(QBrush(QColor("#ec4899")))
            painter.setPen(QPen(QColor("white"), 2))
            painter.drawRoundedRect(x, y, w, h, 5, 5)
            painter.setPen(QColor("white"))
            painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
            painter.drawText(x + 5, y + 20, Path(ac.file_path).name[:30])
            painter.setFont(QFont("Arial", 7))
            painter.drawText(x + 5, y + h - 5, f"VO | {ac.duration:.1f}s")
        painter.setPen(QPen(QColor("#ef4444"), 3))
        playhead_x = int((self.playhead_position - self.scroll_offset) * self.zoom_level)
        painter.drawLine(playhead_x, 0, playhead_x, self.height())
        painter.setBrush(QBrush(QColor("#ef4444")))
        points = [QPointF(playhead_x, 0), QPointF(playhead_x - 8, 15), QPointF(playhead_x + 8, 15)]
        painter.drawPolygon(points)

        if self.hasFocus():
            painter.setPen(QPen(QColor("#f59e0b"), 4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.rect().adjusted(2,2,-2,-2))

    def draw_clip(self, painter, clip, ruler_height):
        x = self.time_to_x(clip.start_time)
        width = int(clip.get_trimmed_duration() * self.zoom_level)
        y = ruler_height + clip.track * self.track_height + 5
        height = self.track_height - 10

        if clip == self.selected_clip:
            color = QColor("#3b82f6")
        else:
            color = QColor("#10b981")

        painter.setBrush(QBrush(color))
        painter.setPen(QPen(QColor("white"), 2))
        painter.drawRoundedRect(x, y, width, height, 5, 5)

        if clip.waveform_pixmap:
            wave_rect = QRectF(x + 5, y + 25, width - 10, height - 35)
            painter.drawPixmap(wave_rect.toRect(), clip.waveform_pixmap)

        painter.setPen(QColor("white"))
        font = QFont("Arial", 9, QFont.Weight.Bold)
        painter.setFont(font)
        text_rect = painter.boundingRect(x + 5, y + 5, width - 10, height - 10, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, clip.name)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, clip.name)

        trans = getattr(clip, 'transition_type', None)
        trans_text = f" | T:{trans}" if trans else ""
        info_text = f"{clip.get_trimmed_duration():.1f}s | {clip.audio_streams} Trk{trans_text}"
        duration_rect = painter.boundingRect(x + 5, y + height - 20, width - 10, 15, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, info_text)
        painter.drawText(duration_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, info_text)
        # Draw transition indicator
        if trans:
            painter.setBrush(QBrush(QColor("#f59e0b")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(x, y, 6, height, 3, 3)

    def time_to_x(self, time):
        return int((time - self.scroll_offset) * self.zoom_level)

    def x_to_time(self, x):
        return (x / self.zoom_level) + self.scroll_offset

    def y_to_track(self, y, ruler_height=40):
        if y < ruler_height:
            return -1
        return max(0, min(self.num_tracks - 1, (y - ruler_height) // self.track_height))

    def set_playhead_position(self, time, auto_scroll=True, emit_signal=True):
        self.playhead_position = max(0, time)
        if auto_scroll:
            playhead_x = (self.playhead_position - self.scroll_offset) * self.zoom_level
            left_margin = self.width() * 0.1
            right_margin = self.width() * 0.9

            if playhead_x > right_margin:
                self.scroll_offset += (playhead_x - right_margin) / self.zoom_level
            elif playhead_x < left_margin and self.scroll_offset > 0:
                self.scroll_offset = max(0, self.scroll_offset - (left_margin - playhead_x) / self.zoom_level)

        self.update()
        if emit_signal:
            self.playhead_moved.emit(self.playhead_position)

    def get_snap_time(self, time):
        snap_threshold_pixels = 15
        snap_threshold_time = snap_threshold_pixels / self.zoom_level
        closest_snap = None
        min_dist = float('inf')

        if abs(time) < snap_threshold_time:
            closest_snap = 0
            min_dist = abs(time)

        for clip in self.clips:
            dist_start = abs(time - clip.start_time)
            if dist_start < snap_threshold_time and dist_start < min_dist:
                min_dist = dist_start
                closest_snap = clip.start_time
            dist_end = abs(time - clip.get_end_time())
            if dist_end < snap_threshold_time and dist_end < min_dist:
                min_dist = dist_end
                closest_snap = clip.get_end_time()

        return closest_snap if closest_snap is not None else time

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            click_x = event.position().x()
            click_y = event.position().y()
            raw_time = self.x_to_time(click_x)
            click_time = self.get_snap_time(raw_time)

            if click_y < 40:
                # Ruler / playhead: whole-timeline preview mode
                self.timeline_clicked.emit()
                self.dragging_playhead = True
                self.set_playhead_position(click_time, auto_scroll=True)
                return
                
            clicked_track = self.y_to_track(click_y)
            if clicked_track < 0:
                return
                
            for clip in reversed(self.clips):
                if (clip.track == clicked_track and clip.start_time <= click_time <= clip.get_end_time()):
                    self.selected_clip = clip
                    self.dragging_clip = clip
                    self.drag_start_pos = click_time
                    self.drag_offset = click_time - clip.start_time
                    self.clip_selected.emit(clip)
                    self.update()
                    return
                    
            # Empty track click: sequence mode, clear clip selection.
            # FIX: Also move playhead to click position so preview doesn't stay black
            self.selected_clip = None
            self.set_playhead_position(click_time, auto_scroll=True)
            self.timeline_clicked.emit()
            self.update()

    def mouseMoveEvent(self, event):
        click_x = event.position().x()
        raw_time = self.x_to_time(click_x)
        click_time = self.get_snap_time(raw_time)

        if self.dragging_playhead:
            self.set_playhead_position(click_time, auto_scroll=True)
            return
        if self.dragging_clip:
            new_time = click_time - self.drag_offset
            snapped_start = self.get_snap_time(new_time)

            new_track = self.y_to_track(event.position().y())
            if new_track >= 0:
                self.dragging_clip.start_time = max(0, snapped_start)
                self.dragging_clip.track = new_track
                self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging_clip = None
            self.drag_start_pos = None
            self.dragging_playhead = False

    def contextMenuEvent(self, event):
        click_x = event.pos().x()
        click_y = event.pos().y()
        click_time = self.x_to_time(click_x)
        clicked_track = self.y_to_track(click_y)
        
        menu = QMenu(self)
        menu.setStyleSheet("QMenu { background: #1f2937; color: white; } QMenu::item:selected { background: #3b82f6; }")
        
        # Check if we right-clicked on a clip
        target_clip = None
        for clip in reversed(self.clips):
            if clip.track == clicked_track and clip.start_time <= click_time <= clip.get_end_time():
                target_clip = clip
                break

        if target_clip:
            self.selected_clip = target_clip
            self.update()
            
            trans_menu = menu.addMenu("Add Transition")
            for t_name in ["fade", "fadeblack", "fadewhite", "wipeleft", "wiperight", "wipeup", "wipedown",
                           "slideleft", "slideright", "slideup", "slidedown",
                           "circlecrop", "rectcrop", "distance", "dissolve",
                           "pixelize", "diagtl", "diagtr", "diagbl", "diagbr",
                           "hlslice", "hrslice", "vuslice", "vdslice",
                           "smoothleft", "smoothright", "smoothup", "smoothdown"]:
                action = trans_menu.addAction(t_name)
                action.triggered.connect(lambda checked, n=t_name, c=target_clip: self._set_transition(c, n))
            
            clear_trans = menu.addAction("Clear Transition")
            clear_trans.triggered.connect(lambda: self._set_transition(target_clip, None))
        
        # Always offer text clip addition
        add_text = menu.addAction("Add Text / Lower Third Here")
        add_text.triggered.connect(lambda: self._add_text_at(click_time))
        
        menu.exec(event.globalPos())

    def _set_transition(self, clip, transition_name):
        if transition_name:
            clip.transition_type = transition_name
            clip.transition_duration = 1.0
        else:
            clip.transition_type = None
            clip.transition_duration = 0
        self.update()

    def _add_text_at(self, time_pos):
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Add Text Overlay", "Enter text:")
        if ok and text.strip():
            dur, ok2 = QInputDialog.getDouble(self, "Duration", "Duration (seconds):", 5.0, 0.5, 300.0, 1)
            if ok2:
                tc = TextClip(text.strip(), time_pos, dur)
                self.text_clips.append(tc)
                self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Left:
            self.set_playhead_position(max(0, self.playhead_position - 1.0))
        elif event.key() == Qt.Key.Key_Right:
            self.set_playhead_position(self.playhead_position + 1.0)
        elif event.key() == Qt.Key.Key_Home:
            self.set_playhead_position(0)
        elif event.key() == Qt.Key.Key_End:
            duration = self.get_timeline_duration()
            self.set_playhead_position(duration)
        elif event.key() == Qt.Key.Key_PageDown:
            scroll_amount = self.width() / self.zoom_level
            self.scroll_offset += scroll_amount
            self.update()
        elif event.key() == Qt.Key.Key_PageUp:
            scroll_amount = self.width() / self.zoom_level
            self.scroll_offset = max(0, self.scroll_offset - scroll_amount)
            self.update()
        else:
            super().keyPressEvent(event)

    def add_clip(self, clip):
        self.clips.append(clip)
        worker = WaveformWorker(clip.file_path)
        worker.finished.connect(self.waveform_ready)
        self.waveform_threads.append(worker)
        worker.start()
        self.update()

    def waveform_ready(self, file_path, image):
        pixmap = QPixmap.fromImage(image)
        for clip in self.clips:
            if clip.file_path == file_path:
                clip.waveform_pixmap = pixmap
        self.update()

    def remove_clip(self, clip):
        if clip in self.clips:
            self.clips.remove(clip)
            if self.selected_clip == clip:
                self.selected_clip = None
            self.update()

    def clear_timeline(self):
        self.clips.clear()
        self.selected_clip = None
        self.playhead_position = 0
        self.update()

    def zoom_in(self):
        self.zoom_level = min(50, self.zoom_level * 1.5)
        self.update()

    def zoom_out(self):
        self.zoom_level = max(1, self.zoom_level / 1.5)
        self.update()

    def get_timeline_duration(self):
        if not self.clips:
            return 0
        return max(clip.get_end_time() for clip in self.clips)


class MediaLibraryItem:
    def __init__(self, file_path):
        self.file_path = file_path
        self.name = Path(file_path).name
        self.duration = self.get_video_duration()
        self.in_point = 0
        self.out_point = self.duration

    def get_video_duration(self):
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.file_path], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return float(result.stdout.strip())
        except:
            return 60.0

    def get_trimmed_duration(self):
        return self.out_point - self.in_point


def _parse_ffmpeg_time(line):
    if "time=" not in line:
        return None
    try:
        time_str = line.split('time=')[1].split()[0].replace(',', '.')
        parts = time_str.split(':')
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
            return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        pass
    return None

def auto_sync_audio(video_file, track1=0, track2=1, sample_duration=30, progress_callback=None):
    import subprocess
    import tempfile
    import os

    def log(msg):
        if progress_callback:
            progress_callback(msg)

    log("Extracting audio tracks...")

    probe_cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'a',
        '-show_entries', 'stream=index',
        '-of', 'csv=p=0',
        video_file
    ]

    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        audio_tracks = [int(x) for x in result.stdout.strip().split('') if x]

        if track1 >= len(audio_tracks) or track2 >= len(audio_tracks):
            raise Exception(f"File has {len(audio_tracks)} audio tracks, cannot access track {max(track1, track2)}")

        if len(audio_tracks) < 2:
            raise Exception(f"File only has {len(audio_tracks)} audio track(s), need at least 2 for sync")

    except Exception as e:
        raise Exception(f"Failed to probe audio tracks: {e}")

    with tempfile.NamedTemporaryFile(suffix='.raw', delete=False) as tmp1, \
         tempfile.NamedTemporaryFile(suffix='.raw', delete=False) as tmp2:

        tmp1_path = tmp1.name
        tmp2_path = tmp2.name

    try:
        sample_rate = 16000

        log(f"Extracting track {track1} (reference)...")
        extract1_cmd = [
            'ffmpeg', '-y', '-v', 'error',
            '-i', video_file,
            '-map', f'0:a:{track1}',
            '-t', str(sample_duration),
            '-ac', '1',
            '-ar', str(sample_rate),
            '-f', 's16le',
            tmp1_path
        ]

        result = subprocess.run(extract1_cmd, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode != 0:
            raise Exception(f"Failed to extract track {track1}: {result.stderr.decode()}")

        log(f"Extracting track {track2} (to sync)...")
        extract2_cmd = [
            'ffmpeg', '-y', '-v', 'error',
            '-i', video_file,
            '-map', f'0:a:{track2}',
            '-t', str(sample_duration),
            '-ac', '1',
            '-ar', str(sample_rate),
            '-f', 's16le',
            tmp2_path
        ]

        result = subprocess.run(extract2_cmd, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode != 0:
            raise Exception(f"Failed to extract track {track2}: {result.stderr.decode()}")

        size1 = os.path.getsize(tmp1_path)
        size2 = os.path.getsize(tmp2_path)

        if size1 < 1000 or size2 < 1000:
            raise Exception("Extracted audio too short, check file has audio on both tracks")

        log("Analyzing correlation...")
        import numpy as np

        audio1 = np.fromfile(tmp1_path, dtype=np.int16)
        audio2 = np.fromfile(tmp2_path, dtype=np.int16)

        audio1 = audio1.astype(np.float32) / 32768.0
        audio2 = audio2.astype(np.float32) / 32768.0

        try:
            from scipy import signal
            log("Using SciPy correlation (fast)...")
            correlation = signal.correlate(audio1, audio2, mode='full', method='fft')
        except ImportError:
            log("Using NumPy correlation (slower)...")
            correlation = np.correlate(audio1, audio2, mode='full')

        peak_index = np.argmax(correlation)
        lag = peak_index - len(audio2) + 1
        offset_ms = int((lag / sample_rate) * 1000)

        max_corr = correlation[peak_index]
        energy1 = np.sum(audio1 ** 2)
        energy2 = np.sum(audio2 ** 2)

        if energy1 > 0 and energy2 > 0:
            confidence = abs(max_corr) / np.sqrt(energy1 * energy2)
            confidence = min(1.0, confidence)
        else:
            confidence = 0.0

        log(f"Analysis complete! Offset: {offset_ms}ms, Confidence: {confidence:.1%}")

        return offset_ms, confidence

    finally:
        try: os.unlink(tmp1_path)
        except: pass
        try: os.unlink(tmp2_path)
        except: pass

import hashlib

class ProxyWorker(QThread):
    finished = pyqtSignal(str, str, bool)
    progress = pyqtSignal(str, int)  # original_path, percent 0-100
    log = pyqtSignal(str)

    def __init__(self, original_path, proxy_path):
        super().__init__()
        self.original_path = original_path
        self.proxy_path = proxy_path
        self.should_stop = False
        self._process = None

    def stop(self):
        self.should_stop = True
        if self._process:
            try:
                self._process.kill()
            except:
                pass

    def _get_duration(self, file_path):
        """Get duration via ffprobe for progress % calculation"""
        try:
            cmd = [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                file_path
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            return float(result.stdout.strip() or 0)
        except:
            return 0

    def run(self):
        # Extremely fast proxy generation (720p intra/IPB hybrid layout with full audio pass-through)
        # Now with real-time progress parsing for accessibility
        duration = self._get_duration(self.original_path)
        if duration <= 0:
            # Fallback: try stream duration
            try:
                cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                       '-show_entries', 'stream=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
                       self.original_path]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                duration = float(result.stdout.strip().splitlines()[0] or 0) if result.stdout.strip() else 0
            except:
                duration = 0

        cmd = [
            'ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.25',
            '-i', self.original_path,
            '-map', '0:v?', '-map', '0:a?',
            '-vf', 'scale=-2:720',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
            '-c:a', 'copy',
            self.proxy_path
        ]
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                universal_newlines=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            # Emit 0% at start
            self.progress.emit(self.original_path, 0)

            for line in iter(self._process.stderr.readline, ''):
                if self.should_stop:
                    self._process.kill()
                    self.finished.emit(self.original_path, self.proxy_path, False)
                    return

                t = _parse_ffmpeg_time(line)
                if t is not None and duration > 0:
                    pct = int((t / duration) * 100)
                    pct = max(0, min(99, pct))
                    self.progress.emit(self.original_path, pct)

            self._process.wait()
            success = self._process.returncode == 0
            if success:
                self.progress.emit(self.original_path, 100)
            self.finished.emit(self.original_path, self.proxy_path, success)
        except Exception as e:
            self.log.emit(f"Proxy error {self.original_path}: {e}")
            self.finished.emit(self.original_path, self.proxy_path, False)
        finally:
            self._process = None


class ProxyManager(QObject):
    status_update = pyqtSignal(str)
    file_progress = pyqtSignal(str, int)  # file_path, percent
    overall_progress = pyqtSignal(int, int, int)  # current_file_pct, completed, total_remaining
    
    def __init__(self):
        super().__init__()
        self.queue = []
        self.active_worker = None
        self.proxy_dir = os.path.join(tempfile.gettempdir(), 'FastEncodeProxies')
        try:
            os.makedirs(self.proxy_dir, exist_ok=True)
        except:
            pass
        self.proxy_map = {}
        self.current_file = None
        self.current_file_progress = 0
        self.total_jobs_initial = 0

    def add_job(self, file_path):
        if not os.path.exists(file_path):
            return
        proxy_name = hashlib.md5(file_path.encode('utf-8')).hexdigest() + ".mkv"
        proxy_path = os.path.join(self.proxy_dir, proxy_name)
        
        if os.path.exists(proxy_path):
            self.proxy_map[file_path] = proxy_path
            return
            
        if self.current_file == file_path:
            return
            
        if any(job[0] == file_path for job in self.queue):
            return
            
        self.queue.append((file_path, proxy_path))
        self.total_jobs_initial = len(self.queue) + (1 if self.active_worker else 0)
        self.process_queue()

    def process_queue(self):
        if self.active_worker and self.active_worker.isRunning():
            return
        if not self.queue:
            self.current_file = None
            self.current_file_progress = 0
            self.total_jobs_initial = 0
            self.status_update.emit("Proxies: Up to date âœ“")
            self.file_progress.emit("", 100)
            self.overall_progress.emit(100, 0, 0)
            return
            
        orig, proxy = self.queue.pop(0)
        self.current_file = orig
        self.current_file_progress = 0
        basename = os.path.basename(orig)
        short_name = (basename[:25] + "...") if len(basename) > 28 else basename
        remaining = len(self.queue) + 1
        completed = max(0, self.total_jobs_initial - remaining)
        self.status_update.emit(f"Proxies: {short_name} â€” 0% ({remaining} left)")
        self.file_progress.emit(orig, 0)
        self.overall_progress.emit(0, completed, remaining)

        self.active_worker = ProxyWorker(orig, proxy)
        self.active_worker.progress.connect(self.on_file_progress)
        self.active_worker.finished.connect(self.on_worker_finished)
        self.active_worker.start()

    def on_file_progress(self, orig, pct):
        self.current_file_progress = pct
        basename = os.path.basename(orig)
        short_name = (basename[:25] + "...") if len(basename) > 28 else basename
        remaining = len(self.queue) + 1
        completed = max(0, self.total_jobs_initial - remaining)
        self.status_update.emit(f"Proxies: {short_name} â€” {pct}% ({remaining} left)")
        self.file_progress.emit(orig, pct)
        self.overall_progress.emit(pct, completed, remaining)

    def on_worker_finished(self, orig, proxy, success):
        if success:
            self.proxy_map[orig] = proxy
        try:
            if self.active_worker:
                self.active_worker.deleteLater()
        except:
            pass
        self.active_worker = None
        self.current_file = None
        self.process_queue()

    def get_proxy(self, file_path):
        p = self.proxy_map.get(file_path)
        if p and os.path.exists(p):
            return p
        return file_path
    
    def stop_all(self):
        """Accessibility: allow cancelling proxy generation"""
        self.queue.clear()
        if self.active_worker:
            self.active_worker.stop()
            self.active_worker.wait(1000)
        self.current_file = None
        self.status_update.emit("Proxies: Cancelled")

    def clear_all_proxies(self):
        """Delete all proxy files from temp dir â€” for user requested cleanup"""
        self.stop_all()
        deleted = 0
        try:
            if os.path.exists(self.proxy_dir):
                for fname in os.listdir(self.proxy_dir):
                    fpath = os.path.join(self.proxy_dir, fname)
                    try:
                        if os.path.isfile(fpath):
                            os.unlink(fpath)
                            deleted += 1
                    except:
                        pass
        except Exception as e:
            print(f"Clear proxies error: {e}")
        self.proxy_map.clear()
        self.queue.clear()
        self.total_jobs_initial = 0
        self.current_file = None
        self.status_update.emit(f"Proxies: Cleared {deleted} files âœ“")
        self.file_progress.emit("", 100)
        return deleted

    def get_proxy_dir(self):
        return self.proxy_dir

    def get_proxy_disk_usage(self):
        """Return count and size in MB"""
        try:
            if not os.path.exists(self.proxy_dir):
                return 0, 0.0
            files = [os.path.join(self.proxy_dir, f) for f in os.listdir(self.proxy_dir) if os.path.isfile(os.path.join(self.proxy_dir, f))]
            total = sum(os.path.getsize(f) for f in files)
            return len(files), total / (1024*1024)
        except:
            return 0, 0.0
    
class TimelineRenderingEngine:
    """
    MASTER CANVAS COMPOSITOR ENGINE (v0.9.4e)
    This entirely replaces the Python-pipe transcoder with a true NLE FFmpeg graph.
    All clips are overlaid onto a blank hardware canvas natively.
    No temp files. No System RAM bottlenecks. 100% GPU utilization.
    """
    def __init__(self, timeline, settings, output_path,
                 log_callback, progress_callback, status_callback, playhead_callback=None):
        self.timeline = timeline
        self.settings = settings
        self.output_path = output_path
        self.log = log_callback
        self.progress = progress_callback
        self.status = status_callback
        self.playhead = playhead_callback
        self.should_stop = False
        self.encoder_process = None

    def stop(self):
        self.should_stop = True
        if self.encoder_process:
            try:
                self.encoder_process.kill()
            except:
                pass

    def get_timeline_duration(self):
        if not self.timeline.clips:
            return 0
        return max(clip.get_end_time() for clip in self.timeline.clips)

    def get_video_metadata(self, file_path):
        try:
            cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries', 'stream=width,height,codec_name', '-of', 'json', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            data = json.loads(result.stdout)
            stream = data['streams'][0]
            return stream['width'], stream['height']
        except:
            return 1920, 1080

    def _get_video_codec(self, file_path):
        try:
            cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries', 'stream=codec_name', '-of', 'json', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            data = json.loads(result.stdout)
            return data['streams'][0]['codec_name']
        except:
            return 'unknown'

    def _build_video_filters(self):
        return build_video_filters_from_settings(self.settings)

    def render(self):
        try:
            self.log("=== HIGH-PERFORMANCE MASTER CANVAS ENGINE v0.9.4e ===")
            self.log("Compiling Timeline NLE Graph...")

            if not self.timeline.clips:
                return False, "No clips on timeline"

            timeline_duration = self.get_timeline_duration()
            timeline_fps = self.settings.get('timeline_fps', 60.0)
            sorted_clips = sorted(self.timeline.clips, key=lambda c: c.start_time)

            source_width, source_height = self.get_video_metadata(sorted_clips[0].file_path)
            export_res_index = self.settings.get('export_res_index', 0)
            if export_res_index == 0:
                export_width, export_height = source_width, source_height
            else:
                res_map = {1: (1920, 1080), 2: (2560, 1440), 3: (3840, 2160), 4: (5120, 2880), 5: (7680, 4320)}
                export_width, export_height = res_map[export_res_index]

            # ENSURE EVEN DIMENSIONS (Prevents NVENC padding crash)
            if export_width % 2 != 0: export_width -= 1
            if export_height % 2 != 0: export_height -= 1

            self.log(f"Resolution: {export_width}x{export_height} @ {timeline_fps} FPS")
            self.log(f"Total Duration: {timeline_duration:.2f}s")

            # FIX v0.9.4: Removed dead cache branch (is_cache_render/valid_cache_path never set - vestigial per Claude)
            # Was reintroducing background-cache path that never fired - removed to fix insta-crash
            use_gpu_decode = self.settings.get('use_gpu_decode', False)
            use_gpu_composite = self.settings.get('use_gpu_composite', False)
            video_codec = self.settings.get('video_codec', 'hevc_nvenc')
            is_nvenc = 'nvenc' in video_codec
                
            if use_gpu_composite and use_gpu_decode and is_nvenc:
                self.log("🚀 5070 TURBO MODE ACTIVE - Full GPU Canvas (VRAM only)")
                self.log("   No CPU copy: scale_cuda + overlay_cuda + NVENC - should max out GPU like DaVinci")
                cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5']
                import multiprocessing
                cpu_cores = multiprocessing.cpu_count() or 8
                cmd.extend(['-filter_threads', str(cpu_cores), '-filter_complex_threads', str(cpu_cores)])
                cmd.extend(['-extra_hw_frames', '16'])
                cmd.extend(['-threads', '0'])
                # 1. ADD INPUTS - keep in GPU VRAM
                for clip in sorted_clips:
                    cmd.extend(['-ss', str(clip.in_point)])
                    cmd.extend(['-t', str(clip.get_trimmed_duration())])
                    cmd.extend(['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda', '-i', clip.file_path])
            else:
                self.log("🚀 Hardware Decode + Robust CPU Compositing Pipeline ACTIVE.")
                if use_gpu_composite and not is_nvenc:
                    self.log("   (Turbo requested but codec not NVENC - falling back to CPU canvas)")
                else:
                    self.log("   (CPU scale/overlay - safe path, enable TURBO for 5070)")
                cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5']
                import multiprocessing
                cpu_cores = multiprocessing.cpu_count() or 8
                cmd.extend(['-filter_threads', str(cpu_cores), '-filter_complex_threads', str(cpu_cores)])
                cmd.extend(['-extra_hw_frames', '64'])
                cmd.extend(['-threads', '0'])
                # 1. ADD INPUTS - big queue per input = CPU pre-decodes ahead
                for clip in sorted_clips:
                    codec = self._get_video_codec(clip.file_path)
                    cmd.extend(['-ss', str(clip.in_point)])
                    cmd.extend(['-t', str(clip.get_trimmed_duration())])
                    cmd.extend(build_hw_decode_input_args(clip.file_path, codec, use_gpu_decode, timeline_fps))

            # 2. BUILD THE COMPOSITING GRAPH
            filter_complex = []

            # Create the master blank canvas at exact output specs
            is_10bit = self.settings.get('pixel_format', 1) == 1
            if self.settings.get('video_codec', '') == 'h264_nvenc':
                is_10bit = False
                
            # Use robust p010le format for 10-bit HDR exports. CPU filters support it flawlessly.
            canvas_format = 'p010le' if is_10bit else 'nv12'
            canvas_format_cuda = 'p010' if is_10bit else 'nv12'
                
            # Detect early if filters will be used - for Hybrid mode to avoid overlay_cuda p010le bug in FFmpeg 9.01
            early_filters_check = self._build_video_filters()
            early_text_check = getattr(self.timeline, 'text_clips', [])
            early_is_turbo_check = self.settings.get('use_gpu_composite', False) and self.settings.get('use_gpu_decode', False) and 'nvenc' in self.settings.get('video_codec','')
            early_hybrid = early_is_turbo_check and bool(early_filters_check or early_text_check)
            # FFmpeg 9.01 overlay_cuda does NOT support p010 main input at all (even full TURBO), so 10-bit must always use CPU overlay
            force_cpu_overlay_for_10bit = is_10bit and early_is_turbo_check
            if early_hybrid:
                self.log(f"TURBO Hybrid: {len(early_filters_check)} filters + {len(early_text_check)} text - GPU scale, CPU overlay to avoid overlay_cuda p010le error. Filters: {','.join(early_filters_check)[:80]}")
            if force_cpu_overlay_for_10bit and not early_hybrid:
                self.log(f"TURBO 10-bit full VRAM workaround: FFmpeg 9.01 overlay_cuda doesn't support p010, using GPU scale -> CPU overlay for all 10-bit")
                
            # Branch canvas based on turbo mode
            if self.settings.get('use_gpu_composite', False) and self.settings.get('use_gpu_decode', False) and 'nvenc' in self.settings.get('video_codec',''):
                if (early_hybrid and is_10bit) or (force_cpu_overlay_for_10bit):
                    # 10-bit: keep canvas on CPU to avoid overlay_cuda p010le unsupported (both Hybrid and full TURBO)
                    filter_complex.append(f"color=c=black:s={export_width}x{export_height}:r={timeline_fps}:d={timeline_duration},format={canvas_format}[bg0]")
                else:
                    # Full TURBO 8-bit: GPU canvas stays in VRAM (overlay_cuda supports nv12)
                    filter_complex.append(f"color=c=black:s={export_width}x{export_height}:r={timeline_fps}:d={timeline_duration},format={canvas_format},hwupload_cuda,scale_cuda=format={canvas_format_cuda}[bg0]")
            else:
                # Always use standard CPU canvas
                filter_complex.append(f"color=c=black:s={export_width}x{export_height}:r={timeline_fps}:d={timeline_duration},format={canvas_format}[bg0]")

            audio_inputs = []

            for i, clip in enumerate(sorted_clips):
                # --- VIDEO GRAPH ---
                v_in = f"[{i}:v]"
                v_scaled = f"[v{i}_scale]"

                st = clip.start_time
                duration = clip.get_trimmed_duration()
                end_time = clip.start_time + duration
                bg_in = f"[bg{i}]"
                bg_out = f"[bg{i+1}]"

                v_trimmed = f"[v{i}_trim]"
                filter_complex.append(
                    f"{v_in}trim=start=0:end={duration},"
                    f"setpts=PTS-STARTPTS+{st:.6f}/TB{v_trimmed}"
                )
                # --- NATIVE RES SKIP + GPU TURBO LOGIC ---
                try:
                    clip_w, clip_h = self.get_video_metadata(clip.file_path)
                except:
                    clip_w, clip_h = export_width, export_height
                # FIX v0.9.4e: is_turbo must check actual use_gpu_decode after forcing, not settings - fixes scale_cuda on CPU input crash -40
                is_turbo = use_gpu_composite and use_gpu_decode and is_nvenc
                # For 10-bit, always use CPU overlay to avoid FFmpeg 9.01 overlay_cuda p010 bug (all resolutions)
                use_cpu_overlay = early_hybrid or (is_10bit and is_turbo)
                if clip_w == export_width and clip_h == export_height:
                    if is_turbo and not use_cpu_overlay:
                        scale_str = f"scale_cuda=format={canvas_format_cuda},setsar=1"
                        self.log(f"Clip {i}: Native {clip_w}x{clip_h} == export -> TURBO SKIPPING scale (VRAM) 8-bit")
                    elif is_turbo and use_cpu_overlay:
                        # FIX v0.9.4g: cuda input -> p010le needs hwdownload, was missing causing -40
                        # FIX v0.9.4h: hwdownload Invalid output format p010le - need scale_cuda=format=p010 first
                        scale_str = f"scale_cuda=format=p010,hwdownload,format={canvas_format},setsar=1"
                        self.log(f"Clip {i}: Native {clip_w}x{clip_h} == export -> {'HYBRID' if early_hybrid else '10-bit TURBO'} GPU->CPU via scale_cuda+hwdownload - {export_width}x{export_height}")
                    else:
                        scale_str = f"format={canvas_format},setsar=1"
                        self.log(f"Clip {i}: Native {clip_w}x{clip_h} == export {export_width}x{export_height} -> SKIPPING scale/pad")
                else:
                    if is_turbo and not use_cpu_overlay:
                        # GPU upscale 1080p->5K etc - 8-bit full TURBO only
                        scale_str = f"scale_cuda={export_width}:{export_height}:force_original_aspect_ratio=decrease:format={canvas_format_cuda}"
                        self.log(f"Clip {i}: {clip_w}x{clip_h} -> {export_width}x{export_height} TURBO GPU scaling 8-bit")
                    elif is_turbo and use_cpu_overlay:
                        # FIX v0.9.4g: cuda input needs hwdownload before scale, was -40
                        # FIX v0.9.4h: Need scale_cuda before hwdownload for p010le
                        scale_str = f"scale_cuda={export_width}:{export_height}:force_original_aspect_ratio=decrease:format=p010,hwdownload,format={canvas_format},pad={export_width}:{export_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
                        self.log(f"Clip {i}: {clip_w}x{clip_h} -> {export_width}x{export_height} {'HYBRID' if early_hybrid else '10-bit TURBO'} GPU->CPU scaling via scale_cuda+hwdownload")
                    else:
                        scale_str = f"scale={export_width}:{export_height}:force_original_aspect_ratio=decrease,format={canvas_format},pad={export_width}:{export_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
                        self.log(f"Clip {i}: {clip_w}x{clip_h} -> {export_width}x{export_height} CPU scaling")
                filter_complex.append(f"{v_trimmed}{scale_str}{v_scaled}")
                if is_turbo and not use_cpu_overlay:
                    filter_complex.append(f"{bg_in}{v_scaled}overlay_cuda=enable='between(t,{clip.start_time},{end_time})':eof_action=pass{bg_out}")
                else:
                    filter_complex.append(f"{bg_in}{v_scaled}overlay=enable='between(t,{clip.start_time},{end_time})':eof_action=pass{bg_out}")

                # --- AUDIO GRAPH --- (Fix :a:1 matches no streams)
                is_turbo_audio = use_gpu_composite and use_gpu_decode and is_nvenc
                if is_turbo_audio:
                    # TURBO: use only first audio stream to avoid 4294967274 error
                    a_in = f"[{i}:a:0]"
                    a_trimmed = f"[a{i}_0_trim]"
                    duration = clip.get_trimmed_duration()
                    filter_complex.append(f"{a_in}atrim=start=0:end={duration},asetpts=PTS-STARTPTS{a_trimmed}")
                    base_delay_ms = int(clip.start_time * 1000)
                    a_ready = f"[a{i}_0_ready]"
                    chain = ""
                    if base_delay_ms > 0:
                        chain += f"adelay={base_delay_ms}|{base_delay_ms},"
                    vol_db = clip.volumes[0] if clip.volumes else 0.0
                    chain += f"volume={vol_db}dB"
                    filter_complex.append(f"{a_trimmed}{chain}{a_ready}")
                    audio_inputs.append(a_ready)
                else:
                    n_streams = clip.audio_streams
                    if n_streams > 2:
                        n_streams = 1
                    for a_idx in range(n_streams):
                        a_in = f"[{i}:a:{a_idx}]"
                        a_trimmed = f"[a{i}_{a_idx}_trim]"
                        duration = clip.get_trimmed_duration()
                        filter_complex.append(f"{a_in}atrim=start=0:end={duration},asetpts=PTS-STARTPTS{a_trimmed}")
                        base_delay_ms = int(clip.start_time * 1000)
                        sync_offset = clip.sync_offset if hasattr(clip, 'sync_offset') else 0
                        if n_streams > 1 and sync_offset != 0:
                            if sync_offset > 0 and a_idx == 0:
                                base_delay_ms += sync_offset
                            elif sync_offset < 0 and a_idx == 1:
                                base_delay_ms += abs(sync_offset)
                        vol_db = clip.volumes[a_idx] if a_idx < len(clip.volumes) else 0.0
                        norm = clip.normalization[a_idx] if a_idx < len(clip.normalization) else False
                        a_ready = f"[a{i}_{a_idx}_ready]"
                        chain = ""
                        if base_delay_ms > 0:
                            chain += f"adelay={base_delay_ms}|{base_delay_ms},"
                        chain += f"volume={vol_db}dB"
                        if norm:
                            chain += ",loudnorm"
                        filter_complex.append(f"{a_trimmed}{chain}{a_ready}")
                        audio_inputs.append(a_ready)

            # --- FINAL OUTPUT MAPPING ---
            last_v = f"[bg{len(sorted_clips)}]"

            user_filters = self._build_video_filters()
            text_clips = getattr(self.timeline, 'text_clips', [])
            is_turbo_final = self.settings.get('use_gpu_composite', False) and self.settings.get('use_gpu_decode', False) and 'nvenc' in self.settings.get('video_codec','')
            cpu_bottleneck_active = bool(user_filters or text_clips)
                
            # Option 2: TURBO + any filter (denoise, color grading, transitions, text) -> Hybrid mode
            # GPU does scale_cuda + overlay_cuda (heavy 5K work), then download to CPU for filters
            # This avoids hwupload_cuda -> auto_scale crash with -pix_fmt p010le
            if cpu_bottleneck_active:
                if is_turbo_final:
                    # If already in Hybrid (early_hybrid), last_v is already CPU from overlay fix, don't hwdownload again
                    try:
                        already_cpu = early_hybrid
                    except NameError:
                        already_cpu = False
                    if already_cpu:
                        self.log(f"HYBRID: last_v already CPU (overlay fix), skipping extra hwdownload")
                    else:
                        filter_desc = ", ".join(user_filters)[:120] if user_filters else "text overlay"
                        self.log(f"TURBO: CPU filters detected ({filter_desc}) - switching to HYBRID mode: GPU scale/overlay (5070 maxed) + CPU filters + CPU encode. This only happens when filters are applied. Without filters, stays full VRAM.")
                        filter_complex.append(f"{last_v}hwdownload,format={canvas_format}[bg_downloaded]")
                        last_v = "[bg_downloaded]"
                elif use_gpu_decode:
                    filter_complex.append(f"{last_v}format={canvas_format}[bg_downloaded]")
                    last_v = "[bg_downloaded]"

            if user_filters:
                if is_10bit and any('hqdn3d' in f for f in user_filters):
                    filter_complex.append(f"{last_v}format=nv12,{','.join(user_filters)},format={canvas_format}[v_filtered]")
                else:
                    filter_complex.append(f"{last_v}{','.join(user_filters)}[v_filtered]")
                # In Hybrid mode (turbo+filters), stay on CPU after filters - don't re-upload
                # This prevents: hwupload_cuda -> auto_scale -> -40 crash with -pix_fmt p010le
                if is_turbo_final:
                    self.log("HYBRID: Keeping frames on CPU after filters for encode (avoids CUDA->CPU auto_scale)")
                    pre_fps_v = "[v_filtered]"
                else:
                    pre_fps_v = "[v_filtered]"
            else:
                pre_fps_v = last_v

            # --- TEXT OVERLAYS (drawtext) ---
            text_clips = getattr(self.timeline, 'text_clips', [])
            if text_clips:
                txt_input = pre_fps_v
                for ti, tc in enumerate(text_clips):
                    txt_out = f"[txt{ti}]"
                    safe_text = tc.text.replace("'", "\\\\'").replace(":", "\\\\:")
                    dt_filter = (
                        f"{txt_input}drawtext=text='{safe_text}'"
                        f":fontsize={tc.font_size}:fontcolor={tc.font_color}"
                        f":x={tc.x}:y={tc.y}"
                        f":enable='between(t,{tc.start_time},{tc.get_end_time()})'"
                        f"{txt_out}"
                    )
                    filter_complex.append(dt_filter)
                    txt_input = txt_out
                pre_fps_v = txt_input

            # Hybrid mode: turbo+filters -> CPU path, needs fps filter on CPU
            # zero_copy_video tracks if map_v stays in VRAM (full TURBO) or CPU (Hybrid/CPU)
            # Determine if 10-bit TURBO must use CPU overlay (FFmpeg 9.01 bug)
            is_10bit_final = self.settings.get('pixel_format', 1) == 1
            if self.settings.get('video_codec', '') == 'h264_nvenc':
                is_10bit_final = False
            force_cpu_for_10bit_final = is_10bit_final and is_turbo_final
                
            if is_turbo_final and user_filters:
                # pre_fps_v is already CPU after hwdownload, add fps here on CPU
                filter_complex.append(f"{pre_fps_v}fps={timeline_fps}[out_v]")
                map_v = "[out_v]"
                zero_copy_video = False  # Hybrid = CPU map, needs pix_fmt
            else:
                zero_copy_video = use_gpu_decode and not cpu_bottleneck_active
                # For 10-bit full TURBO, we forced CPU overlay, so map is CPU, not cuda
                if force_cpu_for_10bit_final:
                    zero_copy_video = False
                if zero_copy_video:
                    map_v = pre_fps_v  # Full TURBO 8-bit = cuda map, NO pix_fmt
                else:
                    filter_complex.append(f"{pre_fps_v}fps={timeline_fps}[out_v]")
                    map_v = "[out_v]"
                    zero_copy_video = False  # CPU path needs pix_fmt

            # --- VOICEOVER AUDIO CLIPS ---
            vo_clips = getattr(self.timeline, 'audio_clips', [])
            vo_input_offset = len(sorted_clips)
            for vi, vo in enumerate(vo_clips):
                vo_idx = vo_input_offset + vi
                vo_in = f"[{vo_idx}:a]"
                vo_ready = f"[vo{vi}_ready]"
                delay_ms = int(vo.start_time * 1000)
                chain = ""
                if delay_ms > 0:
                    chain = f"adelay={delay_ms}|{delay_ms},"
                chain += "volume=0dB"
                filter_complex.append(f"{vo_in}{chain}{vo_ready}")
                audio_inputs.append(vo_ready)

            # Mix Audio
            if audio_inputs:
                inputs_str = "".join(audio_inputs)
                filter_complex.append(f"{inputs_str}amix=inputs={len(audio_inputs)}:duration=longest:dropout_transition=0[out_a]")
                map_a = "[out_a]"
            else:
                # Generate silent audio track if completely muted
                filter_complex.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={timeline_duration}[out_a]")
                map_a = "[out_a]"

            # Add voiceover files as additional inputs BEFORE filter_complex arg
            for vo in vo_clips:
                cmd.extend(['-i', vo.file_path])

            cmd.extend(['-filter_complex', ';'.join(filter_complex)])
            cmd.extend(['-map', map_v, '-map', map_a])
            # Removed redundant -r on output for zero-copy. The color canvas natively enforces perfect PTS.

            # 3. ENCODER SETTINGS
            # FIX v0.9.4: Removed dead is_cache_render branch (never set, vestigial)
            codec = self.settings.get('video_codec', 'hevc_nvenc')
            cmd.extend(['-r', str(timeline_fps)])
            cmd.extend(['-c:v', codec])
            if 'nvenc' in codec:
                cmd.extend(build_nvenc_cbr_args(self.settings, timeline_fps, is_zero_copy=zero_copy_video))
            cmd.extend(['-c:a', 'aac', '-b:a', '320k'])
            cmd.extend(['-t', f"{timeline_duration:.6f}"])
            append_output_file_args(cmd, self.output_path, self.settings, self.log)

            self.log(f"Compositing execution started...")

            start_time = time.time()
            self.encoder_process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )

            # 4. MONITOR PROGRESS
            finalizing_logged = False
            error_log = []
            for line in iter(self.encoder_process.stderr.readline, ''):
                error_log.append(line)
                if self.should_stop:
                    self.encoder_process.kill()
                    return False, "Render cancelled by user"

                line_lower = line.lower()
                if "starting second pass" in line_lower or "moving the moov atom" in line_lower:
                    if not finalizing_logged:
                        finalizing_logged = True
                        self.progress(99)
                        self.status("Finalizing output file metadata...")
                        self.log("Finalizing MP4/MOV metadata. Large files can sit here for a while.")
                    continue

                t = _parse_ffmpeg_time(line)
                if t is not None and timeline_duration > 0:
                    raw_pct = int((t / timeline_duration) * 100)
                    pct = min(99, raw_pct)
                    self.progress(pct)
                    elapsed = time.time() - start_time
                    fps_actual = (t * timeline_fps) / elapsed if elapsed > 0 else 0
                    if raw_pct >= 99:
                        if not finalizing_logged:
                            finalizing_logged = True
                            self.log("Encode reached the end of the timeline; waiting for FFmpeg to close the output file.")
                        self.status("Finalizing output file...")
                    else:
                        self.status(f"Rendering: {pct}% â€” {fps_actual:.1f} fps")
                    if self.playhead:
                        self.playhead(t)

            self.encoder_process.wait()

            if self.encoder_process.returncode != 0:
                crash_log_path = os.path.join(tempfile.gettempdir(), 'ffmpeg_crash.log')
                try:
                    with open(crash_log_path, 'w', encoding='utf-8') as f:
                        f.write("Command:\n" + " ".join(cmd) + "\n\nStderr:\n")
                        f.writelines(error_log)
                except Exception:
                    pass
                return False, f"Export failed with code {self.encoder_process.returncode}"

            elapsed = time.time() - start_time
            self.progress(100)
            return True, f"Render Complete! {elapsed:.1f}s"

        except Exception as e:
            import traceback
            self.log(f"Critical Error: {e}")
            self.log(traceback.format_exc())
            return False, str(e)
        finally:
            self.stop()


class TimelineExportThread(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    log_message = pyqtSignal(str)
    finished = pyqtSignal(bool, str)
    playhead_update = pyqtSignal(float)

    def __init__(self, timeline, output_path, settings):
        super().__init__()
        self.timeline = timeline
        self.output_path = output_path
        self.settings = settings
        self.engine = None

    def run(self):
        self.engine = TimelineRenderingEngine(self.timeline, self.settings, self.output_path,
            log_callback=self._log_immediate, progress_callback=self.progress.emit,
            status_callback=self.status.emit, playhead_callback=self.playhead_update.emit)
        success, message = self.engine.render()
        self.finished.emit(success, message)

    def _log_immediate(self, message):
        self.log_message.emit(message)

    def stop(self):
        if self.engine: self.engine.stop()


class EncodingThread(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    log_message = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, input_file, output_file, settings):
        super().__init__()
        self.input_file = input_file
        self.output_file = output_file
        self.settings = settings
        self.process = None
        self.should_stop = False

    def run(self):
        try:
            cmd = self.build_ffmpeg_command()
            self.log_message.emit(f"Command: {' '.join(cmd)}")
            self.status.emit("Starting encode...")
            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True, bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            duration = self.get_duration()
            for line in iter(self.process.stderr.readline, ''):
                if self.should_stop:
                    self.process.kill()
                    self.finished.emit(False, "Stopped")
                    return
                self.log_message.emit(line.strip())
                if duration > 0:
                    current = _parse_ffmpeg_time(line)
                    if current is not None:
                        pct = int((current / duration) * 100)
                        self.progress.emit(min(pct, 99))
                        self.status.emit(f"Encoding: {pct}%")
            self.process.wait()
            if self.process.returncode == 0:
                self.progress.emit(100)
                self.status.emit("Done!")
                self.finished.emit(True, "Success")
            else:
                self.finished.emit(False, "Encode failed")
        except Exception as e:
            self.finished.emit(False, str(e))

    def build_ffmpeg_command(self):
        cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5']
        import multiprocessing as _mp2
        _c2 = _mp2.cpu_count() or 8
        # FIX v0.9.4: Removed thread_queue_size 1024 + extra_hw_frames 64
        cmd.extend(['-filter_threads', str(_c2), '-filter_complex_threads', str(_c2), '-threads', '0'])
        
        codec = self.settings.get('video_codec', '')
        is_copy_stream = (codec == 'copy')
        
        use_gpu_decode = self.settings.get('use_gpu_decode', False) if not is_copy_stream else False
        codec_name = None
        try:
            codec_name = self.settings.get('input_codec_name')
        except Exception:
            codec_name = None
        target_fps = self.settings.get('timeline_fps') if not is_copy_stream else None
        cmd.extend(build_hw_decode_input_args(self.input_file, codec_name, use_gpu_decode, target_fps))
        filter_complex = []

        if not is_copy_stream:
            denoise = self.settings.get('denoise_level', 0)
            if denoise > 0:
                denoise_values = ['', 'hqdn3d=1.5:1.5:6:6', 'hqdn3d=2:2:8:8', 'hqdn3d=3:3:10:10', 'hqdn3d=4:4:12:12', 'hqdn3d=6:6:15:15', 'hqdn3d=8:8:18:18']
                if denoise < len(denoise_values): filter_complex.append(denoise_values[denoise])
    
            deflicker = self.settings.get('deflicker_level', 0)
            if deflicker > 0:
                deflicker_values = ['', 'deflicker=mode=pm:size=5', 'deflicker=mode=pm:size=10', 'deflicker=mode=pm:size=15', 'deflicker=mode=am:size=20', 'deflicker=mode=am:size=30']
                if deflicker < len(deflicker_values): filter_complex.append(deflicker_values[deflicker])
    
            exposure = self.settings.get('exposure_level', 0)
            if exposure > 0:
                exposure_values = {
                    1: 'eq=brightness=0.05:saturation=1.1',
                    2: 'eq=brightness=0.1:saturation=1.15',
                    3: 'eq=brightness=0.15:saturation=1.2',
                    4: 'eq=brightness=0.2:saturation=1.25',
                    5: 'eq=brightness=0.3:saturation=1.3',
                    6: 'eq=brightness=0.4:saturation=1.35',
                    7: 'eq=brightness=-0.05:saturation=0.95',
                    8: 'eq=brightness=-0.1:saturation=0.9',
                    9: 'eq=brightness=-0.15:saturation=0.85',
                    10: 'eq=brightness=-0.2:saturation=0.8',
                    11: 'eq=brightness=-0.3:saturation=0.75',
                    12: 'eq=brightness=-0.4:saturation=0.7',
                }
                if exposure in exposure_values: filter_complex.append(exposure_values[exposure])
    
            temporal = self.settings.get('temporal_level', 0)
            if temporal > 0:
                temporal_values = ['',
                                   'tmix=frames=3:weights="1 1 1"',
                                   'tmix=frames=5:weights="1 1 2 1 1"',
                                   'tmix=frames=7:weights="1 1 2 2 2 1 1"',
                                   'tmix=frames=9:weights="1 1 2 3 3 3 2 1 1"',
                                   'tmix=frames=11:weights="1 2 2 3 4 4 4 3 2 2 1"']
                if temporal < len(temporal_values): filter_complex.append(temporal_values[temporal])
    
            sharpness = self.settings.get('sharpness_level', 0)
            if sharpness > 0:
                sharpness_values = ['',
                                    'unsharp=3:3:0.3:3:3:0',
                                    'unsharp=5:5:0.5:5:5:0',
                                    'unsharp=5:5:0.8:5:5:0.4',
                                    'unsharp=5:5:1.2:5:5:0.6',
                                    'unsharp=7:7:1.5:7:7:0.8',
                                    'unsharp=7:7:2.0:7:7:1.0']
                if sharpness < len(sharpness_values): filter_complex.append(sharpness_values[sharpness])

        if filter_complex: cmd.extend(['-vf', ','.join(filter_complex)])
        
        # New Remux Support
        if is_copy_stream:
            cmd.extend(['-c:v', 'copy'])
        else:
            cmd.extend(['-c:v', codec])
            if codec == 'prores_ks':
                profile = self.settings['prores_profile']
                target_bitrate_mbps = self.settings.get('bitrate_mbps', 500)
                qscale = 9 if target_bitrate_mbps >= 500 else 11 if target_bitrate_mbps >= 300 else 13 if target_bitrate_mbps >= 150 else 15
                cmd.extend(['-profile:v', str(profile), '-vendor', 'apl0', '-qscale:v', str(qscale)])
            elif 'nvenc' in codec:
                if self.settings['use_gpu']:
                    cmd.extend(build_nvenc_cbr_args(self.settings, 30))
                else: cmd.extend(['-preset', 'medium'])
                
        cmd.extend(['-c:a', self.settings['audio_codec']])
        if self.settings['audio_codec'] == 'aac': cmd.extend(['-b:a', '320k'])
        if self.settings['threads'] > 0: cmd.extend(['-threads', str(self.settings['threads'])])
        append_output_file_args(cmd, self.output_file, self.settings, self.log_message.emit)
        return cmd

    def get_duration(self):
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.input_file], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return float(result.stdout.strip())
        except:
            return 0

    def stop(self):
        self.should_stop = True
        if self.process:
            try: self.process.kill()
            except: pass



class ExportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Timeline Settings")
        self.setMinimumWidth(500)
        self.app = parent
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # --- TIMELINE SETTINGS ---
        timeline_group = QGroupBox("Timeline Settings")
        timeline_layout = QFormLayout()

        self.res_combo = QComboBox()
        self.res_combo.addItems([
            "Source (match input)",
            "1080p  (1920Ã—1080)",
            "1440p  (2560Ã—1440)",
            "4K     (3840Ã—2160)",
            "5K     (5120Ã—2880)",
            "8K     (7680Ã—4320)",
        ])
        self.res_combo.setCurrentIndex(0)
        timeline_layout.addRow("Resolution:", self.res_combo)

        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["23.976", "24", "25", "29.97", "30", "50", "59.94", "60", "120"])
        self.fps_combo.setCurrentIndex(self._default_fps_index())
        timeline_layout.addRow("Frame Rate:", self.fps_combo)

        timeline_group.setLayout(timeline_layout)
        layout.addWidget(timeline_group)

        # --- AUDIO ---
        audio_group = QGroupBox("Audio Format")
        audio_layout = QVBoxLayout()
        self.audio_combo = QComboBox()
        self.audio_combo.addItems(["PCM 24-bit", "PCM 16-bit", "AAC 320kbps", "Copy Stream"])
        audio_layout.addWidget(self.audio_combo)
        audio_group.setLayout(audio_layout)
        layout.addWidget(audio_group)

        # --- VIDEO FORMAT ---
        video_group = QGroupBox("Video Format")
        video_layout = QVBoxLayout()

        # Dynamic codec list based on detected hardware (NVIDIA/AMD/Intel)
        self.hw_caps = getattr(self.app, 'hw_caps', None) if self.app else None
        if not self.hw_caps:
            self.hw_caps = detect_hardware_capabilities()
        self.codec_options = get_codec_display_list(self.hw_caps)

        self.codec_combo = QComboBox()
        for display_name, _ in self.codec_options:
            self.codec_combo.addItem(display_name)
        # Default to HEVC if available
        default_idx = 0
        for idx, (_, cid) in enumerate(self.codec_options):
            if cid == "hevc_nvenc":
                default_idx = idx
                break
            if cid == "hevc_amf" and default_idx == 0:
                default_idx = idx
        self.codec_combo.setCurrentIndex(default_idx)
        video_layout.addWidget(self.codec_combo)

        # Pixel format selector (8-bit vs 10-bit)
        self.pixel_format_label = QLabel("Pixel Format (Bit Depth):")
        self.pixel_format_combo = QComboBox()
        self.pixel_format_combo.addItems([
            "8-bit (NV12 / YUV420P - Highest Compatibility)",
            "10-bit (P010LE - High Dynamic Range/Quality)"
        ])
        self.pixel_format_combo.setCurrentIndex(1) # Default to 10-bit
        video_layout.addWidget(self.pixel_format_label)
        video_layout.addWidget(self.pixel_format_combo)

        # ProRes profile - visible only for ProRes
        self.prores_profile_label = QLabel("ProRes Profile:")
        self.prores_profile_combo = QComboBox()
        self.prores_profile_combo.addItems([
            "Proxy (0)", "LT (1)", "Standard (2)", "HQ (3)", "4444 (4)", "4444 XQ (5)",
        ])
        self.prores_profile_combo.setCurrentIndex(3)
        video_layout.addWidget(self.prores_profile_label)
        video_layout.addWidget(self.prores_profile_combo)

        # NVENC export target (P5/P7) - visible only for NVENC
        self.nvenc_target_label = QLabel("Export Quality Profile:")
        self.nvenc_target_combo = QComboBox()
        self.nvenc_target_combo.addItems(get_export_target_labels())
        self.nvenc_target_combo.setCurrentIndex(0)
        video_layout.addWidget(self.nvenc_target_label)
        video_layout.addWidget(self.nvenc_target_combo)

        # Rate control - visible only for NVENC
        self.rate_control_label = QLabel("Rate Control:")
        self.rate_control_combo = QComboBox()
        self.rate_control_combo.addItems([
            "CBR - Constant Bit Rate (streaming/delivery)",
            "VBR - Variable Bit Rate (better quality/size ratio)",
            "ABR - Average Bit Rate (loose target)",
            "CQP - Constant Quality (manual QP)",
            "Lossless Archive - QP 0 (huge files, not realtime playback)",
        ])
        self.rate_control_combo.setCurrentIndex(0)
        video_layout.addWidget(self.rate_control_label)
        video_layout.addWidget(self.rate_control_combo)

        video_group.setLayout(video_layout)
        layout.addWidget(video_group)

        # --- QUALITY ---
        self.qual_group = QGroupBox("Quality (Bitrate)")
        qual_layout = QVBoxLayout()
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setMinimum(1)
        self.quality_slider.setMaximum(1000)
        self.quality_slider.setValue(100)
        self.quality_val = QLabel("100 Mbps")
        self._last_bitrate_mbps = 100
        self._last_cq_value = 18
        self._syncing_quality_controls = False
        self.quality_slider.valueChanged.connect(self._on_quality_changed)

        # CQP spinbox for direct numeric input (hidden by default, shown in CQP mode)
        cqp_row = QHBoxLayout()
        self.cqp_spin = QSpinBox()
        self.cqp_spin.setMinimum(0)
        self.cqp_spin.setMaximum(51)
        self.cqp_spin.setValue(18)
        self.cqp_spin.setPrefix("QP: ")
        self.cqp_spin.setToolTip("0 = lossless  |  18 = visually lossless  |  28 = high quality  |  51 = lowest quality")
        self.cqp_spin.setMinimumWidth(100)
        self.cqp_label = QLabel("(0=lossless | 18=visually lossless | 28=high quality)")
        self.cqp_spin.valueChanged.connect(self._on_cqp_spin_changed)
        cqp_row.addWidget(self.cqp_spin)
        cqp_row.addWidget(self.cqp_label)
        cqp_row.addStretch()
        self.cqp_widget = QWidget()
        self.cqp_widget.setLayout(cqp_row)
        self.cqp_widget.setVisible(False)

        qual_layout.addWidget(self.quality_slider)
        qual_layout.addWidget(self.quality_val)
        qual_layout.addWidget(self.cqp_widget)
        self.qual_group.setLayout(qual_layout)
        layout.addWidget(self.qual_group)

        # --- HARDWARE ---
        hw_group = QGroupBox("Hardware Options")
        hw_layout = QVBoxLayout()
        
        # Auto-detect row - this was in old codec tab, now back in Export window
        detect_row = QHBoxLayout()
        self.auto_detect_btn = QPushButton("🔍 Auto-Detect Hardware")
        self.auto_detect_btn.setStyleSheet("background-color: #0ea5e9; color: white; padding: 6px; font-weight: bold;")
        self.auto_detect_btn.setToolTip("Scan for NVIDIA NVENC, AMD AMF, Intel QSV encoders and lock out unavailable ones")
        self.auto_detect_btn.clicked.connect(self._on_auto_detect_hardware)
        detect_row.addWidget(self.auto_detect_btn)
        
        self.hw_detect_label = QLabel("Detecting...")
        self.hw_detect_label.setStyleSheet("color: #94a3b8; font-size: 9pt;")
        detect_row.addWidget(self.hw_detect_label)
        detect_row.addStretch()
        hw_layout.addLayout(detect_row)
        
        self.gpu_check = QCheckBox("Enable GPU Acceleration (Auto - uses detected encoder)")
        self.gpu_check.setChecked(True)
        self.gpu_decode_check = QCheckBox("Enable Hardware Decoding (NVDEC/AMF/QSV)")
        self.gpu_decode_check.setChecked(True)
        self.gpu_composite_check = QCheckBox("Enable Full GPU Compositing (5070 TURBO - stays in VRAM)")
        self.gpu_composite_check.setChecked(True)
        self.gpu_composite_check.setStyleSheet("color: #4ade80; font-weight: bold;")
        self.gpu_composite_check.setToolTip("Keeps all frames in GPU VRAM: scale_cuda + overlay_cuda. No CPU copy. Maxes out GPU like DaVinci. For NVIDIA 40/50 series.")
        hw_layout.addWidget(self.gpu_check)
        hw_layout.addWidget(self.gpu_decode_check)
        hw_layout.addWidget(self.gpu_composite_check)
        
        self.turbo_warning_label = QLabel("⚠️ TURBO Hybrid: When ANY filter is applied (denoise, color grading, transitions, text), TURBO switches to Hybrid mode: GPU does scale+overlay (5070 maxed), CPU does filters + encode. Without filters, stays full VRAM. This is automatic.")
        self.turbo_warning_label.setWordWrap(True)
        self.turbo_warning_label.setStyleSheet("color: #fbbf24; font-size: 8pt; background: #1f2937; padding: 4px; border-radius: 4px;")
        hw_layout.addWidget(self.turbo_warning_label)
        
        # FFmpeg update row
        ffmpeg_update_row = QHBoxLayout()
        self.ffmpeg_version_label = QLabel(f"FFmpeg: {UpdateManager.get_ffmpeg_version()[:60]}")
        self.ffmpeg_version_label.setStyleSheet("color: #94a3b8; font-size: 8pt;")
        ffmpeg_update_row.addWidget(self.ffmpeg_version_label)
        self.ffmpeg_update_btn = QPushButton("Update FFmpeg")
        self.ffmpeg_update_btn.setStyleSheet("background-color: #f59e0b; color: black; padding: 4px; font-weight: bold; font-size: 9pt;")
        self.ffmpeg_update_btn.setToolTip("Download latest FFmpeg - fixes missing filters like scale_cuda")
        self.ffmpeg_update_btn.clicked.connect(self._on_update_ffmpeg_clicked)
        ffmpeg_update_row.addWidget(self.ffmpeg_update_btn)
        hw_layout.addLayout(ffmpeg_update_row)
        hw_group.setLayout(hw_layout)
        layout.addWidget(hw_group)

        # Validation message for incompatible combos
        self.validation_label = QLabel("")
        self.validation_label.setStyleSheet("color: #fbbf24; font-weight: bold; padding: 5px;")
        self.validation_label.setWordWrap(True)
        self.validation_label.setVisible(False)
        layout.addWidget(self.validation_label)

        btn_layout = QHBoxLayout()
        self.choose_btn = QPushButton("Choose Location & Start Export")
        self.choose_btn.setStyleSheet("background-color: #8b5cf6; color: white; padding: 10px; font-weight: bold;")
        self.choose_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(self.choose_btn)
        layout.addLayout(btn_layout)

        # Connect show/hide logic
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        self.rate_control_combo.currentIndexChanged.connect(self._on_rate_control_changed)
        self._on_codec_changed(self.codec_combo.currentIndex())
        # Initial hardware label update - show what was detected at startup
        try:
            gpu_name = self.hw_caps.get('gpu_name', 'Unknown')
            vendors = []
            if self.hw_caps.get('nvidia'):
                vendors.append("NVIDIA")
            if self.hw_caps.get('amd'):
                vendors.append("AMD")
            if self.hw_caps.get('intel'):
                vendors.append("Intel")
            vendor_str = "/".join(vendors) if vendors else "CPU only"
            enc_list = ", ".join(self.hw_caps.get('encoders', [])) if self.hw_caps.get('encoders') else "none"
            self.hw_detect_label.setText(f"{gpu_name} | {vendor_str} | {enc_list}")
        except Exception:
            self.hw_detect_label.setText("Hardware detection ready - click Auto-Detect to rescan")

    def _default_fps_index(self):
        fps_values = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0, 120.0]
        fps = 60.0
        try:
            if self.app and getattr(self.app, 'timeline', None) and self.app.timeline.clips:
                fps = get_video_fps_static(self.app.timeline.clips[0].file_path)
        except Exception:
            fps = 60.0
        return min(range(len(fps_values)), key=lambda i: abs(fps_values[i] - fps))

    def _on_codec_changed(self, index):
        # Dynamic check based on actual codec id
        codec_id = self.codec_options[index][1] if index < len(self.codec_options) else "prores_ks"
        is_prores = codec_id.startswith("prores")
        is_hw = not is_prores
        self.prores_profile_label.setVisible(is_prores)
        self.prores_profile_combo.setVisible(is_prores)
        self.nvenc_target_label.setVisible(is_hw)
        self.nvenc_target_combo.setVisible(is_hw)
        self.rate_control_label.setVisible(is_hw)
        self.rate_control_combo.setVisible(is_hw)
        if is_hw:
            self._on_rate_control_changed(self.rate_control_combo.currentIndex())
        else:
            self.qual_group.setTitle("Quality (Bitrate)")
            self.quality_slider.setEnabled(True)
            self.quality_slider.setMinimum(1)
            self.quality_slider.setMaximum(1000)
            self.cqp_widget.setVisible(False)
            self.quality_val.setText(f"{self.quality_slider.value()} Mbps")
        self._validate_export_config()

    def _on_rate_control_changed(self, index):
        rc = self._rate_control_value(index)
        self.cqp_widget.setVisible(rc == 'cqp')
        self._validate_export_config()

        self._syncing_quality_controls = True
        if rc == 'cqp':
            self.qual_group.setTitle("Quality (CQP - lower = better)")
            self.quality_slider.setEnabled(True)
            self.quality_slider.setMinimum(0)
            self.quality_slider.setMaximum(51)
            value = max(0, min(51, self._last_cq_value))
            self.quality_slider.setValue(value)
            self.cqp_spin.setValue(value)
        elif rc == 'lossless':
            self.qual_group.setTitle("Quality - Lossless archive mode (QP=0)")
            self.quality_slider.setMinimum(0)
            self.quality_slider.setMaximum(51)
            self.quality_slider.setValue(0)
            self.quality_slider.setEnabled(False)
            self.cqp_spin.setValue(0)
        else:
            self.qual_group.setTitle("Quality (Bitrate)")
            self.quality_slider.setMinimum(1)
            self.quality_slider.setMaximum(1000)
            self.quality_slider.setEnabled(True)
            value = max(1, min(1000, self._last_bitrate_mbps))
            self.quality_slider.setValue(value)
        self._syncing_quality_controls = False
        self._on_quality_changed(self.quality_slider.value())

    def _rate_control_value(self, index=None):
        rc_map = {0: 'cbr', 1: 'vbr', 2: 'abr', 3: 'cqp', 4: 'lossless'}
        if index is None:
            index = self.rate_control_combo.currentIndex()
        return rc_map.get(index, 'cbr')

    def _format_cq_value(self, value):
        if value == 0:
            label = "lossless"
        elif value <= 18:
            label = "visually lossless"
        elif value <= 28:
            label = "high quality"
        elif value <= 38:
            label = "medium quality"
        else:
            label = "lower quality"
        return f"QP {value} ({label})"

    def _on_quality_changed(self, v):
        if self._syncing_quality_controls:
            return
        rc = self._rate_control_value()
        if rc == 'cqp':
            value = max(0, min(51, int(v)))
            self._last_cq_value = value
            if self.cqp_spin.value() != value:
                self._syncing_quality_controls = True
                self.cqp_spin.setValue(value)
                self._syncing_quality_controls = False
            self.quality_val.setText(self._format_cq_value(value))
        elif rc == 'lossless':
            self.quality_val.setText("Lossless archive (QP 0) - use CQP 12-18 for smoother playback")
        else:
            value = max(1, int(v))
            self._last_bitrate_mbps = value
            self.quality_val.setText(f"{value} Mbps")

    def _on_cqp_spin_changed(self, value):
        if self._syncing_quality_controls:
            return
        value = max(0, min(51, int(value)))
        self._last_cq_value = value
        if self._rate_control_value() == 'cqp':
            self._syncing_quality_controls = True
            self.quality_slider.setValue(value)
            self._syncing_quality_controls = False
            self.quality_val.setText(self._format_cq_value(value))

    def _on_auto_detect_hardware(self):
        """Re-added auto-detect button - was removed when export moved to separate window"""
        self.hw_detect_label.setText("Scanning...")
        QApplication.processEvents()
        self.hw_caps = detect_hardware_capabilities()
        self.codec_options = get_codec_display_list(self.hw_caps)

        # Rebuild combo
        current_codec = self.codec_options[0][1] if self.codec_options else "prores_ks"
        try:
            # Try to keep current selection if still available
            idx = self.codec_combo.currentIndex()
            if 0 <= idx < len(self.codec_options):
                current_codec = self.codec_options[idx][1]
        except:
            pass

        self.codec_combo.blockSignals(True)
        self.codec_combo.clear()
        for display_name, _ in self.codec_options:
            self.codec_combo.addItem(display_name)
        
        # Restore selection or default to HEVC
        new_idx = 0
        for i, (_, cid) in enumerate(self.codec_options):
            if cid == current_codec:
                new_idx = i
                break
        # Prefer HEVC if current not found
        if new_idx == 0:
            for i, (_, cid) in enumerate(self.codec_options):
                if cid in ("hevc_nvenc", "hevc_amf", "hevc_qsv", "hevc_vaapi"):
                    new_idx = i
                    break
        self.codec_combo.setCurrentIndex(new_idx)
        self.codec_combo.blockSignals(False)

        # Update label with what we found
        gpu_name = self.hw_caps.get('gpu_name', 'Unknown')
        vendors = []
        if self.hw_caps.get('nvidia'):
            vendors.append("NVIDIA")
        if self.hw_caps.get('amd'):
            vendors.append("AMD")
        if self.hw_caps.get('intel'):
            vendors.append("Intel")
        vendor_str = "/".join(vendors) if vendors else "CPU only"
        enc_list = ", ".join(self.hw_caps.get('encoders', [])) if self.hw_caps.get('encoders') else "none"
        self.hw_detect_label.setText(f"{gpu_name} | {vendor_str} | {enc_list}")

        # Update checkboxes based on detection - lock out NVENC stuff on AMD-only systems
        self.gpu_check.setChecked(bool(vendors))
        self.gpu_check.setText(f"Enable GPU Acceleration ({vendor_str} detected)" if vendors else "Enable GPU Acceleration (No HW encoder found - CPU only)")

        self._on_codec_changed(new_idx)
        self._validate_export_config()

    def _validate_export_config(self):
        """Next version: disable Export if codec+RC combo won't start - prevents AV1+lossless crash"""
        try:
            idx = self.codec_combo.currentIndex()
            if idx < 0 or idx >= len(self.codec_options):
                return True
            codec_id = self.codec_options[idx][1]
            rc = self._rate_control_value()

            # Check if hardware is actually available
            if "Not Detected" in self.codec_options[idx][0]:
                self.validation_label.setText(f"⚠️ {self.codec_options[idx][0]} - hardware not detected on this system. Install proper GPU driver or choose another codec.")
                self.validation_label.setVisible(True)
                self.choose_btn.setEnabled(False)
                return False

            # Check rate control compatibility
            valid_rcs = VALID_RC_FOR_CODEC.get(codec_id, ['cbr','vbr','cqp'])
            if rc not in valid_rcs:
                reason = INVALID_RC_REASON.get((codec_id, rc), f"{codec_id} does not support {rc} rate control")
                self.validation_label.setText(f"⚠️ {reason}")
                self.validation_label.setVisible(True)
                self.choose_btn.setEnabled(False)
                return False

            # All good
            self.validation_label.setVisible(False)
            self.choose_btn.setEnabled(True)
            return True
        except Exception:
            self.choose_btn.setEnabled(True)
            return True

    def _on_update_ffmpeg_clicked(self):
        self.ffmpeg_version_label.setText("Updating FFmpeg...")
        QApplication.processEvents()
        if os.name == 'nt':
            ok, msg = UpdateManager.update_ffmpeg_windows(self, lambda m: self.ffmpeg_version_label.setText(m[:80]))
        else:
            ok, msg = UpdateManager.update_ffmpeg_linux(lambda m: self.ffmpeg_version_label.setText(m[:80]))
        if ok:
            self.ffmpeg_version_label.setText(f"FFmpeg: {UpdateManager.get_ffmpeg_version()[:60]} - Updated!")
            QMessageBox.information(self, "FFmpeg", f"Updated!\n{msg}")
            self.hw_caps = detect_hardware_capabilities()
            self.hw_detect_label.setText(f"Re-detected after FFmpeg update: {self.hw_caps.get('gpu_name','')}")
        else:
            self.ffmpeg_version_label.setText(f"FFmpeg: {UpdateManager.get_ffmpeg_version()[:60]}")
            QMessageBox.warning(self, "FFmpeg Update", msg)

    def get_settings(self):
        # Dynamic codec map from detected hardware list
        codec_map = {i: cid for i, (_, cid) in enumerate(self.codec_options)}
        audio_map = {0: "pcm_s24le", 1: "pcm_s16le", 2: "aac", 3: "copy"}
        fps_map = {0: 23.976, 1: 24.0, 2: 25.0, 3: 29.97, 4: 30.0,
                   5: 50.0, 6: 59.94, 7: 60.0, 8: 120.0}
        rc = self._rate_control_value()
        return {
            'video_codec': codec_map.get(self.codec_combo.currentIndex(), "hevc_nvenc"),
            'audio_codec': audio_map[self.audio_combo.currentIndex()],
            'use_gpu': self.gpu_check.isChecked(),
            'use_gpu_decode': self.gpu_decode_check.isChecked(),
            'use_gpu_composite': self.gpu_composite_check.isChecked() if hasattr(self, 'gpu_composite_check') else False,
            'bitrate_mbps': self._last_bitrate_mbps,
            'cq_value': self.cqp_spin.value() if rc == 'cqp' else 18,
            'prores_profile': self.prores_profile_combo.currentIndex(),
            'export_target_index': self.nvenc_target_combo.currentIndex(),
            'rate_control': rc,
            'export_res_index': self.res_combo.currentIndex(),
            'timeline_fps': fps_map.get(self.fps_combo.currentIndex(), 60.0),
            'pixel_format': self.pixel_format_combo.currentIndex(),
        }


class RenderProgressDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rendering Timeline")
        self.setMinimumWidth(600)
        # Removed WindowStaysOnTopHint so it doesn't block the QMessageBox
        layout = QVBoxLayout(self)

        self.status_label = QLabel("Initializing render...")
        self.status_label.setStyleSheet("font-weight: bold; color: #4ade80; font-size: 12pt;")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setStyleSheet("""
            QProgressBar { border: 2px solid #4b5563; border-radius: 8px; background-color: #1f2937;
                text-align: center; font-size: 11pt; color: white; min-height: 35px; }
            QProgressBar::chunk { background-color: #4ade80; border-radius: 6px; }
        """)
        layout.addWidget(self.progress_bar)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(250)
        self.log_text.setStyleSheet("background: #0f1419; color: #00d9ff; font-family: 'Courier New', monospace; font-size: 10pt; padding: 5px;")
        layout.addWidget(self.log_text)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.cancel_btn = QPushButton("Cancel Render")
        self.cancel_btn.setStyleSheet("background-color: #ef4444; color: white; padding: 8px 20px; font-size: 11pt; font-weight: bold; border-radius: 6px;")
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

class FastEncodeProApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"FastEncode Pro v{__version__} - Accessible Video Editor")
        screen = QApplication.primaryScreen()
        if screen is not None:
            ag = screen.availableGeometry()
            pad = 24
            w = min(1400, max(800, ag.width() - pad * 2))
            h = min(900, max(600, ag.height() - pad * 2))
            x = ag.x() + max(0, (ag.width() - w) // 2)
            y = ag.y() + max(0, (ag.height() - h) // 2)
            self.setGeometry(x, y, w, h)
        else:
            self.setGeometry(100, 100, 1400, 900)
        self.input_files = []
        self.output_folder = ""
        self.encoding_thread = None
        self.timeline_export_thread = None
        self.current_file_index = 0
        self.media_library = []
        self.current_media = None

        self.video_widget = None

        self.timeline_duration = 0
        self.is_timeline_mode = False
        self._play_uses_timeline_edl = False

        self.dwell_filter = DwellClickFilter(self)
        self.hw_caps = detect_hardware_capabilities()

        self.app_settings = QSettings("FastEncodePro", "App")
        self.output_folder = self.app_settings.value("output_folder", "")
        
        self.proxy_status_label = QLabel("Proxies: Idle")
        self.proxy_status_label.setStyleSheet("color: #94a3b8; font-weight: bold; font-size: 14px;")
        self.proxy_status_label.setToolTip("Proxy generation status â€” shows current file and progress for screen readers")
        self.proxy_progress_bar = QProgressBar()
        self.proxy_progress_bar.setRange(0, 100)
        self.proxy_progress_bar.setValue(0)
        self.proxy_progress_bar.setFixedWidth(180)
        self.proxy_progress_bar.setFixedHeight(20)
        self.proxy_progress_bar.setTextVisible(True)
        self.proxy_progress_bar.setFormat("%p%")
        self.proxy_progress_bar.setStyleSheet("""
            QProgressBar { border: 1px solid #4b5563; border-radius: 6px; background-color: #1f2937; color: white; font-weight: bold; font-size: 11px; text-align: center; }
            QProgressBar::chunk { background-color: #3b82f6; border-radius: 5px; }
        """)
        self.proxy_progress_bar.setToolTip("Current proxy file progress â€” now shows real-time per-file percentage for accessibility")
        self.proxy_progress_bar.setVisible(False)
        self.proxy_manager = ProxyManager()

        def _on_proxy_status(text):
            self.proxy_status_label.setText(text)
            # Show bar when generating, hide when done
            txt_low = text.lower()
            is_active = ("left" in txt_low or "%" in text or "generating" in txt_low)
            is_done = ("up to date" in txt_low or "idle" in txt_low or "cancelled" in txt_low)
            self.proxy_progress_bar.setVisible(is_active and not is_done)
            if is_done:
                self.proxy_progress_bar.setValue(0)
                self.proxy_progress_bar.setVisible(False)

        def _on_proxy_file_progress(path, pct):
            self.proxy_progress_bar.setValue(pct)
            # Ensure visible during active progress
            if pct >= 0 and pct < 100:
                self.proxy_progress_bar.setVisible(True)

        self.proxy_manager.status_update.connect(_on_proxy_status)
        self.proxy_manager.file_progress.connect(_on_proxy_file_progress)

        # Menu bar for Updates (Windows + Linux compatible)
        menubar = self.menuBar()
        help_menu = menubar.addMenu("Help && Updates")
        
        ffmpeg_action = help_menu.addAction("Update FFmpeg...")
        ffmpeg_action.setToolTip("Download latest FFmpeg build (fixes scale_cuda, etc)")
        ffmpeg_action.triggered.connect(self.on_update_ffmpeg)
        
        check_update_action = help_menu.addAction("Check for App Update...")
        check_update_action.setToolTip("Check GitHub repo for newer version")
        check_update_action.triggered.connect(self.on_check_app_update)
        
        help_menu.addSeparator()
        about_action = help_menu.addAction("About v" + __version__)
        about_action.triggered.connect(self.on_about)
        
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(self.tab_style())
        main_layout.addWidget(self.tabs)

        self.timeline_tab = self.create_timeline_tab()
        self.batch_tab = self.create_batch_tab()
        self.access_tab = self.create_accessibility_tab()

        self.tabs.addTab(self.timeline_tab, "ðŸ“½ï¸ Timeline")
        self.tabs.addTab(self.batch_tab, "ðŸ“¦ Batch")
        self.tabs.addTab(self.access_tab, "â™¿ Accessibility")

        self.apply_theme()
        self.load_settings()



    def create_accessibility_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        title = QLabel("â™¿ Accessibility Features")
        title.setStyleSheet("font-size: 18pt; font-weight: bold; color: #4ade80;")
        layout.addWidget(title)

        dwell_group = QGroupBox("ðŸ‘ï¸ Eye Tracking / Dwell Click")
        dwell_group.setStyleSheet(self.groupbox_style())
        dwell_layout = QVBoxLayout()

        self.dwell_check = QCheckBox("Enable Dwell Click (Auto-click when looking at buttons)")
        self.dwell_check.setStyleSheet("font-size: 14pt; font-weight: bold; color: white;")
        self.dwell_check.stateChanged.connect(self.toggle_dwell)
        dwell_layout.addWidget(self.dwell_check)

        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("Dwell Time (seconds):"))
        self.dwell_time_spin = QDoubleSpinBox()
        self.dwell_time_spin.setRange(0.2, 5.0)
        self.dwell_time_spin.setValue(1.2)
        self.dwell_time_spin.setSingleStep(0.1)
        self.dwell_time_spin.setStyleSheet(self.spinbox_style())
        self.dwell_time_spin.valueChanged.connect(self.update_dwell_params)
        time_row.addWidget(self.dwell_time_spin)
        dwell_layout.addLayout(time_row)

        thresh_row = QHBoxLayout()
        thresh_row.addWidget(QLabel("Movement Threshold (Sensitivity):"))
        self.dwell_thresh_spin = QSpinBox()
        self.dwell_thresh_spin.setRange(5, 50)
        self.dwell_thresh_spin.setValue(15)
        self.dwell_thresh_spin.setStyleSheet(self.spinbox_style())
        self.dwell_thresh_spin.valueChanged.connect(self.update_dwell_params)
        thresh_row.addWidget(self.dwell_thresh_spin)
        dwell_layout.addLayout(thresh_row)

        dwell_group.setLayout(dwell_layout)
        layout.addWidget(dwell_group)

        switch_group = QGroupBox("ðŸ”˜ Switch Control / High Contrast")
        switch_group.setStyleSheet(self.groupbox_style())
        switch_layout = QVBoxLayout()
        info = QLabel("High-contrast focus borders are automatically enabled for easier navigation with Tab/Enter keys or Head Switches.")
        info.setWordWrap(True)
        switch_layout.addWidget(info)
        switch_group.setLayout(switch_layout)
        layout.addWidget(switch_group)

        layout.addStretch()
        return tab

    def toggle_dwell(self, state):
        self.dwell_filter.set_enabled(state == 2)

    def update_dwell_params(self):
        self.dwell_filter.set_params(self.dwell_time_spin.value(), self.dwell_thresh_spin.value())

    def on_update_ffmpeg(self):
        reply = QMessageBox.question(self, "Update FFmpeg", 
            f"Current: {UpdateManager.get_ffmpeg_version()}\n\nDownload latest FFmpeg build?\nThis fixes missing filters like scale_cuda and adds 5K/8K improvements.\n\nWindows: Downloads from gyan.dev (essentials build)\nLinux: Shows apt/dnf command",
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        dlg = QProgressDialog("Updating FFmpeg...", "Cancel", 0, 0, self)
        dlg.setWindowTitle("FFmpeg Updater")
        dlg.setModal(True)
        dlg.show()
        QApplication.processEvents()
        def log_fn(msg):
            dlg.setLabelText(msg)
            QApplication.processEvents()
        if os.name == 'nt':
            ok, msg = UpdateManager.update_ffmpeg_windows(self, log_fn)
        else:
            ok, msg = UpdateManager.update_ffmpeg_linux(log_fn)
        dlg.close()
        if ok:
            QMessageBox.information(self, "FFmpeg Updated", f"Success!\n{msg}\n\nNew: {UpdateManager.get_ffmpeg_version()}")
        else:
            QMessageBox.warning(self, "FFmpeg Update", f"Update info:\n{msg}")

    def on_check_app_update(self):
        dlg = QProgressDialog("Checking GitHub releases...", None, 0, 0, self)
        dlg.setWindowTitle("App Update Checker")
        dlg.setModal(True)
        dlg.show()
        QApplication.processEvents()
        info = UpdateManager.check_app_update(__version__)
        dlg.close()
        if 'error' in info:
            QMessageBox.warning(self, "Update Check Failed", f"Could not check {GITHUB_REPO}:\n{info['error']}\n\nSet GITHUB_REPO variable in file to your repo.")
            return
        if not info.get('is_newer'):
            QMessageBox.information(self, "Up to Date", f"You are on latest v{__version__}\nLatest: v{info.get('latest')} on GitHub")
            return
        assets_text = "\n".join([f"- {a['name']} ({a['size']//1024}KB)" for a in info.get('assets', [])[:5]])
        msg = f"New version available!\n\nCurrent: v{info['current']}\nLatest: v{info['latest']}\n\n{info.get('body','')[:400]}\n\nAssets:\n{assets_text}\n\nOpen GitHub release page?"
        reply = QMessageBox.question(self, f"Update to v{info['latest']}?", msg, QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            import webbrowser
            webbrowser.open(info.get('url'))
            for asset in info.get('assets', []):
                if asset['name'].endswith('.exe') and os.name == 'nt':
                    r2 = QMessageBox.question(self, "Auto-Install?", f"Found installer {asset['name']}\nDownload and run it now? (Works with INNO installer)", QMessageBox.Yes | QMessageBox.No)
                    if r2 == QMessageBox.Yes:
                        dlg2 = QProgressDialog(f"Downloading {asset['name']}...", "Cancel", 0, 0, self)
                        dlg2.show()
                        ok, msg = UpdateManager.download_and_install_update(asset['browser_download_url'], "", lambda m: dlg2.setLabelText(m))
                        dlg2.close()
                        QMessageBox.information(self, "Update", msg)
                    break
                elif asset['name'].endswith('.py'):
                    r2 = QMessageBox.question(self, "Auto-Update .py?", f"Found {asset['name']}\nReplace current .py file? Backup will be created.", QMessageBox.Yes | QMessageBox.No)
                    if r2 == QMessageBox.Yes:
                        ok, msg = UpdateManager.download_and_install_update(asset['browser_download_url'], "", None)
                        QMessageBox.information(self, "Update", msg)
                    break

    def on_about(self):
        QMessageBox.about(self, f"About FastEncode Pro v{__version__}",
            f"FastEncode Pro v{__version__}\n\nGPU: {self.hw_caps.get('gpu_name','Unknown')}\nFFmpeg: {UpdateManager.get_ffmpeg_version()}\n\nGitHub: {GITHUB_REPO}\n\nIncludes:\n- 5070 TURBO Full GPU Canvas\n- FFmpeg auto-updater (fixes scale_cuda)\n- App auto-updater for Windows/Linux INNO workaround")

    def create_timeline_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        project_controls = QHBoxLayout()
        save_proj_btn = QPushButton("ðŸ’¾ Save Project")
        save_proj_btn.setStyleSheet(self.button_style("#3b82f6"))
        save_proj_btn.setMinimumHeight(40)
        save_proj_btn.clicked.connect(self.save_project)
        project_controls.addWidget(save_proj_btn)

        load_proj_btn = QPushButton("ðŸ“‚ Load Project")
        load_proj_btn.setStyleSheet(self.button_style("#f59e0b"))
        load_proj_btn.setMinimumHeight(40)
        load_proj_btn.clicked.connect(self.load_project)
        project_controls.addWidget(load_proj_btn)
        
        project_controls.addStretch()
        project_controls.addWidget(self.proxy_status_label)
        project_controls.addWidget(self.proxy_progress_bar)

        # Clear proxies button â€” accessibility: one click cleanup
        self.clear_proxies_btn = QPushButton("ðŸ—‘ï¸ Clear Proxies")
        self.clear_proxies_btn.setStyleSheet(self.button_style("#6b7280"))
        self.clear_proxies_btn.setMinimumHeight(32)
        self.clear_proxies_btn.setFixedWidth(140)
        self.clear_proxies_btn.setToolTip("Delete all proxy files from temp folder to free disk space")
        self.clear_proxies_btn.clicked.connect(self.clear_all_proxies)
        project_controls.addWidget(self.clear_proxies_btn)
        
        layout.addLayout(project_controls)

        top_section = QWidget()
        top_layout = QHBoxLayout(top_section)
        top_layout.setSpacing(10)
        library_panel = QWidget()
        library_layout = QVBoxLayout(library_panel)
        library_layout.setContentsMargins(5, 5, 5, 5)
        lib_title = QLabel("ðŸ“š MEDIA LIBRARY")
        lib_title.setStyleSheet("font-size: 14pt; font-weight: bold; color: #4ade80; padding: 5px;")
        library_layout.addWidget(lib_title)
        self.media_list = QListWidget()
        self.media_list.setStyleSheet(self.list_style())
        self.media_list.itemClicked.connect(self.on_media_selected)
        library_layout.addWidget(self.media_list)
        lib_buttons = QHBoxLayout()
        add_media_btn = QPushButton("âž• Add Media")
        add_media_btn.setStyleSheet(self.button_style("#4ade80"))
        add_media_btn.setMinimumHeight(50)
        add_media_btn.clicked.connect(self.add_media_to_library)
        lib_buttons.addWidget(add_media_btn)
        remove_media_btn = QPushButton("âž– Remove")
        remove_media_btn.setStyleSheet(self.button_style("#ef4444"))
        remove_media_btn.setMinimumHeight(50)
        remove_media_btn.clicked.connect(self.remove_from_library)
        lib_buttons.addWidget(remove_media_btn)
        library_layout.addLayout(lib_buttons)
        top_layout.addWidget(library_panel, stretch=1)
        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(5, 5, 5, 5)
        preview_title = QLabel("ðŸŽ¬ PREVIEW")
        preview_title.setStyleSheet("font-size: 14pt; font-weight: bold; color: #3b82f6; padding: 5px;")
        preview_layout.addWidget(preview_title)

        self.video_container = QWidget()
        self.video_container_layout = QVBoxLayout(self.video_container)
        self.video_container_layout.setContentsMargins(0, 0, 0, 0)

        self.video_widget = MPVVideoWidget()
        self.video_widget.setMinimumSize(640, 360)
        self.video_widget.setStyleSheet("background-color: black; border: 2px solid #4b5563; border-radius: 8px;")
        self.video_container_layout.addWidget(self.video_widget)
        self.video_widget.show()

        self.video_widget.positionChanged.connect(self._on_position_changed)
        self.video_widget.durationChanged.connect(self._on_duration_changed)

        preview_layout.addWidget(self.video_container)
        self.preview_slider = QSlider(Qt.Orientation.Horizontal)
        self.preview_slider.setMinimum(0)
        self.preview_slider.setMaximum(1000)
        self.preview_slider.setStyleSheet(self.slider_style())
        self.preview_slider.sliderMoved.connect(self.seek_preview)
        preview_layout.addWidget(self.preview_slider)
        self.timecode_label = QLabel("00:00:00 / 00:00:00")
        self.timecode_label.setStyleSheet("font-size: 11pt; color: white; padding: 5px;")
        self.timecode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_layout.addWidget(self.timecode_label)
        controls_row = QHBoxLayout()
        self.play_btn = QPushButton("â–¶ï¸ Play")
        self.play_btn.setStyleSheet(self.button_style("#3b82f6"))
        self.play_btn.setMinimumHeight(50)
        self.play_btn.clicked.connect(self.toggle_play)
        controls_row.addWidget(self.play_btn)
        self.fullscreen_btn = QPushButton("â›¶ Fullscreen")
        self.fullscreen_btn.setStyleSheet(self.button_style("#8b5cf6"))
        self.fullscreen_btn.setMinimumHeight(50)
        self.fullscreen_btn.clicked.connect(self.enter_fullscreen)
        controls_row.addWidget(self.fullscreen_btn)
        preview_layout.addLayout(controls_row)

        trim_panel = QWidget()
        trim_layout = QHBoxLayout(trim_panel)
        trim_layout.setContentsMargins(0, 5, 0, 5)

        trim_box = QGroupBox("âœ‚ï¸ Trim")
        trim_box.setStyleSheet(self.groupbox_style())
        trim_box_layout = QVBoxLayout(trim_box)
        trim_buttons = QHBoxLayout()
        set_in_btn = QPushButton("[ Set IN")
        set_in_btn.setStyleSheet(self.button_style("#10b981"))
        set_in_btn.setMinimumHeight(35)
        set_in_btn.clicked.connect(self.set_media_in_point)
        trim_buttons.addWidget(set_in_btn)
        set_out_btn = QPushButton("Set OUT ]")
        set_out_btn.setStyleSheet(self.button_style("#10b981"))
        set_out_btn.setMinimumHeight(35)
        set_out_btn.clicked.connect(self.set_media_out_point)
        trim_buttons.addWidget(set_out_btn)
        trim_box_layout.addLayout(trim_buttons)
        self.trim_info = QLabel("In: 00:00:00 | Out: 00:00:00 | Duration: 00:00:00")
        self.trim_info.setStyleSheet("font-size: 9pt; color: #9ca3af; padding: 2px;")
        self.trim_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trim_box_layout.addWidget(self.trim_info)
        trim_layout.addWidget(trim_box)

        mixer_box = QGroupBox("ðŸŽšï¸ Audio Mixer")
        mixer_box.setStyleSheet(self.groupbox_style())
        mixer_box_layout = QVBoxLayout(mixer_box)

        t1_layout = QHBoxLayout()
        t1_layout.addWidget(QLabel("Audio Track 1"))
        self.track1_norm = QCheckBox("Normalize")
        self.track1_norm.setStyleSheet("color: #4ade80;")
        self.track1_norm.stateChanged.connect(self.update_clip_volume)
        t1_layout.addWidget(self.track1_norm)
        mixer_box_layout.addLayout(t1_layout)

        t1_slider_layout = QHBoxLayout()
        self.track1_slider = QSlider(Qt.Orientation.Horizontal)
        self.track1_slider.setRange(-60, 30)
        self.track1_slider.setValue(0)
        self.track1_slider.setStyleSheet(self.slider_style())
        self.track1_slider.valueChanged.connect(self.update_clip_volume)
        t1_slider_layout.addWidget(self.track1_slider)
        self.t1_val = QLabel("0 dB")
        t1_slider_layout.addWidget(self.t1_val)
        mixer_box_layout.addLayout(t1_slider_layout)

        t2_layout = QHBoxLayout()
        t2_layout.addWidget(QLabel("Audio Track 2"))
        self.track2_norm = QCheckBox("Normalize")
        self.track2_norm.setStyleSheet("color: #4ade80;")
        self.track2_norm.stateChanged.connect(self.update_clip_volume)
        t2_layout.addWidget(self.track2_norm)
        mixer_box_layout.addLayout(t2_layout)

        t2_slider_layout = QHBoxLayout()
        self.track2_slider = QSlider(Qt.Orientation.Horizontal)
        self.track2_slider.setRange(-60, 30)
        self.track2_slider.setValue(0)
        self.track2_slider.setStyleSheet(self.slider_style())
        self.track2_slider.valueChanged.connect(self.update_clip_volume)
        t2_slider_layout.addWidget(self.track2_slider)
        self.t2_val = QLabel("0 dB")
        t2_slider_layout.addWidget(self.t2_val)
        mixer_box_layout.addLayout(t2_slider_layout)

        sync_layout = QHBoxLayout()
        self.auto_sync_btn = QPushButton("ðŸŽ¯ Auto-Sync Audio")
        self.auto_sync_btn.setStyleSheet(self.button_style("#8b5cf6"))
        self.auto_sync_btn.setMinimumHeight(35)
        self.auto_sync_btn.clicked.connect(self.auto_sync_audio_tracks)
        self.auto_sync_btn.setToolTip("Automatically detect and fix audio sync offset between tracks")
        sync_layout.addWidget(self.auto_sync_btn)

        self.sync_status_label = QLabel("")
        self.sync_status_label.setStyleSheet("color: #60a5fa; font-size: 10pt;")
        sync_layout.addWidget(self.sync_status_label)
        sync_layout.addStretch()
        mixer_box_layout.addLayout(sync_layout)

        trim_layout.addWidget(mixer_box)

        preview_layout.addWidget(trim_panel)
        top_layout.addWidget(preview_panel, stretch=2)

        # --- SIDEBAR (COLOR GRADING & FILTERS) ---
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        self.sidebar_tabs = QTabWidget()
        self.sidebar_tabs.setStyleSheet(
            "QTabBar::tab { background: #374151; color: white; padding: 8px 16px; margin-right: 2px; }"
            "QTabBar::tab:selected { background: #3b82f6; font-weight: bold; }"
            "QTabWidget::pane { border: 1px solid #4b5563; background: #1f2937; }"
        )
        
        # --- COLOR GRADING TAB ---
        color_tab = QWidget()
        color_layout = QVBoxLayout(color_tab)
        
        color_actions_layout = QHBoxLayout()
        self.auto_balance_btn = QPushButton("âœ¨ Auto Color Balance")
        self.auto_balance_btn.setStyleSheet(self.button_style("#10b981"))
        self.auto_balance_btn.clicked.connect(self.apply_auto_balance)
        color_actions_layout.addWidget(self.auto_balance_btn)
        
        self.reset_filters_btn = QPushButton("â†º Reset All Filters")
        self.reset_filters_btn.setStyleSheet(self.button_style("#ef4444"))
        self.reset_filters_btn.clicked.connect(self.reset_all_filters)
        color_actions_layout.addWidget(self.reset_filters_btn)
        color_layout.addLayout(color_actions_layout)
        
        # Cinema Scoping
        self.cinema_scope_check = QCheckBox("ðŸŽ¬ Enable Cinema Scoping (2.35:1)")
        self.cinema_scope_check.setStyleSheet("color: white; font-weight: bold; padding: 5px;")
        self.cinema_scope_check.stateChanged.connect(self.update_live_preview_filters)
        color_layout.addWidget(self.cinema_scope_check)

        wheels_group = QGroupBox("Color Wheels")
        wheels_group.setStyleSheet(self.groupbox_style())
        wheels_layout = QHBoxLayout()
        
        self.lift_wheel = ColorWheelWidget("Shadows (Lift)")
        self.gamma_wheel = ColorWheelWidget("Midtones (Gamma)")
        self.gain_wheel = ColorWheelWidget("Highlights (Gain)")
        
        self.lift_wheel.colorChanged.connect(self.update_live_preview_filters)
        self.gamma_wheel.colorChanged.connect(self.update_live_preview_filters)
        self.gain_wheel.colorChanged.connect(self.update_live_preview_filters)
        
        wheels_layout.addWidget(self.lift_wheel)
        wheels_layout.addWidget(self.gamma_wheel)
        wheels_layout.addWidget(self.gain_wheel)
        wheels_group.setLayout(wheels_layout)
        color_layout.addWidget(wheels_group)

        # Legacy Sliders
        legacy_group = QGroupBox("Adjustments")
        legacy_group.setStyleSheet(self.groupbox_style())
        legacy_layout = QVBoxLayout()
        def add_color_slider(name, min_v, max_v, default_v, label_format, attr_name):
            row = QHBoxLayout()
            row.addWidget(QLabel(name))
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setMinimum(min_v)
            slider.setMaximum(max_v)
            slider.setValue(default_v)
            slider.setStyleSheet(self.slider_style())
            val_label = QLabel(label_format.format(default_v))
            val_label.setFixedWidth(50)
            def on_change(v):
                val_label.setText(label_format.format(v))
                self.update_live_preview_filters()
            slider.valueChanged.connect(on_change)
            row.addWidget(slider)
            row.addWidget(val_label)
            setattr(self, attr_name, slider)
            legacy_layout.addLayout(row)

        add_color_slider("Brightness:", -100, 100, 0, "{:d}", "color_brightness_slider")
        add_color_slider("Contrast:", -100, 100, 0, "{:d}", "color_contrast_slider")
        add_color_slider("Saturation:", -100, 200, 0, "{:d}", "color_saturation_slider")
        add_color_slider("Gamma:", -90, 900, 0, "{:d}", "color_gamma_slider")
        legacy_group.setLayout(legacy_layout)
        color_layout.addWidget(legacy_group)
        
        color_layout.addStretch()
        self.sidebar_tabs.addTab(color_tab, "Color")

        # --- FILTERS & TOOLS TAB ---
        filters_tab = QWidget()
        filters_layout = QVBoxLayout(filters_tab)
        
        fx_group = QGroupBox("FX Filters")
        fx_group.setStyleSheet(self.groupbox_style())
        fx_layout = QVBoxLayout()
        def add_filter_combo(name, items, attr_name):
            row = QHBoxLayout()
            row.addWidget(QLabel(name))
            combo = QComboBox()
            combo.addItems(items)
            combo.setStyleSheet(self.combo_style())
            combo.currentIndexChanged.connect(self.update_live_preview_filters)
            row.addWidget(combo)
            setattr(self, attr_name, combo)
            fx_layout.addLayout(row)

        add_filter_combo("Denoise:", ["Off", "Light", "Medium", "Heavy", "Very Heavy"], "denoise_combo")
        add_filter_combo("Deflicker:", ["Off", "Light", "Medium", "Heavy"], "deflicker_combo")
        add_filter_combo("Exposure:", ["Off", "+0.05", "+0.1", "+0.15", "+0.2", "-0.05", "-0.1"], "exposure_combo")
        add_filter_combo("Temporal:", ["Off", "Light", "Medium", "Heavy"], "temporal_combo")
        add_filter_combo("Sharpness:", ["Off", "Light", "Medium", "Heavy"], "sharpness_combo")
        fx_group.setLayout(fx_layout)
        filters_layout.addWidget(fx_group)
        
        tools_group = QGroupBox("Creative Tools")
        tools_group.setStyleSheet(self.groupbox_style())
        tools_layout = QVBoxLayout()
        add_text_btn = QPushButton("Add Text / Lower Third")
        add_text_btn.setStyleSheet(self.button_style("#f59e0b"))
        add_text_btn.clicked.connect(self.add_text_overlay)
        tools_layout.addWidget(add_text_btn)
        record_vo_btn = QPushButton("Record Voiceover")
        record_vo_btn.setStyleSheet(self.button_style("#ec4899"))
        record_vo_btn.clicked.connect(self.record_voiceover)
        tools_layout.addWidget(record_vo_btn)
        tools_group.setLayout(tools_layout)
        filters_layout.addWidget(tools_group)
        
        filters_layout.addStretch()
        self.sidebar_tabs.addTab(filters_tab, "FX & Tools")

        sidebar_scroll.setWidget(self.sidebar_tabs)
        top_layout.addWidget(sidebar_scroll, stretch=1)
        from PyQt6.QtWidgets import QSplitter
        self.main_splitter = QSplitter(Qt.Orientation.Vertical)
        self.main_splitter.addWidget(top_section)
        timeline_section = QWidget()
        timeline_layout = QVBoxLayout(timeline_section)
        timeline_layout.setContentsMargins(5, 5, 5, 5)
        timeline_header = QHBoxLayout()
        timeline_title = QLabel("ðŸŽžï¸ TIMELINE")
        timeline_title.setStyleSheet("font-size: 14pt; font-weight: bold; color: #f59e0b; padding: 5px;")
        timeline_header.addWidget(timeline_title)
        timeline_header.addStretch()
        zoom_in_btn = QPushButton("ðŸ”+")
        zoom_in_btn.setStyleSheet(self.button_style("#6366f1"))
        zoom_in_btn.setFixedSize(60, 40)
        zoom_in_btn.clicked.connect(self.zoom_in_timeline)
        timeline_header.addWidget(zoom_in_btn)
        zoom_out_btn = QPushButton("ðŸ”âˆ’")
        zoom_out_btn.setStyleSheet(self.button_style("#6366f1"))
        zoom_out_btn.setFixedSize(60, 40)
        zoom_out_btn.clicked.connect(self.zoom_out_timeline)
        timeline_header.addWidget(zoom_out_btn)
        timeline_layout.addLayout(timeline_header)
        self.timeline = TimelineWidget()
        self.timeline.setStyleSheet("background-color: #111827; border: 2px solid #4b5563; border-radius: 8px;")
        self.timeline.clip_selected.connect(self.on_timeline_clip_selected)
        self.timeline.playhead_moved.connect(self.on_timeline_playhead_moved)
        self.timeline.timeline_clicked.connect(self.activate_timeline_mode)
        timeline_layout.addWidget(self.timeline, stretch=1)
        timeline_controls = QHBoxLayout()
        add_to_timeline_btn = QPushButton("âž• Add to Timeline")
        add_to_timeline_btn.setStyleSheet(self.button_style("#4ade80"))
        add_to_timeline_btn.setMinimumHeight(50)
        add_to_timeline_btn.clicked.connect(self.add_to_timeline)
        timeline_controls.addWidget(add_to_timeline_btn)
        remove_from_timeline_btn = QPushButton("âž– Remove Clip")
        remove_from_timeline_btn.setStyleSheet(self.button_style("#ef4444"))
        remove_from_timeline_btn.setMinimumHeight(50)
        remove_from_timeline_btn.clicked.connect(self.remove_from_timeline)
        timeline_controls.addWidget(remove_from_timeline_btn)
        clear_timeline_btn = QPushButton("ðŸ—‘ï¸ Clear All")
        clear_timeline_btn.setStyleSheet(self.button_style("#dc2626"))
        clear_timeline_btn.setMinimumHeight(50)
        clear_timeline_btn.clicked.connect(self.clear_timeline)
        timeline_controls.addWidget(clear_timeline_btn)
        self.export_timeline_btn = QPushButton("ðŸ’¾ EXPORT TIMELINE")
        self.export_timeline_btn.setStyleSheet(self.button_style("#8b5cf6"))
        self.export_timeline_btn.setMinimumHeight(50)
        self.export_timeline_btn.clicked.connect(self.export_timeline)
        timeline_controls.addWidget(self.export_timeline_btn)
        self.stop_export_btn = QPushButton("â¹ï¸ STOP RENDER")
        self.stop_export_btn.setStyleSheet(self.button_style("#ef4444"))
        self.stop_export_btn.setMinimumHeight(50)
        self.stop_export_btn.setEnabled(False)
        self.stop_export_btn.clicked.connect(self.stop_timeline_export)
        timeline_controls.addWidget(self.stop_export_btn)
        timeline_layout.addLayout(timeline_controls)
        
        # Accessibility Automation Panel
        access_controls = QHBoxLayout()
        access_label = QLabel("?? Quick Actions (AI Alternative):")
        access_label.setStyleSheet("font-weight: bold; color: #fbbf24;")
        access_controls.addWidget(access_label)
        
        auto_trim_btn = QPushButton("?? Auto-Trim Edges")
        auto_trim_btn.setStyleSheet(self.button_style("#0ea5e9"))
        auto_trim_btn.setToolTip("Trims 1 second off the start and end of the selected clip.")
        auto_trim_btn.clicked.connect(self.auto_trim_selected)
        access_controls.addWidget(auto_trim_btn)

        auto_fade_btn = QPushButton("?? Fade All Clips")
        auto_fade_btn.setStyleSheet(self.button_style("#a855f7"))
        auto_fade_btn.setToolTip("Applies a 1-second crossfade transition between all clips.")
        auto_fade_btn.clicked.connect(self.auto_fade_all)
        access_controls.addWidget(auto_fade_btn)

        auto_bw_btn = QPushButton("?? Make B&&W")
        auto_bw_btn.setStyleSheet(self.button_style("#64748b"))
        auto_bw_btn.setToolTip("Applies a Black & White filter to all clips.")
        auto_bw_btn.clicked.connect(self.auto_black_and_white)
        access_controls.addWidget(auto_bw_btn)

        auto_norm_btn = QPushButton("?? Normalize Audio")
        auto_norm_btn.setStyleSheet(self.button_style("#10b981"))
        auto_norm_btn.setToolTip("Normalizes audio volume for all clips.")
        auto_norm_btn.clicked.connect(self.auto_normalize_audio)
        access_controls.addWidget(auto_norm_btn)
        
        timeline_layout.addLayout(access_controls)

        self.main_splitter.addWidget(timeline_section)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setChildrenCollapsible(False)
        layout.addWidget(self.main_splitter, stretch=1)
        return tab

    def create_batch_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)
        files_group = QGroupBox("ðŸ“ Files")
        files_group.setStyleSheet(self.groupbox_style())
        files_layout = QVBoxLayout()
        self.file_list = QListWidget()
        self.file_list.setStyleSheet(self.list_style())
        files_layout.addWidget(self.file_list)
        file_buttons = QHBoxLayout()
        add_btn = QPushButton("âž• Add Files")
        add_btn.setStyleSheet(self.button_style("#4ade80"))
        add_btn.setMinimumHeight(50)
        add_btn.clicked.connect(self.add_files)
        file_buttons.addWidget(add_btn)
        remove_btn = QPushButton("âž– Remove")
        remove_btn.setStyleSheet(self.button_style("#ef4444"))
        remove_btn.setMinimumHeight(50)
        remove_btn.clicked.connect(self.remove_selected)
        file_buttons.addWidget(remove_btn)
        clear_btn = QPushButton("ðŸ—‘ï¸ Clear All")
        clear_btn.setStyleSheet(self.button_style("#dc2626"))
        clear_btn.setMinimumHeight(50)
        clear_btn.clicked.connect(self.clear_files)
        file_buttons.addWidget(clear_btn)
        files_layout.addLayout(file_buttons)
        files_group.setLayout(files_layout)
        layout.addWidget(files_group)
        output_group = QGroupBox("ðŸ’¾ Output")
        output_group.setStyleSheet(self.groupbox_style())
        output_layout = QVBoxLayout()
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Folder:"))
        self.output_label = QLabel(self.output_folder if self.output_folder else "Not selected")
        self.output_label.setStyleSheet("color: #9ca3af; padding: 5px;")
        output_row.addWidget(self.output_label, stretch=1)
        browse_btn = QPushButton("ðŸ“‚ Browse")
        browse_btn.setStyleSheet(self.button_style("#3b82f6"))
        browse_btn.setMinimumHeight(40)
        browse_btn.clicked.connect(self.select_output)
        output_row.addWidget(browse_btn)
        output_layout.addLayout(output_row)
        output_group.setLayout(output_layout)
        layout.addWidget(output_group)
        progress_group = QGroupBox("ðŸ“Š Progress")
        progress_group.setStyleSheet(self.groupbox_style())
        progress_layout = QVBoxLayout()
        self.file_label = QLabel("")
        self.file_label.setStyleSheet("font-size: 11pt; color: white; padding: 5px;")
        progress_layout.addWidget(self.file_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet("""
            QProgressBar { border: 2px solid #4b5563; border-radius: 8px; background-color: #1f2937;
                text-align: center; font-size: 10pt; color: white; min-height: 30px; }
            QProgressBar::chunk { background-color: #4ade80; border-radius: 6px; }
        """)
        progress_layout.addWidget(self.progress_bar)
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("font-size: 10pt; color: #9ca3af; padding: 5px;")
        progress_layout.addWidget(self.status_label)
        progress_group.setLayout(progress_layout)
        layout.addWidget(progress_group)
        log_group = QGroupBox("ðŸ“ Log")
        log_group.setStyleSheet(self.groupbox_style())
        log_layout = QVBoxLayout()
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("""
            QTextEdit { background-color: #0f1419; color: #4ade80; font-family: 'Courier New', monospace;
                font-size: 9pt; border: 2px solid #4b5563; border-radius: 8px; padding: 5px; }
        """)
        log_layout.addWidget(self.log_text)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)
        control_buttons = QHBoxLayout()
        self.start_btn = QPushButton("â–¶ï¸ START ENCODING")
        self.start_btn.setStyleSheet(self.button_style("#4ade80"))
        self.start_btn.setMinimumHeight(60)
        self.start_btn.clicked.connect(self.start_encoding)
        control_buttons.addWidget(self.start_btn)
        self.stop_btn = QPushButton("â¹ï¸ STOP")
        self.stop_btn.setStyleSheet(self.button_style("#ef4444"))
        self.stop_btn.setMinimumHeight(60)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_encoding)
        control_buttons.addWidget(self.stop_btn)
        layout.addLayout(control_buttons)
        return tab

    def add_media_to_library(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Media Files", "", "Videos (*.mp4 *.mov *.avi *.mkv *.mts *.m2ts);;All (*.*)")
        for f in files:
            if not any(m.file_path == f for m in self.media_library):
                media = MediaLibraryItem(f)
                self.media_library.append(media)
                self.media_list.addItem(media.name)
                self.proxy_manager.add_job(f)

    def remove_from_library(self):
        row = self.media_list.currentRow()
        if row >= 0:
            self.media_list.takeItem(row)
            del self.media_library[row]
            if self.current_media and row == self.media_library.index(self.current_media) if self.current_media in self.media_library else False:
                self.current_media = None
                if self.video_widget:
                    self.video_widget.stop()

    def _on_position_changed(self, position_ms):
        """Handle video position updates"""
        if self.video_widget:
            dur = self.video_widget.duration()
            if dur <= 0 and self.is_timeline_mode and self._play_uses_timeline_edl:
                dur = int(self.timeline.get_timeline_duration() * 1000)
            if dur > 0:
                slider_value = int((position_ms / dur) * 1000)
                self.preview_slider.setValue(slider_value)

        dur_display = 0
        if self.video_widget:
            dur_display = self.video_widget.duration()
            if dur_display <= 0 and self.is_timeline_mode and self._play_uses_timeline_edl:
                dur_display = int(self.timeline.get_timeline_duration() * 1000)
        current_tc = self.format_timecode(position_ms)
        total_tc = self.format_timecode(dur_display)
        self.timecode_label.setText(f"{current_tc} / {total_tc}")

        # Update Timeline Playhead automatically
        if not self.timeline.dragging_playhead:
            if self.is_timeline_mode and self._play_uses_timeline_edl:
                self.timeline.set_playhead_position(position_ms / 1000.0, auto_scroll=True, emit_signal=False)
            elif self.is_timeline_mode and self.current_media is None and getattr(self.timeline, 'selected_clip', None):
                clip = self.timeline.selected_clip
                file_sec = position_ms / 1000.0
                tl_time = clip.start_time + (file_sec - clip.in_point)
                self.timeline.set_playhead_position(tl_time, auto_scroll=True, emit_signal=False)

    def _on_duration_changed(self, duration_ms):
        if duration_ms <= 0:
            if self.is_timeline_mode and self._play_uses_timeline_edl:
                duration_ms = int(self.timeline.get_timeline_duration() * 1000)
            elif getattr(self, 'current_media', None):
                duration_ms = int(getattr(self.current_media, 'duration', 0) * 1000)
        if self.video_widget:
            pending = self.video_widget._pending_seek_ms
            current_ms = pending if pending is not None else self.video_widget._position_ms
        else:
            current_ms = 0
        current_tc = self.format_timecode(current_ms)
        total_tc = self.format_timecode(duration_ms)
        self.timecode_label.setText(f"{current_tc} / {total_tc}")

    def on_media_selected(self, item):
        self.is_timeline_mode = False
        self._play_uses_timeline_edl = False
        row = self.media_list.row(item)
        if 0 <= row < len(self.media_library):
            self.current_media = self.media_library[row]
            file_path = self.current_media.file_path

            if self.video_widget and self.video_widget.load_file(self.proxy_manager.get_proxy(file_path)):
                self.video_widget.pause()
                self.update_live_preview_filters()

                n_streams = get_audio_stream_count_static(file_path)
                if n_streams > 1:
                    filter_parts = []
                    inputs = []
                    for i in range(n_streams):
                        vol_db = 0.0
                        filter_parts.append(f"[aid{i+1}]volume={vol_db}dB[a{i}]")
                        inputs.append(f"[a{i}]")

                    input_tags = "".join(inputs)
                    filter_str = f"{';'.join(filter_parts)};{input_tags}amix=inputs={n_streams}:duration=first:dropout_transition=0[ao]"
                    self.video_widget.set_audio_complex_filter(filter_str)

            self.update_trim_info()

    def activate_timeline_mode(self):
        was_active = self.is_timeline_mode and self._play_uses_timeline_edl
        was_playing = self.video_widget and not self.video_widget.is_paused()
        # FIX: Also reload if mpv has no file loaded (happens after project load or if EDL failed)
        needs_reload = not was_active or not getattr(self.video_widget, 'current_file', None)
        
        self.is_timeline_mode = True
        self._play_uses_timeline_edl = True
        self.trim_info.setText("Timeline Mode Active - Click Play to Preview Sequence")
        
        if needs_reload:
            self.load_timeline_sequence(play=was_playing)

    def load_timeline_sequence(self, play=False):
        if not self.timeline.clips:
            if self.video_widget:
                self.video_widget.stop()
            return

        sorted_clips = sorted(self.timeline.clips, key=lambda c: c.start_time)
        edl_content = "# mpv EDL v0\n"
        for clip in sorted_clips:
            length = clip.get_trimmed_duration()
            if length <= 0.01:
                continue
            src = self.proxy_manager.get_proxy(clip.file_path)
            if not os.path.exists(src):
                src = clip.file_path
            if not os.path.exists(src):
                continue
            fp = src.replace('\\', '/')
            fp_bytes = fp.encode('utf-8')
            edl_content += f"%{len(fp_bytes)}%{fp},{clip.in_point},{length}\n"

        try:
            fd, path = tempfile.mkstemp(suffix='.edl')
            os.close(fd)
            with open(path, 'w', encoding='utf-8', newline='') as f:
                f.write(edl_content)

            self.video_widget.set_audio_complex_filter("")

            edl_path = path.replace('\\', '/')
            # FIX: Clamp playhead so we don't seek past end (black screen)
            timeline_dur = self.timeline.get_timeline_duration()
            playhead_sec = max(0, min(self.timeline.playhead_position, max(0, timeline_dur - 0.1)))
            seek_ms = int(playhead_sec * 1000)

            # Load voiceover audio files into MPV alongside the video EDL
            vo_clips = getattr(self.timeline, 'audio_clips', [])
            if self.video_widget.mpv and vo_clips:
                # Clear any old external audio files
                try:
                    self.video_widget.mpv['audio-files'] = ''
                except Exception:
                    pass

            if self.video_widget.load_file(edl_path, seek_ms=seek_ms):
                # Add voiceover files as external audio tracks
                for vo in vo_clips:
                    if os.path.exists(vo.file_path):
                        try:
                            self.video_widget.mpv.audio_add(vo.file_path)
                        except Exception:
                            pass

                # Set timeline-derived duration immediately as fallback
                # so scrubber/timecode work before MPV's async observer fires
                timeline_dur_ms = int(self.timeline.get_timeline_duration() * 1000)
                if self.video_widget._duration_ms <= 0:
                    self.video_widget._duration_ms = timeline_dur_ms
                    self.video_widget.durationChanged.emit(timeline_dur_ms)
                if play:
                    self.video_widget.play()
                    self.play_btn.setText("â¸ï¸ Pause")
                else:
                    self.video_widget.pause()
                    self.play_btn.setText("â–¶ï¸ Play")
                self.update_live_preview_filters()
        except Exception as e:
            self.status_label.setText(f"Timeline preview error: {e}")
            try:
                import traceback
                print(f"load_timeline_sequence error: {traceback.format_exc()}")
            except:
                pass

    def on_timeline_clip_selected(self, clip):
        self.is_timeline_mode = True
        self._play_uses_timeline_edl = False

        while len(clip.normalization) < len(clip.volumes):
            clip.normalization.append(False)

        if clip.volumes:
            if len(clip.volumes) > 0:
                self.track1_slider.setValue(int(clip.volumes[0]))
                self.track1_norm.setChecked(clip.normalization[0])
            if len(clip.volumes) > 1:
                self.track2_slider.setValue(int(clip.volumes[1]))
                self.track2_norm.setChecked(clip.normalization[1])

        if hasattr(clip, 'sync_offset') and clip.sync_offset != 0:
            self.sync_status_label.setText(f"Sync: {clip.sync_offset:+d}ms")
        else:
            self.sync_status_label.setText("")

        seek_ms = int(clip.in_point * 1000)
        if self.video_widget.load_file(self.proxy_manager.get_proxy(clip.file_path), seek_ms=seek_ms):
            self.video_widget.pause()
            self.apply_audio_mix_preview(clip.file_path, clip.volumes, clip.normalization)
            self.update_live_preview_filters()

        in_tc = self.format_timecode(int(clip.in_point * 1000))
        out_tc = self.format_timecode(int(clip.out_point * 1000))
        dur_tc = self.format_timecode(int(clip.get_trimmed_duration() * 1000))
        self.trim_info.setText(f"Selected: {clip.name} | In: {in_tc} | Out: {out_tc}")

    def update_clip_volume(self):
        self.t1_val.setText(f"{self.track1_slider.value()} dB")
        self.t2_val.setText(f"{self.track2_slider.value()} dB")

        if self.timeline.selected_clip:
            clip = self.timeline.selected_clip
            while len(clip.volumes) < 2:
                clip.volumes.append(0.0)
                clip.normalization.append(False)

            clip.volumes[0] = float(self.track1_slider.value())
            clip.normalization[0] = self.track1_norm.isChecked()

            clip.volumes[1] = float(self.track2_slider.value())
            clip.normalization[1] = self.track2_norm.isChecked()

            self.apply_audio_mix_preview(clip.file_path, clip.volumes, clip.normalization)

    def apply_audio_mix_preview(self, file_path, volumes, normalization=None):
        if not self.video_widget: return

        n_streams = get_audio_stream_count_static(file_path)

        if n_streams > 1:
            filter_parts = []
            inputs = []
            for i in range(n_streams):
                vol_db = volumes[i] if i < len(volumes) else 0.0
                norm = normalization[i] if normalization and i < len(normalization) else False

                chain = f"volume={vol_db}dB"
                if norm:
                    chain = f"loudnorm,{chain}"

                filter_parts.append(f"[aid{i+1}]{chain}[a{i}]")
                inputs.append(f"[a{i}]")

            input_tags = "".join(inputs)
            filter_str = f"{';'.join(filter_parts)};{input_tags}amix=inputs={n_streams}:duration=first:dropout_transition=0[ao]"

            self.video_widget.set_audio_complex_filter(filter_str)
        else:
            vol_db = volumes[0] if volumes else 0.0
            norm = normalization[0] if normalization else False
            chain = f"volume={vol_db}dB"
            if norm:
                chain = f"loudnorm,{chain}"
            self.video_widget.set_audio_complex_filter(f"[aid1]{chain}[ao]")

    def auto_sync_audio_tracks(self):
        if not self.timeline.selected_clip:
            QMessageBox.warning(self, "No Clip Selected", "Please select a clip on the timeline first.")
            return

        clip = self.timeline.selected_clip

        if clip.audio_streams < 2:
            QMessageBox.warning(
                self,
                "Insufficient Audio Tracks",
                f"This clip only has {clip.audio_streams} audio track(s)."
                "Auto-sync requires at least 2 audio tracks:"
                "â€¢ Track 0: Reference (usually desktop audio)"
                "â€¢ Track 1: To sync (usually microphone)"
            )
            return

        reply = QMessageBox.question(
            self,
            "Auto-Sync Audio",
            f"Analyze audio sync for: {clip.name}"
            "This will analyze the first 30 seconds to detect"
            "the sync offset between audio tracks."
            "Track 0 (desktop) will be used as reference."
            "Track 1 (mic) will be synchronized."
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        # FIX: Use QProgressDialog instead of QMessageBox to prevent Wayland ghost-window freeze
        progress = QProgressDialog("Extracting audio tracks...This may take 10-30 seconds.", None, 0, 0, self)
        progress.setWindowTitle("Analyzing Audio Sync")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()

        def update_progress(message):
            progress.setLabelText(message)
            QApplication.processEvents()

        try:
            offset_ms, confidence = auto_sync_audio(
                clip.file_path,
                track1=0,
                track2=1,
                sample_duration=30,
                progress_callback=update_progress
            )

            # FIX: Force destroy the progress dialog so the window manager gives focus to the results
            progress.hide()
            progress.deleteLater()
            QApplication.processEvents()
            time.sleep(0.1) # Yield to Wayland compositor to map out window
            QApplication.processEvents()

            confidence_pct = int(confidence * 100)

            if offset_ms > 0:
                explanation = f"Track 1 (mic) is {offset_ms}ms LATE"
            elif offset_ms < 0:
                explanation = f"Track 1 (mic) is {abs(offset_ms)}ms EARLY"
            else:
                explanation = "Tracks are already in sync!"

            if confidence >= 0.7:
                conf_emoji = "âœ…"
                conf_text = "High"
            elif confidence >= 0.4:
                conf_emoji = "âš ï¸"
                conf_text = "Medium"
            else:
                conf_emoji = "âŒ"
                conf_text = "Low"

            result = QMessageBox(self)
            result.setWindowTitle("Audio Sync Detected")
            result.setText(
                f"<b>Sync Offset Detected:</b><br><br>"
                f"<b style='color: #00d9ff; font-size: 16pt;'>{offset_ms:+d} ms</b><br><br>"
                f"{explanation}<br><br>"
                f"Confidence: {conf_emoji} {conf_text} ({confidence_pct}%)<br><br>"
                f"<i>Apply this offset to synchronize the tracks?</i>"
            )
            result.setIcon(QMessageBox.Icon.Question)
            result.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)

            if confidence < 0.4:
                result.setInformativeText(
                    "âš ï¸ Low confidence detection!"
                    "The audio tracks may not have enough overlap,"
                    "or the sync offset might be inaccurate."
                    "You can still apply it and adjust manually if needed."
                )

            # FIX: Ensure modal focus
            result.setWindowModality(Qt.WindowModality.ApplicationModal)
            apply = result.exec()

            if apply == QMessageBox.StandardButton.Yes:
                clip.sync_offset = offset_ms
                self.sync_status_label.setText(f"Sync: {offset_ms:+d}ms ({conf_text})")
                self.append_log(f"âœ… Audio sync applied: {offset_ms:+d}ms (confidence: {confidence_pct}%)")
                self.append_log(f"   This offset will be applied during timeline export.")
            else:
                self.append_log(f"Audio sync detected ({offset_ms:+d}ms) but not applied")

        except Exception as e:
            progress.hide()
            progress.deleteLater()
            QApplication.processEvents()
            QMessageBox.critical(
                self,
                "Auto-Sync Failed",
                f"Failed to analyze audio sync:{str(e)}"
                "Make sure the clip has multiple audio tracks"
                "and that FFmpeg is installed."
            )
            self.append_log(f"âŒ Auto-sync failed: {e}")

    def on_timeline_playhead_moved(self, time):
        if not self.video_widget:
            return
        if getattr(self, '_play_uses_timeline_edl', False):
            self.video_widget.seek(int(time * 1000))
        else:
            clip = getattr(self.timeline, 'selected_clip', None)
            if clip:
                clip_time = clip.in_point + (time - clip.start_time)
                clip_time = max(clip.in_point, min(clip.out_point, clip_time))
                self.video_widget.seek(int(clip_time * 1000))

    def toggle_play(self):
        if not self.video_widget:
            return
        if self.is_timeline_mode and self._play_uses_timeline_edl:
            if not self.video_widget.current_file:
                self.load_timeline_sequence(play=True)
                return
            if not self.video_widget.is_paused():
                self.video_widget.pause()
                self.play_btn.setText("â–¶ï¸ Play")
            else:
                self.video_widget.play()
                self.play_btn.setText("â¸ï¸ Pause")
            return
            
        if self.video_widget.is_paused():
            self.video_widget.play()
            self.play_btn.setText("â¸ï¸ Pause")
        else:
            self.video_widget.pause()
            self.play_btn.setText("â–¶ï¸ Play")

    def play_timeline_sequence(self):
        self.load_timeline_sequence(play=True)

    def update_play_button(self):
        if self.video_widget:
            if not self.video_widget.is_paused():
                self.play_btn.setText("â¸ï¸ Pause")
            else:
                self.play_btn.setText("â–¶ï¸ Play")

    def seek_preview(self, value):
        if not self.video_widget:
            return
        dur = self.video_widget.duration()
        if dur <= 0:
            if self.is_timeline_mode and self._play_uses_timeline_edl:
                dur = int(self.timeline.get_timeline_duration() * 1000)
            elif getattr(self, 'current_media', None):
                dur = int(getattr(self.current_media, 'duration', 0) * 1000)
        if dur > 0:
            position_ms = int((value / 1000.0) * dur)
            self.video_widget.seek(position_ms)
            if self.is_timeline_mode and getattr(self, '_play_uses_timeline_edl', False):
                self.timeline.set_playhead_position(position_ms / 1000.0, auto_scroll=True, emit_signal=False)

    def format_timecode(self, ms):
        s = ms // 1000
        h = s // 3600
        m = (s % 3600) // 60
        s = s % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def enter_fullscreen(self):
        pass

    def set_media_in_point(self):
        if self.current_media and self.video_widget:
            self.current_media.in_point = self.video_widget.position() / 1000.0
            if self.current_media.out_point <= self.current_media.in_point:
                self.current_media.out_point = self.current_media.duration
            self.update_trim_info()

    def set_media_out_point(self):
        if self.current_media and self.video_widget:
            self.current_media.out_point = self.video_widget.position() / 1000.0
            if self.current_media.out_point <= self.current_media.in_point:
                self.current_media.in_point = 0
            self.update_trim_info()

    def update_trim_info(self):
        if self.current_media:
            in_tc = self.format_timecode(int(self.current_media.in_point * 1000))
            out_tc = self.format_timecode(int(self.current_media.out_point * 1000))
            dur_tc = self.format_timecode(int(self.current_media.get_trimmed_duration() * 1000))
            self.trim_info.setText(f"In: {in_tc} | Out: {out_tc} | Duration: {dur_tc}")

    def add_to_timeline(self):
        if not self.current_media:
            QMessageBox.warning(self, "No Media", "Select media from library first")
            return
        next_time = 0
        if self.timeline.clips:
            last_clip = max(self.timeline.clips, key=lambda c: c.get_end_time())
            next_time = last_clip.get_end_time()
        clip = TimelineClip(self.current_media.file_path, 0, next_time, self.current_media.in_point, self.current_media.out_point, self.current_media.duration)
        self.timeline.add_clip(clip)
        self.update_timeline_duration()

    def remove_from_timeline(self):
        if self.timeline.selected_clip:
            self.timeline.remove_clip(self.timeline.selected_clip)
            self.update_timeline_duration()

    def clear_timeline(self):
        reply = QMessageBox.question(self, "Clear Timeline", "Remove all clips from timeline?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.timeline.clear_timeline()
            self.update_timeline_duration()

    # --- ACCESSIBILITY AUTOMATION METHODS ---
    def auto_trim_selected(self):
        if not self.timeline.selected_clip:
            QMessageBox.warning(self, "No Clip Selected", "Please click a clip on the timeline first.")
            return
        c = self.timeline.selected_clip
        dur = c.duration
        c.in_point = min(dur, c.in_point + 1.0)
        c.out_point = max(0, c.out_point - 1.0)
        if c.out_point <= c.in_point:
            c.out_point = min(dur, c.in_point + 0.1) # Safe fallback
        self.timeline.update()
        self.update_timeline_duration()
        self.status_label.setText("Auto-Trimmed 1s off edges.")

    def auto_fade_all(self):
        if len(self.timeline.clips) < 2:
            QMessageBox.warning(self, "Not Enough Clips", "Add at least two clips to apply transitions.")
            return
        for i in range(len(self.timeline.clips) - 1):
            c = self.timeline.clips[i]
            c.transition_type = 'fade'
            c.transition_duration = 1.0
        self.timeline.update()
        self.update_timeline_duration()
        self.status_label.setText("Applied 1s fade to all clips.")

    def auto_black_and_white(self):
        # Toggle B&W mode: set saturation slider to minimum (-100) for full desaturation
        if hasattr(self, 'color_saturation_slider'):
            current = self.color_saturation_slider.value()
            if current == -100:
                # Already B&W, toggle it off
                self.color_saturation_slider.setValue(0)
                self.app_settings.setValue('color_bw_mode', False)
                self.status_label.setText("Black & White filter removed.")
                QMessageBox.information(self, "Color Restored", "Color has been restored to all timeline clips.")
            else:
                self.color_saturation_slider.setValue(-100)
                self.app_settings.setValue('color_bw_mode', True)
                self.status_label.setText("Applied Black & White Filter.")
                QMessageBox.information(self, "Black & White", "Black and White filter activated across all timeline clips.")
        
    def auto_normalize_audio(self):
        for c in self.timeline.clips:
            # Add dynamic normalization filter to each clip
            if not hasattr(c, 'normalization'):
                c.normalization = []
            if "dynaudnorm=f=150:g=15" not in c.normalization:
                c.normalization.append("dynaudnorm=f=150:g=15")
        self.timeline.update()
        self.update_timeline_duration()
        self.status_label.setText("Audio Normalized.")

    def add_text_overlay(self):
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Add Text Overlay", "Enter text for the overlay:")
        if not ok or not text.strip():
            return
        start, ok2 = QInputDialog.getDouble(self, "Start Time", "Start time (seconds):", 0.0, 0.0, 99999.0, 1)
        if not ok2:
            return
        dur, ok3 = QInputDialog.getDouble(self, "Duration", "Duration (seconds):", 5.0, 0.5, 300.0, 1)
        if not ok3:
            return
        tc = TextClip(text.strip(), start, dur)
        self.timeline.text_clips.append(tc)
        self.timeline.update()
        self.status_label.setText(f"Added text overlay: '{text.strip()[:20]}...'")

    def record_voiceover(self):
        if not SOUNDDEVICE_AVAILABLE:
            QMessageBox.warning(self, "Missing Library",
                "Voice recording requires the 'sounddevice' and 'scipy' packages."
                "Install them by running:"
                "pip install sounddevice scipy")
            return

        # --- STOP ---
        if hasattr(self, '_vo_stream') and self._vo_stream is not None:
            try:
                self._vo_stream.stop()
                self._vo_stream.close()
            except Exception:
                pass
            self._vo_stream = None
            self._finish_voiceover_recording()
            return

        # --- START ---
        playhead_time = self.timeline.playhead_position
        reply = QMessageBox.question(self, "Record Voiceover",
            f"Start recording voiceover at playhead position ({playhead_time:.1f}s)?"
            "Press the Record Voiceover button again to stop.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._vo_frames = []
        self._vo_start_time = playhead_time
        self._vo_sample_rate = 48000

        def callback(indata, frame_count, time_info, status):
            self._vo_frames.append(indata.copy())

        try:
            self._vo_stream = sd.InputStream(
                samplerate=self._vo_sample_rate,
                channels=1,
                dtype='float32',
                callback=callback
            )
            self._vo_stream.start()
            self.status_label.setText("ðŸ”´ Recording voiceover... Click 'Record Voiceover' again to stop.")
        except Exception as e:
            self._vo_stream = None
            self.status_label.setText(f"âŒ Failed to start recording: {e}")

    def _finish_voiceover_recording(self):
        import numpy as np
        if not self._vo_frames:
            self.status_label.setText("âš ï¸ No audio captured - check your microphone.")
            return
        try:
            recording = np.concatenate(self._vo_frames, axis=0)
            tmp_path = os.path.join(tempfile.gettempdir(), f"vo_{int(time.time())}.wav")
            _scipy_wav.write(tmp_path, self._vo_sample_rate, recording)
            duration = len(recording) / self._vo_sample_rate
            ac = AudioClip(tmp_path, self._vo_start_time, duration)
            self.timeline.audio_clips.append(ac)
            self.timeline.update()
            self.status_label.setText(f"âœ… Voiceover recorded: {duration:.1f}s at {self._vo_start_time:.1f}s")
        except Exception as e:
            self.status_label.setText(f"âŒ Failed to save recording: {e}")

    def update_timeline_duration(self):
        if self.timeline.clips:
            self.timeline_duration = sum(clip.get_trimmed_duration() for clip in self.timeline.clips)
        else:
            self.timeline_duration = 0
                
        # Keep EDL synced if we are currently looking at the full timeline
        if getattr(self, 'is_timeline_mode', False) and getattr(self, '_play_uses_timeline_edl', False):
            self.load_timeline_sequence(play=False)

    def zoom_in_timeline(self):
        self.timeline.zoom_in()

    def zoom_out_timeline(self):
        self.timeline.zoom_out()







    def export_timeline(self):
        if not self.timeline.clips:
            QMessageBox.warning(self, "Empty Timeline", "Add clips to timeline before exporting")
            return

        # Show ExportDialog FIRST (before file explorer)
        dialog = ExportDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        # Save the dialog overrides so get_settings() picks them up
        dialog_settings = dialog.get_settings()
        self.app_settings.setValue('export_settings_override', dialog_settings)

        # NOW show file explorer
        ext = get_export_extension_for_codec(dialog_settings['video_codec'])
        output_file, _ = QFileDialog.getSaveFileName(self, "Choose Export Location", f"timeline_export{ext}", f"Video Files (*{ext})")
        if not output_file:
            return

        settings = self.get_settings()
        filters_state = "ON" if settings.get('has_optional_filters') else "OFF"

        self.render_dialog = RenderProgressDialog(self)
        self.render_dialog.cancel_btn.clicked.connect(self.stop_timeline_export)

        self.export_timeline_btn.setEnabled(False)
        self.stop_export_btn.setEnabled(True)
        self.timeline_export_thread = TimelineExportThread(self.timeline, output_file, settings)
        self.timeline_export_thread.progress.connect(self.progress_bar.setValue)
        self.timeline_export_thread.progress.connect(self.render_dialog.progress_bar.setValue)
        self.timeline_export_thread.status.connect(self.status_label.setText)
        self.timeline_export_thread.status.connect(self.render_dialog.status_label.setText)
        self.timeline_export_thread.log_message.connect(self.append_log)
        self.timeline_export_thread.log_message.connect(self.render_dialog.log_text.append)
        self.timeline_export_thread.finished.connect(self.timeline_export_done)
        self.timeline_export_thread.playhead_update.connect(self.timeline.set_playhead_position)
        self.progress_bar.setValue(0)
        
        self.status_label.setText(f"Exporting: {settings['video_codec'].upper()} | Filters: {filters_state}")
        self.render_dialog.show()
        self.timeline_export_thread.start()

    def timeline_export_done(self, success, msg):
        self.export_timeline_btn.setEnabled(True)
        self.stop_export_btn.setEnabled(False)

        if hasattr(self, 'render_dialog') and self.render_dialog:
            self.render_dialog.cancel_btn.setText("Close")
            self.render_dialog.cancel_btn.setStyleSheet("background-color: #3b82f6; color: white; padding: 8px 20px; font-size: 11pt; font-weight: bold; border-radius: 6px;")
            self.render_dialog.cancel_btn.clicked.disconnect()
            self.render_dialog.cancel_btn.clicked.connect(self.render_dialog.accept)
            if success:
                self.render_dialog.status_label.setText("Render Complete!")
            else:
                self.render_dialog.status_label.setText("Render Failed or Stopped.")

        if success:
            QMessageBox.information(self, "Export Complete", msg)
            self.progress_bar.setValue(100)
            if hasattr(self, 'render_dialog'):
                self.render_dialog.progress_bar.setValue(100)
        else:
            if "stopped" not in msg.lower():
                QMessageBox.warning(self, "Export Failed", msg)
        self.status_label.setText("Ready")

    def stop_timeline_export(self):
        if self.timeline_export_thread and self.timeline_export_thread.isRunning():
            self.status_label.setText("Stopping render...")
            self.stop_export_btn.setEnabled(False)

            self.timeline_export_thread.stop()

            if not self.timeline_export_thread.wait(5000):
                self.timeline_export_thread.terminate()
                self.timeline_export_thread.wait()

            self.export_timeline_btn.setEnabled(True)
            self.stop_export_btn.setEnabled(False)
            self.status_label.setText("Render stopped")
            self.log_text.append("=== Render cancelled by user ===")

    def apply_theme(self):
        self.setStyleSheet("""
            QMainWindow {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0a0e1a, stop:0.5 #111827, stop:1 #0a0e1a);
            }
            QWidget {
                background-color: transparent;
                color: #00d9ff;
                font-size: 10pt;
            }
            QLabel {
                color: #00d9ff;
            }
            QGroupBox {
                font-weight: bold;
            }

            /* Scrollbar with depth */
            QScrollBar:vertical {
                background: #0a0e1a;
                width: 14px;
                border: 1px solid #1a2332;
                border-radius: 7px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00d9ff, stop:1 #4ade80);
                border: 2px solid #00d9ff;
                border-radius: 5px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #00f0ff;
                border: 2px solid #fff;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }

            /* High Contrast Focus */
            *:focus {
                border: 4px solid #f59e0b;
                outline: none;
            }
        """)

    def tab_style(self):
        return """
            QTabWidget::pane {
                border: 3px solid #00d9ff;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0f1419, stop:1 #0a0e1a);
                border-radius: 12px;
                border-top-left-radius: 0px;
                /* Panel depth */
                box-shadow:
                    inset 0 2px 4px rgba(0, 0, 0, 0.5),
                    0 4px 8px rgba(0, 0, 0, 0.3);
                margin-top: 4px;
            }
            QTabBar::tab {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1a2332, stop:1 #111827);
                color: #00d9ff;
                padding: 14px 30px;
                margin: 0px 2px;
                border: 2px solid #1a2332;
                border-bottom: none;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
                font-size: 11pt;
                font-weight: bold;
                /* Tab depth */
                box-shadow: 0 -2px 4px rgba(0, 0, 0, 0.3);
            }
            QTabBar::tab:selected {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #00f0ff, stop:0.5 #00d9ff, stop:1 #4ade80);
                color: #000;
                border: 3px solid #00f0ff;
                border-bottom: none;
                padding-bottom: 2px;
                /* Selected tab pops forward */
                box-shadow:
                    0 -4px 8px rgba(0, 0, 0, 0.4),
                    0 0 15px rgba(0, 240, 255, 0.6);
                font-weight: bold;
            }
            QTabBar::tab:hover:!selected {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1a2332, stop:1 #0f1419);
                border: 2px solid #00d9ff;
                border-bottom: none;
                box-shadow: 0 -2px 6px rgba(0, 217, 255, 0.3);
            }
            QTabBar::tab:focus {
                border: 3px solid #f59e0b;
                border-bottom: none;
            }
        """

    def groupbox_style(self):
        return """
            QGroupBox {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1a1f2e, stop:1 #0f1419);
                border: 3px solid #00d9ff;
                border-radius: 12px;
                padding: 25px 15px 15px 15px;
                margin-top: 20px;
                font-size: 12pt;
                font-weight: bold;
                color: #00f0ff;
                /* 3D depth effect */
                box-shadow:
                    0 4px 6px rgba(0, 0, 0, 0.5),
                    inset 0 1px 0 rgba(0, 217, 255, 0.3),
                    0 0 20px rgba(0, 217, 255, 0.2);
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 8px 20px;
                margin-left: 10px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00d9ff, stop:0.5 #00f0ff, stop:1 #00d9ff);
                color: #000;
                border-radius: 6px;
                font-weight: bold;
                border: 2px solid #00f0ff;
                /* Title depth */
                box-shadow:
                    0 2px 4px rgba(0, 0, 0, 0.6),
                    0 0 10px rgba(0, 240, 255, 0.5);
            }
        """

    def button_style(self, color):
        color_map = {
            '#4ade80': '#00f0ff',
            '#3b82f6': '#4ade80',
            '#ef4444': '#ff0066',
            '#f59e0b': '#ffaa00',
        }
        cyber_color = color_map.get(color, color)

        hover = self.brighten(cyber_color, 1.3)
        pressed = self.brighten(cyber_color, 0.7)

        return f"""
            QPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {hover}, stop:0.5 {cyber_color}, stop:1 {pressed});
                color: #000;
                border: 3px solid {cyber_color};
                border-radius: 10px;
                padding: 12px 24px;
                font-size: 11pt;
                font-weight: bold;
                /* 3D button depth */
                box-shadow:
                    0 4px 6px rgba(0, 0, 0, 0.4),
                    inset 0 1px 0 rgba(255, 255, 255, 0.3),
                    0 0 10px rgba(0, 217, 255, 0.3);
            }}
            QPushButton:hover {{
                background: {cyber_color};
                border: 3px solid #fff;
                /* Hover glow */
                box-shadow:
                    0 6px 12px rgba(0, 0, 0, 0.5),
                    0 0 20px {cyber_color},
                    inset 0 1px 0 rgba(255, 255, 255, 0.5);
            }}
            QPushButton:pressed {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {pressed}, stop:1 {cyber_color});
                border: 4px solid {cyber_color};
                /* Pressed inset */
                box-shadow:
                    inset 0 3px 6px rgba(0, 0, 0, 0.6),
                    0 0 15px {cyber_color};
                padding-top: 14px;
                padding-bottom: 10px;
            }}
            QPushButton:disabled {{
                background: #1a1f2e;
                color: #4a5568;
                border: 2px solid #2d3748;
                box-shadow: none;
            }}
            QPushButton:focus {{
                border: 4px solid #f59e0b;
                box-shadow: 0 0 15px #f59e0b;
            }}
        """

    def list_style(self):
        return """
            QListWidget {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0f1419, stop:1 #0a0e1a);
                border: 3px solid #00d9ff;
                border-radius: 10px;
                padding: 8px;
                font-size: 10pt;
                color: #00f0ff;
                font-weight: bold;
                /* List depth */
                box-shadow:
                    inset 0 2px 4px rgba(0, 0, 0, 0.5),
                    0 4px 8px rgba(0, 0, 0, 0.3);
            }
            QListWidget::item {
                padding: 12px;
                border-radius: 6px;
                border: 1px solid transparent;
                margin: 2px;
            }
            QListWidget::item:selected {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00d9ff, stop:0.5 #00f0ff, stop:1 #4ade80);
                color: #000;
                font-weight: bold;
                border: 2px solid #fff;
                /* Selected item pops forward */
                box-shadow:
                    0 3px 6px rgba(0, 0, 0, 0.4),
                    0 0 10px rgba(0, 240, 255, 0.5);
            }
            QListWidget::item:hover {
                background: #1a2332;
                border: 2px solid #00d9ff;
                box-shadow: 0 2px 4px rgba(0, 217, 255, 0.3);
            }
            QListWidget:focus {
                border: 4px solid #f59e0b;
                box-shadow: 0 0 15px #f59e0b;
            }
        """

    def slider_style(self):
        return """
            QSlider::groove:horizontal {
                border: 2px solid #1a2332;
                height: 10px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0a0e1a, stop:1 #1a1f2e);
                border-radius: 5px;
                /* Inset groove */
                box-shadow:
                    inset 0 2px 4px rgba(0, 0, 0, 0.5),
                    0 1px 0 rgba(255, 255, 255, 0.1);
            }
            QSlider::handle:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #00f0ff, stop:0.5 #4ade80, stop:1 #00d9ff);
                border: 3px solid #fff;
                width: 22px;
                height: 22px;
                margin: -8px 0;
                border-radius: 11px;
                /* 3D handle */
                box-shadow:
                    0 3px 6px rgba(0, 0, 0, 0.5),
                    inset 0 1px 0 rgba(255, 255, 255, 0.5),
                    0 0 10px rgba(0, 240, 255, 0.4);
            }
            QSlider::handle:horizontal:hover {
                background: #00f0ff;
                border: 3px solid #fff;
                width: 26px;
                height: 26px;
                margin: -10px 0;
                /* Hover glow */
                box-shadow:
                    0 4px 8px rgba(0, 0, 0, 0.6),
                    0 0 20px rgba(0, 240, 255, 0.8),
                    inset 0 1px 0 rgba(255, 255, 255, 0.6);
            }
            QSlider::sub-page:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #00d9ff, stop:1 #4ade80);
                border-radius: 5px;
                /* Progress glow */
                box-shadow: 0 0 5px rgba(0, 217, 255, 0.5);
            }
            QSlider::handle:horizontal:focus {
                border: 4px solid #f59e0b;
                box-shadow: 0 0 15px #f59e0b;
            }
        """

    def combo_style(self):
        return """
            QComboBox {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1a1f2e, stop:1 #0f1419);
                border: 2px solid #00d9ff;
                border-radius: 8px;
                padding: 8px 10px;
                font-size: 10pt;
                color: #00f0ff;
                font-weight: bold;
                /* Dropdown depth */
                box-shadow:
                    inset 0 2px 4px rgba(0, 0, 0, 0.3),
                    0 2px 4px rgba(0, 0, 0, 0.3);
            }
            QComboBox:hover {
                border: 2px solid #00f0ff;
                box-shadow:
                    inset 0 2px 4px rgba(0, 0, 0, 0.3),
                    0 0 10px rgba(0, 217, 255, 0.4);
            }
            QComboBox::drop-down {
                border: none;
                width: 32px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #00f0ff, stop:1 #00d9ff);
                border-top-right-radius: 6px;
                border-bottom-right-radius: 6px;
                /* Button depth */
                box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.3);
            }
            QComboBox::drop-down:hover {
                background: #00f0ff;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 6px solid transparent;
                border-right: 6px solid transparent;
                border-top: 10px solid #000;
                margin-right: 10px;
            }
            QComboBox QAbstractItemView {
                background: #0f1419;
                border: 3px solid #00d9ff;
                selection-background-color: #00f0ff;
                selection-color: #000;
                color: #00f0ff;
                padding: 8px;
                font-weight: bold;
                /* Dropdown menu depth */
                box-shadow:
                    0 8px 16px rgba(0, 0, 0, 0.6),
                    inset 0 1px 0 rgba(0, 217, 255, 0.2);
            }
            QComboBox QAbstractItemView::item {
                padding: 10px;
                border-bottom: 1px solid #1a2332;
                border-radius: 4px;
            }
            QComboBox QAbstractItemView::item:hover {
                background: #1a2332;
                border: 1px solid #00d9ff;
            }
            QComboBox:focus {
                border: 3px solid #f59e0b;
                box-shadow: 0 0 15px #f59e0b;
            }
        """

    def spinbox_style(self):
        return """
            QSpinBox, QDoubleSpinBox {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #1a1f2e, stop:1 #0f1419);
                border: 2px solid #00d9ff;
                border-radius: 8px;
                padding: 8px;
                font-size: 10pt;
                color: #00f0ff;
                font-weight: bold;
                /* Spinbox depth */
                box-shadow:
                    inset 0 2px 4px rgba(0, 0, 0, 0.3),
                    0 2px 4px rgba(0, 0, 0, 0.3);
            }
            QSpinBox:hover, QDoubleSpinBox:hover {
                border: 2px solid #00f0ff;
                box-shadow:
                    inset 0 2px 4px rgba(0, 0, 0, 0.3),
                    0 0 10px rgba(0, 217, 255, 0.4);
            }
            QSpinBox::up-button, QSpinBox::down-button,
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
                width: 26px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #00f0ff, stop:1 #00d9ff);
                border: none;
                box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.3);
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover,
            QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
                background: #00f0ff;
            }
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-bottom: 8px solid #000;
            }
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 8px solid #000;
            }
            QSpinBox:focus, QDoubleSpinBox:focus {
                border: 3px solid #f59e0b;
                box-shadow: 0 0 15px #f59e0b;
            }
        """

    def brighten(self, hex_color, factor):
        hex_color = hex_color.lstrip('#')
        r, g, b = [int(hex_color[i:i+2], 16) for i in (0, 2, 4)]
        r, g, b = [min(255, max(0, int(c * factor))) for c in (r, g, b)]
        return f"#{r:02x}{g:02x}{b:02x}"

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Videos", "", "Videos (*.mp4 *.mov *.avi *.mkv *.mts *.m2ts);;All (*.*)")
        for f in files:
            if f not in self.input_files:
                self.input_files.append(f)
                self.file_list.addItem(Path(f).name)

    def remove_selected(self):
        row = self.file_list.currentRow()
        if row >= 0:
            self.file_list.takeItem(row)
            del self.input_files[row]

    def clear_files(self):
        self.input_files.clear()
        self.file_list.clear()

    def select_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output")
        if folder:
            self.output_folder = folder
            self.output_label.setText(folder)
            self.save_settings()

    def reset_all(self):
        if hasattr(self, 'lift_wheel'):
            for w in (self.lift_wheel, self.gamma_wheel, self.gain_wheel):
                w.blockSignals(True)
                w.reset(emit=False)
                w.blockSignals(False)
        if hasattr(self, 'cinema_scope_check'):
            self.cinema_scope_check.setChecked(False)
        # Reset sidebar filter combos to defaults
        if hasattr(self, 'denoise_combo'): self.denoise_combo.setCurrentIndex(0)
        if hasattr(self, 'deflicker_combo'): self.deflicker_combo.setCurrentIndex(0)
        if hasattr(self, 'exposure_combo'): self.exposure_combo.setCurrentIndex(0)
        if hasattr(self, 'temporal_combo'): self.temporal_combo.setCurrentIndex(0)
        if hasattr(self, 'sharpness_combo'): self.sharpness_combo.setCurrentIndex(0)
        # Reset color grading sliders
        if hasattr(self, 'color_brightness_slider'): self.color_brightness_slider.setValue(0)
        if hasattr(self, 'color_contrast_slider'): self.color_contrast_slider.setValue(0)
        if hasattr(self, 'color_saturation_slider'): self.color_saturation_slider.setValue(0)
        if hasattr(self, 'color_gamma_slider'): self.color_gamma_slider.setValue(0)
        # Clear B&W mode
        self.app_settings.setValue('color_bw_mode', False)
        self.update_live_preview_filters()
        self.save_settings()

    def get_settings(self):
        settings = {
            'video_codec': 'hevc_nvenc', 'prores_profile': 0, 'pixel_format': 1, 'export_target_index': 0,
            'audio_codec': 'aac', 'use_gpu': True, 'use_gpu_decode': True, 'threads': 0,
            'bitrate_mbps': 100, 'cq_value': 18, 'rate_control': 'cbr',
            'timeline_fps': 60.0, 'export_res_index': 0, 'scale_algo': 'lanczos',
        }
        settings['denoise_level'] = self.denoise_combo.currentIndex() if hasattr(self, 'denoise_combo') else 0
        settings['deflicker_level'] = self.deflicker_combo.currentIndex() if hasattr(self, 'deflicker_combo') else 0
        settings['exposure_level'] = self.exposure_combo.currentIndex() if hasattr(self, 'exposure_combo') else 0
        settings['temporal_level'] = self.temporal_combo.currentIndex() if hasattr(self, 'temporal_combo') else 0
        settings['sharpness_level'] = self.sharpness_combo.currentIndex() if hasattr(self, 'sharpness_combo') else 0
        settings['color_bw_mode'] = self.app_settings.value('color_bw_mode', False, type=bool)
        settings['color_brightness'] = self.color_brightness_slider.value() if hasattr(self, 'color_brightness_slider') else 0
        settings['color_contrast'] = self.color_contrast_slider.value() if hasattr(self, 'color_contrast_slider') else 0
        settings['color_saturation'] = self.color_saturation_slider.value() if hasattr(self, 'color_saturation_slider') else 0
        settings['color_gamma'] = self.color_gamma_slider.value() if hasattr(self, 'color_gamma_slider') else 0
        
        settings['cinema_scope'] = self.cinema_scope_check.isChecked() if hasattr(self, 'cinema_scope_check') else False
        if hasattr(self, 'lift_wheel'):
            settings['lift_x'], settings['lift_y'] = self.lift_wheel.cursor_pos.x(), -self.lift_wheel.cursor_pos.y()
            settings['gamma_x'], settings['gamma_y'] = self.gamma_wheel.cursor_pos.x(), -self.gamma_wheel.cursor_pos.y()
            settings['gain_x'], settings['gain_y'] = self.gain_wheel.cursor_pos.x(), -self.gain_wheel.cursor_pos.y()
        else:
            settings['lift_x'], settings['lift_y'] = 0, 0
            settings['gamma_x'], settings['gamma_y'] = 0, 0
            settings['gain_x'], settings['gain_y'] = 0, 0

        settings['has_optional_filters'] = has_optional_video_filters(settings)
        overrides = self.app_settings.value('export_settings_override', {})
        if overrides and isinstance(overrides, dict):
            settings.update(overrides)
        return settings
    def start_encoding(self):
        if not self.input_files:
            QMessageBox.warning(self, "No Files", "Add files")
            return
        if not self.output_folder:
            QMessageBox.warning(self, "No Output", "Select folder")
            return
        self.current_file_index = 0
        self.encode_next()

    def encode_next(self):
        if self.current_file_index >= len(self.input_files):
            self.encoding_done(True, f"All {len(self.input_files)} done!")
            return
        inp = self.input_files[self.current_file_index]
        settings = self.get_settings()
        ext = get_export_extension_for_codec(settings.get('video_codec', 'hevc_nvenc'))
        out_name = f"{Path(inp).stem}_encoded{ext}"
        out_path = os.path.join(self.output_folder, out_name)
        counter = 1
        while os.path.exists(out_path):
            out_name = f"{Path(inp).stem}_encoded_{counter}{ext}"
            out_path = os.path.join(self.output_folder, out_name)
            counter += 1
        self.file_label.setText(f"File {self.current_file_index + 1}/{len(self.input_files)}: {Path(inp).name}")
        self.encoding_thread = EncodingThread(inp, out_path, settings)
        self.encoding_thread.progress.connect(self.progress_bar.setValue)
        self.encoding_thread.status.connect(self.status_label.setText)
        self.encoding_thread.log_message.connect(self.append_log)
        self.encoding_thread.finished.connect(self.file_done)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setValue(0)
        self.encoding_thread.start()

    def file_done(self, success, msg):
        if success:
            self.current_file_index += 1
            self.encode_next()
        else:
            self.encoding_done(False, msg)

    def stop_encoding(self):
        if self.encoding_thread:
            self.encoding_thread.stop()
            self.encoding_thread.wait()
        self.encoding_done(False, "Stopped")

    def append_log(self, text):
        self.log_text.append(text)
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def encoding_done(self, success, msg):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if success:
            QMessageBox.information(self, "Complete", msg)
            self.progress_bar.setValue(100)
        elif "stopped" not in msg.lower():
            QMessageBox.warning(self, "Issue", msg)
        self.status_label.setText("Ready")
        self.file_label.setText("")
        self.current_file_index = 0

    def save_settings(self):
        self.app_settings.setValue("output_folder", self.output_folder)

    def load_settings(self):
        pass

    def save_project(self):
        if not self.timeline.clips:
            QMessageBox.information(self, "Info", "Timeline is empty")
            return

        file_path, _ = QFileDialog.getSaveFileName(self, "Save Project", "project.fep", "FastEncode Projects (*.fep)")
        if not file_path:
            return

        project_data = {
            "version": __version__,
            "clips": [clip.to_dict() for clip in self.timeline.clips],
            "settings": self.get_settings()
        }

        try:
            with open(file_path, 'w') as f:
                json.dump(project_data, f, indent=4)
            self.status_label.setText(f"Project saved: {Path(file_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save project: {e}")

    def load_project(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Load Project", "", "FastEncode Projects (*.fep)")
        if not file_path:
            return

        try:
            with open(file_path, 'r') as f:
                project_data = json.load(f)

            self.timeline.clear_timeline()
            # FIX: Also clear and rebuild media pool (media_library) so clips show up after loading
            self.media_list.clear()
            self.media_library.clear()

            for clip_data in project_data.get("clips", []):
                clip = TimelineClip.from_dict(clip_data)
                # FIX: Offer relink dialog if file missing, instead of just skipping
                if not os.path.exists(clip.file_path):
                    reply = QMessageBox.question(
                        self, "Missing Media",
                        f"Could not find media:\n{clip.file_path}\n\nWould you like to locate it?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    )
                    if reply == QMessageBox.StandardButton.Yes:
                        new_path, _ = QFileDialog.getOpenFileName(
                            self, f"Locate {Path(clip.file_path).name}", "", "Videos (*.mp4 *.mov *.avi *.mkv *.mts *.m2ts);;All (*.*)"
                        )
                        if new_path and os.path.exists(new_path):
                            clip.file_path = new_path
                        else:
                            continue
                    else:
                        continue
                self.timeline.add_clip(clip)
                # FIX: Add to media pool if not already there
                if not any(m.file_path == clip.file_path for m in self.media_library):
                    try:
                        media_item = MediaLibraryItem(clip.file_path)
                        self.media_library.append(media_item)
                        self.media_list.addItem(media_item.name)
                    except Exception:
                        pass
                self.proxy_manager.add_job(clip.file_path)

            self.update_timeline_duration()
            # FIX: Auto-activate timeline mode so empty-space click and Play work immediately
            if self.timeline.clips:
                self.activate_timeline_mode()
            self.status_label.setText(f"Project loaded: {Path(file_path).name}")

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load project: {e}")

    def clear_all_proxies(self):
        count, size_mb = self.proxy_manager.get_proxy_disk_usage()
        dir_path = self.proxy_manager.get_proxy_dir()
        if count == 0:
            QMessageBox.information(self, "Proxies", f"No proxies to delete.\nFolder: {dir_path}")
            return
        reply = QMessageBox.question(
            self, "Clear Proxies",
            f"Delete {count} proxy files ({size_mb:.1f} MB)?\n\nLocation:\n{dir_path}\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            deleted = self.proxy_manager.clear_all_proxies()
            self.status_label.setText(f"Cleared {deleted} proxy files â€” freed {size_mb:.1f} MB")


    def closeEvent(self, event):
        self.save_settings()

        if self.video_widget:
            try:
                self.video_widget.shutdown()
            except:
                pass

        if self.encoding_thread and self.encoding_thread.isRunning():
            reply = QMessageBox.question(self, "Active", "Stop and quit?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self.encoding_thread.stop()
            self.encoding_thread.wait()
        if self.timeline_export_thread and self.timeline_export_thread.isRunning():
            reply = QMessageBox.question(self, "Export Active", "Stop export and quit?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self.timeline_export_thread.stop()
            self.timeline_export_thread.wait()
        event.accept()


    def update_live_preview_filters(self):
        if not hasattr(self, 'video_widget') or not self.video_widget:
            return
        settings = self.get_settings()
        filters = build_video_filters_from_settings(settings)
        filter_str = "lavfi=[" + ",".join(filters) + "]" if filters else ""
        self.video_widget.set_video_filter(filter_str)

    def apply_auto_balance(self):
        self.status_label.setText("Analyzing timeline clips for auto balance...")
        QApplication.processEvents()
        result = analyze_timeline_auto_balance(self.timeline)
        if result and hasattr(self, 'lift_wheel'):
            self.lift_wheel.cursor_pos = QPointF(result['lift_x'], -result['lift_y'])
            self.gamma_wheel.cursor_pos = QPointF(result['gamma_x'], -result['gamma_y'])
            self.gain_wheel.cursor_pos = QPointF(result['gain_x'], -result['gain_y'])
            self.lift_wheel.update()
            self.gamma_wheel.update()
            self.gain_wheel.update()
            if hasattr(self, 'color_brightness_slider'):
                self.color_brightness_slider.setValue(result.get('color_brightness', 0))
            if hasattr(self, 'color_contrast_slider'):
                self.color_contrast_slider.setValue(result.get('color_contrast', 10))
            if hasattr(self, 'color_saturation_slider'):
                self.color_saturation_slider.setValue(result.get('color_saturation', 15))
        elif hasattr(self, 'lift_wheel'):
            self.lift_wheel.cursor_pos = QPointF(-0.05, -0.05)
            self.gamma_wheel.cursor_pos = QPointF(0.02, 0.05)
            self.gain_wheel.cursor_pos = QPointF(0.1, 0.0)
            self.lift_wheel.update()
            self.gamma_wheel.update()
            self.gain_wheel.update()
            if hasattr(self, 'color_contrast_slider'):
                self.color_contrast_slider.setValue(10)
            if hasattr(self, 'color_saturation_slider'):
                self.color_saturation_slider.setValue(15)
        self.update_live_preview_filters()
        self.status_label.setText("AI Auto Color Balance Applied.")

    def reset_all_filters(self):
        if hasattr(self, 'lift_wheel'):
            for w in (self.lift_wheel, self.gamma_wheel, self.gain_wheel):
                w.blockSignals(True)
                w.reset(emit=False)
                w.blockSignals(False)
        if hasattr(self, 'cinema_scope_check'):
            self.cinema_scope_check.setChecked(False)
        if hasattr(self, 'color_brightness_slider'):
            self.color_brightness_slider.setValue(0)
            self.color_contrast_slider.setValue(0)
            self.color_saturation_slider.setValue(0)
            self.color_gamma_slider.setValue(0)
        self.app_settings.setValue('color_bw_mode', False)
        for c in ('denoise_combo', 'deflicker_combo', 'exposure_combo', 'temporal_combo', 'sharpness_combo'):
            if hasattr(self, c):
                getattr(self, c).setCurrentIndex(0)
        self.update_live_preview_filters()
        self.status_label.setText("All Filters Reset.")

def main():
    app = QApplication(sys.argv)
    app.setDesktopFileName("FastEncodePro")
    app.setStyle("Fusion")
    window = FastEncodeProApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
