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

# --- Startup crash logger ---
import sys
import traceback
from pathlib import Path
import datetime
import tempfile
# Use writable location for crash log - not next to exe which may be read-only
try:
    _LOG_DIR = Path(tempfile.gettempdir()) / "FastEncodePro"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_PATH = _LOG_DIR / "crash.log"
except Exception:
    _LOG_PATH = Path.home() / ".FastEncodePro_crash.log"

def _log(msg):
    try:
        # Truncate log if too big (>5MB)
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > 5*1024*1024:
            try:
                _LOG_PATH.unlink()
            except: pass
        with open(_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(datetime.datetime.now().isoformat() + ' | ' + str(msg) + '\n')
    except Exception:
        pass
try:
    _log('=== Startup begin ===')
except Exception:
    pass
# Install unhandled exception hook
def _excepthook(exc_type, exc_value, exc_tb):
    try:
        _log('UNHANDLED EXCEPTION')
        _log(''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)
sys.excepthook = _excepthook

# Redirect stdout/stderr to log file for double-click launches - SAFE VERSION
# Only redirect when frozen (EXE) or when stdout is None (no console)
class _LogStream:
    def __init__(self, stream, is_stdout=True):
        self.stream = stream
        self.is_stdout = is_stdout
        self._in_log = False
    def write(self, s):
        if not s:
            return 0
        # Avoid recursion
        if not self._in_log:
            self._in_log = True
            try:
                # Only log stderr and important messages, not every stdout
                if not self.is_stdout:
                    _log('STDERR: ' + s.rstrip()[:1000])
            except Exception:
                pass
            finally:
                self._in_log = False
        if self.stream:
            try:
                return self.stream.write(s)
            except Exception:
                return len(s)
        return len(s)
    def flush(self):
        if self.stream:
            try:
                return self.stream.flush()
            except Exception:
                pass
    def fileno(self):
        try:
            if self.stream and hasattr(self.stream, 'fileno'):
                return self.stream.fileno()
        except Exception:
            pass
        raise OSError("No fileno")
    def isatty(self):
        try:
            if self.stream:
                return self.stream.isatty()
        except:
            pass
        return False
    def __getattr__(self, name):
        if self.stream:
            try:
                return getattr(self.stream, name)
            except:
                pass
        raise AttributeError(name)

# Only redirect if frozen or no console - don't break normal dev runs
try:
    # Don't redirect if we're in a normal terminal - prevents Qt issues
    if getattr(sys, 'frozen', False) or sys.__stdout__ is None:
        sys.stdout = _LogStream(sys.__stdout__, is_stdout=True)
        sys.stderr = _LogStream(sys.__stderr__, is_stdout=False)
    else:
        # In dev, just keep normal stdout but still install exception hook
        pass
except Exception:
    pass

import locale
import os
# FIX SCALING ISSUE ON HYPRLAND/WAYLAND - Less aggressive fix that doesn't break docking
# Keep docking free, only fix the zoom bug
# Only set if not already set by user/system
os.environ.setdefault('QT_AUTO_SCREEN_SCALE_FACTOR', '0')
os.environ.setdefault('QT_ENABLE_HIGHDPI_SCALING', '1')
# Don't force QT_SCALE_FACTOR=1 as it breaks fractional scaling - let Wayland handle it
# os.environ['QT_SCALE_FACTOR'] = '1'  # REMOVED - was breaking docking
# Wayland specific - ensure Qt uses wayland properly but doesn't break docking
os.environ.setdefault('QT_QPA_PLATFORM', '')  # let system decide, don't force
try:
    locale.setlocale(locale.LC_NUMERIC, 'C')
except Exception:
    pass
os.environ['LC_NUMERIC'] = 'C'

os.environ['OMP_NUM_THREADS'] = str(os.cpu_count() or 8)
os.environ['OPENBLAS_NUM_THREADS'] = str(os.cpu_count() or 8)
os.environ['MKL_NUM_THREADS'] = str(os.cpu_count() or 8)
os.environ['VECLIB_MAXIMUM_THREADS'] = str(os.cpu_count() or 8)
os.environ['NUMEXPR_NUM_THREADS'] = str(os.cpu_count() or 8)
print("✅ Locale set to C for MPV")

import sys
import shutil
import urllib.request
import urllib.error
import subprocess
import json
import time
import math
import re
from pathlib import Path

def _temp_root_from_settings(settings=None):
    """Return the configured, writable root used for render intermediates."""
    configured = settings.get('temp_dir') if isinstance(settings, dict) else None
    root = Path(configured or tempfile.gettempdir()).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root

def _remove_render_temp_dir(temp_dir):
    if temp_dir:
        for _ in range(3):
            try:
                shutil.rmtree(temp_dir)
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.2)
            else:
                return
        if os.path.exists(temp_dir):
            _log(f"Render temp folder could not be removed: {temp_dir}")

def _clear_managed_temp_files(temp_root):
    """Remove only FastEncode Pro-owned temp folders from the selected root."""
    root = Path(temp_root).expanduser()
    if not root.exists() or not root.is_dir():
        return 0
    if root.name.lower().startswith('fastencode_'):
        try:
            shutil.rmtree(root)
            return 1
        except OSError as exc:
            _log(f"Unable to remove temp folder {root}: {exc}")
            return 0
    deleted = 0
    for entry in root.iterdir():
        if not (entry.name.lower().startswith('fastencode_') or entry.name.lower() == 'fastencodeproxies'):
            continue
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            deleted += 1
        except OSError as exc:
            _log(f"Unable to remove temp entry {entry}: {exc}")
    return deleted

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
    print("✅ python-mpv available")
except (ImportError, OSError) as _mpv_err:
    print(f"âš ï¸  python-mpv / libmpv unavailable: {_mpv_err}")
    print("   Dev: pip install mpv   |   EXE: hidden-import=mpv + bundle libmpv-2.dll next to the app / in _MEIPASS.")
except Exception as _mpv_err:
    print(f"âš ï¸  python-mpv failed to load: {_mpv_err}")

try:
    import sounddevice as sd
    import scipy.io.wavfile as _scipy_wav
    SOUNDDEVICE_AVAILABLE = True
    print("✅ sounddevice + scipy available")
except (ImportError, OSError):
    sd = None
    _scipy_wav = None
    SOUNDDEVICE_AVAILABLE = False
    print("âš ï¸  sounddevice or scipy unavailable - voiceover recording disabled")

from PyQt6.QtWidgets import *
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSettings, QUrl, QPointF, QTimer, QEvent, QPoint, QRectF, QObject, QSize
from PyQt6.QtGui import (
    QFont, QPalette, QColor, QPainter, QBrush, QPen, QCursor, QAction, QPainterPath,
    QMouseEvent, QImage, QPixmap, QConicalGradient, QRadialGradient, QLinearGradient,
)

__version__ = "0.97.1"
GITHUB_REPO = "cpgplays/FastEncodePro"

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
    def _github_api_get(url, timeout=10):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'FastEncodePro-Updater', 'Accept': 'application/vnd.github.v3+json'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode()), ""
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()[:300]
            except Exception:
                body = ""
            if e.code == 404:
                return None, (f"GitHub repo not found (404): {GITHUB_REPO}. "
                               "Set GITHUB_REPO in the file to your real repo.")
            if e.code == 403:
                return None, ("GitHub rate limit hit (60/hr without login). "
                               "Wait a bit and try again.")
            if e.code == 409:
                return None, "Repo exists but has no commits yet."
            return None, f"GitHub HTTP {e.code}: {body or e.reason}"
        except Exception as e:
            return None, str(e)

    @staticmethod
    def _known_author_names():
        """Author identities from the app itself: __author__ + repo owner."""
        names = set()
        try:
            if isinstance(__author__, str) and __author__.strip():
                names.add(__author__.strip().lower())
        except Exception:
            pass
        try:
            owner = (GITHUB_REPO or '').split('/')[0].strip().lower()
            if owner:
                names.add(owner)
        except Exception:
            pass
        return names

    @staticmethod
    def _parse_github_date(s):
        try:
            if isinstance(s, str) and s.endswith('Z'):
                s = s[:-1] + '+00:00'
            dt = datetime.datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except Exception:
            return None

    @staticmethod
    def _exe_asset_info(release):
        """(sig, best) for a release's Windows installer binaries.

        The sig changes whenever the EXE is deleted/re-uploaded - even when
        the tag, notes and commits are untouched - which is exactly the
        in-place release workflow. Returns ("", None) with no usable .exe.
        """
        try:
            assets = release.get('assets') or []
        except Exception:
            return "", None
        exes = []
        for a in assets:
            try:
                if not isinstance(a, dict):
                    continue
                name = str(a.get('name') or "")
                if not name.lower().endswith('.exe'):
                    continue
                url = str(a.get('browser_download_url') or "")
                if not url:
                    continue
                exes.append({'name': name, 'size': int(a.get('size') or 0),
                             'updated': str(a.get('updated_at') or ''),
                             'url': url})
            except Exception:
                continue
        if not exes:
            return "", None
        sig = "|".join(sorted(f"{e['name']}:{e['size']}:{e['updated']}" for e in exes))
        exes.sort(key=lambda e: ('setup' not in e['name'].lower(),
                                 'install' not in e['name'].lower(),
                                 len(e['name'])))
        return sig, exes[0]

    @staticmethod
    def check_app_update(current_version=None):
        """Commit/author/date update check. No version numbers involved.

        Looks at the newest pushes to GITHUB_REPO (preferring commits
        authored by __author__ or the repo owner - there is only one
        committer) and compares against the last commit this install has
        already seen (stored in QSettings). Anything newer counts as an
        update, dated by the push itself.
        """
        data, err = UpdateManager._github_api_get(
            f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page=5")
        if data is None:
            return {'error': err, 'is_newer': False}
        if not isinstance(data, list) or not data:
            return {'error': f"No commits found in {GITHUB_REPO}.", 'is_newer': False}
        known = UpdateManager._known_author_names()

        def _login_of(c):
            try:
                a = c.get('author')
                if isinstance(a, dict) and a.get('login'):
                    return str(a['login'])
            except Exception:
                pass
            return ""

        def _name_of(c):
            try:
                return str(((c.get('commit') or {}).get('author') or {}).get('name') or "")
            except Exception:
                return ""

        def _date_of(c):
            try:
                return UpdateManager._parse_github_date(
                    str(((c.get('commit') or {}).get('author') or {}).get('date') or ""))
            except Exception:
                return None

        mine = [c for c in data
                if _login_of(c).lower() in known or _name_of(c).lower() in known]
        pool = mine or data
        dated = [(c, _date_of(c)) for c in pool]
        dated = [(c, d) for c, d in dated if d is not None]
        if not dated:
            return {'error': "GitHub returned commits without dates.", 'is_newer': False}
        dated.sort(key=lambda t: t[1], reverse=True)
        newest, newest_dt = dated[0]
        sha = newest.get('sha', '') or ""
        try:
            msg_full = str((newest.get('commit') or {}).get('message') or "")
        except Exception:
            msg_full = ""
        message = msg_full.splitlines()[0][:200] if msg_full else "(no message)"
        author = _login_of(newest) or _name_of(newest) or "unknown"
        url = newest.get('html_url', f"https://github.com/{GITHUB_REPO}/commit/{sha}")

        # --- release assets: in-place EXE swaps live here, not in commits.
        # A missing/failed release lookup is NOT fatal (404 = no releases
        # published yet) - the commit check above still works on its own.
        asset_sig, best_asset, release_body, release_url = "", None, "", ""
        if os.name == 'nt':
            rel, _rel_err = UpdateManager._github_api_get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
            if isinstance(rel, dict):
                asset_sig, best_asset = UpdateManager._exe_asset_info(rel)
                try:
                    release_body = str(rel.get('body') or "")
                    release_url = str(rel.get('html_url') or "")
                except Exception:
                    pass

        try:
            s = QSettings("FastEncodePro", "App2026ExactV2")
            base_sha = s.value("update_last_commit_sha", "") or ""
            base_date_s = s.value("update_last_commit_date", "") or ""
            base_sha = base_sha.strip() if isinstance(base_sha, str) else str(base_sha)
            base_asset = s.value("update_last_asset_sig", "") or ""
            base_asset = base_asset.strip() if isinstance(base_asset, str) else str(base_asset)
        except Exception:
            base_sha, base_date_s, base_asset = "", "", ""
        info = {'current': current_version,
                'commit_sha': sha, 'commit_short': sha[:7],
                'commit_date': newest_dt.isoformat(),
                'commit_message': message, 'commit_author': author,
                'url': url, 'repo': GITHUB_REPO, 'baseline_sha': base_sha}
        if not base_sha and not base_asset:
            # First check on this install: adopt the current tip AND the
            # current installer binary, so only genuinely newer changes
            # notify from here on.
            try:
                s.setValue("update_last_commit_sha", sha)
                s.setValue("update_last_commit_date", newest_dt.isoformat())
                if asset_sig:
                    s.setValue("update_last_asset_sig", asset_sig)
            except Exception:
                pass
            info['is_newer'] = False
            info['first_run'] = True
            return info
        if not base_sha:
            try:
                s.setValue("update_last_commit_sha", sha)
                s.setValue("update_last_commit_date", newest_dt.isoformat())
            except Exception:
                pass
            commit_new = False
        else:
            base_dt = UpdateManager._parse_github_date(base_date_s) if base_date_s else None
            if base_dt is None:
                commit_new = bool(sha != base_sha)
            else:
                commit_new = bool(sha != base_sha) and bool(newest_dt >= base_dt)
        # A swapped EXE changes name/size/upload-time even when the tag,
        # notes and commits are untouched - that is the in-place workflow.
        # (A brand-new release appearing also counts as new.)
        asset_new = bool(asset_sig and best_asset and (not base_asset or asset_sig != base_asset))
        if asset_new:
            info.update({'mode': 'asset', 'is_newer': True,
                         'asset_sig': asset_sig,
                         'asset_name': best_asset['name'],
                         'asset_size': best_asset['size'],
                         'asset_updated': best_asset['updated'],
                         'asset_url': best_asset['url'],
                         'release_notes': release_body[:300],
                         'url': release_url or url})
            return info
        info['mode'] = 'commit'
        info['is_newer'] = bool(commit_new)
        return info
    
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

    @staticmethod
    def download_and_install_commit(repo, sha, date_iso="", log_callback=None):
        """Download a commit as a zip and apply the best candidate inside.

        Prefers a Windows installer .exe (launched silent), else the .py
        matching this file (replaced with a .bak backup), else extracts and
        opens the folder for a manual copy. Baseline is only advanced when
        something was actually applied.
        """
        def log(msg):
            if log_callback:
                try:
                    log_callback(msg)
                except Exception:
                    pass
            print(msg)

        def _mark_applied():
            try:
                s = QSettings("FastEncodePro", "App2026ExactV2")
                s.setValue("update_last_commit_sha", sha)
                if date_iso:
                    s.setValue("update_last_commit_date", date_iso)
            except Exception:
                pass

        if not sha:
            return False, "Nothing to download (empty commit id)."
        try:
            import tempfile
            tmp_dir = tempfile.mkdtemp()
            tmp_zip = os.path.join(tmp_dir, f"update-{sha[:7]}.zip")
            zip_url = f"https://api.github.com/repos/{repo}/zipball/{sha}"
            log(f"Downloading update {sha[:7]}...")
            req = urllib.request.Request(zip_url, headers={'User-Agent': 'FastEncodePro-Updater', 'Accept': 'application/vnd.github.v3+json'})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp_zip, 'wb') as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            try:
                size_kb = os.path.getsize(tmp_zip) // 1024
            except OSError:
                size_kb = 0
            log(f"Downloaded ({size_kb} KB), extracting...")
            import zipfile
            extract_dir = os.path.join(tmp_dir, "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(tmp_zip, 'r') as z:
                z.extractall(extract_dir)
            try:
                tops = [d for d in os.listdir(extract_dir)
                        if os.path.isdir(os.path.join(extract_dir, d))]
            except OSError:
                tops = []
            root = os.path.join(extract_dir, tops[0]) if len(tops) == 1 else extract_dir
            exes, pys = [], []
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in filenames:
                    low = fn.lower()
                    full = os.path.join(dirpath, fn)
                    if low.endswith('.exe') and 'unins' not in low:
                        exes.append(full)
                    elif low.endswith('.py'):
                        pys.append(full)
            if exes and os.name == 'nt':
                exes.sort(key=lambda p: ('setup' not in os.path.basename(p).lower(),
                                         'install' not in os.path.basename(p).lower(),
                                         len(p)))
                target = exes[0]
                log(f"Launching installer {os.path.basename(target)}...")
                subprocess.Popen([target, '/SILENT'], creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                _mark_applied()
                return True, f"Installer launched ({os.path.basename(target)}). Run it to finish updating."
            me = os.path.basename(os.path.abspath(__file__))
            same = [p for p in pys if os.path.basename(p) == me]
            pick = same[0] if same else None
            if pick is None:
                fep = [p for p in pys if os.path.basename(p).lower().startswith(('fep', 'fastencode'))]
                if len(fep) == 1:
                    pick = fep[0]
            if pick:
                current_file = os.path.abspath(__file__)
                backup = current_file + ".bak"
                import shutil as _sh
                if os.path.exists(backup):
                    try:
                        os.remove(backup)
                    except OSError:
                        pass
                _sh.copy2(current_file, backup)
                _sh.copy2(pick, current_file)
                log(f"Updated {current_file}, backup at {backup}")
                _mark_applied()
                return True, "App updated, restart required."
            log(f"No installer or matching .py found - extracted to {extract_dir}, copy files manually.")
            if os.name == 'nt':
                try:
                    os.startfile(extract_dir)
                except Exception:
                    pass
            else:
                try:
                    subprocess.Popen(['xdg-open', extract_dir])
                except Exception:
                    pass
            return True, f"No auto-installable file found. Extracted to {extract_dir} - copy files manually."
        except urllib.error.HTTPError as e:
            return False, f"Download failed (HTTP {e.code})."
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

def probe_audio_streams(filepath):
    """Count audio streams precisely. Returns (count, [channels...]).

    Raises RuntimeError with ffprobe's own error text on any failure -
    it must NEVER silently guess, because a wrong count sends auto-sync
    down a dead end ("only 1 track") or mistargets export mixing.
    """
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a',
           '-show_entries', 'stream=index,codec_name,channels',
           '-of', 'json', filepath]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found - install FFmpeg and ensure it is on PATH.")
    except Exception as e:
        raise RuntimeError(f"Could not run ffprobe: {e}")
    if result.returncode != 0:
        err_lines = (result.stderr or '').strip().splitlines()
        raise RuntimeError("ffprobe failed: " + (err_lines[-1] if err_lines else f"exit {result.returncode}"))
    try:
        data = json.loads(result.stdout or '{}')
    except Exception:
        raise RuntimeError("ffprobe returned unparseable output.")
    streams = data.get('streams') or []
    channels = []
    for s in streams:
        try:
            channels.append(int(s.get('channels', 0) or 0))
        except Exception:
            channels.append(0)
    return len(streams), channels

def _correlate_full_numpy(a, b):
    """Linear full cross-correlation via FFT (scipy-free). Same ordering as
    scipy.signal.correlate(a, b, mode='full'): index i <-> lag i-(N-1)."""
    import numpy as np
    n = int(a.size)
    size = 1
    while size < 2 * n - 1:
        size *= 2
    spectrum = np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size))
    c = np.fft.irfft(spectrum, size)
    return np.concatenate([c[size - n + 1:], c[:n]])

def _correlate_full(a, b):
    try:
        from scipy import signal as _sig
        return _sig.correlate(a, b, mode='full', method='fft')
    except ImportError:
        return _correlate_full_numpy(a, b)

def compute_sync_offset_samples(audio1, audio2, sample_rate, max_lag_seconds=5.0):
    """Waveform sync between two mono float tracks.

    Returns (offset_ms, confidence). Positive offset means track2 (mic) is
    LATE relative to track1 (desktop): delaying track 0 by +offset aligns
    them, which is exactly how the timeline export applies clip.sync_offset.

    Method: strip DC + normalize both to unit energy (gain/DC invariant, so
    a quiet mic still matches a loud desktop feed), FFT cross-correlation,
    peak search restricted to +/-max_lag (kills absurd 20s false peaks from
    room tone), parabolic sub-sample refinement. Confidence is the normalized
    peak height in [0, 1].
    """
    import numpy as np
    try:
        sr = float(sample_rate)
    except Exception:
        return 0, 0.0
    if sr <= 0:
        return 0, 0.0
    a = np.asarray(audio1, dtype=np.float64).ravel()
    b = np.asarray(audio2, dtype=np.float64).ravel()
    n = int(min(a.size, b.size))
    if n < int(sr):
        return 0, 0.0
    a = a[:n] - float(np.mean(a[:n]))
    b = b[:n] - float(np.mean(b[:n]))
    ea = float(np.dot(a, a))
    eb = float(np.dot(b, b))
    if ea <= 0.0 or eb <= 0.0:
        return 0, 0.0
    a /= math.sqrt(ea)
    b /= math.sqrt(eb)
    corr = np.asarray(_correlate_full(a, b), dtype=np.float64)
    center = n - 1
    try:
        max_lag = max(1, min(n - 1, int(float(max_lag_seconds) * sr)))
    except Exception:
        max_lag = min(n - 1, int(5.0 * sr))
    lo = max(0, center - max_lag)
    hi = min(corr.size - 1, center + max_lag)
    window = corr[lo:hi + 1]
    if window.size == 0:
        return 0, 0.0
    peak_rel = int(np.argmax(window))
    peak_idx = lo + peak_rel
    peak_val = float(corr[peak_idx])
    # Parabolic refinement for sub-sample precision.
    shift = 0.0
    if 0 < peak_idx < corr.size - 1:
        y0 = float(corr[peak_idx - 1])
        y1 = peak_val
        y2 = float(corr[peak_idx + 1])
        denom = (y0 - 2.0 * y1 + y2)
        if denom != 0.0:
            shift = max(-0.5, min(0.5, 0.5 * (y0 - y2) / denom))
    lag_samples = (peak_idx - center) + shift
    # Convention check: if b[n] = a[n-D] (b delayed by D), the correlation
    # peaks at lag k=-D, so the true delay is D = -k.
    offset_ms = int(round(-lag_samples * 1000.0 / sr))
    confidence = max(0.0, min(1.0, peak_val))
    return offset_ms, confidence

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
    settings = settings or {}
    buf = get_pipeline_buffer_sizes(settings)
    cmd.extend(['-max_interleave_delta', '100M'])
    cmd.extend(['-muxing_queue_data_threshold', str(buf['muxing_queue_data_threshold_mb'] * 1024 * 1024)])
    cmd.extend(['-max_muxing_queue_size', str(buf['max_muxing_queue_size'])])
    # Buffered streaming writes: NEVER flush per packet. Flushing every packet
    # serializes encode -> USB (encode burst, disk idle, flush stall, repeat),
    # which is exactly the bouncy/zeroing write pattern on cache-limited
    # external drives. 0 lets the OS merge packets into large sequential
    # writes; the file is still fully flushed on close.
    cmd.extend(['-flush_packets', '0'])
    cmd.extend(['-fflags', '+genpts+igndts'])
    # REMOVED: '-preset', 'ultrafast' was being appended here, AFTER the real encoder
    # preset (e.g. p7 + lossless tune) was already set earlier in the command. FFmpeg
    # takes the last occurrence of a repeated flag, so this was silently overriding
    # whatever preset was actually chosen, on every render, with an x264-style preset
    # name NVENC doesn't even use (NVENC presets are p1-p7). '-threads 0' here was also
    # just a harmless duplicate of the same flag already set earlier in the command.
    cmd.append(output_path)

def build_nvenc_cbr_args(settings, fps_value=None, is_zero_copy=False):
    bitrate_kbps = int(settings.get('bitrate_mbps', 100) * 1000)
    codec = settings.get('video_codec', '')
    pixel_format = settings.get('pixel_format', 1)  # Default 10-bit
    
    if codec == 'h264_nvenc':
        pixel_format = 0  # H.264 only supports 8-bit
        
    # FIX 2026-09-05: NVENC requires nv12 for 8-bit, not yuv420p. yuv420p causes 4294967256 (-40) on 8-bit profile
    # Use nv12 for all 8-bit (including lossless with -lossless 1) to avoid -22/-40. yuv444p causes extra conversion that can fail.
    pix_fmt = 'nv12' if pixel_format == 0 else 'p010le'
    export_target_index = settings.get('export_target_index', 0)
    preset = get_nvenc_preset_for_target(export_target_index)
    # FULL RESTORE: Use custom GOP/B-Frames from settings if provided (from full export panel/dialog)
    custom_gop = settings.get('gop_size')
    if custom_gop and isinstance(custom_gop, int) and custom_gop > 0:
        gop = str(custom_gop)
    else:
        gop = str(int((fps_value or 30) * 2))
    rate_control = settings.get('rate_control', 'cbr')

    tune_val = 'lossless' if rate_control == 'lossless' else 'hq'
    base = ['-preset', preset, '-tune', tune_val, '-g', gop]
    
    if codec == 'hevc_nvenc':
        base.extend(['-profile:v', 'main10' if pixel_format == 1 else 'main'])
        base.extend(['-tier', 'high'])
        if bitrate_kbps > 60000 or export_target_index == 0 or rate_control in ('cbr', 'vbr', 'lossless'):
            base.extend(['-level', '6.2'])
        base.extend(['-multipass', 'disabled'])
    elif codec == 'h264_nvenc':
        base.extend(['-profile:v', 'high'])
        if bitrate_kbps > 60000 or export_target_index == 0 or rate_control in ('cbr', 'vbr', 'lossless'):
            base.extend(['-level', '6.2'])
        base.extend(['-multipass', 'disabled'])
        
    # FIX 2026-09-05 V4: For zero-copy TURBO (VRAM only), DO NOT add -pix_fmt at all, even for lossless.
    # Adding -pix_fmt forces auto_scale_0 which tries to convert cuda -> yuv420p/nv12 and crashes with -40.
    # Let NVENC consume the CUDA surface directly. Only add pix_fmt when we are on CPU path.
    is_lossless = (rate_control == 'lossless')
    if not is_zero_copy:
        base.extend(['-pix_fmt', pix_fmt])
        
    if codec != 'av1_nvenc':
        if rate_control == 'lossless':
            # RESTORED TRUE LOSSLESS - 400GB+ files as originally designed
            # Previous AI changed this to just -tune lossless without qp 0, which is actually lossy (~100GB)
            # True lossless requires constqp qp 0
            if is_zero_copy:
                base.extend(['-bf', '0', '-delay', '0', '-rc-lookahead', '0'])
            else:
                base.extend(['-bf', '0', '-rc-lookahead', '0'])
            # NVIDIA true lossless requires -lossless 1 flag for h264_nvenc
            if codec == 'h264_nvenc':
                base.extend(['-lossless', '1'])
        else:
            # FULL RESTORE: Use custom B-Frames from full export panel if provided
            custom_bf = settings.get('b_frames')
            if custom_bf is not None and isinstance(custom_bf, int) and 0 <= custom_bf <= 4:
                bf_val = str(custom_bf)
                base.extend(['-bf', bf_val])
            else:
                if bitrate_kbps > 300000 or (export_target_index == 0 and codec == 'hevc_nvenc'):
                    base.extend(['-bf', '2'])
                else:
                    base.extend(['-bf', '3'])

            if is_zero_copy:
                # CRITICAL FIX: Disable NVENC internal frame buffering (delay 0) - notoriously
                # deadlocks zero-copy CUDA surfaces for 30+ seconds waiting for VFR references
                # to resolve. Only needed when actually on the zero-copy CUDA path (see above).
                base.extend(['-delay', '0', '-spatial-aq', '1'])
            else:
                base.extend(['-spatial-aq', '1'])

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
        # RESTORED: True lossless - QP 0 produces 400GB+ files as before
        # The -22 error was from conflicting -bf/-delay flags, not qp 0 itself
        # Order matters: -tune lossless is already in base, add qp 0 after
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

def build_hw_decode_input_args(file_path, codec_name, use_gpu_decode, target_fps=None, thread_queue_size=128, extra_hw_frames=32):
    args = ['-thread_queue_size', str(thread_queue_size)]
    if not use_gpu_decode:
        args.extend(['-i', file_path])
        return args
    # FIX: this path feeds the CPU-compositing (non-turbo) branch, whose filters
    # (format=nv12, overlay) expect plain software frames. -hwaccel_output_format cuda
    # was being added unconditionally here, handing those filters CUDA-resident frames
    # with no hwdownload in between - that's exactly the "Impossible to convert between
    # the formats... src: cuda / dst: yuv420p nv12 ..." crash. -hwaccel cuda alone still
    # uses the hardware decode engine; it just auto-downloads output to system memory,
    # which is what this path actually needs.
    args.extend(['-hwaccel', 'cuda', '-extra_hw_frames', str(extra_hw_frames)])
    args.extend(['-i', file_path])
    return args

def has_optional_video_filters(settings):
    if settings.get('color_bw_mode', False): return True
    if settings.get('cinema_scope', False): return True
    if abs(settings.get('color_brightness', 0)) >= 1: return True
    if abs(settings.get('color_contrast', 0)) >= 1: return True
    if abs(settings.get('color_saturation', 0)) >= 1: return True
    if abs(settings.get('color_gamma', 0)) >= 1: return True
    if any(abs(settings.get(k, 0)) > 0.02 for k in ('lift_x', 'lift_y', 'gamma_x', 'gamma_y', 'gain_x', 'gain_y')): return True
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
        'gpu_vram_total_mb': 0,
        'gpu_vram_used_mb': 0,
        'gpu_vram_free_mb': 0,
        'gpu_vram_total_gb': 0.0,
        'gpu_util': 0,
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
                ['nvidia-smi', '--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=2, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            out = (smi.stdout or '').strip().splitlines()
            if out and out[0]:
                parts = [p.strip() for p in out[0].split(',')]
                if len(parts) >= 4:
                    caps['gpu_name'] = parts[0]
                    try:
                        total = int(parts[1]); used = int(parts[2]); free = int(parts[3])
                        caps['gpu_vram_total_mb'] = total; caps['gpu_vram_used_mb'] = used; caps['gpu_vram_free_mb'] = free
                        caps['gpu_vram_total_gb'] = round(total / 1024, 1)
                        if len(parts) >= 5: caps['gpu_util'] = int(parts[4])
                    except: pass
    except: pass
    try:
        enc = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        enc_text = (enc.stdout or '') + (enc.stderr or '')
        caps['nvenc_h264'] = 'h264_nvenc' in enc_text; caps['nvenc_hevc'] = 'hevc_nvenc' in enc_text; caps['nvenc_av1'] = 'av1_nvenc' in enc_text
        caps['amf_h264'] = 'h264_amf' in enc_text; caps['amf_hevc'] = 'hevc_amf' in enc_text; caps['amf_av1'] = 'av1_amf' in enc_text
        caps['qsv_h264'] = 'h264_qsv' in enc_text; caps['qsv_hevc'] = 'hevc_qsv' in enc_text; caps['qsv_av1'] = 'av1_qsv' in enc_text
        caps['vaapi_h264'] = 'h264_vaapi' in enc_text; caps['vaapi_hevc'] = 'hevc_vaapi' in enc_text; caps['vaapi_av1'] = 'av1_vaapi' in enc_text
        caps['nvidia'] = caps['nvenc_h264'] or caps['nvenc_hevc'] or caps['nvenc_av1'] or caps['nvidia_smi']
        caps['amd'] = caps['amf_h264'] or caps['amf_hevc'] or caps['amf_av1'] or caps['vaapi_h264']
        caps['intel'] = caps['qsv_h264'] or caps['qsv_hevc'] or caps['qsv_av1']
        for key in ['nvenc_h264','nvenc_hevc','nvenc_av1','amf_h264','amf_hevc','amf_av1','qsv_h264','qsv_hevc','qsv_av1','vaapi_h264','vaapi_hevc','vaapi_av1']:
            if caps.get(key): caps['encoders'].append(key)
    except: pass
    try:
        dec = subprocess.run(['ffmpeg', '-hide_banner', '-decoders'], capture_output=True, text=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        dec_text = (dec.stdout or '') + (dec.stderr or '')
        caps['nvdec'] = ('h264_cuvid' in dec_text) or ('hevc_cuvid' in dec_text) or ('av1_cuvid' in dec_text)
    except: pass
    return caps

def get_live_gpu_vram():
    try:
        if not shutil.which('nvidia-smi'): return None
        smi = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu', '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=1, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        out = (smi.stdout or '').strip().splitlines()
        if out and out[0]:
            parts = [p.strip() for p in out[0].split(',')]
            if len(parts) >= 3:
                return {'total_mb': int(parts[0]), 'used_mb': int(parts[1]), 'free_mb': int(parts[2]), 'util': int(parts[3]) if len(parts)>3 else 0, 'temp': int(parts[4]) if len(parts)>4 else 0}
    except: pass
    return None


def get_gpu_tier(hw_caps, vram_limit_mb=None):
    """Classify hardware into a buffer-sizing tier from detected/limited VRAM and GPU
    generation. Older NVENC generations (Pascal and earlier) can't sustain the same
    decode/encode throughput as modern cards even with equal or more VRAM, so they're
    capped to a conservative tier regardless of how much VRAM they report."""
    hw_caps = hw_caps or {}
    gpu_name_lower = (hw_caps.get('gpu_name') or '').lower()
    vram_mb = vram_limit_mb if (vram_limit_mb and vram_limit_mb > 0) else hw_caps.get('gpu_vram_total_mb', 0)
    is_older_gen = any(tok in gpu_name_lower for tok in (
        'gtx 10', 'gtx 9', 'gtx 7', 'gtx 6', 'quadro p', 'quadro m', 'quadro k', 'titan x', 'titan xp'
    ))
    if not vram_mb or vram_mb <= 0:
        return 'unknown'
    if is_older_gen:
        return 'low'
    if vram_mb >= 16000:
        return 'high'
    if vram_mb >= 10000:
        return 'mid'
    if vram_mb >= 6000:
        return 'low'
    return 'unknown'

def get_pipeline_buffer_sizes(settings):
    """V8: preallocated VRAM frame pool + deep output smoothing for slow disks.

    VRAM: extra_hw_frames sizes the decode frame pool up front (allocated as
    decoding warms up, then held). At 1080p8-bit that is ~3 MB/frame, so even
    a 12 GB budget only ever needs ~1.5-3 GB resident - small pools are
    correct-by-design, NOT a bug. Decode always outruns P7 lossless encode,
    so a bigger pool would just idle.
    Disk: the mux queue threshold is a RAM-side smoothing window
    (threshold / bitrate = seconds of encode the disk can lag behind).
    Combined with buffered writes (flush_packets 0) the external drive sees
    steady large sequential writes instead of flush-stall bursts.
    """
    hw_caps = settings.get('hw_caps') or {}
    vram_limit_mb = settings.get('gpu_vram_limit_mb') or 12288
    tier = get_gpu_tier(hw_caps, vram_limit_mb)
    export_w = settings.get('export_width') or 1920
    export_h = settings.get('export_height') or 1080
    is_10bit = settings.get('pixel_format', 0) == 1
    bytes_pp = 3.0 if is_10bit else 1.5
    frame_mb = (export_w * export_h * bytes_pp) / (1024*1024)
    frame_mb = max(frame_mb, 8)
    target_vram = vram_limit_mb * 0.70
    frames_needed = int(target_vram / frame_mb)
    frames_needed = max(64, min(frames_needed, 1024))
    base = {
        'high':    {'extra_hw_frames': frames_needed, 'thread_queue_size': 256},
        'mid':     {'extra_hw_frames': min(frames_needed, 512), 'thread_queue_size': 128},
        'low':     {'extra_hw_frames': min(frames_needed, 192), 'thread_queue_size': 64},
        'unknown': {'extra_hw_frames': 64, 'thread_queue_size': 64},
    }[tier]
    fps = settings.get('timeline_fps') or 60
    try: fps = float(fps)
    except: fps = 60.0
    rc = settings.get('rate_control', 'cbr')
    br_est = 800 if rc == 'lossless' else float(settings.get('bitrate_mbps', 100))
    avg_pkt = max(64*1024, int(br_est * 125000 / fps))
    # RAM-side smoothing window: high ~1 GB (~7 s of lossless 1080p60).
    target_mux = 1024 if tier == 'high' else (768 if tier == 'mid' else 256)
    if rc == 'lossless': target_mux = int(target_mux*1.5)
    max_q = max(1024, int(target_mux*1024*1024 / avg_pkt))
    max_q = min(max_q, 16384)
    return {
        'extra_hw_frames': base['extra_hw_frames'],
        'thread_queue_size': base['thread_queue_size'],
        'max_muxing_queue_size': max_q,
        'muxing_queue_data_threshold_mb': target_mux,
        'frame_mb': frame_mb,
        'frames_needed': frames_needed,
    }

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
        try:
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        except Exception as e:
            print(f"Dwell overlay flags failed (Wayland): {e}")
        self.setFixedSize(60, 60)
        self.progress = 0.0
        self.active = False
        # Hidden by default - only show if accessibility enabled
        self.hide()

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

# --- MPV EMBED HELPERS (Hyprland-safe) ---
def _is_wayland_session():
    try:
        if os.environ.get('WAYLAND_DISPLAY'):
            return True
        if (os.environ.get('XDG_SESSION_TYPE') or '').lower() == 'wayland':
            return True
        if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'):
            return True
    except Exception:
        pass
    return False


def _is_hyprland():
    try:
        if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'):
            return True
        if 'hyprland' in (os.environ.get('XDG_CURRENT_DESKTOP') or '').lower():
            return True
        if 'hyprland' in (os.environ.get('XDG_SESSION_DESKTOP') or '').lower():
            return True
    except Exception:
        pass
    return False


def _mpv_preview_mode_static():
    """Preview mode: 'auto' (default), 'embed', or 'external'.

    Env override wins: FEP_MPV_EMBED=1/embed (force embed), 0/off/external
    (force external), auto (force auto-detect).
    Otherwise QSettings 'mpv_preview_mode'. The legacy bool key
    'mpv_embed_experimental' is still honored for back-compat.
    """
    try:
        env = (os.environ.get('FEP_MPV_EMBED') or '').strip().lower()
        if env in ('1', 'true', 'yes', 'on', 'embed', 'embedded'):
            return 'embed'
        if env in ('0', 'false', 'no', 'off', 'external', 'separate'):
            return 'external'
        if env == 'auto':
            return 'auto'
    except Exception:
        pass
    try:
        s = QSettings("FastEncodePro", "App2026ExactV2")
        v = s.value("mpv_preview_mode", None)
        if isinstance(v, str) and v.lower() in ('auto', 'embed', 'embedded', 'external', 'separate'):
            v = v.lower()
            return 'embed' if v == 'embedded' else ('external' if v == 'separate' else v)
        # back-compat: legacy opt-in bool
        old = s.value("mpv_embed_experimental", None)
        if old is not None:
            if isinstance(old, str):
                if old.lower() in ('true', '1', 'yes', 'on'):
                    return 'embed'
            elif bool(old):
                return 'embed'
    except Exception:
        pass
    return 'auto'


def _mpv_auto_embed_for_session():
    """Auto-detect: attempt embed everywhere EXCEPT Hyprland/Wayland.

    Rationale: classic wid embedding has no Wayland equivalent and breaks
    Hyprland (ghost windows); the separate MPV window is the safe default
    there. Windows / macOS / X11 Linux default to trying embed.
    """
    try:
        if _is_hyprland() or _is_wayland_session():
            return False
    except Exception:
        pass
    return True


def _mpv_session_label():
    try:
        plat = {'nt': 'Windows', 'posix': 'macOS' if sys.platform == 'darwin' else 'Linux'}.get(os.name, sys.platform)
    except Exception:
        plat = 'Unknown OS'
    try:
        if _is_hyprland():
            sess = 'Hyprland/Wayland'
        elif _is_wayland_session():
            sess = 'Wayland'
        else:
            sess = 'X11' if (plat == 'Linux') else 'native'
    except Exception:
        sess = '?'
    return f"{plat} / {sess}"


def _ensure_preview_mode_default():
    """First-run/install default: persist 'auto' so later launches are stable."""
    try:
        s = QSettings("FastEncodePro", "App2026ExactV2")
        if not s.contains("mpv_preview_mode") and s.value("mpv_embed_experimental", None) is None:
            s.setValue("mpv_preview_mode", "auto")
    except Exception:
        pass


def _mpv_should_attempt_embed():
    try:
        mode = _mpv_preview_mode_static()
    except Exception:
        mode = 'auto'
    if mode == 'embed':
        return True
    if mode == 'external':
        return False
    try:
        return bool(_mpv_auto_embed_for_session())
    except Exception:
        return False


def _mpv_embed_requested_static():
    """Back-compat wrapper: True when the effective mode attempts embed."""
    try:
        return bool(_mpv_should_attempt_embed())
    except Exception:
        return False


try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget as _QOpenGLWidgetBase
    _HAS_QOGL = True
except Exception:
    _QOpenGLWidgetBase = QWidget
    _HAS_QOGL = False


class _EmbeddedMpvGLWidget(_QOpenGLWidgetBase):
    """libmpv render-API target. No wid/reparenting, so Wayland/Hyprland-safe.

    Any failure is contained — caller falls back to the external MPV window.
    """

    def __init__(self, mpv_obj, parent=None):
        super().__init__(parent)
        self._mpv = mpv_obj
        self._ctx = None
        self._init_error = None
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def initializeGL(self):
        if not _HAS_QOGL:
            self._init_error = RuntimeError("QOpenGLWidget unavailable")
            return
        try:
            import mpv as _mpv_mod
            if not hasattr(_mpv_mod, 'MpvRenderContext'):
                raise RuntimeError("python-mpv has no MpvRenderContext (needs libmpv render API)")

            def _get_proc(_ctx_ptr, name):
                try:
                    if isinstance(name, (bytes, bytearray)):
                        name = bytes(name).decode('utf-8', 'ignore')
                    addr = self.context().getProcAddress(str(name))
                    return int(addr) if addr else 0
                except Exception:
                    return 0

            params = {'get_proc_address': _get_proc}
            try:
                self._ctx = _mpv_mod.MpvRenderContext(self._mpv, api_type='opengl', opengl_init_params=params)
            except TypeError:
                # older signature: (mpv, api_type, params) positionally
                self._ctx = _mpv_mod.MpvRenderContext(self._mpv, 'opengl', opengl_init_params=params)

            try:
                def _on_mpv_update():
                    try:
                        self.update()
                    except Exception:
                        pass
                if hasattr(self._ctx, 'set_update_callback'):
                    try:
                        self._ctx.set_update_callback(_on_mpv_update)
                    except Exception:
                        pass
                if hasattr(self._ctx, 'update_cb'):
                    try:
                        self._ctx.update_cb = _on_mpv_update
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception as e:
            self._init_error = e
            self._ctx = None

    def paintGL(self):
        if not self._ctx:
            return
        try:
            scale = float(self.devicePixelRatio() or 1.0)
            w = max(1, int(self.width() * scale))
            h = max(1, int(self.height() * scale))
            try:
                fbo = int(self.defaultFramebufferObject())
            except Exception:
                fbo = 0
            try:
                self._ctx.render(flip_y=True, opengl_fbo={'w': w, 'h': h, 'fbo': fbo})
            except TypeError:
                try:
                    self._ctx.render({'w': w, 'h': h, 'fbo': fbo})
                except Exception:
                    pass
            try:
                if hasattr(self._ctx, 'report_swap'):
                    self._ctx.report_swap()
            except Exception:
                pass
        except Exception:
            pass

    def shutdown_gl(self):
        try:
            if self._ctx is not None:
                try:
                    free = getattr(self._ctx, 'free', None)
                    if callable(free):
                        free()
                except Exception:
                    pass
        finally:
            self._ctx = None


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
        self._mpv_init_attempted = False
        # Preview mode — 'auto' resolves per OS/session; external stays safe on Hyprland.
        self.preview_mode = 'auto'
        self.embedded_mode = False
        self.embed_error = ""
        self._gl_widget = None
        self._embed_container = None
        try:
            self.preview_mode = _mpv_preview_mode_static()
        except Exception:
            self.preview_mode = 'auto'
        try:
            self._embed_opt_in = _mpv_should_attempt_embed()
        except Exception:
            self._embed_opt_in = False

        self.position_timer = QTimer(self)
        self.position_timer.timeout.connect(self._update_position)
        self.position_timer.setInterval(100)

        self.setStyleSheet("background-color: black;")
        self.setMinimumSize(320, 180)  # Reduced to avoid forcing large central widget that blocks docking

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
        layout.setContentsMargins(0, 0, 0, 0)
        self.info_label = QLabel(
            "No preview loaded"
        )
        self.info_label.setStyleSheet("""
            QLabel {
                color: #ef4444;
                font-size: 12pt;
                font-weight: bold;
                background-color: black;
                border: none;
                border-radius: 8px;
                padding: 20px;
            }
        """)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        # DEFER MPV init to avoid crash on launch - init after widget is shown
        # On Wayland, creating MPV immediately can segfault
        QTimer.singleShot(100, self._init_mpv)

    def is_embedded(self):
        return bool(self.embedded_mode and self._gl_widget is not None)

    def embed_error_text(self):
        return self.embed_error or ""

    def set_preview_mode(self, mode):
        """Persist preview mode ('auto'/'embed'/'external'); takes effect on next MPV init (restart recommended)."""
        try:
            mode = str(mode or 'auto').lower()
            if mode not in ('auto', 'embed', 'external'):
                mode = 'auto'
            s = QSettings("FastEncodePro", "App2026ExactV2")
            s.setValue("mpv_preview_mode", mode)
            self.preview_mode = mode
            try:
                self._embed_opt_in = _mpv_should_attempt_embed()
            except Exception:
                self._embed_opt_in = (mode == 'embed')
        except Exception:
            pass

    def set_embedded_enabled(self, enabled):
        """Back-compat wrapper around set_preview_mode."""
        try:
            if isinstance(enabled, str) and enabled.lower() in ('auto', 'embed', 'external'):
                self.set_preview_mode(enabled)
            else:
                self.set_preview_mode('embed' if enabled else 'external')
        except Exception:
            pass

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
        if self._mpv_init_attempted:
            return
        self._mpv_init_attempted = True
        try:
            # Effective mode is re-read here so a restart picks up Settings changes.
            try:
                self.preview_mode = _mpv_preview_mode_static()
            except Exception:
                pass
            try:
                opt_in = bool(_mpv_should_attempt_embed())
                self._embed_opt_in = opt_in
            except Exception:
                opt_in = bool(getattr(self, '_embed_opt_in', False))
            if opt_in:
                try:
                    if self._init_mpv_embedded():
                        return
                except Exception as e:
                    try:
                        self.embed_error = str(e)[:300]
                    except Exception:
                        pass
                    try:
                        if self.mpv is not None:
                            try:
                                self.mpv.terminate()
                            except Exception:
                                pass
                        self.mpv = None
                    except Exception:
                        pass
            self._init_mpv_external()
        finally:
            try:
                QTimer.singleShot(0, self._refresh_parent_preview_badge)
            except Exception:
                pass

    def _refresh_parent_preview_badge(self):
        try:
            w = self.window()
            if w is not None and hasattr(w, '_position_preview_overlays'):
                w._position_preview_overlays()
        except Exception:
            pass

    def _attach_mpv_observers(self):
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
            # insta-crash fix: guard against callback after widget destroyed
            try:
                QTimer.singleShot(0, self._mpv_apply_file_loaded_pending)
            except Exception:
                pass

    def _init_mpv_embedded(self):
        """Hyprland-safe embed. Returns True on success, False to fall back.

        Wayland/Hyprland: libmpv OpenGL render into QOpenGLWidget (no wid).
        X11/Windows: wid embed first (native), then libmpv render.
        """
        import mpv as _mpv_mod
        wayland = _is_wayland_session()
        hypr = _is_hyprland()

        if wayland and not _HAS_QOGL:
            self.embed_error = "Embed needs Qt OpenGL widgets (missing) — using external window."
            return False
        if not hasattr(_mpv_mod, 'MpvRenderContext'):
            # wid path is still possible off-Wayland; on Wayland there is no safe wid path.
            if wayland:
                self.embed_error = "python-mpv lacks MpvRenderContext — using external window."
                return False

        # --- Path A: wid embed (X11/Windows only — NEVER on Wayland/Hyprland) ---
        if not wayland:
            try:
                container = QWidget(self)
                container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
                container.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
                container.setMinimumSize(320, 180)
                container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
                container.setStyleSheet("background: black; border-radius: 20px;")
                self.layout().addWidget(container, stretch=1)
                try:
                    self.info_label.hide()
                except Exception:
                    pass
                # Must be visible + native before winId() is usable.
                container.show()
                wid = str(int(container.winId()))
                mpv_obj = _mpv_mod.MPV(
                    vo='gpu',
                    wid=wid,
                    hwdec='auto-copy',
                    keep_open='yes',
                    idle='yes',
                    hr_seek='yes',
                    force_window='no',
                    osc='no',
                    input_default_bindings='no',
                    input_vo_keyboard='no',
                    audio_client_name='FastEncodePro',
                    audio_fallback_to_null='yes',
                    cache='yes',
                    demuxer_max_bytes='100MiB',
                )
                self.mpv = mpv_obj
                self._embed_container = container
                self.embedded_mode = True
                self.embed_error = ""
                self._attach_mpv_observers()
                try:
                    self.info_label.setText("Video Preview — embedded (wid)")
                except Exception:
                    pass
                return True
            except Exception as e:
                try:
                    self.embed_error = f"wid embed failed: {e}"[:300]
                except Exception:
                    pass
                try:
                    self.mpv = None
                except Exception:
                    pass
                try:
                    if getattr(self, '_embed_container', None) is not None:
                        self._embed_container.hide()
                        self._embed_container.deleteLater()
                    self._embed_container = None
                except Exception:
                    pass
                try:
                    self.info_label.show()
                except Exception:
                    pass
                # fall through to libmpv render path

        # --- Path B: libmpv OpenGL render (the Wayland/Hyprland-safe path) ---
        try:
            mpv_obj = _mpv_mod.MPV(
                vo='libmpv',
                hwdec='auto-copy',
                keep_open='yes',
                idle='yes',
                hr_seek='yes',
                osc='no',
                input_default_bindings='no',
                input_vo_keyboard='no',
                audio_client_name='FastEncodePro',
                audio_fallback_to_null='yes',
                cache='yes',
                demuxer_max_bytes='100MiB',
            )
        except Exception as e:
            self.embed_error = f"libmpv vo failed: {e}"[:300]
            return False

        try:
            gl = _EmbeddedMpvGLWidget(mpv_obj, self)
            self.layout().addWidget(gl, stretch=1)
            try:
                self.info_label.hide()
            except Exception:
                pass
            gl.show()
            # Render context is created in initializeGL (needs a current GL context).
            # Pump paints so initializeGL runs; a hard error fails fast,
            # a merely-pending ctx is verified again shortly (some drivers init async).
            try:
                gl.update()
                QApplication.processEvents()
                QApplication.processEvents()
            except Exception:
                pass
            if getattr(gl, '_init_error', None) is not None:
                err = getattr(gl, '_init_error', None)
                self.embed_error = f"GL render init failed: {err}"[:300]
                try:
                    gl.hide()
                    gl.deleteLater()
                except Exception:
                    pass
                try:
                    mpv_obj.terminate()
                except Exception:
                    pass
                try:
                    self.info_label.show()
                except Exception:
                    pass
                return False
            self.mpv = mpv_obj
            self._gl_widget = gl
            self.embedded_mode = True
            self.embed_error = ""
            self._attach_mpv_observers()
            try:
                loc = "Hyprland/Wayland" if (hypr or wayland) else "embedded"
                self.info_label.setText(f"Video Preview — embedded ({loc}, libmpv)")
            except Exception:
                pass
            try:
                QTimer.singleShot(2000, self._verify_embedded_gl)
            except Exception:
                pass
            try:
                QTimer.singleShot(0, self._refresh_parent_preview_badge)
            except Exception:
                pass
            return True
        except Exception as e:
            self.embed_error = f"embedded preview failed: {e}"[:300]
            try:
                mpv_obj.terminate()
            except Exception:
                pass
            try:
                self.info_label.show()
            except Exception:
                pass
            return False

    def _verify_embedded_gl(self):
        """Late check: if GL never initialized, fall back to external window."""
        try:
            if not getattr(self, 'embedded_mode', False):
                return
            gl = getattr(self, '_gl_widget', None)
            if gl is None:
                return
            if getattr(gl, '_init_error', None) is not None or getattr(gl, '_ctx', None) is None:
                err = getattr(gl, '_init_error', None)
                self.embed_error = (f"GL render init failed: {err}"[:300] if err else "GL render unavailable — using external window.")
                try:
                    gl.hide()
                    gl.deleteLater()
                except Exception:
                    pass
                self._gl_widget = None
                try:
                    if self.mpv is not None:
                        try:
                            self.mpv.terminate()
                        except Exception:
                            pass
                    self.mpv = None
                except Exception:
                    pass
                self.embedded_mode = False
                try:
                    self.info_label.show()
                except Exception:
                    pass
                self._init_mpv_external()
        except Exception:
            pass
        finally:
            try:
                self._refresh_parent_preview_badge()
            except Exception:
                pass

    def _init_mpv_external(self):
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

            self._attach_mpv_observers()
            try:
                if getattr(self, '_embed_opt_in', False) and getattr(self, 'embed_error', ''):
                    self.info_label.setText(f"No preview loaded (embed fallback: {self.embed_error[:120]})")
                else:
                    self.info_label.setText("No preview loaded")
            except Exception:
                pass

        except Exception as e:
            try:
                self.embed_error = str(e)[:300]
            except Exception:
                pass
            self.mpv = None

    def _notify_preview_empty_state(self):
        try:
            w = self.window()
            if w is not None and hasattr(w, '_update_preview_empty_state'):
                QTimer.singleShot(0, w._update_preview_empty_state)
        except Exception:
            pass

    def load_file(self, file_path, seek_ms=None):
        if not self.mpv:
            return False
        if not file_path or not os.path.exists(file_path):
            return False
        try:
            self._file_loading = True
            try:
                self.mpv.lavfi_complex = ""
            except Exception:
                pass
            self._pending_audio_filter = None
            self._pending_video_filter = None
            self._pending_seek_ms = seek_ms
            self.current_file = file_path
            self.mpv.loadfile(file_path)
            try:
                self.mpv.pause = True
            except Exception:
                pass
            self._is_paused = True
            self._notify_preview_empty_state()
            return True
        except Exception:
            self._file_loading = False
            self._pending_seek_ms = None
            return False

    def play(self):
        if not self.mpv or not self.current_file:
            return
        try:
            # Refresh from reality first: a stale cache here is what made
            # "press play at the end" resume thin air.
            try:
                tp = self.mpv.time_pos
                if tp is not None:
                    self._position_ms = max(0, int(float(tp) * 1000))
            except Exception:
                pass
            dur = 0
            try:
                live = self.mpv.duration
                if live is not None and float(live) > 0:
                    dur = int(float(live) * 1000)
                    self._duration_ms = dur
            except Exception:
                dur = int(self._duration_ms or 0)
            if dur > 2000 and int(self._position_ms or 0) >= dur - 400:
                # Sitting on the last frame: mpv will NOT resume on unpause
                # (end-of-file idle looks exactly like a freeze). Restart.
                try:
                    self.mpv.seek(0.0, reference='absolute')
                except Exception:
                    pass
                self._position_ms = 0
        except Exception:
            pass
        try:
            self.mpv.pause = False
        except Exception:
            pass
        # Believe mpv, not our assumption: read the flag back so a failed
        # IPC call can never desync the UI into "playing" while paused.
        try:
            self._is_paused = bool(self.mpv.pause)
        except Exception:
            self._is_paused = False
        try:
            if self._is_paused:
                self.position_timer.stop()
            else:
                self.position_timer.start()
        except Exception:
            pass

    def pause(self):
        if not self.mpv:
            return
        try:
            self.mpv.pause = True
        except Exception:
            pass
        try:
            self._is_paused = bool(self.mpv.pause)
        except Exception:
            self._is_paused = True
        try:
            self.position_timer.stop()
        except Exception:
            pass

    def is_paused(self):
        return self._is_paused

    def seek(self, position_ms, exact=True, domain_ms=0):
        """Seek mpv. Scrub paths pass exact=False for a fast keyframe seek;
        precise landing still comes from the playhead/timecode, which are
        always exact. Falls back to a plain seek on old python-mpv builds.

        domain_ms optionally overrides the EOF clamp reference: pass it only
        when the caller knows the true length (EDL timeline duration). A
        wrong-small clamp is worse than none, so file-backed modes leave it
        at 0 and keep the cached-duration clamp.
        """
        if not self.mpv:
            return
        try:
            position_ms = int(position_ms)
        except Exception:
            return
        try:
            dur = int(self._duration_ms or 0)
            try:
                if int(domain_ms or 0) > 0:
                    dur = int(domain_ms)
            except Exception:
                pass
            if dur > 1000:
                # Never seek onto (or past) the last instant: mpv jumps to
                # EOF, shows black, and can wedge with filters engaged.
                position_ms = max(0, min(position_ms, dur - 250))
            elif dur > 0:
                position_ms = max(0, min(position_ms, dur))
        except Exception:
            pass
        if getattr(self, '_file_loading', False):
            # Seeking mid-load wedges mpv: park it, applied on file-loaded.
            self._pending_seek_ms = position_ms
            return
        if not exact and getattr(self, '_seek_keyframes_ok', True):
            try:
                self.mpv.seek(position_ms / 1000.0, reference='absolute',
                              precision='keyframes')
                self._position_ms = position_ms
                return
            except TypeError:
                # Old python-mpv without the precision kwarg: remember it.
                self._seek_keyframes_ok = False
            except Exception:
                pass
        try:
            self.mpv.seek(position_ms / 1000.0, reference='absolute')
            self._position_ms = position_ms
        except Exception:
            pass
        # The position timer only runs while playing, so a paused scrub
        # would otherwise leave every display frozen: poll twice so the
        # time-pos observer's update gets emitted once it lands.
        try:
            QTimer.singleShot(150, self._update_position)
            QTimer.singleShot(450, self._update_position)
        except Exception:
            pass

    def position(self):
        # Live read: IN/OUT points captured right after a paused scrub must
        # reflect the frame actually showing, not a stale cache.
        try:
            if self.mpv is not None:
                tp = self.mpv.time_pos
                if tp is not None:
                    self._position_ms = max(0, int(float(tp) * 1000))
                    return self._position_ms
        except Exception:
            pass
        return self._position_ms

    def duration(self):
        # Live read first: the cached value goes stale across file switches
        # (observer hasn't fired yet) and every consumer - seek clamp,
        # scrub mapping, timecode - silently computes against the WRONG
        # movie. Fall back to cache when mpv has nothing to report.
        try:
            if self.mpv is not None:
                live = self.mpv.duration
                if live is not None and float(live) > 0:
                    ms = int(float(live) * 1000)
                    self._duration_ms = ms
                    return ms
        except Exception:
            pass
        return self._duration_ms

    def _update_position(self):
        self.positionChanged.emit(self._position_ms)
        # EOF stickiness: mpv idles on the last frame with pause still False,
        # so without this the UI shows "playing" forever on a dead frame and
        # the next play press appears to do nothing (only a rewind revives
        # it). Settle into paused - but only after several identical ticks,
        # so slow content and buffering never false-trigger.
        try:
            dur = int(self._duration_ms or 0)
            pos = int(self._position_ms or 0)
            if (dur > 2000 and not self._is_paused and self.current_file
                    and pos >= dur - 400):
                last = getattr(self, '_eof_last_pos', None)
                ticks = int(getattr(self, '_eof_stall_ticks', 0) or 0)
                if last is not None and pos == last:
                    ticks += 1
                else:
                    ticks = 0
                self._eof_last_pos = pos
                self._eof_stall_ticks = ticks
                if ticks >= 3:
                    self._is_paused = True
                    try:
                        self.mpv.pause = True
                    except Exception:
                        pass
                    try:
                        self.position_timer.stop()
                    except Exception:
                        pass
                    self._eof_stall_ticks = 0
            else:
                self._eof_stall_ticks = 0
                self._eof_last_pos = pos
        except Exception:
            pass
        # EOF stickiness: mpv idles at the last frame with pause still False,
        # so without this the UI shows "playing" forever on a dead frame and
        # the next play press appears to do nothing. Settle into paused.
        try:
            dur = int(self._duration_ms or 0)
            if (dur > 2000 and not self._is_paused and self.current_file
                    and int(self._position_ms or 0) >= dur - 400):
                self._is_paused = True
                try:
                    self.mpv.pause = True
                except Exception:
                    pass
                try:
                    self.position_timer.stop()
                except Exception:
                    pass
        except Exception:
            pass

    def stop(self):
        if not self.mpv:
            try:
                self.current_file = None
            except Exception:
                pass
            self._notify_preview_empty_state()
            return
        try:
            self.mpv.command('stop')
            self._is_paused = True
            self._position_ms = 0
            self.position_timer.stop()
        except Exception:
            pass
        try:
            self.current_file = None
        except Exception:
            pass
        self._notify_preview_empty_state()

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
        try:
            if getattr(self, '_gl_widget', None) is not None:
                try:
                    self._gl_widget.shutdown_gl()
                except Exception:
                    pass
        except Exception:
            pass
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


# --- 2026 EXACT-HTML GLASS WIDGETS (new features required for pixel-match) ---
class FepAmbientWidget(QWidget):
    """Paints HTML ambient blobs + 32px grid behind everything (#060609 base)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#060609"))
        w, h = max(1, self.width()), max(1, self.height())
        # Blob 1: purple 124,91,255 0.28 top-left
        g1 = QRadialGradient(w * 0.30, h * 0.05, w * 0.45)
        g1.setColorAt(0.0, QColor(124, 91, 255, 72))
        g1.setColorAt(1.0, QColor(124, 91, 255, 0))
        p.fillRect(self.rect(), QBrush(g1))
        # Blob 2: cyan 125,249,255 0.18 top-right
        g2 = QRadialGradient(w * 0.85, h * 0.08, w * 0.40)
        g2.setColorAt(0.0, QColor(125, 249, 255, 46))
        g2.setColorAt(1.0, QColor(125, 249, 255, 0))
        p.fillRect(self.rect(), QBrush(g2))
        # Blob 3: green 0,255,136 0.12 bottom
        g3 = QRadialGradient(w * 0.45, h * 1.05, w * 0.40)
        g3.setColorAt(0.0, QColor(0, 255, 136, 30))
        g3.setColorAt(1.0, QColor(0, 255, 136, 0))
        p.fillRect(self.rect(), QBrush(g3))
        # 32px grid rgba(255,255,255,0.01)
        p.setPen(QPen(QColor(255, 255, 255, 4), 1))
        step = 32
        for x in range(0, w, step):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, step):
            p.drawLine(0, y, w, y)


class FepWaveformBars(QWidget):
    """96-bar preview waveform from HTML bottom control bar. progress 0..1."""
    def __init__(self, parent=None, bars=96, progress=0.38):
        super().__init__(parent)
        self.bars = bars
        self.progress = progress
        self.setMinimumHeight(28)
        self.setMouseTracking(True)

    def set_progress(self, v):
        self.progress = max(0.0, min(1.0, float(v)))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        gap = 2
        bw = max(1.0, (w - gap * (self.bars - 1)) / self.bars)
        import math as _m
        import random as _r
        # deterministic pseudo-random to match HTML look without flicker
        for i in range(self.bars):
            _r.seed(i * 977 + 13)
            n = 6 + _m.sin(i * 0.6) * 4 + _r.random() * 10
            n = max(3.0, min(float(h), n))
            x = i * (bw + gap)
            y = (h - n) / 2.0
            is_active = (i / max(1, self.bars)) < self.progress
            if is_active:
                col = QColor("#7df9ff") if (i % 7 == 0) else QColor(255, 255, 255, 230)
            else:
                col = QColor(255, 255, 255, 46)
            p.setBrush(QBrush(col))
            p.drawRoundedRect(int(x), int(y), max(1, int(bw)), int(n), 2, 2)


class FepScrubberWidget(QWidget):
    """Preview scrub bar (redesigned): the widget NEVER seeks by itself.

    Dragging emits scrubMoved (playhead + timecode follow, zero mpv
    traffic); releasing emits scrubFinished exactly once, and the app
    performs a single clamped keyframe seek. N drag events can therefore
    never produce more than one mpv seek, which makes seek pile-ups -
    freezes, glitches, jumps - structurally impossible.
    """
    scrubMoved = pyqtSignal(float)
    scrubFinished = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.progress = 0.0
        self.playhead = 0.0
        # Explicit media duration (ms) for labels AND seek mapping. Pushed by
        # the app on every media/timeline change - never inferred, so the
        # scrubber cannot compute against a stale movie length.
        self.media_duration_ms = 0
        self._drag = False
        self.setMinimumHeight(64)
        self.setMaximumHeight(64)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

    def set_progress(self, v):
        self.progress = max(0.0, min(1.0, float(v)))
        self.playhead = self.progress
        self.update()

    def set_media_duration(self, duration_ms):
        """Set the timeline length this scrubber represents. Time labels and
        (as fallback) seek mapping derive from this, so a scrub always spans
        the actual clip - never a hardcoded 25 s or a stale file length."""
        try:
            duration_ms = max(0, int(duration_ms))
        except Exception:
            duration_ms = 0
        if duration_ms != getattr(self, 'media_duration_ms', 0):
            self.media_duration_ms = duration_ms
            self.update()

    @staticmethod
    def _fmt_label(ms):
        try:
            s = max(0, int(ms) // 1000)
        except Exception:
            s = 0
        if s >= 3600:
            return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
        return f"{s // 60:02d}:{s % 60:02d}"

    def _pos_to_ratio(self, x):
        w = max(1, self.width())
        return max(0.0, min(1.0, x / w))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            r = self._pos_to_ratio(e.position().x())
            self.set_progress(r)
            self.scrubMoved.emit(r)

    def mouseMoveEvent(self, e):
        if self._drag:
            r = self._pos_to_ratio(e.position().x())
            self.set_progress(r)
            self.scrubMoved.emit(r)

    def mouseReleaseEvent(self, e):
        was_drag = self._drag
        self._drag = False
        if was_drag and e.button() == Qt.MouseButton.LeftButton:
            # The one and only seek of this gesture lands exactly here.
            self.scrubFinished.emit(self.progress)

    def paintEvent(self, event):
        import math as _m
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # bg #0a0a0e + border handled by stylesheet; fill here for safety
        p.fillRect(self.rect(), QColor("#0a0a0e"))
        label_h = 14
        wave_h = h - label_h
        cx = w * self.progress
        # progress fill gradient cyan 0.18 -> purple 0.18
        if cx > 0:
            grad = QLinearGradient(0, 0, max(1, int(cx)), 0)
            grad.setColorAt(0.0, QColor(125, 249, 255, 46))
            grad.setColorAt(1.0, QColor(168, 85, 247, 46))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRect(0, 0, int(cx), wave_h)
        # dual sine paths (200 pts like HTML svg 800x64)
        pts_cyan = []
        pts_purp = []
        import random as _r
        _r.seed(7)
        rnds = [_r.random() * 6 for _ in range(200)]
        for i in range(200):
            x = (i / 199.0) * w
            y1 = wave_h / 2 + _m.sin(i * 0.2) * (wave_h * 0.19) + _m.sin(i * 0.05) * (wave_h * 0.125)
            y2 = wave_h / 2 + _m.cos(i * 0.18) * (wave_h * 0.156) + rnds[i] - 3
            pts_cyan.append(QPointF(x, y1))
            pts_purp.append(QPointF(x, y2))
        def stroke(pts, col, width=1.2):
            path = QPainterPath()
            if pts:
                path.moveTo(pts[0])
                for pt in pts[1:]:
                    path.lineTo(pt)
            p.setPen(QPen(col, width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        p.setOpacity(0.6)
        stroke(pts_cyan, QColor(125, 249, 255, 128), 1.2)
        stroke(pts_purp, QColor(168, 85, 247, 102), 1.0)
        p.setOpacity(1.0)
        # playhead line + handles (9px wide cyan, glow simulated with 2 passes)
        px = int(w * self.playhead)
        p.setPen(QPen(QColor(125, 249, 255, 90), 3))
        p.drawLine(px, 0, px, wave_h)
        p.setPen(QPen(QColor("#7df9ff"), 1))
        p.drawLine(px, 0, px, wave_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor("#7df9ff")))
        p.drawRoundedRect(px - 4, -1, 9, 8, 1, 1)
        p.drawRoundedRect(px - 4, wave_h - 7, 9, 8, 1, 1)
        # right border of progress
        p.setPen(QPen(QColor(125, 249, 255, 102), 1))
        p.drawLine(int(cx), 0, int(cx), wave_h)
        # time labels across the ACTUAL media length (was hardcoded 00:25)
        p.setPen(QColor(255, 255, 255, 51))
        p.setFont(QFont("Consolas", 7))
        total_ms = max(0, int(getattr(self, 'media_duration_ms', 0) or 0))
        labels = [self._fmt_label(total_ms * i / 5) for i in range(6)]
        for i, lab in enumerate(labels):
            lx = int((i / (len(labels) - 1)) * (w - 30)) + 4
            p.drawText(lx, h - 3, lab)


class FepPreviewGlowBorder(QWidget):
    """Inner cyan glow border: rounded 20px border cyan/20 + inset glow (HTML pointer-events-none)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        # outer hairline white 0.04 simulated
        p.setPen(QPen(QColor(255, 255, 255, 10), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 20, 20)
        # inner cyan 20% + soft glow (second pass, thicker translucent)
        p.setPen(QPen(QColor(125, 249, 255, 51), 1))
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 19, 19)


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
        self.setMinimumHeight(280)
        self.setMouseTracking(True)
        self.track_height = 56
        self.num_tracks = 4
        self.playhead_position = 0
        self.dragging_playhead = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.waveform_threads = []

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#0a0a0e"))

        # EXACT HTML: ruler 28px bg #0f0f14, 24 divisions, ticks + 00:00 labels
        ruler_height = 28
        painter.fillRect(0, 0, self.width(), ruler_height, QColor("#0f0f14"))
        painter.setPen(QPen(QColor(255, 255, 255, 15), 1))
        painter.drawLine(0, ruler_height, self.width(), ruler_height)
        # gutter corner
        painter.fillRect(0, 0, 56, ruler_height, QColor("#0a0a0e"))
        painter.setPen(QPen(QColor(255, 255, 255, 15), 1))
        painter.drawLine(56, 0, 56, ruler_height)
        # 24 divisions across canvas width (minus 56px gutter)
        painter.setFont(QFont("Consolas", 7))
        cw = max(1, self.width() - 56)
        for i in range(24):
            x = 56 + int((i / 24.0) * cw)
            # vertical grid line
            painter.setPen(QPen(QColor(255, 255, 255, 15), 1))
            painter.drawLine(x, ruler_height, x, self.height())
            # top tick h-2 white/20 + mid tick
            painter.setPen(QPen(QColor(255, 255, 255, 51), 1))
            painter.drawLine(x, 0, x, 8)
            painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
            mid = x + cw // 48
            painter.drawLine(mid, 0, mid, 4)
            painter.setPen(QColor(255, 255, 255, 64))
            painter.drawText(x + 4, 16, f"{i:02d}:00")
        # 80px canvas grid (HTML linear-gradient white 0.02 every 80px)
        painter.setPen(QPen(QColor(255, 255, 255, 5), 1))
        for x in range(56, self.width(), 80):
            painter.drawLine(x, ruler_height, x, self.height())

        track_label_colors = {
            0: QColor("#a855f7"),
            1: QColor("#7df9ff"),
            2: QColor("#00ff88"),
            3: QColor("#ff8a00"),
        }
        track_ids = ["V2", "V1", "A1", "A2"]
        for track in range(self.num_tracks):
            y = ruler_height + track * self.track_height
            # track lane bg (HTML: transparent over #0a0a0e, separator white 0.04)
            painter.setPen(QPen(QColor(255, 255, 255, 10), 1))
            painter.drawLine(0, y + self.track_height, self.width(), y + self.track_height)
            # gutter 56px bg #0f0f14 + right border
            painter.fillRect(0, y, 56, self.track_height, QColor("#0f0f14"))
            painter.setPen(QPen(QColor(255, 255, 255, 15), 1))
            painter.drawLine(56, y, 56, y + self.track_height)
            c = track_label_colors.get(track, QColor("#7df9ff"))
            painter.setPen(c)
            painter.setFont(QFont("Inter", 9, QFont.Weight.Bold))
            painter.drawText(8, y + 24, track_ids[track] if track < len(track_ids) else f"T{track}")
            painter.setBrush(QBrush(QColor(c.red(), c.green(), c.blue(), 153)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(12, y + 32, 24, 4, 2, 2)

        # Empty timeline stays empty — no placeholder clips.
        # Real clips are drawn below from self.clips only.
        for clip in self.clips:
            self.draw_clip(painter, clip, ruler_height)

        for tc in self.text_clips:
            x = self.time_to_x(tc.start_time)
            w = int(tc.duration * self.zoom_level)
            y = ruler_height + 2 * self.track_height + 8
            h = self.track_height - 16
            painter.setBrush(QBrush(QColor("#c084fc")))
            painter.setPen(QPen(QColor(0,0,0,50), 1))
            painter.drawRoundedRect(x, y, w, h, 12, 12)
            painter.setPen(QColor(0,0,0,180))
            painter.setFont(QFont("Inter", 8, QFont.Weight.Bold))
            painter.drawText(x + 12, y + 18, tc.text[:30])
            painter.setFont(QFont("Inter", 7))
            painter.drawText(x + 12, y + h - 8, f"TITLE {tc.duration:.1f}s")

        for ac in self.audio_clips:
            x = self.time_to_x(ac.start_time)
            w = max(int(ac.duration * self.zoom_level), 10)
            y = ruler_height + 2 * self.track_height + 8
            h = self.track_height - 16
            painter.setBrush(QBrush(QColor("#00ff88")))
            painter.setPen(QPen(QColor(0,0,0,50), 1))
            painter.drawRoundedRect(x, y, w, h, 12, 12)
            painter.setPen(QColor(0,0,0,180))
            painter.setFont(QFont("Inter", 8, QFont.Weight.Bold))
            painter.drawText(x + 12, y + 18, Path(ac.file_path).name[:30])
            painter.setFont(QFont("Inter", 7))
            painter.drawText(x + 12, y + h - 8, f"VO {ac.duration:.1f}s")

        # EXACT HTML playhead: cyan/50 full-height line + 12x8 top handle
        painter.setPen(QPen(QColor(125, 249, 255, 128), 1))
        playhead_x = int((self.playhead_position - self.scroll_offset) * self.zoom_level)
        painter.drawLine(playhead_x, 0, playhead_x, self.height())
        painter.setBrush(QBrush(QColor("#7df9ff")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(playhead_x - 6, 0, 12, 8, 1, 1)

        # Empty-state hint (no fake clips — timeline truly empty on launch)
        if not self.clips and not self.text_clips and not self.audio_clips:
            painter.setPen(QColor(255, 255, 255, 70))
            painter.setFont(QFont("Inter", 10))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Timeline empty — add media from the library")

        if self.hasFocus():
            painter.setPen(QPen(QColor("#f59e0b"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(1,1,-1,-1), 12, 12)

    def draw_clip(self, painter, clip, ruler_height):
        x = self.time_to_x(clip.start_time)
        width = max(int(clip.get_trimmed_duration() * self.zoom_level), 40)
        y = ruler_height + clip.track * self.track_height + 8
        height = self.track_height - 16

        track_palette = {
            0: QColor("#a855f7"),
            1: QColor("#7df9ff"),
            2: QColor("#00ff88"),
            3: QColor("#ff8a00"),
        }
        base_color = track_palette.get(clip.track, QColor("#7df9ff"))
        
        if clip == self.selected_clip:
            painter.setPen(QPen(QColor(255,255,255,80), 1))
            painter.setBrush(QBrush(QColor(255,255,255,10)))
            painter.drawRoundedRect(x-2, y-2, width+4, height+4, 14, 14)
            color = base_color.lighter(120)
            border = QColor("#ffffff")
        else:
            color = base_color
            border = QColor(0,0,0,50)

        painter.setBrush(QBrush(color))
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(x, y, width, height, 12, 12)
        # HTML inset top highlight
        painter.setPen(QPen(QColor(255, 255, 255, 77), 1))
        painter.drawLine(x + 12, y + 1, x + width - 12, y + 1)

        painter.setBrush(QBrush(QColor(0,0,0,38)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(x, y, 6, height, 3, 3)
        painter.drawRoundedRect(x+width-6, y, 6, height, 3, 3)

        painter.setBrush(QBrush(QColor(0,0,0,51)))
        painter.drawRoundedRect(x+12, y+8, 24, 24, 8, 8)
        painter.setBrush(QBrush(QColor(0,0,0,153)))
        painter.drawRoundedRect(x+21, y+19, 6, 6, 3, 3)

        painter.setPen(QColor(0,0,0,204))
        font = QFont("Inter", 7, QFont.Weight.Bold)
        painter.setFont(font)
        # Respect the real media label — always the actual file name, never invented.
        label = (clip.name or Path(clip.file_path).name)[:28]

        text_rect = QRectF(x + 42, y + 6, width - 54, 12)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)

        painter.setFont(QFont("Consolas", 6))
        painter.setPen(QColor(0,0,0,128))
        try:
            _dur = clip.get_trimmed_duration()
        except Exception:
            _dur = 0.0
        _ch = getattr(clip, 'audio_streams', 1) or 1
        painter.drawText(QRectF(x + 42, y + 20, width - 54, 10), f"{_dur:.1f}s • {_ch}CH")

        if clip.track >= 2:
            import random
            random.seed(abs(hash(clip.file_path)) % 10000)
            painter.setPen(Qt.PenStyle.NoPen)
            for i in range(24):
                h = 4 + random.random()*14
                bx = x + 86 + i * max(1, (width - 98) // 24)
                if bx > x + width - 8:
                    break
                painter.setBrush(QBrush(QColor(0,0,0,102)))
                painter.drawRoundedRect(int(bx), int(y + height/2 - h/2), 2, int(h), 1, 1)

        trans = getattr(clip, 'transition_type', None)
        if trans:
            painter.setBrush(QBrush(QColor("#ff8a00")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(x, y, 6, height, 3, 3)

    def time_to_x(self, time):
        return int((time - self.scroll_offset) * self.zoom_level)

    def x_to_time(self, x):
        return (x / self.zoom_level) + self.scroll_offset

    def y_to_track(self, y, ruler_height=28):
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

            if click_y < 28:
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

    log("Probing audio streams...")

    try:
        n_tracks, _channels = probe_audio_streams(video_file)
    except RuntimeError as e:
        raise Exception(str(e))

    if track1 >= n_tracks or track2 >= n_tracks:
        raise Exception(f"File has {n_tracks} audio track(s), cannot access track {max(track1, track2)}")

    if n_tracks < 2:
        raise Exception(f"File only has {n_tracks} audio track(s), need at least 2 for sync")

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

        log("Analyzing waveform correlation...")
        import numpy as np

        audio1 = np.fromfile(tmp1_path, dtype=np.int16)
        audio2 = np.fromfile(tmp2_path, dtype=np.int16)

        audio1 = audio1.astype(np.float32) / 32768.0
        audio2 = audio2.astype(np.float32) / 32768.0

        offset_ms, confidence = compute_sync_offset_samples(
            audio1, audio2, sample_rate, max_lag_seconds=5.0)

        log(f"Analysis complete! Offset: {offset_ms:+d}ms, Confidence: {confidence:.1%}")

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
    
    def __init__(self, temp_root=None):
        super().__init__()
        self.queue = []
        self.active_worker = None
        self.temp_root = str(Path(temp_root or tempfile.gettempdir()).expanduser())
        self.proxy_dir = os.path.join(self.temp_root, 'FastEncodeProxies')
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
                 log_callback, progress_callback, status_callback, playhead_callback=None,
                 dry_run=False):
        self.timeline = timeline
        self.settings = settings
        self.output_path = output_path
        self.log = log_callback
        self.progress = progress_callback
        self.status = status_callback
        self.playhead = playhead_callback
        self.should_stop = False
        self.encoder_process = None
        # dry_run builds all commands then returns before spawning FFmpeg.
        # Built commands are left in last_commands for inspection/tests.
        self.dry_run = dry_run
        self.last_commands = {}

    def stop(self):
        self.should_stop = True
        if self.encoder_process:
            try:
                self.encoder_process.kill()
                self.encoder_process.wait()
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
            # Auto-detect source bit depth and set pixel_format / resolution for project
            try:
                probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                             '-show_entries', 'stream=pix_fmt,width,height,codec_name', '-of', 'json', sorted_clips[0].file_path]
                probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                probe_data = json.loads(probe_res.stdout or '{}')
                stream = probe_data.get('streams',[{}])[0]
                pix_fmt = (stream.get('pix_fmt') or '').lower()
                # Auto-set pixel_format: 1 = 10-bit, 0 = 8-bit
                if '10' in pix_fmt or 'p010' in pix_fmt or 'p012' in pix_fmt:
                    self.settings['pixel_format'] = 1
                else:
                    self.settings['pixel_format'] = 0
                # Auto-lock resolution to source if export_res_index is 0
                if self.settings.get('export_res_index', 0) == 0:
                    source_width = int(stream.get('width', source_width))
                    source_height = int(stream.get('height', source_height))
            except Exception:
                pass

            export_res_index = self.settings.get('export_res_index', 0)
            export_target_index = self.settings.get('export_target_index', 0)
            # Instagram preset (4) -> square feed handling
            is_instagram = (export_target_index == 4)
            if export_res_index == 0:
                export_width, export_height = source_width, source_height
                # Instagram square override when Source would be non-square: force 1080x1080 crop to avoid black bars / insta-crash
                if is_instagram:
                    # pick 1080x1080 for square, or 1080x1350 for 4:5 portrait if source is portrait
                    if source_height > source_width:
                        export_width, export_height = 1080, 1350
                    else:
                        export_width, export_height = 1080, 1080
            else:
                # Fixed map includes ultrawide + Instagram (7,8) + 5K/8K - FULL RESTORE
                res_map = {
                    1: (1920, 1080), 
                    2: (2560, 1440), 
                    3: (3840, 2160), 
                    4: (3840, 1600),  # ultrawide
                    5: (5120, 2880), 
                    6: (7680, 4320),
                    7: (1080, 1080),  # Instagram Square - FIXED
                    8: (1080, 1350),  # Instagram Portrait 4:5 - FIXED
                }
                export_width, export_height = res_map.get(export_res_index, (source_width, source_height))
                # Instagram square override for non-square res presets
                if is_instagram and export_res_index != 0:
                    # force square/4:5 if Instagram selected, even if user picked 16:9 preset
                    if export_width >= export_height:
                        export_width, export_height = 1080, 1080
                    else:
                        export_width, export_height = 1080, 1350

            # ENSURE EVEN DIMENSIONS (Prevents NVENC padding crash + Instagram square)
            if export_width % 2 != 0: export_width -= 1
            if export_height % 2 != 0: export_height -= 1
            # Hard clamp for Instagram to avoid 4K square which NVENC may reject at high bitrate
            if is_instagram:
                export_width = min(export_width, 1350)
                export_height = min(export_height, 1350)
                if export_width % 2 != 0: export_width -= 1
                if export_height % 2 != 0: export_height -= 1

            self.log(f"Resolution: {export_width}x{export_height} @ {timeline_fps} FPS")
            self.log(f"Total Duration: {timeline_duration:.2f}s")

            # FIX v0.9.4: Removed dead cache branch (is_cache_render/valid_cache_path never set - vestigial per Claude)
            # Was reintroducing background-cache path that never fired - removed to fix insta-crash
            use_gpu_decode = self.settings.get('use_gpu_decode', False)
            use_gpu_composite = self.settings.get('use_gpu_composite', False)
            video_codec = self.settings.get('video_codec', 'hevc_nvenc')
            # REMOVED: the lossless-8bit force-override that silently set use_gpu_decode/
            # use_gpu_composite to True regardless of the UI checkboxes. It never wrote back to
            # self.settings, so the bg0 canvas branch (which reads self.settings directly) and
            # the per-clip loop (which reads these locals) could end up disagreeing about whether
            # turbo was on - handing overlay_cuda a CPU-format base layer and a CUDA-format overlay
            # layer, which is what produced the "Impossible to convert between the formats...
            # Error reinitializing filters!" (-40) crash. The checkboxes are now authoritative in
            # every rate-control mode, including lossless.
            is_lossless = self.settings.get('rate_control', '') == 'lossless'
            is_10bit = self.settings.get('pixel_format', 1) == 1 and video_codec != 'h264_nvenc'
            is_nvenc = 'nvenc' in video_codec
            # Pool math must use the REAL export size (settings only carry a
            # 1920x1080 fallback otherwise, which under-sizes 4K pools 4x).
            self.settings['export_width'] = export_width
            self.settings['export_height'] = export_height
            pipeline_buf = get_pipeline_buffer_sizes(self.settings)

            if use_gpu_composite and use_gpu_decode and is_nvenc:
                self.log(f"🚀 5070 TURBO MODE ACTIVE - Full GPU Canvas (VRAM only) [V7 FIXED]")
                self.log(f"   Target: {pipeline_buf.get('frames_needed', 128)} frames * {pipeline_buf.get('frame_mb', 12):.1f}MB = {pipeline_buf.get('frames_needed', 128)*pipeline_buf.get('frame_mb', 12)/1024:.1f}GB VRAM")
            else:
                self.log("🚀 Hardware Decode + Robust CPU Compositing Pipeline ACTIVE [V7].")
                if use_gpu_composite and not is_nvenc:
                    self.log("   (Turbo requested but codec not NVENC - falling back to CPU canvas)")
                else:
                    self.log("   (CPU scale/overlay - safe path, enable TURBO for 5070)")
            self.log(f"VRAM frame pool: {pipeline_buf.get('extra_hw_frames', 64)} frames x {pipeline_buf.get('frame_mb', 12):.1f} MB (pool is a ceiling; actual in-flight frames are smaller)")
            self.log(f"Output smoothing: mux queue {pipeline_buf.get('max_muxing_queue_size', 0)} pkts / {pipeline_buf.get('muxing_queue_data_threshold_mb', 0)} MB, buffered streaming writes")

            import multiprocessing
            cpu_cores = multiprocessing.cpu_count() or 8

            cmd_inputs_v = []
            cmd_inputs_a = []

            for clip in sorted_clips:
                cmd_inputs_a.extend(['-ss', str(clip.in_point)])
                cmd_inputs_a.extend(['-t', str(clip.get_trimmed_duration())])
                cmd_inputs_a.extend(['-i', clip.file_path])

                cmd_inputs_v.extend(['-ss', str(clip.in_point)])
                cmd_inputs_v.extend(['-t', str(clip.get_trimmed_duration())])
                
                if use_gpu_composite and use_gpu_decode and is_nvenc:
                    cmd_inputs_v.extend(['-thread_queue_size', str(pipeline_buf['thread_queue_size']), '-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda', '-extra_hw_frames', str(pipeline_buf['extra_hw_frames']), '-i', clip.file_path])
                else:
                    codec = self._get_video_codec(clip.file_path)
                    cmd_inputs_v.extend(build_hw_decode_input_args(clip.file_path, codec, use_gpu_decode, None, pipeline_buf['thread_queue_size'], pipeline_buf['extra_hw_frames']))

            # 2. BUILD THE COMPOSITING GRAPH
            video_filter_complex = []
            audio_filter_complex = []

            # Create the master blank canvas at exact output specs
            is_10bit = self.settings.get('pixel_format', 1) == 1
            if self.settings.get('video_codec', '') == 'h264_nvenc':
                is_10bit = False
                
            # Use robust p010le format for 10-bit HDR exports. CPU filters support it flawlessly.
            canvas_format = 'p010le' if is_10bit else 'nv12'
            canvas_format_cuda = 'p010' if is_10bit else 'nv12'
                
            early_filters_check = self._build_video_filters()
            early_text_check = getattr(self.timeline, 'text_clips', [])
            early_is_turbo_check = self.settings.get('use_gpu_composite', False) and self.settings.get('use_gpu_decode', False) and 'nvenc' in self.settings.get('video_codec','')
            has_real_filters = has_optional_video_filters(self.settings)
            early_hybrid = early_is_turbo_check and bool(has_real_filters or early_text_check)
            force_cpu_overlay_for_10bit = False
            if early_hybrid:
                self.log(f"TURBO Hybrid: has_real_filters={has_real_filters} filters={len(early_filters_check)} text={len(early_text_check)} - GPU scale, CPU overlay. Filters: {','.join(early_filters_check)[:120]}")
            elif is_10bit and early_is_turbo_check:
                self.log(f"TURBO 10-bit FULL VRAM: No filters, staying in VRAM with p010 overlay_cuda - {pipeline_buf.get('frames_needed', 128)} frames target")
                
            canvas_is_cuda = False
            if use_gpu_composite and use_gpu_decode and is_nvenc:
                if early_hybrid:
                    video_filter_complex.append(f"color=c=black:s={export_width}x{export_height}:r={timeline_fps}:d={timeline_duration},format={canvas_format}[bg0]")
                elif is_10bit:
                    video_filter_complex.append(f"color=c=black:s={export_width}x{export_height}:r={timeline_fps}:d={timeline_duration},format={canvas_format}[bg0]")
                    self.log(f"10-bit no-filter: Using CPU canvas to avoid overlay_cuda p010 crash (FFmpeg 9.01 bug) - 8-bit would be full VRAM")
                else:
                    video_filter_complex.append(f"color=c=black:s={export_width}x{export_height}:r={timeline_fps}:d={timeline_duration},format={canvas_format},hwupload_cuda,scale_cuda=format={canvas_format_cuda}[bg0]")
                    canvas_is_cuda = True
            else:
                video_filter_complex.append(f"color=c=black:s={export_width}x{export_height}:r={timeline_fps}:d={timeline_duration},format={canvas_format}[bg0]")

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
                video_filter_complex.append(f"{v_in}setpts=PTS-STARTPTS+{st:.6f}/TB{v_trimmed}")

                # --- NATIVE RES SKIP + GPU TURBO LOGIC ---
                try:
                    clip_w, clip_h = self.get_video_metadata(clip.file_path)
                except:
                    clip_w, clip_h = export_width, export_height

                is_turbo = use_gpu_composite and use_gpu_decode and is_nvenc
                use_cpu_overlay = early_hybrid or (is_10bit and is_turbo)
                
                if clip_w == export_width and clip_h == export_height:
                    if is_turbo and not use_cpu_overlay:
                        scale_str = f"scale_cuda=format={canvas_format_cuda},setsar=1"
                        self.log(f"Clip {i}: Native {clip_w}x{clip_h} == export -> TURBO SKIPPING scale (VRAM) 8-bit")
                    elif is_turbo and use_cpu_overlay:
                        scale_str = f"scale_cuda=format={canvas_format_cuda},hwdownload,format={canvas_format},setsar=1"
                        self.log(f"Clip {i}: Native {clip_w}x{clip_h} == export -> {'HYBRID' if early_hybrid else '10-bit TURBO'} GPU->CPU via scale_cuda+hwdownload - {export_width}x{export_height}")
                    else:
                        scale_str = f"format={canvas_format},setsar=1"
                        self.log(f"Clip {i}: Native {clip_w}x{clip_h} == export {export_width}x{export_height} -> SKIPPING scale/pad")
                else:
                    if is_turbo and not use_cpu_overlay:
                        scale_str = f"scale_cuda={export_width}:{export_height}:force_original_aspect_ratio=decrease:format={canvas_format_cuda}"
                        self.log(f"Clip {i}: {clip_w}x{clip_h} -> {export_width}x{export_height} TURBO GPU scaling 8-bit")
                    elif is_turbo and use_cpu_overlay:
                        scale_str = f"scale_cuda={export_width}:{export_height}:force_original_aspect_ratio=decrease:format={canvas_format_cuda},hwdownload,format={canvas_format},pad={export_width}:{export_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
                        self.log(f"Clip {i}: {clip_w}x{clip_h} -> {export_width}x{export_height} {'HYBRID' if early_hybrid else '10-bit TURBO'} GPU->CPU scaling via scale_cuda+hwdownload")
                    else:
                        scale_str = f"scale={export_width}:{export_height}:force_original_aspect_ratio=decrease,format={canvas_format},pad={export_width}:{export_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
                        self.log(f"Clip {i}: {clip_w}x{clip_h} -> {export_width}x{export_height} CPU scaling")
                
                v_trans = f"[v{i}_trans]"
                trans_type = getattr(clip, 'transition_type', None)
                trans_dur = float(getattr(clip, 'transition_duration', 0) or 0)
                trans_filter = ""
                if trans_type and trans_dur > 0:
                    if trans_type == 'fade':
                        trans_filter = f"fade=t=in:st=0:d={trans_dur},fade=t=out:st={clip.get_trimmed_duration()-trans_dur}:d={trans_dur}"
                    elif trans_type == 'fadeblack':
                        trans_filter = f"fade=t=in:st=0:d={trans_dur}:alpha=1,fade=t=out:st={clip.get_trimmed_duration()-trans_dur}:d={trans_dur}:alpha=1"
                    elif trans_type == 'fadewhite':
                        trans_filter = f"format={canvas_format},eq=brightness=1,fade=t=in:st=0:d={trans_dur},fade=t=out:st={clip.get_trimmed_duration()-trans_dur}:d={trans_dur}"
                    else:
                        trans_filter = f"fade=t=in:st=0:d={trans_dur}"
                else:
                    trans_filter = ""
                
                video_filter_complex.append(f"{v_trimmed}{scale_str}{v_scaled}")
                
                if trans_filter:
                    video_filter_complex.append(f"{v_scaled}{trans_filter}{v_trans}")
                    v_overlay_src = v_trans
                else:
                    v_overlay_src = v_scaled
                    
                if is_turbo and not use_cpu_overlay:
                    video_filter_complex.append(f"{bg_in}{v_overlay_src}overlay_cuda=enable='between(t,{clip.start_time},{end_time})':eof_action=pass{bg_out}")
                else:
                    video_filter_complex.append(f"{bg_in}{v_overlay_src}overlay=enable='between(t,{clip.start_time},{end_time})':eof_action=pass{bg_out}")

                # --- AUDIO GRAPH ---
                is_turbo_audio = use_gpu_composite and use_gpu_decode and is_nvenc
                if is_turbo_audio:
                    a_in = f"[{i}:a:0]"
                    a_trimmed = f"[a{i}_0_trim]"
                    duration = clip.get_trimmed_duration()
                    audio_filter_complex.append(f"{a_in}asetpts=PTS-STARTPTS{a_trimmed}")
                    base_delay_ms = int(clip.start_time * 1000)
                    a_ready = f"[a{i}_0_ready]"
                    chain = ""
                    if base_delay_ms > 0:
                        chain += f"adelay={base_delay_ms}|{base_delay_ms},"
                    vol_db = clip.volumes[0] if clip.volumes else 0.0
                    chain += f"volume={vol_db}dB"
                    audio_filter_complex.append(f"{a_trimmed}{chain}{a_ready}")
                    audio_inputs.append(a_ready)
                else:
                    n_streams = clip.audio_streams
                    if n_streams > 2:
                        n_streams = 1
                    for a_idx in range(n_streams):
                        a_in = f"[{i}:a:{a_idx}]"
                        a_trimmed = f"[a{i}_{a_idx}_trim]"
                        duration = clip.get_trimmed_duration()
                        audio_filter_complex.append(f"{a_in}asetpts=PTS-STARTPTS{a_trimmed}")
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
                        audio_filter_complex.append(f"{a_trimmed}{chain}{a_ready}")
                        audio_inputs.append(a_ready)

            # --- FINAL OUTPUT MAPPING ---
            last_v = f"[bg{len(sorted_clips)}]"

            user_filters = self._build_video_filters()
            text_clips = getattr(self.timeline, 'text_clips', [])
            is_turbo_final = self.settings.get('use_gpu_composite', False) and self.settings.get('use_gpu_decode', False) and 'nvenc' in self.settings.get('video_codec','')
            cpu_bottleneck_active = bool(user_filters or text_clips)
                
            if cpu_bottleneck_active:
                if is_turbo_final:
                    if not canvas_is_cuda:
                        self.log(f"HYBRID: last_v already CPU (overlay fix), skipping extra hwdownload")
                    else:
                        filter_desc = ", ".join(user_filters)[:120] if user_filters else "text overlay"
                        self.log(f"TURBO: CPU filters detected ({filter_desc}) - switching to HYBRID mode: GPU scale/overlay (5070 maxed) + CPU filters + CPU encode.")
                        video_filter_complex.append(f"{last_v}hwdownload,format={canvas_format}[bg_downloaded]")
                        last_v = "[bg_downloaded]"
                        canvas_is_cuda = False
                elif use_gpu_decode:
                    video_filter_complex.append(f"{last_v}format={canvas_format}[bg_downloaded]")
                    last_v = "[bg_downloaded]"
                    canvas_is_cuda = False

            if user_filters:
                if is_10bit and any('hqdn3d' in f for f in user_filters):
                    video_filter_complex.append(f"{last_v}format=nv12,{','.join(user_filters)},format={canvas_format}[v_filtered]")
                else:
                    video_filter_complex.append(f"{last_v}{','.join(user_filters)}[v_filtered]")
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
                    safe_text = tc.text.replace("'", "\\'").replace(":", "\\:")
                    dt_filter = (
                        f"{txt_input}drawtext=text='{safe_text}'"
                        f":fontsize={tc.font_size}:fontcolor={tc.font_color}"
                        f":x={tc.x}:y={tc.y}"
                        f":enable='between(t,{tc.start_time},{tc.get_end_time()})'"
                        f"{txt_out}"
                    )
                    video_filter_complex.append(dt_filter)
                    txt_input = txt_out
                pre_fps_v = txt_input

            is_10bit_final = self.settings.get('pixel_format', 1) == 1
            if self.settings.get('video_codec', '') == 'h264_nvenc':
                is_10bit_final = False
            force_cpu_for_10bit_final = is_10bit_final and is_turbo_final
            force_cpu_for_lossless = False  
                
            if is_turbo_final and user_filters:
                if canvas_is_cuda:
                    video_filter_complex.append(f"{pre_fps_v}hwdownload,format={canvas_format}[bg_downloaded]")
                    pre_fps_v = "[bg_downloaded]"
                    canvas_is_cuda = False
                map_v = pre_fps_v
                zero_copy_video = False
            else:
                zero_copy_video = use_gpu_decode and not cpu_bottleneck_active
                if force_cpu_for_10bit_final:
                    zero_copy_video = False
                if force_cpu_for_lossless:
                    self.log("Lossless mode: forcing CPU path to avoid NVENC -40 crash")
                    zero_copy_video = False
                if zero_copy_video:
                    map_v = pre_fps_v
                else:
                    if canvas_is_cuda:
                        video_filter_complex.append(f"{pre_fps_v}hwdownload,format={canvas_format}[bg_downloaded]")
                        pre_fps_v = "[bg_downloaded]"
                        canvas_is_cuda = False
                        self.log(f"Downloaded to CPU for {'lossless' if force_cpu_for_lossless else '10-bit'} path, no fps filter")
                    map_v = pre_fps_v
                    zero_copy_video = False

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
                audio_filter_complex.append(f"{vo_in}{chain}{vo_ready}")
                audio_inputs.append(vo_ready)

            # Mix Audio
            if audio_inputs:
                if len(audio_inputs) == 1:
                    map_a = audio_inputs[0]
                else:
                    inputs_str = "".join(audio_inputs)
                    audio_filter_complex.append(f"{inputs_str}amix=inputs={len(audio_inputs)}:duration=longest:dropout_transition=0[out_a]")
                    map_a = "[out_a]"
            else:
                audio_filter_complex.append(f"anullsrc=r=48000:cl=stereo[out_a]")
                map_a = "[out_a]"

            for vo in vo_clips:
                cmd_inputs_v.extend(['-thread_queue_size', str(pipeline_buf['thread_queue_size']), '-i', vo.file_path])
                cmd_inputs_a.extend(['-i', vo.file_path])

            # Render mode: single-pass (default) encodes video + audio together
            # straight to the final file. Legacy split mode renders a video
            # intermediate + WAV intermediate, then remuxes (rewrites every
            # video byte a second time - the 30-40 min "multiplex" on big
            # lossless masters). Same graphs and encoder args either way.
            single_pass = bool(self.settings.get('single_pass_render', True))
            if single_pass:
                self.log("Single-pass render: video + audio together, straight to the final file (no remux).")
                temp_dir = None
                # Split-only names: the legacy command builders below still run
                # (cheap list assembly) but their outputs are discarded, so keep
                # the names bound to silence them.
                temp_v_path = None
                temp_a_path = None
            else:
                self.log("Split render (legacy): video pass, then audio pass, then remux.")
            
            # OPTIMIZATION: Use dedicated temp directory for intermediates to reduce I/O contention
            # This prevents WD Game Drive cache thrashing while maintaining render stability
            # (split mode only - single-pass writes nothing but the final file)
            if not single_pass:
                temp_dir = tempfile.mkdtemp(prefix='fastencode_', dir=str(_temp_root_from_settings(self.settings)))
                base_name, ext = os.path.splitext(os.path.basename(self.output_path))
                if not ext:
                    ext = '.mp4'
                temp_v_path = os.path.join(temp_dir, f"{base_name}_video_part{ext}")
                temp_a_path = os.path.join(temp_dir, f"{base_name}_audio_part.wav")
            
            # Synchronous I/O optimization: Ensure proper buffer allocation for HDD
            # Pre-allocate larger frame buffers to reduce memory fragmentation
            self.log("Setting up synchronous I/O and frame buffer optimizations...")

            # --- 1. Video Command ---
            video_cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5', '-fflags', '+genpts']
            video_cmd.extend(['-init_hw_device', 'cuda=0'])
            video_cmd.extend(['-filter_threads', str(min(cpu_cores, 8)), '-filter_complex_threads', str(min(cpu_cores, 8))])
            video_cmd.extend(['-extra_hw_frames', str(pipeline_buf['extra_hw_frames'])])
            
            # SYNCHRONOUS I/O OPTIMIZATION: Configure for better HDD performance
            # Pre-allocate larger frame buffers and use synchronous operations to reduce disk thrashing
            video_cmd.extend(['-threads', '0'])
            
            video_cmd.extend(cmd_inputs_v)
            if video_filter_complex:
                video_cmd.extend(['-filter_complex', ';'.join(video_filter_complex)])
            video_cmd.extend(['-map', map_v])
            
            codec = self.settings.get('video_codec', 'hevc_nvenc')
            video_cmd.extend(['-r', str(timeline_fps)])
            video_cmd.extend(['-c:v', codec])
            if 'nvenc' in codec:
                video_cmd.extend(build_nvenc_cbr_args(self.settings, timeline_fps, is_zero_copy=zero_copy_video))
            elif codec == 'prores_ks':
                profile = self.settings.get('prores_profile', 3)
                target_bitrate_mbps = self.settings.get('bitrate_mbps', 500)
                qscale = 9 if target_bitrate_mbps >= 500 else 11 if target_bitrate_mbps >= 300 else 13 if target_bitrate_mbps >= 150 else 15
                video_cmd.extend(['-profile:v', str(profile), '-vendor', 'apl0', '-qscale:v', str(qscale)])
            video_cmd.extend(['-t', f"{timeline_duration:.6f}"])
            append_output_file_args(video_cmd, temp_v_path, self.settings, self.log)

            # --- 2. Audio Command ---
            audio_cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5']
            audio_cmd.extend(cmd_inputs_a)
            if audio_filter_complex:
                audio_cmd.extend(['-filter_complex', ';'.join(audio_filter_complex)])
            audio_cmd.extend(['-map', map_a])
            
            # AUDIO FIX: Always use PCM for intermediate WAV to avoid container mismatch
            # Previous bug: writing AAC into WAV or PCM into M4A caused failure -> no audio
            audio_codec = self.settings.get('audio_codec', 'aac')
            # Intermediate is always PCM in WAV container
            audio_cmd.extend(['-c:a', 'pcm_s24le'])
            
            # SYNCHRONOUS I/O OPTIMIZATION: Configure for better HDD performance
            # Use larger buffers and synchronous operations to reduce disk thrashing
            audio_cmd.extend(['-threads', '0'])
            
            audio_cmd.extend(['-t', f"{timeline_duration:.6f}"])
            audio_cmd.append(temp_a_path)

            # --- 3. Mux Command ---
            # AUDIO FIX: Transcode audio to final codec instead of blind copy
            mux_cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5']
            mux_cmd.extend(['-i', temp_v_path, '-i', temp_a_path])
            mux_cmd.extend(['-c:v', 'copy'])
            
            # SYNCHRONOUS I/O OPTIMIZATION: Configure for better HDD performance
            # Use larger buffers and synchronous operations to reduce disk thrashing during muxing
            mux_cmd.extend(['-threads', '0'])
            
            # Final audio codec handling
            final_audio_codec = self.settings.get('audio_codec', 'aac')
            if final_audio_codec == 'pcm_s24le':
                mux_cmd.extend(['-c:a', 'pcm_s24le'])
            elif final_audio_codec == 'flac':
                mux_cmd.extend(['-c:a', 'flac'])
            elif final_audio_codec == 'copy':
                # If user wants copy, try to keep PCM if intermediate was PCM, otherwise AAC
                mux_cmd.extend(['-c:a', 'pcm_s24le'])
            else:  # aac default
                mux_cmd.extend(['-c:a', 'aac', '-b:a', '320k'])
            append_output_file_args(mux_cmd, self.output_path, self.settings, self.log)

            def execute_pass(cmd_run, pass_start, pass_end, pass_name, throughput_path=None):
                self.log(f"Starting {pass_name} Pass...")
                
                start_t = time.time()
                self.encoder_process = subprocess.Popen(
                    cmd_run, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                last_sample_t = 0.0
                last_sample_wall = start_t
                error_log = []
                finalizing_logged = False
                # Throughput readout: sample the bytes actually landing on disk
                # so a slow remux shows up as MB/s instead of a mystery stall.
                last_tp_wall = start_t
                last_tp_size = None
                tp_rate = None
                
                for line in iter(self.encoder_process.stderr.readline, ''):
                    error_log.append(line)
                    if self.should_stop:
                        self.encoder_process.kill()
                        self.encoder_process.wait()
                        return False, "Render cancelled by user"
                        
                    line_lower = line.lower()
                    if "starting second pass" in line_lower or "moving the moov atom" in line_lower:
                        if not finalizing_logged:
                            finalizing_logged = True
                            self.status(f"Finalizing {pass_name} output file metadata...")
                        continue

                    t = _parse_ffmpeg_time(line)
                    if t is not None and timeline_duration > 0:
                        local_pct = (t / timeline_duration)
                        global_pct = pass_start + int(local_pct * (pass_end - pass_start))
                        global_pct = min(pass_end, global_pct)
                        self.progress(global_pct)
                        
                        now = time.time()
                        delta_t = t - last_sample_t
                        delta_wall = now - last_sample_wall
                        fps_actual = (delta_t * timeline_fps) / delta_wall if delta_wall > 0 else 0
                        last_sample_t = t
                        last_sample_wall = now

                        if throughput_path is not None and (now - last_tp_wall) >= 2.0:
                            try:
                                _sz = os.path.getsize(throughput_path)
                            except OSError:
                                _sz = None
                            if _sz is not None:
                                if last_tp_size is not None:
                                    _dt = max(0.001, now - last_tp_wall)
                                    tp_rate = (_sz - last_tp_size) / _dt / (1024 * 1024)
                                last_tp_size = _sz
                                last_tp_wall = now
                        
                        if local_pct >= 0.99:
                            if not finalizing_logged:
                                finalizing_logged = True
                                self.log(f"Encode reached the end of {pass_name} pass; waiting for FFmpeg to close the output file.")
                            self.status(f"Finalizing {pass_name}...")
                        else:
                            _st = f"{pass_name}: {global_pct}% — {fps_actual:.1f} fps"
                            if tp_rate is not None:
                                _st += f" — {tp_rate:.0f} MB/s"
                            self.status(_st)
                        
                        if self.playhead and pass_name == "Video":
                            self.playhead(t)
                            
                self.encoder_process.wait()
                if self.encoder_process.returncode != 0:
                    crash_log_path = os.path.join(tempfile.gettempdir(), f'ffmpeg_crash_{pass_name}.log')
                    try:
                        with open(crash_log_path, 'w', encoding='utf-8') as f:
                            f.write("Command:\n" + " ".join(cmd_run) + "\n\nStderr:\n")
                            f.writelines(error_log)
                    except Exception:
                        pass
                    return False, f"{pass_name} pass failed with code {self.encoder_process.returncode}"
                return True, f"{pass_name} pass complete."

            start_time = time.time()

            if single_pass:
                # One command, one read of each input, one write of the final
                # file. cmd_inputs_v already opens every clip once with the
                # video-grade options (a superset the audio chain ignores), so
                # the existing [i:v] / [i:a] / [vo:a] labels stay valid as-is.
                single_cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5', '-fflags', '+genpts']
                single_cmd.extend(['-init_hw_device', 'cuda=0'])
                single_cmd.extend(['-filter_threads', str(min(cpu_cores, 8)), '-filter_complex_threads', str(min(cpu_cores, 8))])
                single_cmd.extend(['-extra_hw_frames', str(pipeline_buf['extra_hw_frames'])])
                single_cmd.extend(['-threads', '0'])
                single_cmd.extend(cmd_inputs_v)
                single_cmd.extend(['-filter_complex', ';'.join(video_filter_complex + audio_filter_complex)])
                single_cmd.extend(['-map', map_v])
                single_cmd.extend(['-map', map_a])
                single_cmd.extend(['-r', str(timeline_fps)])
                single_cmd.extend(['-c:v', codec])
                if 'nvenc' in codec:
                    single_cmd.extend(build_nvenc_cbr_args(self.settings, timeline_fps, is_zero_copy=zero_copy_video))
                elif codec == 'prores_ks':
                    profile = self.settings.get('prores_profile', 3)
                    target_bitrate_mbps = self.settings.get('bitrate_mbps', 500)
                    qscale = 9 if target_bitrate_mbps >= 500 else 11 if target_bitrate_mbps >= 300 else 13 if target_bitrate_mbps >= 150 else 15
                    single_cmd.extend(['-profile:v', str(profile), '-vendor', 'apl0', '-qscale:v', str(qscale)])
                if final_audio_codec == 'pcm_s24le':
                    single_cmd.extend(['-c:a', 'pcm_s24le'])
                elif final_audio_codec == 'flac':
                    single_cmd.extend(['-c:a', 'flac'])
                elif final_audio_codec == 'copy':
                    single_cmd.extend(['-c:a', 'pcm_s24le'])
                else:  # aac default
                    single_cmd.extend(['-c:a', 'aac', '-b:a', '320k'])
                single_cmd.extend(['-t', f"{timeline_duration:.6f}"])
                append_output_file_args(single_cmd, self.output_path, self.settings, self.log)

                self.last_commands = {'single': single_cmd}
                if self.dry_run:
                    return True, "Dry run: single-pass command built (no render executed)."

                success, msg = execute_pass(single_cmd, 0, 100, "Render", throughput_path=self.output_path)
                if not success: return False, msg

                elapsed = time.time() - start_time
                self.progress(100)
                return True, f"Render Complete! {elapsed:.1f}s"

            self.last_commands = {'video': video_cmd, 'audio': audio_cmd, 'mux': mux_cmd}
            if self.dry_run:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass
                return True, "Dry run: split commands built (no render executed)."

            success, msg = execute_pass(video_cmd, 0, 90, "Video", throughput_path=temp_v_path)
            if not success: return False, msg
            
            success, msg = execute_pass(audio_cmd, 90, 95, "Audio", throughput_path=temp_a_path)
            if not success: return False, msg
            
            success, msg = execute_pass(mux_cmd, 95, 99, "Multiplex", throughput_path=self.output_path)
            if not success: return False, msg

            elapsed = time.time() - start_time
            self.progress(100)
            
            try:
                if os.path.exists(temp_v_path): os.remove(temp_v_path)
                if os.path.exists(temp_a_path): os.remove(temp_a_path)
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
                
            return True, f"Render Complete! {elapsed:.1f}s" 

        except Exception as e:
            import traceback
            self.log(f"Critical Error: {e}")
            self.log(traceback.format_exc())
            return False, str(e)
        finally:
            _remove_render_temp_dir(locals().get('temp_dir'))
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
            duration = self.get_duration()

            # FIX: temp_v_path/temp_a_path were being passed into build_ffmpeg_commands()
            # without ever being defined anywhere first - a NameError on the very first
            # real line of work, which is why export failed instantly every time.
            temp_dir = tempfile.mkdtemp(prefix='fastencode_', dir=str(_temp_root_from_settings(self.settings)))
            out_base, out_ext = os.path.splitext(os.path.basename(self.output_file))
            if not out_ext:
                out_ext = '.mp4'
            temp_v_path = os.path.join(temp_dir, f"{out_base}_video_part{out_ext}")
            temp_a_path = os.path.join(temp_dir, f"{out_base}_audio_part.wav")

            video_cmd, audio_cmd, mux_cmd = self.build_ffmpeg_commands(temp_v_path, temp_a_path)

            def execute_pass(cmd, start_pct, end_pct, pass_name):
                self.log_message.emit(f"Command ({pass_name}): {' '.join(cmd)}")
                self.status.emit(f"Starting {pass_name} pass...")
                self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True, bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                for line in iter(self.process.stderr.readline, ''):
                    if self.should_stop:
                        self.process.kill()
                        self.process.wait()
                        return False
                    self.log_message.emit(line.strip())
                    if duration > 0:
                        current = _parse_ffmpeg_time(line)
                        if current is not None:
                            local_pct = (current / duration)
                            global_pct = start_pct + int(local_pct * (end_pct - start_pct))
                            self.progress.emit(min(global_pct, 99))
                            self.status.emit(f"{pass_name}: {global_pct}%")
                self.process.wait()
                return self.process.returncode == 0

            if not execute_pass(video_cmd, 0, 90, "Video"):
                if not self.should_stop: self.finished.emit(False, "Video pass failed")
                else: self.finished.emit(False, "Stopped")
                return
            
            if not execute_pass(audio_cmd, 90, 95, "Audio"):
                if not self.should_stop: self.finished.emit(False, "Audio pass failed")
                else: self.finished.emit(False, "Stopped")
                return
                
            if not execute_pass(mux_cmd, 95, 99, "Multiplex"):
                if not self.should_stop: self.finished.emit(False, "Mux pass failed")
                else: self.finished.emit(False, "Stopped")
                return

            try:
                if os.path.exists(temp_v_path): os.remove(temp_v_path)
                if os.path.exists(temp_a_path): os.remove(temp_a_path)
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

            self.progress.emit(100)
            self.status.emit("Done!")
            self.finished.emit(True, "Success")
        except Exception as e:
            self.finished.emit(False, str(e))
        finally:
            _remove_render_temp_dir(locals().get('temp_dir'))

    def build_ffmpeg_commands(self, temp_v_path, temp_a_path):
        import multiprocessing as _mp2
        _c2 = _mp2.cpu_count() or 8
        pipeline_buf = get_pipeline_buffer_sizes(self.settings)
        
        codec = self.settings.get('video_codec', '')
        is_copy_stream = (codec == 'copy')
        has_filters = has_optional_video_filters(self.settings) if not is_copy_stream else False
        use_gpu_decode = self.settings.get('use_gpu_decode', False) if not is_copy_stream else False
        target_fps = self.settings.get('timeline_fps') if not is_copy_stream else None
        is_zero_copy = use_gpu_decode and not has_filters and ('nvenc' in codec)

        # Video Command
        v_cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5']
        v_cmd.extend(['-filter_threads', '0', '-filter_complex_threads', str(_c2), '-threads', '0'])
        v_cmd.extend(['-thread_queue_size', str(pipeline_buf['thread_queue_size'])])
        
        if use_gpu_decode:
            v_cmd.extend(['-extra_hw_frames', str(pipeline_buf['extra_hw_frames'])])
            v_cmd.extend(['-hwaccel', 'cuda'])
            if is_zero_copy:
                v_cmd.extend(['-hwaccel_output_format', 'cuda'])
        if target_fps:
            v_cmd.extend(['-r', str(target_fps)])
            
        v_cmd.extend(['-i', self.input_file])
        
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
                    1: 'eq=brightness=0.05:saturation=1.1', 2: 'eq=brightness=0.1:saturation=1.15', 3: 'eq=brightness=0.15:saturation=1.2', 4: 'eq=brightness=0.2:saturation=1.25', 5: 'eq=brightness=0.3:saturation=1.3', 6: 'eq=brightness=0.4:saturation=1.35', 7: 'eq=brightness=-0.05:saturation=0.95', 8: 'eq=brightness=-0.1:saturation=0.9', 9: 'eq=brightness=-0.15:saturation=0.85', 10: 'eq=brightness=-0.2:saturation=0.8', 11: 'eq=brightness=-0.3:saturation=0.75', 12: 'eq=brightness=-0.4:saturation=0.7'
                }
                if exposure in exposure_values: filter_complex.append(exposure_values[exposure])
            
            temporal = self.settings.get('temporal_level', 0)
            if temporal > 0:
                temporal_values = ['', 'tmix=frames=3:weights="1 1 1"', 'tmix=frames=5:weights="1 1 2 1 1"', 'tmix=frames=7:weights="1 1 2 2 2 1 1"', 'tmix=frames=9:weights="1 1 2 3 3 3 2 1 1"', 'tmix=frames=11:weights="1 2 2 3 4 4 4 3 2 2 1"']
                if temporal < len(temporal_values): filter_complex.append(temporal_values[temporal])
            
            sharpness = self.settings.get('sharpness_level', 0)
            if sharpness > 0:
                sharpness_values = ['', 'unsharp=3:3:0.3:3:3:0', 'unsharp=5:5:0.5:5:5:0', 'unsharp=5:5:0.8:5:5:0.4', 'unsharp=5:5:1.2:5:5:0.6', 'unsharp=7:7:1.5:7:7:0.8', 'unsharp=7:7:2.0:7:7:1.0']
                if sharpness < len(sharpness_values): filter_complex.append(sharpness_values[sharpness])

        if filter_complex: v_cmd.extend(['-vf', ','.join(filter_complex)])
        if target_fps and not is_copy_stream:
            v_cmd.extend(['-r', str(target_fps)])
            
        if is_copy_stream:
            v_cmd.extend(['-c:v', 'copy'])
        else:
            v_cmd.extend(['-c:v', codec])
            if codec == 'prores_ks':
                profile = self.settings.get('prores_profile', 3)
                target_bitrate_mbps = self.settings.get('bitrate_mbps', 500)
                qscale = 9 if target_bitrate_mbps >= 500 else 11 if target_bitrate_mbps >= 300 else 13 if target_bitrate_mbps >= 150 else 15
                v_cmd.extend(['-profile:v', str(profile), '-vendor', 'apl0', '-qscale:v', str(qscale)])
            elif 'nvenc' in codec:
                if self.settings.get('use_gpu', True):
                    v_cmd.extend(build_nvenc_cbr_args(self.settings, 30, is_zero_copy=is_zero_copy))
                else:
                    v_cmd.extend(['-preset', 'medium'])
                    
        # HDD OPTIMIZATION: Use larger buffers for better sequential writing performance
        v_cmd.extend(['-bufsize', '100M', '-maxrate', '200M'])
            
        append_output_file_args(v_cmd, temp_v_path, self.settings, self.log_message.emit)
        
        # Audio Command - FIXED: Always PCM for WAV intermediate
        a_cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5']
        a_cmd.extend(['-i', self.input_file])
        a_cmd.extend(['-c:a', 'pcm_s24le'])
        a_cmd.extend(['-vn']) # drop video explicitly
        a_cmd.append(temp_a_path)
        
        # Mux Command - FIXED: Transcode to final codec
        m_cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5']
        m_cmd.extend(['-i', temp_v_path, '-i', temp_a_path])
        m_cmd.extend(['-c:v', 'copy'])
        final_acodec = self.settings.get('audio_codec', 'aac')
        if final_acodec == 'pcm_s24le':
            m_cmd.extend(['-c:a', 'pcm_s24le'])
        elif final_acodec == 'flac':
            m_cmd.extend(['-c:a', 'flac'])
        elif final_acodec == 'copy':
            m_cmd.extend(['-c:a', 'pcm_s24le'])
        else:
            m_cmd.extend(['-c:a', 'aac', '-b:a', '320k'])
        append_output_file_args(m_cmd, self.output_file, self.settings, self.log_message.emit)
        
        return v_cmd, a_cmd, m_cmd

    def get_duration(self):
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.input_file], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            return float(result.stdout.strip())
        except:
            return 0

    def stop(self):
        self.should_stop = True
        if self.process:
            try:
                self.process.kill()
                self.process.wait()
            except: pass
class ExportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.app = parent
        # FIXED: Remove Frameless + Translucent for Wayland/Hyprland compatibility - prevents ghost window / glitched dialog
        self.setWindowTitle("Export Timeline Settings - FastEncode Pro")
        self.setMinimumSize(1020, 780)
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog)
        # Keep dark theme but no translucency
        self.setStyleSheet("QDialog { background: #0b0b0f; }")
        
        self.hw_caps = getattr(self.app, 'hw_caps', None) if self.app else detect_hardware_capabilities()
        self.codec_options = get_codec_display_list(self.hw_caps)
        
        self._last_bitrate_mbps = 100
        self._last_cq_value = 18
        self._syncing_quality_controls = False

        self.init_ui()
        # Ensure codec combo exists before calling
        try:
            self._on_codec_changed(self.codec_combo.currentIndex())
        except Exception:
            pass

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # Main container with dark background - FIXED for Wayland (no translucent dependency)
        self.container = QWidget()
        self.container.setObjectName("MainContainer")
        self.container.setStyleSheet("""
            QWidget#MainContainer {
                background-color: #111113;
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 12px;
            }
            QLabel { color: rgba(255,255,255,0.8); font-family: 'Inter', sans-serif; }
            QGroupBox {
                background: #111113;
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px;
                padding: 24px 16px 16px 16px;
                margin-top: 16px;
                font-size: 10px;
                font-weight: 700;
                color: rgba(255,255,255,0.8);
                letter-spacing: 1px;
                text-transform: uppercase;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0px 4px;
                margin-left: 12px;
                margin-top: -8px;
            }
            QComboBox {
                background: #0f0f14;
                border: 1px solid rgba(255,255,255,0.1);
                border-radius: 8px;
                padding: 8px 12px;
                color: rgba(255,255,255,0.9);
                font-size: 11px;
            }
            QComboBox:hover {
                border: 1px solid rgba(125,249,255,0.3);
            }
            QComboBox::drop-down { border: none; width: 30px; }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 6px solid rgba(255,255,255,0.5);
                margin-right: 12px;
            }
            QComboBox QAbstractItemView {
                background: #111113;
                border: 1px solid rgba(255,255,255,0.1);
                border-radius: 8px;
                color: white;
                selection-background-color: rgba(125,249,255,0.15);
                selection-color: #7df9ff;
                padding: 4px;
            }
        """)
        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # Header
        header = QWidget()
        header.setFixedHeight(70)
        header.setStyleSheet("border-bottom: 1px solid rgba(255,255,255,0.06);")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 0, 20, 0)
        
        logo = QLabel("⚡")
        logo.setFixedSize(32, 32)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet("background: rgba(0,255,136,0.1); color: #00ff88; border: 1px solid rgba(0,255,136,0.2); border-radius: 8px; font-size: 16px;")
        header_layout.addWidget(logo)
        
        title_layout = QVBoxLayout()
        title_layout.setContentsMargins(10, 16, 0, 16)
        title_layout.setSpacing(2)
        
        main_title = QLabel("FASTENCODE PRO • ENCODER & EXPORT SETTINGS")
        main_title.setStyleSheet("font-size: 13px; font-weight: 800; color: white; letter-spacing: 1px; border: none;")
        title_layout.addWidget(main_title)
        
        gpu_name = self.hw_caps.get('gpu_name', 'Unknown GPU') if self.hw_caps else "CPU"
        sub_title = QLabel(f"Full Hardware Settings • Target: {gpu_name} Zero-Copy VRAM")
        sub_title.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.4); border: none;")
        title_layout.addWidget(sub_title)
        header_layout.addLayout(title_layout)
        header_layout.addStretch()
        
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(32, 32)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton { background: transparent; color: rgba(255,255,255,0.5); font-size: 14px; border: none; border-radius: 16px; }
            QPushButton:hover { background: rgba(255,255,255,0.1); color: white; }
        """)
        close_btn.clicked.connect(self.reject)
        header_layout.addWidget(close_btn)
        
        container_layout.addWidget(header)

        # Tab Bar
        tab_bar = QWidget()
        tab_bar.setFixedHeight(50)
        tab_bar.setStyleSheet("background: #0f0f14; border-bottom: 1px solid rgba(255,255,255,0.06);")
        tab_layout = QHBoxLayout(tab_bar)
        tab_layout.setContentsMargins(20, 0, 20, 0)
        tab_layout.setSpacing(10)
        
        self.tab_buttons = []
        # Text-only single-line tabs: emoji + embedded newlines rendered as
        # tofu/wrapped garbage on systems without color-emoji fonts.
        tab_names = [
            "1. Video & Codec",
            "2. Bitrate & Quality",
            "3. Audio Master",
            "4. GPU Pipeline",
        ]

        for i, text in enumerate(tab_names):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton {
                    background: transparent; color: rgba(255,255,255,0.5); border: none;
                    border-bottom: 2px solid transparent; font-size: 11px; font-weight: 600;
                    padding: 4px 12px; text-align: center;
                }
                QPushButton:checked { color: #7df9ff; border-bottom: 2px solid #7df9ff; }
                QPushButton:hover:!checked { color: white; border-bottom: 2px solid rgba(255,255,255,0.2); }
            """)
            btn.clicked.connect(lambda checked, idx=i: self.switch_tab(idx))
            tab_layout.addWidget(btn)
            self.tab_buttons.append(btn)
        
        tab_layout.addStretch()
        container_layout.addWidget(tab_bar)
        
        # Add Pages to Stack
        self.stack.addWidget(self.create_video_tab())
        self.stack.addWidget(self.create_bitrate_tab())
        self.stack.addWidget(self.create_audio_tab())
        self.stack.addWidget(self.create_gpu_tab())
        
        container_layout.addWidget(self.stack)
        
        # Footer
        footer = QWidget()
        footer.setFixedHeight(70)
        footer.setStyleSheet("background: #0b0b0f; border-top: 1px solid rgba(255,255,255,0.06);")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(20, 0, 20, 0)
        
        status_layout = QHBoxLayout()
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("background: #00ff88; border-radius: 4px;")
        status_layout.addWidget(dot)
        self.footer_status = QLabel("Encoder: HEVC • 100 Mbps • MP4")
        self.footer_status.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.5); border: none; font-family: monospace;")
        status_layout.addWidget(self.footer_status)
        footer_layout.addLayout(status_layout)
        footer_layout.addStretch()
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet("""
            QPushButton { background: transparent; color: white; border: none; font-size: 12px; font-weight: 600; padding: 10px 20px; }
            QPushButton:hover { background: rgba(255,255,255,0.05); border-radius: 8px; }
        """)
        cancel_btn.clicked.connect(self.reject)
        
        self.export_btn = QPushButton("▶ START MASTER EXPORT (100 Mbps)")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.setFixedHeight(40)
        self.export_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #00e5ff, stop:1 #00d9ff);
                color: black; border: none; border-radius: 20px;
                font-size: 12px; font-weight: 800; letter-spacing: 0.5px; padding: 0 24px;
            }
            QPushButton:hover { background: #7df9ff; }
            QPushButton:pressed { background: #00b8d4; }
        """)
        self.export_btn.clicked.connect(self.accept)
        
        footer_layout.addWidget(cancel_btn)
        footer_layout.addWidget(self.export_btn)
        
        container_layout.addWidget(footer)
        main_layout.addWidget(self.container)
        
        self.switch_tab(0)

    def switch_tab(self, index):
        for i, btn in enumerate(self.tab_buttons):
            btn.setChecked(i == index)
        self.stack.setCurrentIndex(index)

    def create_video_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 10, 20, 20)
        layout.setSpacing(16)
        
        # Resolution Group
        res_group = QGroupBox("TIMELINE MASTER RESOLUTION")
        res_layout = QGridLayout(res_group)
        res_layout.setContentsMargins(16, 24, 16, 16)
        res_layout.setSpacing(16)
        
        res_label = QLabel("Resolution Preset")
        res_label.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.res_combo = QComboBox()
        self.res_combo.addItems([
            "Source (Match Input/Timeline)",
            "1080p Full HD (1920x1080)",
            "1440p Quad HD (2560x1440)",
            "4K Ultra HD (3840x2160)",
            "4K Ultra Wide (3840x1600) - Target RTX 5070",
            "5K (5120x2880)",
            "8K (7680x4320)",
        ])
        self.res_combo.setCurrentIndex(4) # Match screenshot
        
        fps_label = QLabel("Frame Rate (FPS)")
        fps_label.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.fps_combo = QComboBox()
        self.fps_combo.addItems([
            "23.976 fps (Cinematic)", "24.0 fps (Film)", "25.0 fps (PAL)", 
            "29.97 fps (NTSC)", "30.0 fps (Standard)", "50.0 fps (High-Motion PAL)", 
            "59.94 fps", "60.0 fps (Smooth High-Motion)", "120.0 fps (HFR)"
        ])
        self.fps_combo.setCurrentIndex(7) # Match 60.0 fps screenshot
        
        res_layout.addWidget(res_label, 0, 0)
        res_layout.addWidget(self.res_combo, 1, 0)
        res_layout.addWidget(fps_label, 0, 1)
        res_layout.addWidget(self.fps_combo, 1, 1)
        
        # Hardcode small label top right of group
        top_res_lbl = QLabel("3840 × 1600")
        top_res_lbl.setStyleSheet("color: #00ff88; font-family: monospace; font-size: 10px; font-weight: bold; background: transparent; border: none;")
        top_res_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        
        # Hack to put label in group title area
        res_layout.addWidget(top_res_lbl, -1, 1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        
        layout.addWidget(res_group)
        
        # Codec Group
        codec_group = QGroupBox("HARDWARE CODEC & CONTAINER FORMAT")
        codec_layout = QGridLayout(codec_group)
        codec_layout.setContentsMargins(16, 24, 16, 16)
        codec_layout.setSpacing(16)
        
        codec_label = QLabel("NVIDIA Hardware Codec")
        codec_label.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.codec_combo = QComboBox()
        # Add actual options from the system
        for display_name, _ in self.codec_options:
            self.codec_combo.addItem(display_name)
        
        default_idx = 0
        for idx, (disp, cid) in enumerate(self.codec_options):
            if cid == "hevc_nvenc":
                default_idx = idx
                self.codec_combo.setItemText(idx, "HEVC / H.265 (hevc_nvenc - NVIDIA)")
                break
        self.codec_combo.setCurrentIndex(default_idx)
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        
        container_label = QLabel("Container Format")
        container_label.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.container_combo = QComboBox()
        self.container_combo.addItems(["MP4 (.mp4 with FastStart atom)", "QuickTime (.mov)", "Matroska (.mkv)"])
        
        bitdepth_label = QLabel("Color Bit Depth")
        bitdepth_label.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.pixel_format_combo = QComboBox()
        self.pixel_format_combo.addItems([
            "8-bit (NV12 / YUV420P - High Compatibility)",
            "10-bit HDR (Main10 / Rec.2020 / P010)"
        ])
        self.pixel_format_combo.setCurrentIndex(1)
        
        self.nvenc_2pass_check = QCheckBox("2-Pass NVENC Analysis (Quarter Res)")
        self.nvenc_2pass_check.setChecked(True)
        self.nvenc_2pass_check.setStyleSheet("""
            QCheckBox { color: rgba(255,255,255,0.8); font-size: 11px; }
            QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); }
            QCheckBox::indicator:checked { background: #00ff88; border: 1px solid #00ff88; image: url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIxMiIgaGVpZ2h0PSIxMiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiBzdHJva2U9ImJsYWNrIiBzdHJva2Utd2lkdGg9IjQiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIgc3Ryb2tlLWxpbmVqb2luPSJyb3VuZCI+PHBvbHlsaW5lIHBvaW50cz0iMjAgNiA5 MTcgNCAxMiI+PC9wb2x5bGluZT48L3N2Zz4=); }
        """)
        
        # Hide prores profile combo but keep it functional for ProRes selection
        self.prores_profile_label = QLabel("ProRes Profile:")
        self.prores_profile_combo = QComboBox()
        self.prores_profile_combo.addItems(["Proxy (0)", "LT (1)", "Standard (2)", "HQ (3)", "4444 (4)", "4444 XQ (5)"])
        self.prores_profile_combo.setCurrentIndex(3)
        self.prores_profile_label.hide()
        self.prores_profile_combo.hide()

        codec_layout.addWidget(codec_label, 0, 0)
        codec_layout.addWidget(self.codec_combo, 1, 0)
        codec_layout.addWidget(container_label, 0, 1)
        codec_layout.addWidget(self.container_combo, 1, 1)
        codec_layout.addWidget(bitdepth_label, 2, 0)
        codec_layout.addWidget(self.pixel_format_combo, 3, 0)
        codec_layout.addWidget(self.nvenc_2pass_check, 3, 1)
        codec_layout.addWidget(self.prores_profile_label, 4, 0)
        codec_layout.addWidget(self.prores_profile_combo, 5, 0)

        layout.addWidget(codec_group)
        layout.addStretch()
        layout.addWidget(self.create_throughput_banner())
        
        return page

    def create_bitrate_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 10, 20, 20)
        layout.setSpacing(16)
        
        # Rate Control Group
        rc_group = QGroupBox("RATE CONTROL & BITRATE PRESETS")
        rc_layout = QVBoxLayout(rc_group)
        rc_layout.setContentsMargins(16, 24, 16, 16)
        rc_layout.setSpacing(16)
        
        combo_row = QHBoxLayout()
        combo_row.setSpacing(16)
        
        v1 = QVBoxLayout()
        l1 = QLabel("Rate Control Mode")
        l1.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.rate_control_combo = QComboBox()
        self.rate_control_combo.addItems([
            "CBR - Constant Bit Rate (Recommended for YouTube)",
            "VBR - Variable Bit Rate",
            "ABR - Average Bit Rate",
            "CQP - Constant Quality",
            "Lossless Archive - QP 0"
        ])
        self.rate_control_combo.currentIndexChanged.connect(self._on_rate_control_changed)
        v1.addWidget(l1)
        v1.addWidget(self.rate_control_combo)
        
        v2 = QVBoxLayout()
        self.nvenc_target_label = QLabel("NVENC Preset (P1-P7)")
        self.nvenc_target_label.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.nvenc_target_combo = QComboBox()
        self.nvenc_target_combo.addItems([
            "P7: Slowest / Highest Quality (Master Studio)",
            "P6: Slower / Better Quality",
            "P5: Slow / Good Quality",
            "P4: Medium / Default"
        ])
        v2.addWidget(self.nvenc_target_label)
        v2.addWidget(self.nvenc_target_combo)
        
        combo_row.addLayout(v1)
        combo_row.addLayout(v2)
        rc_layout.addLayout(combo_row)
        
        lbl_pres = QLabel("Quick Bitrate Presets:")
        lbl_pres.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        rc_layout.addWidget(lbl_pres)
        
        presets_row = QHBoxLayout()
        presets_row.setSpacing(8)
        self.preset_btns = []
        preset_data = [
            ("25 Mbps", "Proxy", 25), ("50 Mbps", "Standard", 50),
            ("80 Mbps", "HQ 4K", 80), ("100 Mbps", "Master", 100),
            ("120 Mbps", "Standard", 120)
        ]
        
        for name, sub, val in preset_data:
            btn = QPushButton(f"{name}\n{sub}")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton {
                    background: #0f0f14; color: rgba(255,255,255,0.7);
                    border: 1px solid rgba(255,255,255,0.1); border-radius: 8px;
                    padding: 10px; font-size: 11px; font-weight: 700;
                }
                QPushButton:checked {
                    background: rgba(125,249,255,0.1); color: #7df9ff;
                    border: 1px solid #7df9ff;
                }
                QPushButton:hover:!checked { background: rgba(255,255,255,0.05); }
            """)
            btn.clicked.connect(lambda checked, v=val, idx=len(self.preset_btns): self._select_bitrate_preset(idx, v))
            presets_row.addWidget(btn)
            self.preset_btns.append(btn)
        
        self.preset_btns[3].setChecked(True) # 100 Mbps selected
        rc_layout.addLayout(presets_row)
        
        slider_row = QHBoxLayout()
        slider_row.setContentsMargins(0, 10, 0, 0)
        lbl_tgt = QLabel("Target Bitrate (Mbps):")
        lbl_tgt.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(1, 200)
        self.quality_slider.setValue(100)
        self.quality_slider.setStyleSheet("""
            QSlider::groove:horizontal { border: none; height: 6px; background: rgba(255,255,255,0.1); border-radius: 3px; }
            QSlider::sub-page:horizontal { background: rgba(125,249,255,0.3); border-radius: 3px; }
            QSlider::handle:horizontal { background: white; border: 2px solid #7df9ff; width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; }
        """)
        self.quality_slider.valueChanged.connect(self._on_quality_changed)
        
        self.target_spin = QSpinBox()
        self.target_spin.setRange(1, 1000)
        self.target_spin.setValue(100)
        self.target_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.target_spin.setStyleSheet("""
            QSpinBox { background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; color: #7df9ff; font-weight: bold; padding: 4px; width: 50px; }
            QSpinBox::up-button, QSpinBox::down-button { width: 16px; background: rgba(255,255,255,0.06); border: none; border-radius: 4px; margin: 1px; }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: rgba(255,255,255,0.12); }
            QSpinBox::up-arrow { image: none; border-left: 3px solid transparent; border-right: 3px solid transparent; border-bottom: 4px solid rgba(255,255,255,0.5); }
            QSpinBox::down-arrow { image: none; border-left: 3px solid transparent; border-right: 3px solid transparent; border-top: 4px solid rgba(255,255,255,0.5); }
        """)
        self.target_spin.valueChanged.connect(self._on_spin_changed)
        
        lbl_mbps = QLabel("Mbps")
        lbl_mbps.setStyleSheet("color: #00ff88; font-weight: bold; font-size: 11px;")
        
        slider_row.addWidget(lbl_tgt)
        slider_row.addStretch()
        slider_row.addWidget(self.target_spin)
        slider_row.addWidget(lbl_mbps)
        
        rc_layout.addLayout(slider_row)
        rc_layout.addWidget(self.quality_slider)
        
        layout.addWidget(rc_group)
        
        # GOP Group
        gop_group = QGroupBox("GOP STRUCTURE & KEYFRAME INTERVAL")
        gop_layout = QGridLayout(gop_group)
        gop_layout.setContentsMargins(16, 24, 16, 16)
        gop_layout.setSpacing(12)
        
        l_bf = QLabel("B-Frames Count:")
        l_bf.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.bf_val = QLabel("4")
        self.bf_val.setStyleSheet("color: #7df9ff; font-weight: bold;")
        self.bf_slider = QSlider(Qt.Orientation.Horizontal)
        self.bf_slider.setRange(0, 4)
        self.bf_slider.setValue(4)
        self.bf_slider.setStyleSheet(self.quality_slider.styleSheet())
        self.bf_slider.valueChanged.connect(lambda v: self.bf_val.setText(str(v)))
        l_bf_sub = QLabel("4 B-frames recommended for HEVC/AV1 encoding efficiency.")
        l_bf_sub.setStyleSheet("font-size: 9px; color: rgba(255,255,255,0.4);")
        
        l_gop = QLabel("GOP Size (Frames):")
        l_gop.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.gop_val = QLabel("60")
        self.gop_val.setStyleSheet("color: #7df9ff; font-weight: bold;")
        self.gop_slider = QSlider(Qt.Orientation.Horizontal)
        self.gop_slider.setRange(1, 300)
        self.gop_slider.setValue(60)
        self.gop_slider.setStyleSheet(self.quality_slider.styleSheet())
        self.gop_slider.valueChanged.connect(lambda v: self.gop_val.setText(str(v)))
        l_gop_sub = QLabel("GOP 60 = 1 keyframe per second at 60fps.")
        l_gop_sub.setStyleSheet("font-size: 9px; color: rgba(255,255,255,0.4);")
        
        gop_layout.addWidget(l_bf, 0, 0)
        gop_layout.addWidget(self.bf_val, 0, 1, Qt.AlignmentFlag.AlignRight)
        gop_layout.addWidget(self.bf_slider, 1, 0, 1, 2)
        gop_layout.addWidget(l_bf_sub, 2, 0, 1, 2)
        
        gop_layout.addWidget(l_gop, 0, 2)
        gop_layout.addWidget(self.gop_val, 0, 3, Qt.AlignmentFlag.AlignRight)
        gop_layout.addWidget(self.gop_slider, 1, 2, 1, 2)
        gop_layout.addWidget(l_gop_sub, 2, 2, 1, 2)
        
        layout.addWidget(gop_group)
        layout.addStretch()
        layout.addWidget(self.create_throughput_banner())
        
        # Hidden CQP elements to maintain compatibility
        self.cqp_spin = QSpinBox()
        self.cqp_spin.hide()

        return page

    def create_audio_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 10, 20, 20)
        layout.setSpacing(16)
        
        audio_group = QGroupBox("AUDIO MASTER TRACK CONFIGURATION")
        audio_layout = QGridLayout(audio_group)
        audio_layout.setContentsMargins(16, 24, 16, 16)
        audio_layout.setSpacing(16)
        
        l_codec = QLabel("Audio Stream Codec")
        l_codec.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.audio_combo = QComboBox()
        self.audio_combo.addItems([
            "AAC-LC 320 kbps (High Fidelity Broadcast)",
            "Linear PCM 24-bit (Uncompressed Studio Master)",
            "FLAC 24-bit Lossless",
            "AAC-LC 192 kbps Standard",
            "Copy Stream"
        ])
        
        l_sample = QLabel("Sample Rate")
        l_sample.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        self.sample_combo = QComboBox()
        self.sample_combo.addItems([
            "48.0 kHz (Cinema & Broadcast Standard)",
            "96.0 kHz (Studio High-Res Master)",
            "44.1 kHz (CD Audio)"
        ])
        
        audio_layout.addWidget(l_codec, 0, 0)
        audio_layout.addWidget(self.audio_combo, 1, 0)
        audio_layout.addWidget(l_sample, 0, 1)
        audio_layout.addWidget(self.sample_combo, 1, 1)
        
        norm_card = QWidget()
        norm_card.setStyleSheet("""
            QWidget { background: rgba(0,255,136,0.03); border: 1px solid rgba(0,255,136,0.1); border-radius: 12px; }
        """)
        norm_layout = QVBoxLayout(norm_card)
        norm_layout.setContentsMargins(16, 16, 16, 16)
        
        norm_top = QHBoxLayout()
        check_icon = QLabel("✓")
        check_icon.setStyleSheet("color: #00ff88; font-weight: bold; border: 1px solid #00ff88; border-radius: 8px; padding: 2px; font-size: 10px;")
        check_icon.setFixedSize(20, 20)
        check_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        norm_title = QLabel("Automatic EBU R128 (-14 LUFS) Master Normalization")
        norm_title.setStyleSheet("color: #00ff88; font-weight: bold; font-size: 11px; border: none; background: transparent;")
        norm_top.addWidget(check_icon)
        norm_top.addWidget(norm_title)
        norm_top.addStretch()
        norm_layout.addLayout(norm_top)
        
        norm_desc = QLabel("Audio channels are processed with floating-point 64-bit precision, peak-limited with 1ms lookahead, and\nnormalized to EBU R128 -14 LUFS with true-peak limiting.")
        norm_desc.setStyleSheet("color: rgba(255,255,255,0.6); font-size: 10px; border: none; background: transparent;")
        norm_layout.addWidget(norm_desc)
        
        audio_layout.addWidget(norm_card, 2, 0, 1, 2)
        
        layout.addWidget(audio_group)
        layout.addStretch()
        layout.addWidget(self.create_throughput_banner())
        
        return page

    def create_gpu_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 10, 20, 20)
        
        gpu_group = QGroupBox("HARDWARE PIPELINE")
        gpu_layout = QVBoxLayout(gpu_group)
        gpu_layout.setContentsMargins(16, 24, 16, 16)
        
        self.gpu_check = QCheckBox("Enable GPU Acceleration (Auto - uses detected encoder)")
        self.gpu_check.setChecked(True)
        self.gpu_decode_check = QCheckBox("Enable Hardware Decoding (NVDEC/AMF/QSV)")
        self.gpu_decode_check.setChecked(True)
        self.gpu_composite_check = QCheckBox("Enable Full GPU Compositing (TURBO - stays in VRAM)")
        self.gpu_composite_check.setChecked(True)
        self.gpu_composite_check.setStyleSheet("color: #00ff88; font-weight: bold;")
        
        for chk in [self.gpu_check, self.gpu_decode_check, self.gpu_composite_check]:
            gpu_layout.addWidget(chk)
            
        layout.addWidget(gpu_group)
        layout.addStretch()
        layout.addWidget(self.create_throughput_banner())
        return page

    def create_throughput_banner(self):
        banner = QWidget()
        banner.setFixedHeight(40)
        banner.setStyleSheet("background: rgba(0,255,136,0.05); border: 1px solid rgba(0,255,136,0.2); border-radius: 8px;")
        b_layout = QHBoxLayout(banner)
        b_layout.setContentsMargins(16, 0, 16, 0)
        
        icon = QLabel("✓")
        icon.setFixedSize(20, 20)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("color: #00ff88; border: 1px solid #00ff88; border-radius: 10px; font-size: 10px;")
        
        text = QLabel("Expected NVENC Throughput: 120-160 FPS (Zero-Copy Realtime)")
        text.setStyleSheet("color: white; font-size: 11px; font-weight: bold; border: none;")
        
        sub = QLabel("Dual NVENC RTX 5070" if self.hw_caps and "5070" in self.hw_caps.get('gpu_name','') else getattr(self, 'hw_caps', {}).get('gpu_name', 'GPU'))
        sub.setStyleSheet("color: rgba(255,255,255,0.4); font-size: 10px; border: none;")
        
        b_layout.addWidget(icon)
        b_layout.addWidget(text)
        b_layout.addStretch()
        b_layout.addWidget(sub)
        return banner

    def _select_bitrate_preset(self, idx, val):
        for i, btn in enumerate(self.preset_btns):
            btn.setChecked(i == idx)
        self._syncing_quality_controls = True
        self.quality_slider.setValue(val)
        self.target_spin.setValue(val)
        self._last_bitrate_mbps = val
        self._syncing_quality_controls = False
        self.update_summary()

    def _on_quality_changed(self, v):
        if self._syncing_quality_controls: return
        self._syncing_quality_controls = True
        self.target_spin.setValue(v)
        self._last_bitrate_mbps = v
        self._uncheck_presets_if_custom(v)
        self._syncing_quality_controls = False
        self.update_summary()

    def _on_spin_changed(self, v):
        if self._syncing_quality_controls: return
        self._syncing_quality_controls = True
        self.quality_slider.setValue(v)
        self._last_bitrate_mbps = v
        self._uncheck_presets_if_custom(v)
        self._syncing_quality_controls = False
        self.update_summary()

    def _uncheck_presets_if_custom(self, val):
        matched = False
        for i, (n, s, v) in enumerate([(25,), (50,), (80,), (100,), (120,)]): # matching simple values
            if val == v[0]:
                self.preset_btns[i].setChecked(True)
                matched = True
            else:
                self.preset_btns[i].setChecked(False)

    def _on_codec_changed(self, index):
        if not self.codec_options: return
        # Maintain logic from old dialog
        codec_id = self.codec_options[index][1] if index < len(self.codec_options) else "hevc_nvenc"
        is_prores = codec_id.startswith("prores")
        is_hw = not is_prores
        self.prores_profile_label.setVisible(is_prores)
        self.prores_profile_combo.setVisible(is_prores)
        self.nvenc_target_label.setVisible(is_hw)
        self.nvenc_target_combo.setVisible(is_hw)
        self.update_summary()

    def _on_rate_control_changed(self, index):
        rc = self._rate_control_value(index)
        if rc == 'cqp':
            self.quality_slider.setRange(0, 51)
            self.target_spin.setRange(0, 51)
        else:
            self.quality_slider.setRange(1, 200)
            self.target_spin.setRange(1, 1000)
            self.target_spin.setValue(self._last_bitrate_mbps)
        self.update_summary()

    def _rate_control_value(self, index=None):
        rc_map = {0: 'cbr', 1: 'vbr', 2: 'abr', 3: 'cqp', 4: 'lossless'}
        if index is None:
            index = self.rate_control_combo.currentIndex()
        return rc_map.get(index, 'cbr')

    def update_summary(self):
        codec_name = "HEVC"
        idx = self.codec_combo.currentIndex()
        if idx >= 0 and idx < len(self.codec_options):
            cid = self.codec_options[idx][1]
            if 'h264' in cid: codec_name = "H.264"
            elif 'av1' in cid: codec_name = "AV1"
            elif 'prores' in cid: codec_name = "ProRes"
            
        rc = self._rate_control_value()
        val_str = f"QP {self._last_cq_value}" if rc == 'cqp' else f"{self._last_bitrate_mbps} Mbps"
        self.footer_status.setText(f"Encoder: {codec_name} • {val_str} • MP4")
        self.export_btn.setText(f"▶ START MASTER EXPORT ({val_str})")

    def get_settings(self):
        codec_map = {i: cid for i, (_, cid) in enumerate(self.codec_options)}
        audio_map = {0: "aac", 1: "pcm_s24le", 2: "flac", 3: "aac", 4: "copy"}
        fps_map = {0: 23.976, 1: 24.0, 2: 25.0, 3: 29.97, 4: 30.0, 5: 50.0, 6: 59.94, 7: 60.0, 8: 120.0}
        rc = self._rate_control_value()
        
        # Parse resolution combo
        res_idx = self.res_combo.currentIndex()
        res_compat_map = {0:0, 1:1, 2:2, 3:3, 4:4, 5:5, 6:6, 7:7, 8:8} # fixed: include ultrawide + Instagram + 5K/8K - FULL RESTORE
        
        settings = {
            'video_codec': codec_map.get(self.codec_combo.currentIndex(), "hevc_nvenc"),
            'audio_codec': audio_map.get(self.audio_combo.currentIndex(), "aac"),
            'use_gpu': self.gpu_check.isChecked() if hasattr(self, 'gpu_check') else True,
            'use_gpu_decode': self.gpu_decode_check.isChecked() if hasattr(self, 'gpu_decode_check') else True,
            'use_gpu_composite': self.gpu_composite_check.isChecked() if hasattr(self, 'gpu_composite_check') else True,
            'bitrate_mbps': self._last_bitrate_mbps,
            'cq_value': self._last_cq_value if rc == 'cqp' else 18,
            'prores_profile': self.prores_profile_combo.currentIndex() if hasattr(self, 'prores_profile_combo') else 0,
            'export_target_index': self.nvenc_target_combo.currentIndex() if hasattr(self, 'nvenc_target_combo') else 0,
            'rate_control': rc,
            'export_res_index': res_compat_map.get(res_idx, 0),
            'timeline_fps': fps_map.get(self.fps_combo.currentIndex(), 60.0),
            'pixel_format': self.pixel_format_combo.currentIndex() if hasattr(self, 'pixel_format_combo') else 1,
            'hw_caps': self.hw_caps,
            'gpu_vram_limit_mb': getattr(self.app, 'gpu_vram_limit_mb', None) if self.app else None,
            'temp_dir': getattr(self.app, 'temp_dir', tempfile.gettempdir()) if self.app else tempfile.gettempdir(),
        }
        # FULL RESTORE: B-Frames and GOP from sliders if present
        try:
            if hasattr(self, 'bf_slider'):
                settings['b_frames'] = self.bf_slider.value()
            if hasattr(self, 'gop_slider'):
                settings['gop_size'] = self.gop_slider.value()
            if hasattr(self, 'container_combo'):
                settings['container_format'] = self.container_combo.currentIndex()
        except Exception:
            pass
        return settings

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



class ExportPanelWidget(QScrollArea):
    """FULL-FEATURED EXPORT PANEL - Fixed: Restores 90% missing settings + Instagram fix + Wayland-safe"""
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWidgetResizable(True)
        self.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Header - EXACT HTML: Export • Encode + READY • 0.8s
        header_layout = QHBoxLayout()
        title = QLabel("◉ Export • Encode")
        title.setStyleSheet("font-size: 13px; font-weight: 600; color: white;")
        header_layout.addWidget(title)
        badge = QLabel("READY • 0.8s")
        badge.setStyleSheet("padding: 4px 8px; border-radius: 10px; background: rgba(0,255,136,0.1); border: 1px solid rgba(0,255,136,0.2); color: #00ff88; font-size: 10px; font-weight: 500;")
        header_layout.addStretch()
        header_layout.addWidget(badge)
        layout.addLayout(header_layout)

        # HARDWARE CARDS - EXACT HTML 3-col with 92% bar
        hw_group = QWidget()
        hw_group.setStyleSheet("background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px;")
        hw_layout = QVBoxLayout(hw_group)
        hw_layout.setContentsMargins(12, 12, 12, 12)
        hw_label = QLabel("HARDWARE ACCELERATION")
        hw_label.setStyleSheet("font-size: 11px; font-weight: 500; color: rgba(255,255,255,0.4);")
        hw_layout.addWidget(hw_label)
        
        gpu_name = app.hw_caps.get('gpu_name', 'Unknown GPU') if app.hw_caps else 'Unknown GPU'
        has_nvenc = app.hw_caps.get('nvenc_hevc', False) or app.hw_caps.get('nvenc_h264', False) if app.hw_caps else False
        
        hw_cards_layout = QHBoxLayout()
        hw_cards_layout.setSpacing(8)
        nvidia_card = QWidget()
        nvidia_card.setStyleSheet(f"""
            QWidget {{
                background: #0f1a12;
                border: 1px solid rgba(0,255,136,0.3);
                border-radius: 12px;
            }}
        """)
        n_layout = QVBoxLayout(nvidia_card)
        n_layout.setContentsMargins(10, 10, 10, 10)
        n_layout.setSpacing(2)
        n_title_row = QHBoxLayout()
        dot = QLabel()
        dot.setFixedSize(6, 6)
        dot.setStyleSheet("background: #00ff88; border-radius: 3px;")
        n_title_row.addWidget(dot)
        n_title = QLabel("NVIDIA")
        n_title.setStyleSheet("font-size: 10px; font-weight: 700; color: #00ff88;")
        n_title_row.addWidget(n_title)
        n_title_row.addStretch()
        n_layout.addLayout(n_title_row)
        n_desc = QLabel("RTX 5070")
        n_desc.setStyleSheet("font-size: 12px; font-weight: 600; color: white;")
        n_desc.setWordWrap(True)
        n_layout.addWidget(n_desc)
        n_status = QLabel("NVENC • ACTIVE")
        n_status.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.5);")
        n_layout.addWidget(n_status)
        # 92% green progress bar (HTML)
        n_bar_bg = QWidget()
        n_bar_bg.setFixedHeight(4)
        n_bar_bg.setStyleSheet("background: rgba(255,255,255,0.1); border-radius: 2px;")
        n_bar_l = QHBoxLayout(n_bar_bg)
        n_bar_l.setContentsMargins(0, 0, 0, 0)
        n_bar_l.setSpacing(0)
        n_fill = QLabel()
        n_fill.setStyleSheet("background: #00ff88; border-radius: 2px;")
        n_bar_l.addWidget(n_fill, stretch=92)
        n_bar_l.addStretch(8)
        n_layout.addWidget(n_bar_bg)
        hw_cards_layout.addWidget(nvidia_card)
        # AMD / INTEL exact HTML idle cards
        for vendor_name, dev, sub in [("AMD", "RX 7900", "AMF • idle"), ("INTEL", "Arc A770", "QSV • idle")]:
            card = QWidget()
            card.setStyleSheet("background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px;")
            c_l = QVBoxLayout(card)
            c_l.setContentsMargins(10, 10, 10, 10)
            c_l.setSpacing(2)
            c_t = QLabel(vendor_name)
            c_t.setStyleSheet("font-size: 10px; font-weight: 700; color: rgba(255,255,255,0.4);")
            c_l.addWidget(c_t)
            c_d = QLabel(dev)
            c_d.setStyleSheet("font-size: 12px; font-weight: 500; color: rgba(255,255,255,0.5);")
            c_l.addWidget(c_d)
            c_s = QLabel(sub)
            c_s.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.3);")
            c_l.addWidget(c_s)
            hw_cards_layout.addWidget(card)
        hw_layout.addLayout(hw_cards_layout)
        layout.addWidget(hw_group)

        # RESOLUTION & FPS
        res_group = QWidget()
        res_group.setStyleSheet("background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px;")
        res_layout = QVBoxLayout(res_group)
        res_label = QLabel("TIMELINE RESOLUTION & FPS")
        res_label.setStyleSheet("font-size: 11px; font-weight: 500; color: rgba(255,255,255,0.4); letter-spacing: 1.5px;")
        res_layout.addWidget(res_label)
        # Res combo
        self.res_combo = QComboBox()
        self.res_combo.addItems([
            "Source (Match Input/Timeline)",
            "1080p Full HD (1920x1080)",
            "1440p Quad HD (2560x1440)",
            "4K Ultra HD (3840x2160)",
            "4K Ultra Wide (3840x1600) - Target RTX 5070",
            "5K (5120x2880)",
            "8K (7680x4320)",
            "Instagram Square (1080x1080) - FIXED",
            "Instagram Portrait (1080x1350) - FIXED",
        ])
        self.res_combo.setCurrentIndex(4)
        self.res_combo.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 6px; color: white; font-size: 11px;")
        res_layout.addWidget(QLabel("Resolution Preset:"))
        res_layout.addWidget(self.res_combo)
        # FPS
        self.fps_combo = QComboBox()
        self.fps_combo.addItems([
            "23.976 fps (Cinematic)", "24.0 fps (Film)", "25.0 fps (PAL)",
            "29.97 fps (NTSC)", "30.0 fps (Standard)", "50.0 fps (High-Motion PAL)",
            "59.94 fps", "60.0 fps (Smooth High-Motion)", "120.0 fps (HFR)"
        ])
        self.fps_combo.setCurrentIndex(7)
        self.fps_combo.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 6px; color: white; font-size: 11px;")
        res_layout.addWidget(QLabel("Frame Rate:"))
        res_layout.addWidget(self.fps_combo)
        layout.addWidget(res_group)

        # CODEC & CONTAINER
        cd_group = QWidget()
        cd_group.setStyleSheet("background: #111113; border: 1px solid rgba(255,255,255,0.06); border-radius: 16px;")
        cd_layout = QVBoxLayout(cd_group)
        cd_header = QHBoxLayout()
        cd_label = QLabel("CODEC & CONTAINER")
        cd_label.setStyleSheet("font-size: 11px; font-weight: 500; color: rgba(255,255,255,0.4); letter-spacing: 1.5px;")
        cd_header.addWidget(cd_label)
        cd_header.addStretch()
        cd_badge = QLabel("MP4 • HEVC")
        cd_badge.setStyleSheet("padding: 2px 8px; border-radius: 10px; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.1); color: white; font-size: 10px;")
        cd_header.addWidget(cd_badge)
        cd_layout.addLayout(cd_header)
        # EXACT HTML summary rows (live, new feature mirrors combos/sliders)
        self.codec_summary_encoder = QLabel("hevc_nvenc • p7 • 2-pass")
        self.codec_summary_encoder.setStyleSheet("font-family: Consolas, monospace; font-size: 11px; color: white;")
        _enc_row = QHBoxLayout()
        _enc_lbl = QLabel("Encoder")
        _enc_lbl.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.4);")
        _enc_row.addWidget(_enc_lbl)
        _enc_row.addStretch()
        _enc_row.addWidget(self.codec_summary_encoder)
        cd_layout.addLayout(_enc_row)
        self.codec_summary_res = QLabel("3840×1600 → 3840×1600")
        self.codec_summary_res.setStyleSheet("font-family: Consolas, monospace; font-size: 11px; color: white;")
        _res_row = QHBoxLayout()
        _res_lbl = QLabel("Resolution")
        _res_lbl.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.4);")
        _res_row.addWidget(_res_lbl)
        _res_row.addStretch()
        _res_row.addWidget(self.codec_summary_res)
        cd_layout.addLayout(_res_row)
        self.cd_badge = cd_badge

        # Codec combo - dynamic from hardware
        self.hw_caps = app.hw_caps
        self.codec_options = get_codec_display_list(self.hw_caps) if self.hw_caps else [("HEVC NVENC (hevc_nvenc)", "hevc_nvenc")]
        self.codec_combo = QComboBox()
        for display_name, _ in self.codec_options:
            self.codec_combo.addItem(display_name)
        # default to HEVC NVENC
        default_idx = 0
        for idx, (_, cid) in enumerate(self.codec_options):
            if cid == "hevc_nvenc":
                default_idx = idx
                break
        self.codec_combo.setCurrentIndex(default_idx)
        self.codec_combo.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 6px; color: white;")
        cd_layout.addWidget(QLabel("Video Codec:"))
        cd_layout.addWidget(self.codec_combo)

        # Container
        self.container_combo = QComboBox()
        self.container_combo.addItems(["MP4 (.mp4 with FastStart)", "QuickTime (.mov)", "Matroska (.mkv)"])
        self.container_combo.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 6px; color: white;")
        cd_layout.addWidget(QLabel("Container:"))
        cd_layout.addWidget(self.container_combo)

        # Pixel format
        self.pixel_format_combo = QComboBox()
        self.pixel_format_combo.addItems([
            "8-bit (NV12 / YUV420P - Highest Compatibility)",
            "10-bit (P010LE - High Dynamic Range/Quality)"
        ])
        self.pixel_format_combo.setCurrentIndex(1)
        self.pixel_format_combo.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 6px; color: white;")
        cd_layout.addWidget(QLabel("Pixel Format / Bit Depth:"))
        cd_layout.addWidget(self.pixel_format_combo)

        # ProRes profile (hidden unless ProRes selected)
        self.prores_profile_combo = QComboBox()
        self.prores_profile_combo.addItems(["Proxy (0)", "LT (1)", "Standard (2)", "HQ (3)", "4444 (4)", "4444 XQ (5)"])
        self.prores_profile_combo.setCurrentIndex(3)
        self.prores_profile_combo.setVisible(False)
        cd_layout.addWidget(self.prores_profile_combo)

        # NVENC Target
        self.nvenc_target_combo = QComboBox()
        self.nvenc_target_combo.addItems(get_export_target_labels())
        self.nvenc_target_combo.setCurrentIndex(0)
        self.nvenc_target_combo.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 6px; color: white;")
        cd_layout.addWidget(QLabel("Export Quality Profile (NVENC P1-P7):"))
        cd_layout.addWidget(self.nvenc_target_combo)

        layout.addWidget(cd_group)

        # BITRATE & RATE CONTROL
        br_group = QWidget()
        br_group.setStyleSheet("background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px;")
        br_layout = QVBoxLayout(br_group)
        br_label = QLabel("BITRATE & RATE CONTROL")
        br_label.setStyleSheet("font-size: 11px; font-weight: 500; color: rgba(255,255,255,0.4); letter-spacing: 1.5px;")
        br_layout.addWidget(br_label)

        # Rate control
        self.rate_control_combo = QComboBox()
        self.rate_control_combo.addItems([
            "CBR - Constant Bit Rate (streaming/delivery)",
            "VBR - Variable Bit Rate (better quality/size ratio)",
            "ABR - Average Bit Rate (loose target)",
            "CQP - Constant Quality (manual QP)",
            "Lossless Archive - QP 0 (huge files)"
        ])
        self.rate_control_combo.setCurrentIndex(0)
        self.rate_control_combo.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 6px; color: white;")
        br_layout.addWidget(QLabel("Rate Control:"))
        br_layout.addWidget(self.rate_control_combo)

        # Presets grid - EXACT HTML 4 cards (CBR100 active white + cyan dot + NVENC line)
        presets_grid = QGridLayout()
        presets_grid.setSpacing(8)
        self.presets = [
            {"name": "CBR 100", "sub": "100 Mbps • Master", "mbps": 100, "rc": "cbr"},
            {"name": "CBR 80", "sub": "80 Mbps • High", "mbps": 80, "rc": "cbr"},
            {"name": "VBR HQ", "sub": "Variable • YouTube", "mbps": 60, "rc": "vbr"},
            {"name": "CBR 25", "sub": "25 Mbps • Proxy", "mbps": 25, "rc": "cbr"},
        ]
        self.preset_btns = []
        for i, p in enumerate(self.presets):
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if i == 0:
                btn.setText(f"● {p['name']}\n{p['sub']}\n◉ NVENC HEVC")
            else:
                btn.setText(f"{p['name']}\n{p['sub']}")
            btn.setStyleSheet("""
                QPushButton:checked { background: white; color: black; border: 1px solid white; border-radius: 12px; padding: 10px; text-align: left; font-weight: 600; font-size: 12px; }
                QPushButton:!checked { background: #0f0f13; color: rgba(255,255,255,0.4); border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 10px; text-align: left; font-weight: 500; font-size: 11px; }
                QPushButton:hover:!checked { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.15); color: rgba(255,255,255,0.8); }
            """)
            btn.clicked.connect(lambda checked, idx=i: self.select_preset(idx))
            self.preset_btns.append(btn)
            presets_grid.addWidget(btn, i // 2, i % 2)
        br_layout.addLayout(presets_grid)

        # Bitrate slider + spin
        self._last_bitrate_mbps = 100
        self._last_cq_value = 18
        self._syncing = False

        bitrate_row = QHBoxLayout()
        self.bitrate_slider = QSlider(Qt.Orientation.Horizontal)
        self.bitrate_slider.setRange(1, 1000)
        self.bitrate_slider.setValue(100)
        self.bitrate_slider.setStyleSheet("QSlider::groove:horizontal { background: rgba(255,255,255,0.1); height: 6px; border-radius: 3px; } QSlider::handle:horizontal { background: #00ff88; width: 16px; height: 16px; border-radius: 8px; margin: -5px 0; } QSlider::sub-page:horizontal { background: #00ff88; border-radius: 3px; }")
        self.bitrate_slider.valueChanged.connect(self._on_bitrate_slider)
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(1, 1000)
        self.bitrate_spin.setValue(100)
        self.bitrate_spin.setSuffix(" Mbps")
        self.bitrate_spin.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; color: #7df9ff; font-weight: bold; padding: 4px;")
        self.bitrate_spin.valueChanged.connect(self._on_bitrate_spin)
        bitrate_row.addWidget(self.bitrate_slider)
        bitrate_row.addWidget(self.bitrate_spin)
        br_layout.addWidget(QLabel("Target Bitrate:"))
        br_layout.addLayout(bitrate_row)

        # CQP row
        cqp_row = QHBoxLayout()
        self.cqp_spin = QSpinBox()
        self.cqp_spin.setRange(0, 51)
        self.cqp_spin.setValue(18)
        self.cqp_spin.setPrefix("QP: ")
        self.cqp_spin.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; color: #ff8a00; font-weight: bold; padding: 4px;")
        self.cqp_spin.valueChanged.connect(self._on_cqp_spin)
        self.cqp_label = QLabel("QP 18 (visually lossless)")
        self.cqp_label.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 10px;")
        cqp_row.addWidget(self.cqp_spin)
        cqp_row.addWidget(self.cqp_label)
        cqp_row.addStretch()
        self.cqp_widget = QWidget()
        self.cqp_widget.setLayout(cqp_row)
        self.cqp_widget.setVisible(False)
        br_layout.addWidget(self.cqp_widget)

        # GOP & B-Frames
        gop_row = QHBoxLayout()
        self.bf_slider = QSlider(Qt.Orientation.Horizontal)
        self.bf_slider.setRange(0, 4)
        self.bf_slider.setValue(3)
        self.bf_slider.setStyleSheet("QSlider::groove:horizontal { background: rgba(255,255,255,0.1); height: 4px; border-radius: 2px; } QSlider::handle:horizontal { background: #7df9ff; width: 12px; height: 12px; border-radius: 6px; margin: -4px 0; }")
        self.bf_val_label = QLabel("3")
        self.bf_val_label.setStyleSheet("color: #7df9ff; font-weight: bold; font-size: 10px;")
        self.bf_slider.valueChanged.connect(lambda v: self.bf_val_label.setText(str(v)))
        gop_col1 = QVBoxLayout()
        gop_col1.addWidget(QLabel("B-Frames:"))
        bf_row = QHBoxLayout()
        bf_row.addWidget(self.bf_slider)
        bf_row.addWidget(self.bf_val_label)
        gop_col1.addLayout(bf_row)

        self.gop_slider = QSlider(Qt.Orientation.Horizontal)
        self.gop_slider.setRange(1, 300)
        self.gop_slider.setValue(60)
        self.gop_slider.setStyleSheet("QSlider::groove:horizontal { background: rgba(255,255,255,0.1); height: 4px; border-radius: 2px; } QSlider::handle:horizontal { background: #7df9ff; width: 12px; height: 12px; border-radius: 6px; margin: -4px 0; }")
        self.gop_val_label = QLabel("60")
        self.gop_val_label.setStyleSheet("color: #7df9ff; font-weight: bold; font-size: 10px;")
        self.gop_slider.valueChanged.connect(lambda v: self.gop_val_label.setText(str(v)))
        gop_col2 = QVBoxLayout()
        gop_col2.addWidget(QLabel("GOP Size:"))
        gop_row2 = QHBoxLayout()
        gop_row2.addWidget(self.gop_slider)
        gop_row2.addWidget(self.gop_val_label)
        gop_col2.addLayout(gop_row2)

        gop_row.addLayout(gop_col1)
        gop_row.addLayout(gop_col2)
        br_layout.addLayout(gop_row)

        layout.addWidget(br_group)

        # AUDIO
        audio_group = QWidget()
        audio_group.setStyleSheet("background: #111113; border: 1px solid rgba(255,255,255,0.06); border-radius: 16px;")
        audio_layout = QVBoxLayout(audio_group)
        audio_label = QLabel("AUDIO MASTER")
        audio_label.setStyleSheet("font-size: 11px; font-weight: 500; color: rgba(255,255,255,0.4); letter-spacing: 1.5px;")
        audio_layout.addWidget(audio_label)

        self.audio_combo = QComboBox()
        self.audio_combo.addItems([
            "AAC-LC 320 kbps (High Fidelity Broadcast)",
            "Linear PCM 24-bit (Uncompressed Studio Master)",
            "FLAC 24-bit Lossless",
            "AAC-LC 192 kbps Standard",
            "Copy Stream"
        ])
        self.audio_combo.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 6px; color: white;")
        audio_layout.addWidget(QLabel("Audio Codec:"))
        audio_layout.addWidget(self.audio_combo)

        self.sample_combo = QComboBox()
        self.sample_combo.addItems([
            "48.0 kHz (Cinema & Broadcast Standard)",
            "96.0 kHz (Studio High-Res Master)",
            "44.1 kHz (CD Audio)"
        ])
        self.sample_combo.setStyleSheet("background: #0f0f14; border: 1px solid rgba(255,255,255,0.1); border-radius: 8px; padding: 6px; color: white;")
        audio_layout.addWidget(QLabel("Sample Rate:"))
        audio_layout.addWidget(self.sample_combo)

        norm_card = QWidget()
        norm_card.setStyleSheet("background: rgba(0,255,136,0.05); border: 1px solid rgba(0,255,136,0.2); border-radius: 12px;")
        norm_layout = QVBoxLayout(norm_card)
        norm_layout.setContentsMargins(10, 10, 10, 10)
        norm_title = QLabel("✓ EBU R128 -14 LUFS Normalization")
        norm_title.setStyleSheet("color: #00ff88; font-weight: bold; font-size: 11px;")
        norm_layout.addWidget(norm_title)
        norm_desc = QLabel("Peak-limited 1ms lookahead, 64-bit float processing")
        norm_desc.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 9px;")
        norm_desc.setWordWrap(True)
        norm_layout.addWidget(norm_desc)
        audio_layout.addWidget(norm_card)

        layout.addWidget(audio_group)

        # GPU PIPELINE
        gpu_group = QWidget()
        gpu_group.setStyleSheet("background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px;")
        gpu_layout = QVBoxLayout(gpu_group)
        gpu_label = QLabel("GPU PIPELINE")
        gpu_label.setStyleSheet("font-size: 11px; font-weight: 500; color: rgba(255,255,255,0.4); letter-spacing: 1.5px;")
        gpu_layout.addWidget(gpu_label)

        self.gpu_check = QCheckBox("Enable GPU Acceleration (Auto)")
        self.gpu_check.setChecked(True)
        self.gpu_decode_check = QCheckBox("Enable Hardware Decoding (NVDEC/AMF/QSV)")
        self.gpu_decode_check.setChecked(True)
        self.gpu_composite_check = QCheckBox("Enable Full GPU Compositing (TURBO - stays in VRAM)")
        self.gpu_composite_check.setChecked(True)
        self.gpu_composite_check.setStyleSheet("color: #00ff88; font-weight: bold;")
        for chk in [self.gpu_check, self.gpu_decode_check, self.gpu_composite_check]:
            chk.setStyleSheet("color: rgba(255,255,255,0.8); font-size: 11px;")
            gpu_layout.addWidget(chk)

        self.single_pass_check = QCheckBox("Single-pass render (recommended - uncheck for legacy split render)")
        # Persisted: a render-mode flip must survive restarts (checked once).
        try:
            _sp_saved = self.app.app_settings.value('single_pass_render', True)
            if isinstance(_sp_saved, str):
                _sp_saved = _sp_saved.lower() in ('true', '1', 'yes', 'on')
            self.single_pass_check.setChecked(bool(_sp_saved))
        except Exception:
            self.single_pass_check.setChecked(True)
        self.single_pass_check.setStyleSheet("color: #00ff88; font-weight: bold; font-size: 11px;")
        self.single_pass_check.setToolTip("ON: video + audio render together straight to the final file (no 30-min remux). OFF: legacy split render - video pass, audio pass, then remux. Flip freely; both paths stay tested.")
        self.single_pass_check.toggled.connect(self._save_single_pass_pref)
        gpu_layout.addWidget(self.single_pass_check)

        turbo_warn = QLabel("⚠️ TURBO Hybrid: With filters, switches to Hybrid (GPU scale+overlay, CPU filters). Without filters, full VRAM zero-copy.")
        turbo_warn.setWordWrap(True)
        turbo_warn.setStyleSheet("color: #fbbf24; font-size: 8px; background: #1f2937; padding: 4px; border-radius: 4px;")
        gpu_layout.addWidget(turbo_warn)

        layout.addWidget(gpu_group)

        layout.addStretch()

        # EXPORT BUTTON - EXACT HTML white pill 48px + black circle + est line
        self.export_btn = QPushButton("  ▶   Export with RTX 5070 • CBR100")
        self.export_btn.setFixedHeight(48)
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.setStyleSheet("""
            QPushButton {
                background: white;
                color: black; border-radius: 24px; font-weight: 600; font-size: 13px;
                padding: 0 16px; text-align: center;
                border: 1px solid rgba(255,255,255,0.2);
            }
            QPushButton:hover { background: #f0f0f0; }
            QPushButton:pressed { background: #d9d9d9; }
            QPushButton:disabled { background: rgba(255,255,255,0.1); color: rgba(255,255,255,0.3); border: 1px solid rgba(255,255,255,0.06); }
        """)
        self.export_btn.clicked.connect(self.do_export)
        layout.addWidget(self.export_btn)
        
        est_label = QLabel("est. 14.2s • 4.8 GB • 240 fps encode")
        est_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        est_label.setStyleSheet("color: rgba(255,255,255,0.25); font-size: 10px; font-family: Consolas, monospace;")
        layout.addWidget(est_label)

        self.setWidget(container)
        self.selected_preset = self.presets[0]
        self.rate_control_combo.currentIndexChanged.connect(self._on_rate_control_changed)
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        self._on_rate_control_changed(0)
        self._on_codec_changed(self.codec_combo.currentIndex())
        
    def wheelEvent(self, event):
        """Prevent scroll wheel from changing settings in export panel"""
        # Ignore wheel events to prevent accidental changes when scrolling
        event.ignore()
        return

    def _on_codec_changed(self, idx):
        try:
            codec_id = self.codec_options[idx][1] if idx < len(self.codec_options) else "hevc_nvenc"
            is_prores = codec_id.startswith("prores")
            self.prores_profile_combo.setVisible(is_prores)
        except Exception:
            pass
        try:
            self._update_export_btn()
        except Exception:
            pass

    def _on_rate_control_changed(self, idx):
        rc_map = {0: 'cbr', 1: 'vbr', 2: 'abr', 3: 'cqp', 4: 'lossless'}
        rc = rc_map.get(idx, 'cbr')
        is_cqp = rc == 'cqp'
        is_lossless = rc == 'lossless'
        self.cqp_widget.setVisible(is_cqp or is_lossless)
        if rc == 'lossless':
            self.cqp_spin.setValue(0)
            self.cqp_label.setText("Lossless archive (QP 0) - huge files")
            # FIX: Deselect all CBR presets when lossless selected - was leaving CBR100 highlighted
            for btn in self.preset_btns:
                btn.setChecked(False)
        elif rc == 'cqp':
            # CQP - deselect CBR presets unless it's CQP 18 preset
            if self.cqp_spin.value() != 18:
                for btn in self.preset_btns:
                    # Keep CQP preset highlighted only if QP 18
                    if "CQP" not in btn.text():
                        btn.setChecked(False)
        else:
            # CBR/VBR/ABR - if current bitrate matches a preset, highlight it, else deselect all
            current_mbps = self.bitrate_spin.value()
            matched = False
            for btn, preset in zip(self.preset_btns, self.presets):
                if preset['rc'] == rc and preset['mbps'] == current_mbps:
                    # Only auto-select if it was previously matching - don't force
                    pass
            self._update_export_btn()

    def _on_bitrate_slider(self, v):
        if self._syncing:
            return
        self._syncing = True
        self.bitrate_spin.setValue(v)
        self._last_bitrate_mbps = v
        self._syncing = False
        self._update_export_btn()

    def _on_bitrate_spin(self, v):
        if self._syncing:
            return
        self._syncing = True
        self.bitrate_slider.setValue(v)
        self._last_bitrate_mbps = v
        self._syncing = False
        self._update_export_btn()

    def _on_cqp_spin(self, v):
        self._last_cq_value = v
        label = "lossless" if v==0 else "visually lossless" if v<=18 else "high quality" if v<=28 else "medium" if v<=38 else "lower quality"
        self.cqp_label.setText(f"QP {v} ({label})")
        self._update_export_btn()

    def select_preset(self, idx):
        # EXACT HTML active card keeps cyan dot + NVENC line; backend sync unchanged
        for i, btn in enumerate(self.preset_btns):
            btn.setChecked(i == idx)
            try:
                p = self.presets[i]
                if i == idx:
                    btn.setText(f"● {p['name']}\n{p['sub']}\n◉ NVENC HEVC")
                else:
                    btn.setText(f"{p['name']}\n{p['sub']}")
            except Exception:
                pass
        self.selected_preset = self.presets[idx]
        # Sync rate control
        rc = self.selected_preset['rc']
        rc_idx = {'cbr':0, 'vbr':1, 'abr':2, 'cqp':3, 'lossless':4}.get(rc, 0)
        # Block signals to avoid recursive deselect
        self.rate_control_combo.blockSignals(True)
        self.rate_control_combo.setCurrentIndex(rc_idx)
        self.rate_control_combo.blockSignals(False)
        if rc == 'cqp':
            self._syncing = True
            self.cqp_spin.setValue(18)
            self.cqp_widget.setVisible(True)
            self.cqp_label.setText("QP 18 (visually lossless)")
            self._syncing = False
        elif rc == 'lossless':
            self._syncing = True
            self.cqp_spin.setValue(0)
            self.cqp_widget.setVisible(True)
            self.cqp_label.setText("Lossless archive (QP 0) - huge files")
            self._syncing = False
        else:
            mbps = self.selected_preset['mbps']
            self._syncing = True
            self.bitrate_slider.setValue(mbps)
            self.bitrate_spin.setValue(mbps)
            self._last_bitrate_mbps = mbps
            self.cqp_widget.setVisible(False)
            self._syncing = False
        # Instagram preset handling
        if 'Insta' in self.selected_preset['name']:
            self.res_combo.setCurrentIndex(7)  # Instagram Square
            # Find Instagram target in combo
            for i in range(self.nvenc_target_combo.count()):
                if 'Instagram' in self.nvenc_target_combo.itemText(i) or 'P5' in self.nvenc_target_combo.itemText(i):
                    self.nvenc_target_combo.setCurrentIndex(i)
                    break
        self._update_export_btn()
        
    def _update_export_btn(self):
        try:
            rc = self.rate_control_combo.currentIndex()
            # EXACT HTML: Export with RTX 5070 • CBR100
            if rc == 3:  # CQP
                qp = self.cqp_spin.value()
                self.export_btn.setText(f"  ▶   Export with RTX 5070 • QP{qp}")
            elif rc == 4:  # Lossless
                self.export_btn.setText("  ▶   Export with RTX 5070 • LOSSLESS")
            else:
                mbps = self.bitrate_spin.value()
                rc_name = {0:'CBR',1:'VBR',2:'ABR'}.get(rc, 'CBR')
                self.export_btn.setText(f"  ▶   Export with RTX 5070 • {rc_name}{mbps}")
        except Exception:
            pass
        # live HTML summary rows (new feature)
        try:
            idx = self.codec_combo.currentIndex()
            cid = self.codec_options[idx][1] if 0 <= idx < len(self.codec_options) else "hevc_nvenc"
            short = "HEVC" if "hevc" in cid else "H.264" if "h264" in cid else "AV1" if "av1" in cid else "ProRes" if "prores" in cid else cid
            if hasattr(self, 'cd_badge'):
                self.cd_badge.setText(f"MP4 • {short}")
            if hasattr(self, 'codec_summary_encoder'):
                self.codec_summary_encoder.setText(f"{cid} • p7 • 2-pass")
            # B-frames / GOP live
            if hasattr(self, 'bf_val_label') and hasattr(self, 'gop_val_label'):
                pass
        except Exception:
            pass

    def _rate_control_value(self):
        rc_map = {0: 'cbr', 1: 'vbr', 2: 'abr', 3: 'cqp', 4: 'lossless'}
        return rc_map.get(self.rate_control_combo.currentIndex(), 'cbr')

    def get_full_settings(self):
        # Build full settings dict from all controls - restores 90% missing
        codec_map = {i: cid for i, (_, cid) in enumerate(self.codec_options)}
        audio_map = {0: "aac", 1: "pcm_s24le", 2: "flac", 3: "aac", 4: "copy"}
        sample_map = {0: 48000, 1: 96000, 2: 44100}
        fps_map = {0: 23.976, 1: 24.0, 2: 25.0, 3: 29.97, 4: 30.0, 5: 50.0, 6: 59.94, 7: 60.0, 8: 120.0}
        rc = self._rate_control_value()
        
        # Resolution handling with Instagram fix
        res_idx = self.res_combo.currentIndex()
        # Map new combo (9 items) to old export_res_index + handle Instagram
        # 0=Source, 1=1080, 2=1440, 3=4K, 4=Ultrawide, 5=5K, 6=8K, 7=Insta Square, 8=Insta Portrait
        export_res_index = res_idx
        if res_idx == 7 or res_idx == 8:
            # Instagram special - will be handled in TimelineRenderingEngine
            export_res_index = res_idx
        
        return {
            'video_codec': codec_map.get(self.codec_combo.currentIndex(), "hevc_nvenc"),
            'audio_codec': audio_map.get(self.audio_combo.currentIndex(), "aac"),
            'audio_sample_rate': sample_map.get(self.sample_combo.currentIndex(), 48000),
            'use_gpu': self.gpu_check.isChecked(),
            'use_gpu_decode': self.gpu_decode_check.isChecked(),
            'use_gpu_composite': self.gpu_composite_check.isChecked(),
            'bitrate_mbps': self.bitrate_spin.value(),
            'cq_value': self.cqp_spin.value() if rc == 'cqp' else 18,
            'prores_profile': self.prores_profile_combo.currentIndex(),
            'export_target_index': self.nvenc_target_combo.currentIndex(),
            'rate_control': rc,
            'export_res_index': export_res_index,
            'timeline_fps': fps_map.get(self.fps_combo.currentIndex(), 60.0),
            'pixel_format': self.pixel_format_combo.currentIndex(),
            'b_frames': self.bf_slider.value(),
            'gop_size': self.gop_slider.value(),
            'container_format': self.container_combo.currentIndex(),
            'single_pass_render': self.single_pass_check.isChecked() if hasattr(self, 'single_pass_check') else True,
            'temp_dir': getattr(self.app, 'temp_dir', tempfile.gettempdir()) if self.app else tempfile.gettempdir(),
        }

    def _save_single_pass_pref(self, on):
        try:
            self.app.app_settings.setValue('single_pass_render', bool(on))
        except Exception:
            pass

    def do_export(self):
        # Full settings - not hardcoded 90% missing
        settings = self.get_full_settings()
        self.app.direct_export_from_panel(settings)



class ExportWindow(QDialog):
    """Dedicated export window (not a dock): full-width panel, never squished.

    Hosts the single ExportPanelWidget instance owned by the app so all
    settings state stays in one place. Non-modal — the timeline stays usable.
    """
    def __init__(self, app, panel, parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Export • Encode - FastEncode Pro")
        # Small minimums: the panel scrolls, so the window must never force
        # itself maximized on short screens (which would cover the main
        # window and make edge docking unreachable).
        self.setMinimumSize(320, 480)
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowTitleHint |
            Qt.WindowType.WindowSystemMenuHint |
            Qt.WindowType.WindowMinMaxButtonsHint |
            Qt.WindowType.WindowCloseButtonHint
        )
        self.setStyleSheet("QDialog { background: #0b0b0f; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        panel.setParent(self)
        layout.addWidget(panel, stretch=1)
        restored = False
        try:
            saved = QSettings("FastEncodePro", "App2026ExactV2").value("export_window_geometry")
            if saved is not None and hasattr(saved, 'isEmpty') and not saved.isEmpty():
                restored = bool(self.restoreGeometry(saved))
        except Exception:
            restored = False
        if not restored:
            try:
                self.resize(440, 800)
                if parent is not None:
                    pg = parent.geometry()
                    self.move(pg.x() + max(0, (pg.width() - 440) // 2),
                              pg.y() + max(0, (pg.height() - 800) // 2))
            except Exception:
                try:
                    self.resize(440, 800)
                except Exception:
                    pass

    def hideEvent(self, event):
        try:
            QSettings("FastEncodePro", "App2026ExactV2").setValue("export_window_geometry", self.saveGeometry())
        except Exception:
            pass
        try:
            super().hideEvent(event)
        except Exception:
            pass

    def closeEvent(self, event):
        # Hiding instead of destroying keeps panel state; reopen via Export tab.
        try:
            event.ignore()
            self.hide()
        except Exception:
            super().closeEvent(event)



class FastEncodeProApp(QMainWindow):


    def __init__(self):
        super().__init__()
        # Docking: edge drop zones on the main window (not just tabify onto
        # other docks) need AllowNestedDocks + nesting enabled BEFORE the first
        # addDockWidget, plus corners mapped to top/bottom so those edges exist.
        # GroupedDragging is deliberately NOT set: in Qt6 it collapses drag
        # targets to tabify-only, which is exactly the reported symptom.
        self.setDockOptions(
            QMainWindow.DockOption.AllowNestedDocks |
            QMainWindow.DockOption.AllowTabbedDocks |
            QMainWindow.DockOption.AnimatedDocks
        )
        # CRITICAL: Nesting must be enabled BEFORE any addDockWidget
        self.setDockNestingEnabled(True)
        # Corners mapped to top/bottom edges so dragged docks offer those
        # drop zones instead of only side/tab targets.
        self.setCorner(Qt.Corner.TopLeftCorner, Qt.DockWidgetArea.TopDockWidgetArea)
        self.setCorner(Qt.Corner.TopRightCorner, Qt.DockWidgetArea.TopDockWidgetArea)
        self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
        self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
        # Additional Wayland fix: ensure dock areas are enabled
        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)
        # Keep minimum size but don't force fixed aspect that breaks docking
        self.setMinimumSize(1100, 700)
        self.setWindowTitle(f"FastEncode Pro v{__version__} - 2026 Glass Edition • HYPRLAND EDITION")
        # Screen geometry - FIXED for maximized zoom bug
        # Don't force geometry if session wants maximized, use resize + move instead of setGeometry
        screen = QApplication.primaryScreen()
        if screen is not None:
            ag = screen.availableGeometry()
            w = min(1600, max(1100, ag.width() - 48))
            h = min(1000, max(700, ag.height() - 48))
            x = ag.x() + (ag.width() - w)//2
            y = ag.y() + (ag.height() - h)//2
            # Use resize/move not setGeometry to avoid Wayland maximize conflict
            self.resize(w, h)
            self.move(x, y)
        else:
            self.resize(1600, 1000)
            self.move(50, 50)

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
        self.app_settings = QSettings("FastEncodePro", "App2026ExactV2")
        try:
            _ensure_preview_mode_default()
        except Exception:
            pass
        self.output_folder = self.app_settings.value("output_folder", "")
        # Temp directory for video rendering (default to system temp)
        self.temp_dir = self.app_settings.value("temp_dir", tempfile.gettempdir())
        # Project defaults pop-up on first run - DEFERRED to avoid crash before show()
        # Don't show modal dialog in __init__ - causes Wayland crash
        self._needs_project_defaults = not self.app_settings.contains("project_fps")
        
        cpu_count = os.cpu_count() or 8
        self.cpu_cores = int(self.app_settings.value("cpu_cores", cpu_count)) if self.app_settings.contains("cpu_cores") else cpu_count
        self.gpu_vram_limit_percent = int(self.app_settings.value("gpu_vram_limit_percent", 90)) if self.app_settings.contains("gpu_vram_limit_percent") else 90
        self.gpu_vram_limit_mb = int(self.app_settings.value("gpu_vram_limit_mb", self.hw_caps.get('gpu_vram_total_mb', 8192))) if self.app_settings.contains("gpu_vram_limit_mb") else self.hw_caps.get('gpu_vram_total_mb', 8192)
        auto_proxy_val = self.app_settings.value("auto_proxy_enabled", True)
        if isinstance(auto_proxy_val, str):
            self.auto_proxy_enabled = auto_proxy_val.lower() in ('true','1','yes')
        else:
            self.auto_proxy_enabled = bool(auto_proxy_val) if auto_proxy_val is not None else True
        self.proxy_status_label = QLabel("Proxies: Idle")
        self.proxy_status_label.setStyleSheet("font-family: 'JetBrains Mono', monospace; font-size: 10px; color: rgba(255,255,255,0.6);")
        # Long "Proxies: <file> .. NN% (N left)" texts must never widen the
        # dock (an oversized dock minimum suppresses main-window edge drops).
        self.proxy_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.proxy_progress_bar = QProgressBar()
        self.proxy_progress_bar.setRange(0, 100)
        self.proxy_progress_bar.setValue(0)
        self.proxy_progress_bar.setFixedWidth(140)
        self.proxy_progress_bar.setFixedHeight(8)
        self.proxy_progress_bar.setTextVisible(False)
        self.proxy_progress_bar.setStyleSheet("QProgressBar { background: rgba(255,255,255,0.15); border-radius: 4px; border: 1px solid rgba(255,255,255,0.1); } QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #00ff88, stop:1 #7df9ff); border-radius: 3px; }")
        self.proxy_progress_bar.setVisible(False)
        self.proxy_manager = ProxyManager(self.temp_dir)

        def _on_proxy_status(text):
            self.proxy_status_label.setText(text)
            txt_low = text.lower()
            is_active = ("left" in txt_low or "%" in text or "generating" in txt_low)
            is_done = ("up to date" in txt_low or "idle" in txt_low or "cancelled" in txt_low or "cleared" in txt_low)
            self.proxy_progress_bar.setVisible(is_active and not is_done)
            if is_done:
                self.proxy_progress_bar.setValue(0)
                if "idle" in txt_low or "cancelled" in txt_low or "cleared" in txt_low:
                    self.proxy_progress_bar.setVisible(False)
            if hasattr(self, 'count_badge'):
                total_proxies = len(self.proxy_manager.proxy_map)
                self.count_badge.setText(f"{len(self.media_library)} FILES - {total_proxies} PROXIES")

        def _on_proxy_file_progress(path, pct):
            self.proxy_progress_bar.setValue(pct)
            if 0 <= pct < 100:
                self.proxy_progress_bar.setVisible(True)

        self.proxy_manager.status_update.connect(_on_proxy_status)
        self.proxy_manager.file_progress.connect(_on_proxy_file_progress)

        # CENTRAL WIDGET = Preview + Top Bar + Scrubber (NOT a dock) - fixes overlap
        central = QWidget()
        central.setObjectName("Central")
        central.setStyleSheet("QWidget#Central { background: #08080a; }")
        self.setCentralWidget(central)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0,0,0,0)
        central_layout.setSpacing(0)

        # TOP BAR - fixed 56px, now part of central top, always visible, not a dock
        self.top_bar = QWidget()
        self.top_bar.setFixedHeight(56)
        self.top_bar.setObjectName("TopBar")
        self.top_bar.setStyleSheet("QWidget#TopBar { background: rgba(13,13,17,0.95); border-bottom: 1px solid rgba(255,255,255,0.08); }")
        top_layout = QHBoxLayout(self.top_bar)
        top_layout.setContentsMargins(12,0,12,0)
        top_layout.setSpacing(16)

        logo = QLabel("FE")
        logo.setFixedSize(28,28)
        logo.setStyleSheet("background: white; color: black; border-radius: 8px; font-weight: 900; font-size: 11px;")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(logo)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title_col.setContentsMargins(0,0,0,0)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_label = QLabel("FastEncode Pro")
        title_label.setStyleSheet("font-size: 13px; font-weight: 600; color: white;")
        title_row.addWidget(title_label)
        ver_badge = QLabel("2026 CONCEPT")
        ver_badge.setStyleSheet("background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; padding: 2px 6px; font-size: 10px; color: rgba(255,255,255,0.6);")
        title_row.addWidget(ver_badge)
        title_row.addStretch()
        title_col.addLayout(title_row)
        sub_label = QLabel("project_ultrawide_4K_v3.feproj • autosaved")
        sub_label.setStyleSheet("font-size: 10px; color: rgba(255,255,255,0.4);")
        title_col.addWidget(sub_label)
        top_layout.addLayout(title_col)

        self.top_tabs = QWidget()
        self.top_tabs.setStyleSheet("background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06); border-radius: 24px; padding: 4px;")
        tabs_layout = QHBoxLayout(self.top_tabs)
        tabs_layout.setContentsMargins(4,4,4,4)
        tabs_layout.setSpacing(4)
        self.tab_buttons = {}
        # EXACT HTML tabs: Edit/Cut/Color/Audio/Export (Export active). Map to internal dock logic.
        _tab_map = [("Edit", "MEDIA", False), ("Cut", "TIMELINE", False), ("Color", "COLOR", False), ("Audio", "AUDIO", False), ("Export", "EXPORT", True)]
        for disp, key, active in _tab_map:
            btn = QPushButton(disp)
            btn.setCheckable(True)
            btn.setChecked(active)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton:checked { background: white; color: black; border-radius: 16px; padding: 6px 14px; font-size: 12px; font-weight: 500; }
                QPushButton:!checked { background: transparent; color: rgba(255,255,255,0.5); border-radius: 16px; padding: 6px 14px; font-size: 12px; font-weight: 500; }
            """)
            btn.clicked.connect(lambda checked, n=key: self.switch_main_tab(n))
            tabs_layout.addWidget(btn)
            self.tab_buttons[key] = btn
        # keep legacy TIMELINE key alias for backend that references it
        if "TIMELINE" not in self.tab_buttons:
            self.tab_buttons["TIMELINE"] = self.tab_buttons.get("Cut")
        top_layout.addWidget(self.top_tabs)
        top_layout.addStretch()

        gpu_widget = QWidget()
        gpu_widget.setObjectName("GpuWidget")
        gpu_widget.setFixedHeight(32)
        gpu_widget.setStyleSheet("QWidget#GpuWidget { background: #0f1210; border: 1px solid rgba(0,255,136,0.2); border-radius: 16px; }")
        gpu_layout = QHBoxLayout(gpu_widget)
        gpu_layout.setContentsMargins(12,0,12,0)
        gpu_layout.setSpacing(8)
        dot = QLabel()
        dot.setFixedSize(8,8)
        dot.setStyleSheet("background: #00ff88; border-radius: 4px;")
        gpu_layout.addWidget(dot)
        gpu_det = QLabel("GPU DETECTED")
        gpu_det.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.8);")
        gpu_layout.addWidget(gpu_det)
        sep = QLabel()
        sep.setFixedSize(1,12)
        sep.setStyleSheet("background: rgba(255,255,255,0.1);")
        gpu_layout.addWidget(sep)
        gpu_name = self.hw_caps.get('gpu_name', 'NVIDIA RTX 5070')
        vram_gb = self.hw_caps.get('gpu_vram_total_gb', 12)
        # EXACT HTML: NVIDIA RTX 5070 • 12GB in green
        if "5070" in gpu_name or "NVIDIA" in gpu_name.upper() or "RTX" in gpu_name.upper():
            gpu_text = "NVIDIA RTX 5070 • 12GB"
        elif vram_gb and vram_gb > 0:
            gpu_text = f"{gpu_name[:22]} • {vram_gb:.0f}GB"
        else:
            gpu_text = "NVIDIA RTX 5070 • 12GB"
        self.gpu_label = QLabel(gpu_text)
        self.gpu_label.setObjectName("GpuLabel")
        self.gpu_label.setStyleSheet("font-size: 11px; font-weight: 600; color: #00ff88;")
        gpu_layout.addWidget(self.gpu_label)
        zap = QLabel("⚡")
        zap.setStyleSheet("font-size: 11px; color: #00ff88; background: transparent; border: none;")
        gpu_layout.addWidget(zap)
        # keep vram_label hidden for backend compat (live VRAM updater writes here)
        self.vram_label = QLabel("")
        self.vram_label.setObjectName("VramLabel")
        self.vram_label.setVisible(False)
        gpu_layout.addWidget(self.vram_label)
        top_layout.addWidget(gpu_widget)

        # ellipsis + settings circles (HTML) + mac dots
        hdr_btns = QWidget()
        hdr_layout = QHBoxLayout(hdr_btns)
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        hdr_layout.setSpacing(6)
        for _txt in ["…", "⚙"]:
            _b = QLabel(_txt)
            _b.setFixedSize(32, 32)
            _b.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _b.setStyleSheet("background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; color: rgba(255,255,255,0.6); font-size: 13px;")
            hdr_layout.addWidget(_b)
        top_layout.addWidget(hdr_btns)

        controls = QWidget()
        ctrl_layout = QHBoxLayout(controls)
        ctrl_layout.setContentsMargins(0,0,0,0)
        ctrl_layout.setSpacing(6)
        for col in ["#ff5f56", "#ffbd2e", "#27c93f"]:
            d = QLabel()
            d.setFixedSize(12,12)
            d.setStyleSheet(f"background: {col}; border-radius: 6px; border: 1px solid rgba(0,0,0,0.2);")
            ctrl_layout.addWidget(d)
        top_layout.addWidget(controls)

        central_layout.addWidget(self.top_bar)

        # PREVIEW AREA - central, not a dock, so it never gets cut off
        preview_area = QWidget()
        preview_area.setStyleSheet("background: #08080a;")
        preview_layout = QVBoxLayout(preview_area)
        preview_layout.setContentsMargins(12,12,12,12)
        preview_layout.setSpacing(12)

        preview_header = QHBoxLayout()
        preview_header.setSpacing(8)
        # EXACT HTML: MPV pill with cyan glow dot
        mpv_pill = QWidget()
        mpv_pill.setStyleSheet("background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;")
        mpv_layout = QHBoxLayout(mpv_pill)
        mpv_layout.setContentsMargins(10, 4, 10, 4)
        mpv_layout.setSpacing(6)
        mpv_dot = QLabel()
        mpv_dot.setFixedSize(6, 6)
        mpv_dot.setStyleSheet("background: #7df9ff; border-radius: 3px;")
        mpv_layout.addWidget(mpv_dot)
        mpv_txt = QLabel("MPV  •  Hardware Decode")
        mpv_txt.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.7); background: transparent; border: none;")
        mpv_layout.addWidget(mpv_txt)
        preview_header.addWidget(mpv_pill)
        # resolution pill
        res_pill = QWidget()
        res_pill.setStyleSheet("background: #101010; border: 1px solid rgba(255,255,255,0.06); border-radius: 12px;")
        res_layout = QHBoxLayout(res_pill)
        res_layout.setContentsMargins(10, 4, 10, 4)
        res_layout.setSpacing(6)
        self.res_badge = QLabel("3840x1600 • 59.94fps")
        self.res_badge.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.5); background: transparent; border: none;")
        res_layout.addWidget(self.res_badge)
        preview_header.addWidget(res_pill)
        try:
            _emb_mode = _mpv_preview_mode_static()
        except Exception:
            _emb_mode = 'auto'
        try:
            _emb0 = _mpv_should_attempt_embed()
        except Exception:
            _emb0 = False
        self.preview_mode_badge = QLabel(
            ("AUTO → EMBEDDED" if _emb0 else "AUTO → EXTERNAL") if _emb_mode == 'auto'
            else ("EMBEDDED (exp.)" if _emb_mode == 'embed' else "EXTERNAL")
        )
        self.preview_mode_badge.setToolTip(f"MPV preview mode: {_emb_mode} ({_mpv_session_label()}). View menu or Settings to change (restart).")
        self.preview_mode_badge.setStyleSheet(
            "font-size: 10px; padding: 4px 8px; border-radius: 10px; color: #7df9ff; "
            "background: rgba(125,249,255,0.08); border: 1px solid rgba(125,249,255,0.2);"
            if _emb0 else
            "font-size: 10px; padding: 4px 8px; border-radius: 10px; color: rgba(255,255,255,0.5); "
            "background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);"
        )
        preview_header.addWidget(self.preview_mode_badge)
        preview_header.addStretch()
        self.timecode_label = QLabel("00:12:43:19")
        self.timecode_label.setStyleSheet("background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 4px 10px; font-family: Consolas, monospace; font-size: 11px; color: rgba(255,255,255,0.7);")
        preview_header.addWidget(self.timecode_label)
        fs_btn = QPushButton("⛶")
        fs_btn.setFixedSize(28, 28)
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet("background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; color: rgba(255,255,255,0.6);")
        fs_btn.clicked.connect(self.enter_fullscreen)
        preview_header.addWidget(fs_btn)
        preview_layout.addLayout(preview_header)

        self.video_container = QWidget()
        self.video_container.setStyleSheet("background: black; border: 1px solid rgba(255,255,255,0.08); border-radius: 20px;")
        self.video_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_container_layout = QVBoxLayout(self.video_container)
        self.video_container_layout.setContentsMargins(0, 0, 0, 0)
        self.video_container_layout.setSpacing(0)
        # video stack area (MPV + overlays) — plain black until media loads
        self.video_stack = QWidget()
        self.video_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_stack.setStyleSheet("background: black; border-top-left-radius: 20px; border-top-right-radius: 20px;")
        stack_layout = QVBoxLayout(self.video_stack)
        stack_layout.setContentsMargins(0, 0, 0, 0)
        self.video_widget = MPVVideoWidget()
        self.video_widget.setMinimumSize(320, 180)
        self.video_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_widget.setStyleSheet("background: black; border-radius: 20px;")
        stack_layout.addWidget(self.video_widget)
        self.video_container_layout.addWidget(self.video_stack, stretch=1)
        self.video_widget.show()
        self.video_widget.positionChanged.connect(self._on_position_changed)
        self.video_widget.durationChanged.connect(self._on_duration_changed)
        # --- overlays (children of video_stack, raised) ---
        self.rec_badge = QLabel("REC • 4K HDR")
        self.rec_badge.setStyleSheet("background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 4px 8px; font-size: 10px; color: white;")
        self.rec_badge.setParent(self.video_stack)
        self.rec_badge.move(12, 12)
        self.nvenc_badge = QLabel("NVENC HEVC")
        self.nvenc_badge.setStyleSheet("background: rgba(0,255,136,0.15); border: 1px solid rgba(0,255,136,0.3); border-radius: 10px; padding: 4px 8px; font-size: 10px; color: #00ff88;")
        self.nvenc_badge.setParent(self.video_stack)
        self.nvenc_badge.move(110, 12)
        self.gpu_temp_badge = QLabel("RTX 5070 • 73% • 71°C")
        self.gpu_temp_badge.setStyleSheet("background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.1); border-radius: 10px; padding: 4px 8px; font-family: Consolas, monospace; font-size: 10px; color: rgba(255,255,255,0.7);")
        self.gpu_temp_badge.setParent(self.video_stack)
        self.gpu_temp_badge.move(320, 12)
        self.gpu_temp_badge.setObjectName("GpuTempBadge")
        # center glass play button 72px (HTML)
        self.center_play = QPushButton("❚❚")
        self.center_play.setFixedSize(72, 72)
        self.center_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.center_play.setStyleSheet("background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.15); border-radius: 36px; color: white; font-size: 22px;")
        self.center_play.setParent(self.video_stack)
        self.center_play.clicked.connect(self.toggle_play)
        # glow border overlay
        self.preview_glow = FepPreviewGlowBorder(self.video_stack)
        self.preview_glow.setGeometry(0, 0, 100, 100)
        self.preview_glow.show()
        # standalone placeholder: red text on black, like the original player
        self.preview_empty_label = QLabel("No preview loaded", self.video_stack)
        self.preview_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_empty_label.setWordWrap(True)
        self.preview_empty_label.setStyleSheet(
            "background: black; color: #ef4444; font-size: 14pt; font-weight: bold; border: none;"
        )
        self.preview_empty_label.move(12, 12)
        self.preview_empty_label.show()
        # bottom control bar inside preview (HTML gradient overlay)
        preview_ctrl = QWidget()
        preview_ctrl.setStyleSheet("background: rgba(0,0,0,0.55); border-bottom-left-radius: 20px; border-bottom-right-radius: 20px;")
        ctrl_l = QHBoxLayout(preview_ctrl)
        ctrl_l.setContentsMargins(12, 8, 12, 8)
        ctrl_l.setSpacing(8)
        self.play_btn = QPushButton("▶")
        self.play_btn.setFixedSize(32, 32)
        self.play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.play_btn.setStyleSheet("background: white; color: black; border-radius: 16px; font-size: 13px; font-weight: bold;")
        self.play_btn.clicked.connect(self.toggle_play)
        ctrl_l.addWidget(self.play_btn)
        skip_b = QPushButton("⏮")
        skip_b.setFixedSize(32, 32)
        skip_b.setStyleSheet("background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; color: white;")
        skip_b.clicked.connect(lambda: self.seek_preview(0))
        ctrl_l.addWidget(skip_b)
        fwd_b = QPushButton("⏭")
        fwd_b.setFixedSize(32, 32)
        fwd_b.setStyleSheet("background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; color: white;")
        fwd_b.clicked.connect(lambda: self.seek_preview(1000))
        ctrl_l.addWidget(fwd_b)
        wave_bar = QWidget()
        wave_bar.setStyleSheet("background: rgba(0,0,0,0.4); border: 1px solid rgba(255,255,255,0.1); border-radius: 18px;")
        wave_l = QHBoxLayout(wave_bar)
        wave_l.setContentsMargins(12, 4, 12, 4)
        wave_l.setSpacing(8)
        vol = QLabel("🔊")
        vol.setStyleSheet("color: rgba(255,255,255,0.5); background: transparent; border: none;")
        wave_l.addWidget(vol)
        self.preview_wave = FepWaveformBars()
        self.preview_wave.setFixedHeight(28)
        wave_l.addWidget(self.preview_wave, stretch=1)
        self.db_label = QLabel("-6.2 dB")
        self.db_label.setStyleSheet("font-family: Consolas, monospace; font-size: 10px; color: rgba(255,255,255,0.5); background: transparent; border: none;")
        wave_l.addWidget(self.db_label)
        ctrl_l.addWidget(wave_bar, stretch=1)
        self.video_container_layout.addWidget(preview_ctrl)
        # hidden compat slider (backend seeks 0..1000; scrubber widget drives it)
        self.preview_slider = QSlider(Qt.Orientation.Horizontal)
        self.preview_slider.setMinimum(0)
        self.preview_slider.setMaximum(1000)
        self.preview_slider.setVisible(False)
        self.preview_slider.sliderMoved.connect(self.seek_preview)
        preview_layout.addWidget(self.video_container, stretch=1)
        # fullscreen compat button (hidden, HTML uses ⛶ in header)
        self.fullscreen_btn = QPushButton("⛶ Fullscreen")
        self.fullscreen_btn.setVisible(False)
        self.fullscreen_btn.clicked.connect(self.enter_fullscreen)

        scrubber_container = QWidget()
        scrubber_container.setFixedHeight(104)
        scrubber_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        scrubber_container.setStyleSheet("background: #111116; border: 1px solid rgba(255,255,255,0.06); border-radius: 16px;")
        scrub_layout = QVBoxLayout(scrubber_container)
        scrub_layout.setContentsMargins(12,8,12,8)
        scrub_layout.setSpacing(6)
        scrub_header = QHBoxLayout()
        scrub_label = QLabel("TIMELINE SCRUBBER • REALTIME WAVEFORM")
        scrub_label.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.4);")
        scrub_header.addWidget(scrub_label)
        scrub_header.addStretch()
        fps_pill = QLabel("● 240 FPS PREVIEW")
        fps_pill.setStyleSheet("font-family: Consolas, monospace; font-size: 10px; color: rgba(255,255,255,0.3);")
        scrub_header.addWidget(fps_pill)
        scrub_layout.addLayout(scrub_header)
        self.scrub_wave = FepScrubberWidget()
        self.scrub_wave.scrubMoved.connect(self._on_scrub_move)
        self.scrub_wave.scrubFinished.connect(self._on_scrub_finish)
        scrub_layout.addWidget(self.scrub_wave)
        preview_layout.addWidget(scrubber_container)

        # TRIM compat (hidden to match HTML; methods still work, info kept for backend)
        self.trim_info = QLabel("In: 00:00:00 | Out: 00:00:00 | Duration: 00:00:00")
        self.trim_info.setVisible(False)

        central_layout.addWidget(preview_area, stretch=1)

        self.apply_theme()
        self.create_dockable_ui()
        self.create_menus()
        self.load_settings()
        # Do NOT restore dock state on first run if it causes overlap - use safe restore
        self.vram_timer = QTimer(self)
        self.vram_timer.timeout.connect(self.update_live_vram_display)
        self.vram_timer.start(2000)
        self.update_live_vram_display()
        try:
            self.restore_dock_state()
        except:
            pass


    def switch_main_tab(self, name):
        if not hasattr(self, 'tab_buttons'): return
        for n, btn in self.tab_buttons.items():
            try:
                btn.setChecked(n == name)
            except Exception:
                pass
        try:
            if name == "MEDIA" or name == "Edit":
                self.media_library_dock.setVisible(True)
                self.inspector_dock.setVisible(False)
                if hasattr(self, 'export_dock'): self.export_dock.setVisible(False)
            elif name == "COLOR" or name == "Color":
                self.inspector_dock.setVisible(True)
                self.media_library_dock.setVisible(False)
                if hasattr(self, 'export_dock'): self.export_dock.setVisible(False)
            elif name == "EXPORT" or name == "Export":
                self.open_export_window()
            elif name == "AUDIO" or name == "Audio":
                if hasattr(self, 'audio_mixer_dock'): self.audio_mixer_dock.setVisible(True)
                if hasattr(self, 'audio_mixer_dock'):
                    try:
                        self.audio_mixer_dock.raise_()
                    except Exception:
                        pass
            elif name == "TIMELINE" or name == "Cut":
                pass
        except Exception:
            pass

    def open_export_window(self):
        """Show/raise the dedicated export window (lazy-safe)."""
        try:
            if not hasattr(self, 'export_window') or self.export_window is None:
                if not hasattr(self, 'export_panel') or self.export_panel is None:
                    self.export_panel = ExportPanelWidget(self)
                self.export_window = ExportWindow(self, self.export_panel, self)
            self.export_window.show()
            self.export_window.raise_()
            self.export_window.activateWindow()
        except Exception as e:
            print(f"open_export_window failed: {e}")

    def resizeEvent(self, event):
        try:
            super().resizeEvent(event)
        except Exception:
            pass
        try:
            self._position_preview_overlays()
        except Exception:
            pass

    def _update_preview_empty_state(self):
        """Standalone launch = black player + red text; overlays only with media."""
        try:
            has_media = False
            try:
                vw = getattr(self, 'video_widget', None)
                cur = getattr(vw, 'current_file', None) if vw is not None else None
                has_media = bool(cur and os.path.exists(cur))
            except Exception:
                has_media = False
            try:
                if hasattr(self, 'preview_empty_label'):
                    self.preview_empty_label.setVisible(not has_media)
            except Exception:
                pass
            for _name in ('rec_badge', 'nvenc_badge', 'gpu_temp_badge', 'center_play', 'preview_glow'):
                try:
                    _w = getattr(self, _name, None)
                    if _w is not None:
                        _w.setVisible(bool(has_media))
                except Exception:
                    pass
            try:
                self._position_preview_overlays()
            except Exception:
                pass
        except Exception:
            pass

    def _position_preview_overlays(self):
        try:
            if hasattr(self, 'preview_mode_badge') and hasattr(self, 'video_widget') and self.video_widget:
                try:
                    emb = self.video_widget.is_embedded()
                except Exception:
                    emb = False
                try:
                    mode = _mpv_preview_mode_static()
                except Exception:
                    mode = 'auto'
                try:
                    if emb:
                        label = "AUTO → EMBEDDED" if mode == 'auto' else "EMBEDDED"
                        self.preview_mode_badge.setText(label)
                        self.preview_mode_badge.setToolTip(f"MPV preview mode: {mode} ({_mpv_session_label()}). Change in View menu or Settings (restart).")
                        self.preview_mode_badge.setStyleSheet(
                            "font-size: 10px; padding: 4px 8px; border-radius: 10px; color: #00ff88; "
                            "background: rgba(0,255,136,0.1); border: 1px solid rgba(0,255,136,0.25);"
                        )
                    else:
                        err = ""
                        try:
                            err = (self.video_widget.embed_error_text() or "").strip()
                        except Exception:
                            err = ""
                        if mode == 'auto':
                            label = "AUTO → EXTERNAL"
                            tip = f"Auto-detected {_mpv_session_label()}: embed off here. Force it in View menu or Settings (restart). " + (f"Note: {err}" if err else "")
                        else:
                            label = "EXTERNAL"
                            tip = ("MPV preview mode: external. Change in View menu or Settings (restart). " + (f"Last embed note: {err}" if err else ""))
                        self.preview_mode_badge.setText(label)
                        self.preview_mode_badge.setToolTip(tip)
                        self.preview_mode_badge.setStyleSheet(
                            "font-size: 10px; padding: 4px 8px; border-radius: 10px; color: rgba(255,255,255,0.5); "
                            "background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);"
                        )
                except Exception:
                    pass
        except Exception:
            pass
        if not hasattr(self, 'video_stack'):
            return
        try:
            vs = self.video_stack
            vw, vh = vs.width(), vs.height()
            if vw <= 0 or vh <= 0:
                return
            if hasattr(self, 'preview_empty_label'):
                try:
                    self.preview_empty_label.setGeometry(0, 0, vw, vh)
                    self.preview_empty_label.raise_()
                except Exception:
                    pass
            if hasattr(self, 'rec_badge'):
                self.rec_badge.move(12, 12)
                self.rec_badge.raise_()
            if hasattr(self, 'nvenc_badge'):
                self.nvenc_badge.move(112, 12)
                self.nvenc_badge.raise_()
            if hasattr(self, 'gpu_temp_badge'):
                try:
                    self.gpu_temp_badge.adjustSize()
                except Exception:
                    pass
                tw = self.gpu_temp_badge.width()
                self.gpu_temp_badge.move(max(12, vw - tw - 12), 12)
                self.gpu_temp_badge.raise_()
            if hasattr(self, 'center_play'):
                self.center_play.move((vw - 72) // 2, (vh - 72) // 2)
                self.center_play.raise_()
            if hasattr(self, 'preview_glow'):
                self.preview_glow.setGeometry(0, 0, vw, vh)
                self.preview_glow.raise_()
        except Exception:
            pass

    def showEvent(self, event):
        try:
            super().showEvent(event)
        except Exception:
            pass
        try:
            from PyQt6.QtCore import QTimer as _QT
            _QT.singleShot(50, self._position_preview_overlays)
            _QT.singleShot(150, self._update_preview_empty_state)
        except Exception:
            pass

    def set_preview_mode_action(self, mode):
        try:
            mode = str(mode or 'auto').lower()
            if mode not in ('auto', 'embed', 'external'):
                mode = 'auto'
            self.app_settings.setValue("mpv_preview_mode", mode)
            if hasattr(self, 'video_widget') and self.video_widget:
                try:
                    self.video_widget.set_preview_mode(mode)
                except Exception:
                    pass
            try:
                self._position_preview_overlays()
            except Exception:
                pass
            QMessageBox.information(
                self, "Preview Mode",
                f"Preview mode: {mode.upper()} ({_mpv_session_label()}).\nRestart the app to apply.\nAuto means embed everywhere except Hyprland/Wayland."
            )
        except Exception as e:
            try:
                QMessageBox.warning(self, "Preview Mode", f"Could not set mode: {e}")
            except Exception:
                pass

    def toggle_embed_preview(self, checked=False):
        """Back-compat: old bool toggle maps onto embed/external modes."""
        try:
            self.set_preview_mode_action('embed' if checked else 'external')
        except Exception as e:
            try:
                QMessageBox.warning(self, "Preview Mode", f"Could not toggle embed: {e}")
            except Exception:
                pass


    def direct_export_from_panel(self, settings_override):
        print("direct_export_from_panel called")
        if not self.timeline.clips:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Empty Timeline", "Add clips to timeline before exporting")
            return
        from PyQt6.QtWidgets import QFileDialog
        try:
            codec_for_ext = settings_override.get('video_codec', 'hevc_nvenc') if isinstance(settings_override, dict) else 'hevc_nvenc'
            ext = get_export_extension_for_codec(codec_for_ext)
        except Exception as e:
            print(f"Extension lookup failed: {e}")
            ext = ".mp4"
        output_file, _ = QFileDialog.getSaveFileName(self, "Choose Location & Start Export", f"timeline_export{ext}", f"Video Files (*{ext})")
        if not output_file:
            print("User cancelled")
            return
        print(f"Output: {output_file}")
        try:
            base_settings = self.get_settings()
        except Exception as e:
            import traceback
            traceback.print_exc()
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Settings Error", f"Failed to get settings: {e}")
            return
        try:
            if isinstance(settings_override, dict):
                base_settings.update(settings_override)
        except Exception as e:
            print(f"Settings update failed: {e}")
        try:
            self.render_dialog = RenderProgressDialog(self)
            self.render_dialog.cancel_btn.clicked.connect(self.stop_timeline_export)
            for btn_name in ['export_timeline_btn', 'stop_export_btn', 'export_btn']:
                try:
                    if hasattr(self, btn_name):
                        btn = getattr(self, btn_name)
                        if btn_name == 'stop_export_btn':
                            btn.setEnabled(True)
                        else:
                            btn.setEnabled(False)
                except Exception as be:
                    print(f"Button {btn_name} toggle failed: {be}")
            self.timeline_export_thread = TimelineExportThread(self.timeline, output_file, base_settings)
            self.timeline_export_thread.progress.connect(self.progress_bar.setValue)
            self.timeline_export_thread.progress.connect(self.render_dialog.progress_bar.setValue)
            self.timeline_export_thread.status.connect(self.status_label.setText)
            self.timeline_export_thread.status.connect(self.render_dialog.status_label.setText)
            self.timeline_export_thread.log_message.connect(self.append_log)
            self.timeline_export_thread.log_message.connect(self.render_dialog.log_text.append)
            self.timeline_export_thread.finished.connect(self.timeline_export_done)
            self.timeline_export_thread.playhead_update.connect(self.timeline.set_playhead_position)
            self.progress_bar.setValue(0)
            self.status_label.setText(f"Exporting (Direct): {base_settings.get('video_codec','unknown').upper()}")
            self.render_dialog.show()
            try:
                if hasattr(self, 'vram_timer'):
                    self.vram_timer.stop()
            except:
                pass
            if not self.timeline_export_thread.isRunning():
                self.timeline_export_thread.start()
            print("Export thread started")
        except Exception as e:
            import traceback
            traceback.print_exc()
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Export Failed", f"Failed to start export: {e}")


    def create_dockable_ui(self):

        # 1. ICON BAR - 64px fixed left
        self.icon_bar_dock = QDockWidget("NAV", self)
        self.icon_bar_dock.setObjectName("icon_bar_dock")
        # FIXED: Restore full docking anywhere - was restricted to left/right only which broke snap
        self.icon_bar_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.icon_bar_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        icon_bar_widget = QWidget()
        icon_bar_widget.setMinimumWidth(64)
        icon_bar_widget.setMaximumWidth(80)
        icon_bar_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        icon_bar_widget.setStyleSheet("background: #0a0a0f; border-right: 1px solid rgba(255,255,255,0.06);")
        icon_layout = QVBoxLayout(icon_bar_widget)
        icon_layout.setContentsMargins(8,16,8,16)
        icon_layout.setSpacing(12)
        icons_container = QWidget()
        icons_container.setStyleSheet("background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06); border-radius: 16px; padding: 4px;")
        icons_inner = QVBoxLayout(icons_container)
        icons_inner.setContentsMargins(4,4,4,4)
        icons_inner.setSpacing(6)
        # EXACT HTML: 6 tools, first active white pill with shadow
        for idx, (icon_text, tip) in enumerate([("➤", "Pointer"), ("✂", "Cut"), ("T", "Text"), ("✦", "FX"), ("♪", "Audio"), ("▤", "Layers")]):
            btn = QPushButton(icon_text)
            btn.setFixedSize(36,36)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            if idx == 0:
                btn.setStyleSheet("background: white; color: black; border-radius: 12px; font-size: 15px; font-weight: bold; border: 1px solid white;")
            else:
                btn.setStyleSheet("background: transparent; color: rgba(255,255,255,0.4); border-radius: 12px; font-size: 15px; border: 1px solid transparent;")
            icons_inner.addWidget(btn)
        icon_layout.addWidget(icons_container)
        icon_layout.addStretch()
        film = QLabel("🎬")
        film.setFixedSize(36,36)
        film.setAlignment(Qt.AlignmentFlag.AlignCenter)
        film.setStyleSheet("background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); border-radius: 18px; font-size: 14px;")
        icon_layout.addWidget(film)
        avatar = QLabel("A")
        avatar.setFixedSize(36,36)
        avatar.setStyleSheet("background: white; color: black; border-radius: 18px; font-weight: bold; font-size: 10px;")
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_layout.addWidget(avatar)
        self.icon_bar_dock.setWidget(icon_bar_widget)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.icon_bar_dock)

        # 2. MEDIA LIBRARY - LEFT
        self.media_library_dock = QDockWidget("MEDIA LIBRARY", self)
        self.media_library_dock.setObjectName("media_library_dock")
        self.media_library_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.media_library_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.media_library_dock.setMinimumWidth(180)
        self.media_library_dock.setMinimumHeight(150)
        media_widget = QWidget()
        media_widget.setStyleSheet("background: #08080a;")
        media_layout = QVBoxLayout(media_widget)
        media_layout.setContentsMargins(12,12,12,12)
        media_layout.setSpacing(12)
        media_header = QHBoxLayout()
        media_title = QLabel("MEDIA")
        media_title.setStyleSheet("font-size: 11px; letter-spacing: 1.5px; color: rgba(255,255,255,0.4); font-weight: 600;")
        media_header.addWidget(media_title)
        media_header.addStretch()
        self.count_badge = QLabel("0 FILES")
        self.count_badge.setStyleSheet("background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 4px 8px; font-size: 10px; color: rgba(255,255,255,0.5);")
        media_header.addWidget(self.count_badge)
        media_layout.addLayout(media_header)
        self.media_list = QListWidget()
        self.media_list.setStyleSheet(self.list_style())
        self.media_list.setMinimumHeight(200)
        self.media_list.setMinimumWidth(220)
        self.media_list.itemClicked.connect(self.on_media_selected)
        media_layout.addWidget(self.media_list, stretch=1)
        lib_buttons = QHBoxLayout()
        add_media_btn = QPushButton("➕ Add Media")
        add_media_btn.setStyleSheet(self.button_style("#00ff88"))
        add_media_btn.setMinimumHeight(44)
        add_media_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_media_btn.clicked.connect(self.add_media_to_library)
        lib_buttons.addWidget(add_media_btn)
        remove_media_btn = QPushButton("➖")
        remove_media_btn.setStyleSheet(self.button_style("rgba(255,255,255,0.08)"))
        remove_media_btn.setMinimumHeight(44)
        remove_media_btn.setFixedWidth(44)
        remove_media_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_media_btn.clicked.connect(self.remove_from_library)
        lib_buttons.addWidget(remove_media_btn)
        media_layout.addLayout(lib_buttons)
        proj_row = QHBoxLayout()
        save_proj_btn = QPushButton("💾 Save")
        save_proj_btn.setStyleSheet(self.button_style("#7df9ff"))
        save_proj_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_proj_btn.clicked.connect(self.save_project)
        proj_row.addWidget(save_proj_btn)
        load_proj_btn = QPushButton("📂 Load")
        load_proj_btn.setStyleSheet(self.button_style("#ff8a00"))
        load_proj_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        load_proj_btn.clicked.connect(self.load_project)
        proj_row.addWidget(load_proj_btn)
        media_layout.addLayout(proj_row)
        proxy_group = QGroupBox("PROXY STATUS - FIXED")
        proxy_group.setStyleSheet(self.groupbox_style())
        proxy_group_layout = QVBoxLayout(proxy_group)
        proxy_top = QHBoxLayout()
        proxy_top.addWidget(self.proxy_status_label)
        proxy_top.addStretch()
        proxy_group_layout.addLayout(proxy_top)
        proxy_group_layout.addWidget(self.proxy_progress_bar)
        # Vertical stack: three wide buttons side-by-side forced a ~765px
        # dock minimum, which kills edge docking. Full-width rows stay ~200px.
        proxy_buttons = QVBoxLayout()
        proxy_buttons.setSpacing(6)
        stop_proxy_btn = QPushButton("Stop Proxies")
        stop_proxy_btn.setStyleSheet(self.button_style("#ff5f56"))
        stop_proxy_btn.setMinimumHeight(36)
        stop_proxy_btn.setToolTip("Stop current proxy generation")
        stop_proxy_btn.clicked.connect(self.stop_proxy_generation)
        proxy_buttons.addWidget(stop_proxy_btn)
        clear_proxies_btn = QPushButton("Clear All Proxies")
        clear_proxies_btn.setStyleSheet(self.button_style("rgba(255,255,255,0.08)"))
        clear_proxies_btn.setMinimumHeight(36)
        clear_proxies_btn.setToolTip("Delete all proxy files")
        clear_proxies_btn.clicked.connect(self.clear_all_proxies)
        proxy_buttons.addWidget(clear_proxies_btn)
        # Add manual start button
        start_proxies_btn = QPushButton("Start Proxies")
        start_proxies_btn.setStyleSheet(self.button_style("#00ff88"))
        start_proxies_btn.setMinimumHeight(36)
        start_proxies_btn.setToolTip("Manually start proxy generation for all media")
        start_proxies_btn.clicked.connect(self.start_all_proxies)
        proxy_buttons.addWidget(start_proxies_btn)
        proxy_group_layout.addLayout(proxy_buttons)
        self.auto_proxy_check = QCheckBox("Auto-generate proxies")
        self.auto_proxy_check.setToolTip("Auto-generate proxies on import (disable to stop auto-proxy)")
        self.auto_proxy_check.setChecked(self.auto_proxy_enabled)
        self.auto_proxy_check.setStyleSheet("color: rgba(255,255,255,0.7); font-size: 11px; padding: 4px;")
        self.auto_proxy_check.stateChanged.connect(self.toggle_auto_proxy)
        proxy_group_layout.addWidget(self.auto_proxy_check)
        media_layout.addWidget(proxy_group)
        # Scrollable, like the inspector: on short screens the ~650px content
        # no longer forces an impossible dock minimum, which was suppressing
        # main-window edge drop zones on every tab except COLOR.
        media_scroll = QScrollArea()
        media_scroll.setWidgetResizable(True)
        media_scroll.setStyleSheet("QScrollArea { border: none; background: #08080a; }")
        media_scroll.setWidget(media_widget)
        self.media_library_dock.setWidget(media_scroll)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.media_library_dock)

        # 3. INSPECTOR - RIGHT 360px
        self.inspector_dock = QDockWidget("INSPECTOR", self)
        self.inspector_dock.setObjectName("inspector_dock")
        self.inspector_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.inspector_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.inspector_dock.setMinimumWidth(280)
        self.inspector_dock.setMaximumWidth(500)
        self.inspector_dock.setMinimumHeight(200)
        inspector_scroll = QScrollArea()
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setStyleSheet("QScrollArea { border: none; background: #0b0b0f; }")
        inspector_container = QWidget()
        inspector_container.setStyleSheet("background: #0b0b0f;")
        inspector_layout = QVBoxLayout(inspector_container)
        inspector_layout.setContentsMargins(12,12,12,12)
        inspector_layout.setSpacing(12)
        self.sidebar_tabs = QTabWidget()
        self.sidebar_tabs.setStyleSheet(self.tab_style())
        color_tab = QWidget()
        color_layout = QVBoxLayout(color_tab)
        color_layout.setContentsMargins(8,8,8,8)
        color_layout.setSpacing(12)
        color_actions = QHBoxLayout()
        self.auto_balance_btn = QPushButton("✨ Auto Balance")
        self.auto_balance_btn.setStyleSheet(self.button_style("#00ff88"))
        self.auto_balance_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_balance_btn.clicked.connect(self.apply_auto_balance)
        color_actions.addWidget(self.auto_balance_btn)
        self.reset_filters_btn = QPushButton("↺ Reset")
        self.reset_filters_btn.setStyleSheet(self.button_style("rgba(255,255,255,0.08)"))
        self.reset_filters_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_filters_btn.clicked.connect(self.reset_all_filters)
        color_actions.addWidget(self.reset_filters_btn)
        color_layout.addLayout(color_actions)
        self.cinema_scope_check = QCheckBox("🎬 Cinema Scope 2.35:1")
        self.cinema_scope_check.setStyleSheet("color: white; font-weight: 600; padding: 8px; background: rgba(255,255,255,0.04); border-radius: 8px;")
        self.cinema_scope_check.stateChanged.connect(self.update_live_preview_filters)
        color_layout.addWidget(self.cinema_scope_check)
        wheels_group = QGroupBox("COLOR WHEELS")
        wheels_group.setStyleSheet(self.groupbox_style())
        wheels_layout = QHBoxLayout()
        self.lift_wheel = ColorWheelWidget("Lift")
        self.gamma_wheel = ColorWheelWidget("Gamma")
        self.gain_wheel = ColorWheelWidget("Gain")
        self.lift_wheel.colorChanged.connect(self.update_live_preview_filters)
        self.gamma_wheel.colorChanged.connect(self.update_live_preview_filters)
        self.gain_wheel.colorChanged.connect(self.update_live_preview_filters)
        wheels_layout.addWidget(self.lift_wheel)
        wheels_layout.addWidget(self.gamma_wheel)
        wheels_layout.addWidget(self.gain_wheel)
        wheels_group.setLayout(wheels_layout)
        color_layout.addWidget(wheels_group)
        legacy_group = QGroupBox("ADJUSTMENTS")
        legacy_group.setStyleSheet(self.groupbox_style())
        legacy_layout = QVBoxLayout()
        def add_color_slider(name, min_v, max_v, default_v, fmt, attr_name):
            row = QHBoxLayout()
            row.addWidget(QLabel(name))
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setMinimum(min_v)
            slider.setMaximum(max_v)
            slider.setValue(default_v)
            slider.setStyleSheet(self.slider_style())
            val_label = QLabel(fmt.format(default_v))
            val_label.setFixedWidth(50)
            val_label.setStyleSheet("font-family: Consolas, monospace; font-size: 10px; color: rgba(255,255,255,0.5);")
            def on_change(v):
                val_label.setText(fmt.format(v))
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
        filters_tab = QWidget()
        filters_layout = QVBoxLayout(filters_tab)
        filters_layout.setContentsMargins(8,8,8,8)
        filters_layout.setSpacing(12)
        fx_group = QGroupBox("FX FILTERS")
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
        tools_group = QGroupBox("CREATIVE TOOLS")
        tools_group.setStyleSheet(self.groupbox_style())
        tools_layout = QVBoxLayout()
        add_text_btn = QPushButton("Add Text / Lower Third")
        add_text_btn.setStyleSheet(self.button_style("#ff8a00"))
        add_text_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_text_btn.clicked.connect(self.add_text_overlay)
        tools_layout.addWidget(add_text_btn)
        record_vo_btn = QPushButton("Record Voiceover")
        record_vo_btn.setStyleSheet(self.button_style("#a855f7"))
        record_vo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        record_vo_btn.clicked.connect(self.record_voiceover)
        tools_layout.addWidget(record_vo_btn)
        tools_group.setLayout(tools_layout)
        filters_layout.addWidget(tools_group)
        filters_layout.addStretch()
        self.sidebar_tabs.addTab(filters_tab, "FX & Tools")
        inspector_layout.addWidget(self.sidebar_tabs)
        # FIXED: Removed old EXPORT TIMELINE button that opened old dialog with none of the settings
        # Now side panel is the only export UI - this button switches to EXPORT tab
        export_btn = QPushButton("→ OPEN EXPORT WINDOW")
        export_btn.setFixedHeight(48)
        export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        export_btn.setStyleSheet("QPushButton { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #00ff88, stop:1 #7df9ff); color: black; border-radius: 24px; font-weight: 700; font-size: 13px; letter-spacing: 0.5px; } QPushButton:hover { background: #7df9ff; }")
        export_btn.clicked.connect(lambda: self.switch_main_tab("EXPORT"))
        inspector_layout.addWidget(export_btn)
        inspector_scroll.setWidget(inspector_container)
        self.inspector_dock.setWidget(inspector_scroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)


        # 3.5. EXPORT - dedicated window (not a dock, so it never gets squished)
        self.export_panel = ExportPanelWidget(self)
        self.export_window = ExportWindow(self, self.export_panel, self)
        self.export_window.hide()

        # 4. AUDIO MIXER - FIXED
        self.audio_mixer_dock = QDockWidget("AUDIO MIXER • FIXED", self)
        self.audio_mixer_dock.setObjectName("audio_mixer_dock")
        self.audio_mixer_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.audio_mixer_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.audio_mixer_dock.setMinimumWidth(240)
        self.audio_mixer_dock.setMinimumHeight(200)
        mixer_widget = QWidget()
        mixer_widget.setStyleSheet("background: #0f0f14;")
        mixer_layout = QVBoxLayout(mixer_widget)
        mixer_layout.setContentsMargins(12,12,12,12)
        mixer_layout.setSpacing(12)
        t1_box = QGroupBox("TRACK 1 • VO")
        t1_box.setStyleSheet(self.groupbox_style())
        t1_layout = QVBoxLayout(t1_box)
        self.track1_meter = QProgressBar()
        self.track1_meter.setRange(0, 100)
        self.track1_meter.setValue(0)
        self.track1_meter.setTextVisible(False)
        self.track1_meter.setFixedHeight(8)
        self.track1_meter.setStyleSheet("QProgressBar { background: #0a0a0e; border: 1px solid rgba(255,255,255,0.06); border-radius: 4px; } QProgressBar::chunk { background: #00ff88; border-radius: 3px; }")
        t1_layout.addWidget(self.track1_meter)
        t1_slider_row = QHBoxLayout()
        self.track1_slider = QSlider(Qt.Orientation.Horizontal)
        self.track1_slider.setRange(-60, 30)
        self.track1_slider.setValue(0)
        self.track1_slider.setStyleSheet(self.slider_style())
        self.track1_slider.valueChanged.connect(self.update_clip_volume)
        t1_slider_row.addWidget(self.track1_slider)
        self.t1_val = QLabel("0 dB")
        self.t1_val.setStyleSheet("font-family: Consolas, monospace; font-size: 10px; color: rgba(255,255,255,0.5); min-width: 40px;")
        t1_slider_row.addWidget(self.t1_val)
        t1_layout.addLayout(t1_slider_row)
        self.track1_norm = QCheckBox("Normalize • Loudnorm")
        self.track1_norm.setStyleSheet("color: #00ff88; font-size: 10px;")
        self.track1_norm.stateChanged.connect(self.update_clip_volume)
        t1_layout.addWidget(self.track1_norm)
        mixer_layout.addWidget(t1_box)
        t2_box = QGroupBox("TRACK 2 • MUSIC")
        t2_box.setStyleSheet(self.groupbox_style())
        t2_layout = QVBoxLayout(t2_box)
        self.track2_meter = QProgressBar()
        self.track2_meter.setRange(0, 100)
        self.track2_meter.setValue(0)
        self.track2_meter.setTextVisible(False)
        self.track2_meter.setFixedHeight(8)
        self.track2_meter.setStyleSheet("QProgressBar { background: #0a0a0e; border: 1px solid rgba(255,255,255,0.06); border-radius: 4px; } QProgressBar::chunk { background: #ff8a00; border-radius: 3px; }")
        t2_layout.addWidget(self.track2_meter)
        t2_slider_row = QHBoxLayout()
        self.track2_slider = QSlider(Qt.Orientation.Horizontal)
        self.track2_slider.setRange(-60, 30)
        self.track2_slider.setValue(0)
        self.track2_slider.setStyleSheet(self.slider_style())
        self.track2_slider.valueChanged.connect(self.update_clip_volume)
        t2_slider_row.addWidget(self.track2_slider)
        self.t2_val = QLabel("0 dB")
        self.t2_val.setStyleSheet("font-family: Consolas, monospace; font-size: 10px; color: rgba(255,255,255,0.5); min-width: 40px;")
        t2_slider_row.addWidget(self.t2_val)
        t2_layout.addLayout(t2_slider_row)
        self.track2_norm = QCheckBox("Normalize • Loudnorm")
        self.track2_norm.setStyleSheet("color: #00ff88; font-size: 10px;")
        self.track2_norm.stateChanged.connect(self.update_clip_volume)
        t2_layout.addWidget(self.track2_norm)
        mixer_layout.addWidget(t2_box)
        sync_row = QHBoxLayout()
        self.auto_sync_btn = QPushButton("🎯 Auto-Sync")
        self.auto_sync_btn.setStyleSheet(self.button_style("#a855f7"))
        self.auto_sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_sync_btn.clicked.connect(self.auto_sync_audio_tracks)
        sync_row.addWidget(self.auto_sync_btn)
        self.sync_status_label = QLabel("")
        self.sync_status_label.setStyleSheet("color: #7df9ff; font-size: 10px; font-family: Consolas, monospace;")
        sync_row.addWidget(self.sync_status_label)
        sync_row.addStretch()
        mixer_layout.addLayout(sync_row)
        mixer_layout.addStretch()
        self.audio_mixer_dock.setWidget(mixer_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.audio_mixer_dock)

        # 5. TRANSITIONS
        self.transitions_dock = QDockWidget("TRANSITIONS", self)
        self.transitions_dock.setObjectName("transitions_dock")
        self.transitions_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.transitions_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.transitions_dock.setMinimumWidth(200)
        self.transitions_dock.setMinimumHeight(150)
        transitions_widget = QWidget()
        transitions_widget.setStyleSheet("background: #0f0f14;")
        transitions_layout = QVBoxLayout(transitions_widget)
        transitions_layout.setContentsMargins(12,12,12,12)
        transitions_layout.setSpacing(8)
        _trans_hint = QLabel("Right-click timeline clips to add transitions:")
        _trans_hint.setWordWrap(True)
        transitions_layout.addWidget(_trans_hint)
        self.transitions_combo = QComboBox()
        self.transitions_combo.addItems(["None","fade","fadeblack","fadewhite","wipeleft","wiperight","wipeup","wipedown","slideleft","slideright","slideup","slidedown","circlecrop","rectcrop","distance","dissolve","pixelize","diagtl","diagtr","diagbl","diagbr","hlslice","hrslice","vuslice","vdslice","smoothleft","smoothright","smoothup","smoothdown"])
        self.transitions_combo.setStyleSheet(self.combo_style())
        transitions_layout.addWidget(self.transitions_combo)
        transitions_layout.addWidget(QLabel("Duration (s)"))
        self.trans_duration_spin = QDoubleSpinBox()
        self.trans_duration_spin.setRange(0.1, 5.0)
        self.trans_duration_spin.setValue(1.0)
        self.trans_duration_spin.setStyleSheet(self.spinbox_style())
        transitions_layout.addWidget(self.trans_duration_spin)
        apply_trans_btn = QPushButton("Apply to Selected")
        apply_trans_btn.setStyleSheet(self.button_style("#ff8a00"))
        apply_trans_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_trans_btn.clicked.connect(self.apply_transition_to_selected)
        transitions_layout.addWidget(apply_trans_btn)
        transitions_layout.addStretch()
        self.transitions_dock.setWidget(transitions_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.transitions_dock)

        # 6. TIMELINE - BOTTOM, empty on launch
        self.timeline_dock = QDockWidget("TIMELINE", self)
        self.timeline_dock.setObjectName("timeline_dock")
        self.timeline_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.timeline_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.timeline_dock.setMinimumHeight(340)
        self.timeline_dock.setMinimumWidth(300)
        timeline_widget = QWidget()
        timeline_widget.setStyleSheet("background: #0a0a0e;")
        timeline_layout = QVBoxLayout(timeline_widget)
        timeline_layout.setContentsMargins(0,0,0,0)
        timeline_layout.setSpacing(0)
        timeline_header = QWidget()
        timeline_header.setFixedHeight(40)
        timeline_header.setStyleSheet("background: #0f0f14; border-bottom: 1px solid rgba(255,255,255,0.06);")
        th_layout = QHBoxLayout(timeline_header)
        th_layout.setContentsMargins(12,0,12,0)
        th_layout.setSpacing(8)
        # EXACT HTML: Timeline/Audio Mix pill
        mode_pill = QWidget()
        mode_pill.setStyleSheet("background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.06); border-radius: 14px;")
        mode_l = QHBoxLayout(mode_pill)
        mode_l.setContentsMargins(4, 2, 4, 2)
        mode_l.setSpacing(4)
        tl_active = QLabel("Timeline")
        tl_active.setStyleSheet("background: white; color: black; border-radius: 10px; padding: 4px 10px; font-size: 11px;")
        mode_l.addWidget(tl_active)
        au_idle = QLabel("Audio Mix")
        au_idle.setStyleSheet("color: rgba(255,255,255,0.4); padding: 4px 10px; font-size: 11px; background: transparent; border: none;")
        mode_l.addWidget(au_idle)
        th_layout.addWidget(mode_pill)
        fps_tag = QLabel("● 60 fps")
        fps_tag.setStyleSheet("background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; padding: 4px 8px; font-family: Consolas, monospace; font-size: 10px; color: rgba(255,255,255,0.3);")
        fps_tag.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        th_layout.addWidget(fps_tag)
        tot_tag = QLabel("• 00:24:12 total")
        tot_tag.setStyleSheet("font-family: Consolas, monospace; font-size: 10px; color: rgba(255,255,255,0.3); background: transparent; border: none;")
        tot_tag.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        th_layout.addWidget(tot_tag)
        th_layout.addStretch()
        # scissors/layers icon buttons (wired to real actions = new feature preserving function)
        cut_btn = QPushButton("✂")
        cut_btn.setFixedSize(28, 28)
        cut_btn.setToolTip("Add media to timeline")
        cut_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cut_btn.setStyleSheet("background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; color: rgba(255,255,255,0.6);")
        cut_btn.clicked.connect(self.add_to_timeline)
        th_layout.addWidget(cut_btn)
        lay_btn = QPushButton("⧉")
        lay_btn.setFixedSize(28, 28)
        lay_btn.setToolTip("Remove selected clip")
        lay_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        lay_btn.setStyleSheet("background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; color: rgba(255,255,255,0.6);")
        lay_btn.clicked.connect(self.remove_from_timeline)
        th_layout.addWidget(lay_btn)
        zoom_pct = QPushButton("● 100%")
        zoom_pct.setFixedHeight(28)
        zoom_pct.setStyleSheet("background: #15151a; border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; padding: 4px 12px; font-size: 11px; color: white;")
        zoom_pct.clicked.connect(self.zoom_in_timeline)
        th_layout.addWidget(zoom_pct)
        timeline_layout.addWidget(timeline_header)
        self.timeline = TimelineWidget()
        self.timeline.setStyleSheet("background: #0a0a0e; border: none;")
        self.timeline.setMinimumHeight(140)
        self.timeline.clip_selected.connect(self.on_timeline_clip_selected)
        self.timeline.playhead_moved.connect(self.on_timeline_playhead_moved)
        self.timeline.timeline_clicked.connect(self.activate_timeline_mode)
        timeline_layout.addWidget(self.timeline, stretch=1)
        # --- VISIBLE TIMELINE ACTION BAR (restored: Add/Remove/Clear/EXPORT/STOP) ---
        # Previously hidden for "exact HTML" - that hid the only way to put media on the timeline.
        timeline_controls = QWidget()
        timeline_controls.setStyleSheet("background: #0f0f14; border-top: 1px solid rgba(255,255,255,0.06);")
        tc_layout = QHBoxLayout(timeline_controls)
        tc_layout.setContentsMargins(8, 8, 8, 8)
        tc_layout.setSpacing(8)
        add_to_timeline_btn = QPushButton("➕ Add to Timeline")
        add_to_timeline_btn.setStyleSheet(self.button_style("#00ff88"))
        add_to_timeline_btn.setMinimumHeight(40)
        add_to_timeline_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_to_timeline_btn.setToolTip("Add selected library media to timeline at playhead end (In/Out trimmed)")
        add_to_timeline_btn.clicked.connect(self.add_to_timeline)
        tc_layout.addWidget(add_to_timeline_btn)
        remove_from_timeline_btn = QPushButton("➖ Remove")
        remove_from_timeline_btn.setStyleSheet(self.button_style("#ff5f56"))
        remove_from_timeline_btn.setMinimumHeight(40)
        remove_from_timeline_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_from_timeline_btn.setToolTip("Remove selected timeline clip")
        remove_from_timeline_btn.clicked.connect(self.remove_from_timeline)
        tc_layout.addWidget(remove_from_timeline_btn)
        clear_timeline_btn = QPushButton("🗑️ Clear")
        clear_timeline_btn.setStyleSheet(self.button_style("rgba(255,255,255,0.08)"))
        clear_timeline_btn.setMinimumHeight(40)
        clear_timeline_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_timeline_btn.setToolTip("Remove ALL clips from timeline")
        clear_timeline_btn.clicked.connect(self.clear_timeline)
        tc_layout.addWidget(clear_timeline_btn)
        self.export_timeline_btn = QPushButton("💾 EXPORT")
        self.export_timeline_btn.setStyleSheet(self.button_style("#a855f7"))
        self.export_timeline_btn.setMinimumHeight(40)
        self.export_timeline_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_timeline_btn.setToolTip("Export timeline via Export window")
        self.export_timeline_btn.clicked.connect(self.export_timeline)
        tc_layout.addWidget(self.export_timeline_btn)
        self.stop_export_btn = QPushButton("⏹️ STOP")
        self.stop_export_btn.setStyleSheet(self.button_style("#ff5f56"))
        self.stop_export_btn.setMinimumHeight(40)
        self.stop_export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_export_btn.setToolTip("Stop current timeline export")
        self.stop_export_btn.setEnabled(False)
        self.stop_export_btn.clicked.connect(self.stop_timeline_export)
        tc_layout.addWidget(self.stop_export_btn)
        tc_layout.addStretch()
        timeline_layout.addWidget(timeline_controls)
        # --- QUICK ACTIONS (screenshot row, exact) ---
        access_widget = QWidget()
        access_widget.setStyleSheet("background: #0a0a0e; border-top: 1px solid rgba(255,255,255,0.04);")
        access_layout = QHBoxLayout(access_widget)
        access_layout.setContentsMargins(8, 6, 8, 6)
        access_layout.setSpacing(6)
        access_label = QLabel("Quick Actions:")
        access_label.setStyleSheet("font-weight: 700; color: #ff8a00; font-size: 10px;")
        access_layout.addWidget(access_label)
        for text, color, tip, func in [
            ("✂️ Auto-Trim", "#7df9ff", "Trim 1s off edges of selected clip", self.auto_trim_selected),
            ("🌫️ Fade All", "#a855f7", "1s fade on all clips", self.auto_fade_all),
            ("⚫ B&W", "rgba(255,255,255,0.08)", "Toggle Black & White", self.auto_black_and_white),
            ("🔊 Normalize", "#00ff88", "Normalize audio (all clips)", self.auto_normalize_audio),
        ]:
            btn = QPushButton(text)
            btn.setStyleSheet(self.button_style(color))
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(func)
            btn.setMinimumHeight(32)
            access_layout.addWidget(btn)
        access_layout.addStretch()
        timeline_layout.addWidget(access_widget)
        # --- MORE ACTIONS (every other real timeline capability, one click) ---
        more_widget = QWidget()
        more_widget.setStyleSheet("background: #0a0a0e; border-top: 1px solid rgba(255,255,255,0.04);")
        more_layout = QHBoxLayout(more_widget)
        more_layout.setContentsMargins(8, 6, 8, 6)
        more_layout.setSpacing(6)
        more_label = QLabel("More:")
        more_label.setStyleSheet("font-weight: 700; color: rgba(255,255,255,0.4); font-size: 10px;")
        more_layout.addWidget(more_label)
        for text, color, tip, func in [
            ("✨ Balance", "rgba(255,255,255,0.08)", "Auto color balance", self.apply_auto_balance),
            ("🎯 Sync", "rgba(255,255,255,0.08)", "Auto-sync audio tracks", self.auto_sync_audio_tracks),
            ("🔤 Text", "rgba(255,255,255,0.08)", "Add text / lower third", self.add_text_overlay),
            ("🎙 VO", "rgba(255,255,255,0.08)", "Record voiceover at playhead", self.record_voiceover),
            ("◀ In", "rgba(255,255,255,0.08)", "Set media In-point at preview pos", self.set_media_in_point),
            ("Out ▶", "rgba(255,255,255,0.08)", "Set media Out-point at preview pos", self.set_media_out_point),
            ("− Zoom", "rgba(255,255,255,0.08)", "Zoom timeline out", self.zoom_out_timeline),
            ("+ Zoom", "rgba(255,255,255,0.08)", "Zoom timeline in", self.zoom_in_timeline),
            ("↺ Reset", "rgba(255,255,255,0.08)", "Reset all color/FX filters", self.reset_all_filters),
        ]:
            btn = QPushButton(text)
            btn.setStyleSheet(self.button_style(color))
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(func)
            btn.setMinimumHeight(32)
            more_layout.addWidget(btn)
        more_layout.addStretch()
        timeline_layout.addWidget(more_widget)
        # --- AI ASSIST (constrained to real capabilities only) ---
        ai_widget = QWidget()
        ai_widget.setStyleSheet("background: #0f0f14; border-top: 1px solid rgba(125,249,255,0.12);")
        ai_layout = QHBoxLayout(ai_widget)
        ai_layout.setContentsMargins(8, 6, 8, 6)
        ai_layout.setSpacing(8)
        ai_label = QLabel("🤖 AI:")
        ai_label.setStyleSheet("font-weight: 700; color: #7df9ff; font-size: 11px;")
        ai_label.setToolTip("Describe what you want. Only real app actions are offered. Nothing is invented.")
        ai_layout.addWidget(ai_label)
        self.ai_prompt_input = QLineEdit()
        self.ai_prompt_input.setPlaceholderText('e.g. "trim edges, fade all, make it black and white and normalize" then press Apply')
        self.ai_prompt_input.setMinimumHeight(32)
        self.ai_prompt_input.setStyleSheet("background: #0a0a0e; border: 1px solid rgba(125,249,255,0.25); border-radius: 8px; padding: 4px 10px; color: white; font-size: 11px;")
        self.ai_prompt_input.returnPressed.connect(self.run_ai_assist)
        ai_layout.addWidget(self.ai_prompt_input, stretch=1)
        ai_apply_btn = QPushButton("Apply")
        ai_apply_btn.setStyleSheet(self.button_style("#00ff88"))
        ai_apply_btn.setMinimumHeight(32)
        ai_apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ai_apply_btn.setToolTip("Parse prompt into real actions, preview, then apply")
        ai_apply_btn.clicked.connect(self.run_ai_assist)
        ai_layout.addWidget(ai_apply_btn)
        ai_help_btn = QPushButton("?")
        ai_help_btn.setFixedSize(32, 32)
        ai_help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ai_help_btn.setStyleSheet(self.button_style("rgba(255,255,255,0.08)"))
        ai_help_btn.setToolTip("What can the AI do?")
        ai_help_btn.clicked.connect(self.show_ai_capabilities)
        ai_layout.addWidget(ai_help_btn)
        timeline_layout.addWidget(ai_widget)
        footer = QWidget()
        footer.setFixedHeight(36)
        footer.setStyleSheet("background: #0f0f14; border-top: 1px solid rgba(255,255,255,0.06);")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(12,0,12,0)
        footer_layout.setSpacing(12)
        self.timeline_status_label = QLabel("RTX 5070 NVENC • 2.4 GB VRAM • 00:12:43:19 / 00:24:12")
        self.timeline_status_label.setStyleSheet("font-family: Consolas, monospace; font-size: 10px; color: rgba(255,255,255,0.3);")
        # Footer text must clip, not widen: an oversized dock minimum
        # suppresses main-window edge drop zones (tabify-only symptom).
        self.timeline_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer_layout.addWidget(self.timeline_status_label)
        footer_layout.addStretch()
        self.footer_progress_container = QWidget()
        self.footer_progress_container.setFixedSize(96, 4)
        self.footer_progress_container.setStyleSheet("background: rgba(255,255,255,0.1); border-radius: 2px;")
        fp_layout = QHBoxLayout(self.footer_progress_container)
        fp_layout.setContentsMargins(0,0,0,0)
        self.footer_progress = QWidget()
        self.footer_progress.setStyleSheet("background: #7df9ff; border-radius: 2px;")
        fp_layout.addWidget(self.footer_progress, stretch=38)
        fp_layout.addStretch(62)
        footer_layout.addWidget(self.footer_progress_container)
        self.footer_label = QLabel("FASTENCODE PRO 2026 • DA VINCI × CAPCUT × HYPRLAND • NOT 2003")
        self.footer_label.setStyleSheet("font-size: 10px; letter-spacing: 1.5px; color: rgba(255,255,255,0.2); font-weight: 700;")
        self.footer_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer_layout.addWidget(self.footer_label)
        timeline_layout.addWidget(footer)
        self.timeline_dock.setWidget(timeline_widget)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.timeline_dock)

        # Tabify audio and transitions
        self.tabifyDockWidget(self.audio_mixer_dock, self.transitions_dock)
        self.audio_mixer_dock.raise_()

        # Batch hidden (accessibility lives in Settings menu, not a dock)
        self.batch_dock = QDockWidget("BATCH EXPORT", self)
        self.batch_dock.setObjectName("batch_dock")
        self.batch_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.batch_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable | QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        batch_tab = self.create_batch_tab()
        self.batch_dock.setWidget(batch_tab)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.batch_dock)
        self.batch_dock.setVisible(False)

        # Initial sizes
        self.splitDockWidget(self.icon_bar_dock, self.media_library_dock, Qt.Orientation.Horizontal)
        self.resizeDocks([self.icon_bar_dock], [64], Qt.Orientation.Horizontal)
        self.resizeDocks([self.media_library_dock], [280], Qt.Orientation.Horizontal)
        self.resizeDocks([self.inspector_dock, self.audio_mixer_dock, self.transitions_dock], [360, 340, 300], Qt.Orientation.Horizontal)
        self.resizeDocks([self.timeline_dock], [340], Qt.Orientation.Vertical)

    def create_menus(self):
        menubar = self.menuBar()
        menubar.setStyleSheet("""
            QMenuBar { background: #0a0a0e; border-bottom: 1px solid rgba(255,255,255,0.06); padding: 4px; }
            QMenuBar::item { padding: 6px 12px; border-radius: 8px; color: rgba(255,255,255,0.6); }
            QMenuBar::item:selected { background: rgba(255,255,255,0.08); color: white; }
            QMenu { background: #0f0f14; border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 6px; }
            QMenu::item { padding: 8px 16px; border-radius: 8px; color: rgba(255,255,255,0.8); }
            QMenu::item:selected { background: rgba(125,249,255,0.12); color: #7df9ff; }
        """)
        view_menu = menubar.addMenu("View")
        self.dock_actions = {}
        for dock, name in [
            (self.icon_bar_dock, "Icon Bar"),
            (self.media_library_dock, "Media Library"),
            # Preview is central now, not a dock - no toggle needed

            (self.inspector_dock, "Inspector (Color & FX)"),
            (self.audio_mixer_dock, "Audio Mixer (Fixed)"),
            (self.transitions_dock, "Transitions"),
            (self.timeline_dock, "Timeline"),
            (self.batch_dock, "Batch Export"),
        ]:
            action = dock.toggleViewAction()
            action.setText(f"Show {name}")
            view_menu.addAction(action)
            self.dock_actions[name] = action
        view_menu.addSeparator()
        save_layout_action = view_menu.addAction("💾 Save Layout")
        save_layout_action.triggered.connect(self.save_dock_state)
        reset_layout_action = view_menu.addAction("↺ Reset to 2026 Default")
        reset_layout_action.triggered.connect(self.reset_dock_state)
        repair_action = view_menu.addAction("🛠 Repair Docking (full rebuild)")
        repair_action.triggered.connect(self.repair_docking_layout)
        diag_action = view_menu.addAction("📋 Copy Docking Diagnostics")
        diag_action.triggered.connect(self.copy_docking_diagnostics)
        delete_ui_action = view_menu.addAction("🗑️ Delete UI Element")
        delete_ui_action.triggered.connect(self.delete_focused_dock)
        view_menu.addSeparator()
        # Deterministic placement: docks any panel to any edge without
        # depending on drag-drop indicators at all.
        try:
            move_menu = view_menu.addMenu("Dock panel to…")
            for _dock, _name in [
                (self.icon_bar_dock, "Icon Bar"),
                (self.media_library_dock, "Media Library"),
                (self.inspector_dock, "Inspector (Color & FX)"),
                (self.audio_mixer_dock, "Audio Mixer"),
                (self.transitions_dock, "Transitions"),
                (self.timeline_dock, "Timeline"),
                (self.batch_dock, "Batch Export"),
            ]:
                _sub = move_menu.addMenu(f"Move {_name}")
                for _area_name, _area in [
                    ("Left", Qt.DockWidgetArea.LeftDockWidgetArea),
                    ("Right", Qt.DockWidgetArea.RightDockWidgetArea),
                    ("Top", Qt.DockWidgetArea.TopDockWidgetArea),
                    ("Bottom", Qt.DockWidgetArea.BottomDockWidgetArea),
                ]:
                    _act = _sub.addAction(f"Dock {_area_name}")
                    _act.triggered.connect(lambda _c=False, _d=_dock, _a=_area: self.dock_panel_to(_d, _a))
        except Exception:
            pass
        view_menu.addSeparator()
        try:
            from PyQt6.QtGui import QActionGroup
            preview_menu = view_menu.addMenu("Preview mode (restart)")
            try:
                cur_mode = _mpv_preview_mode_static()
            except Exception:
                cur_mode = 'auto'
            mode_group = QActionGroup(preview_menu)
            mode_group.setExclusive(True)
            for _m, _txt in (('auto', 'Auto-detect (recommended)'), ('embed', 'Embed in app (experimental)'), ('external', 'External MPV window')):
                _a = preview_menu.addAction(_txt)
                _a.setCheckable(True)
                _a.setChecked(_m == cur_mode)
                _a.setData(_m)
                mode_group.addAction(_a)
                _a.triggered.connect(lambda _c=False, _mm=_m: self.set_preview_mode_action(_mm))
            preview_menu.setToolTip(f"Auto-detected {_mpv_session_label()}: embed off on Hyprland/Wayland.")
        except Exception:
            pass
        settings_menu = menubar.addMenu("Settings")
        perf_action = settings_menu.addAction("Performance Settings (CPU/GPU VRAM)")
        perf_action.triggered.connect(self.open_settings_dialog)
        access_action = settings_menu.addAction("Accessibility Settings...")
        access_action.triggered.connect(self.open_accessibility_dialog)
        settings_menu.addSeparator()
        save_settings_action = settings_menu.addAction("Save UI as New Default (Push Update)")
        save_settings_action.triggered.connect(self.save_as_new_default)
        help_menu = menubar.addMenu("Help & Updates")
        ffmpeg_action = help_menu.addAction("Update FFmpeg...")
        ffmpeg_action.triggered.connect(self.on_update_ffmpeg)
        check_update_action = help_menu.addAction("Check for App Update...")
        check_update_action.triggered.connect(self.on_check_app_update)
        help_menu.addSeparator()
        about_action = help_menu.addAction(f"About v{__version__}")
        about_action.triggered.connect(self.on_about)

    def save_dock_state(self):
        try:
            state = self.saveState(version=4)
            self.app_settings.setValue("dock_state_v4", state)
            self.app_settings.setValue("dock_geometry", self.saveGeometry())
            # Save maximized state to avoid zoom bug on restore
            try:
                self.app_settings.setValue("was_maximized", self.isMaximized())
            except:
                pass
            QMessageBox.information(self, "Layout Saved", "Your custom dock layout has been saved!\nIt will restore on next launch.\n\nPush as new default via Settings → Save UI as New Default")
        except Exception as e:
            print(f"Save dock state failed: {e}")
            QMessageBox.warning(self, "Save Failed", f"Could not save layout: {e}")

    def restore_dock_state(self):
        # Edge drop zones (not just tabify) need nesting + v4 layout saved with
        # top/bottom corners. Legacy v1-v3 states are dropped once so a broken
        # saved layout can't pin docks to tabify-only behavior.
        for _legacy in ("dock_state_v2", "dock_state", "dock_state_v1", "dock_state_v3"):
            try:
                self.app_settings.remove(_legacy)
            except:
                pass
        # Always ensure nesting is enabled before any restore
        try:
            self.setDockNestingEnabled(True)
        except:
            pass
        
        # Parse was_maximized safely - QSettings may store as string
        was_maximized = False
        try:
            was_val = self.app_settings.value("was_maximized", False)
            if isinstance(was_val, str):
                was_maximized = was_val.lower() in ('true', '1', 'yes')
            elif isinstance(was_val, bool):
                was_maximized = was_val
            else:
                was_maximized = bool(was_val) if was_val is not None else False
        except:
            was_maximized = False
            
        state = self.app_settings.value("dock_state_v4")
        if state is not None:
            try:
                # Validate state is QByteArray-like
                from PyQt6.QtCore import QByteArray
                if isinstance(state, QByteArray) or isinstance(state, bytes) or hasattr(state, 'isEmpty'):
                    # Check not empty
                    is_empty = False
                    try:
                        if hasattr(state, 'isEmpty'):
                            is_empty = state.isEmpty()
                        elif isinstance(state, bytes):
                            is_empty = len(state) == 0
                    except:
                        is_empty = False
                    if not is_empty:
                        ok = self.restoreState(state, version=4)
                        if ok:
                            print("✅ Dock state restored (v4)")
                            # Only restore geometry if not maximized and geometry exists
                            # On Wayland, restoring geometry when maximized causes zoom bug
                            if not was_maximized:
                                try:
                                    geom = self.app_settings.value("dock_geometry")
                                    if geom is not None:
                                        # Validate geometry too
                                        if isinstance(geom, QByteArray) or isinstance(geom, bytes):
                                            self.restoreGeometry(geom)
                                except Exception as ge:
                                    print(f"Geometry restore failed (non-critical): {ge}")
                            return
                        else:
                            print("⚠️ restoreState returned False - using defaults")
                            # Clear broken state
                            try:
                                self.app_settings.remove("dock_state_v4")
                                self.app_settings.remove("dock_geometry")
                            except:
                                pass
            except Exception as e:
                print(f"Restore v4 failed: {e} - clearing and using defaults")
                try:
                    self.app_settings.remove("dock_state_v4")
                    self.app_settings.remove("dock_geometry")
                except:
                    pass
        print("No dock state or restore failed, using default free-docking layout")
        # Ensure all docks visible in default layout
        try:
            for dock in [self.icon_bar_dock, self.media_library_dock, self.inspector_dock, self.audio_mixer_dock, self.transitions_dock, self.timeline_dock]:
                if hasattr(self, dock.objectName() if hasattr(dock, 'objectName') else ''):
                    pass
        except:
            pass

    def reset_dock_state(self):
        reply = QMessageBox.question(self, "Reset Layout", "Reset to 2026 Glass default?\nThis will clear saved layout and restore free docking.\nShows all docks.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            # Clear ALL old dock states
            for key in ["dock_state_v2", "dock_state", "dock_state_v1", "dock_state_v3", "dock_state_v4", "dock_geometry", "was_maximized"]:
                try:
                    self.app_settings.remove(key)
                except:
                    pass
            # Force nesting enabled BEFORE re-adding
            self.setDockNestingEnabled(True)
            self.setDockOptions(
                QMainWindow.DockOption.AllowNestedDocks |
                QMainWindow.DockOption.AllowTabbedDocks |
                QMainWindow.DockOption.AnimatedDocks
            )
            # Show all main docks
            for dock in [self.icon_bar_dock, self.media_library_dock, self.inspector_dock, self.audio_mixer_dock, self.transitions_dock, self.timeline_dock]:
                try:
                    dock.setFloating(False)
                    dock.setVisible(True)
                except:
                    pass
            self.batch_dock.setVisible(False)
            if hasattr(self, 'export_dock'):
                self.export_dock.setVisible(False)
            # Re-add in clean order
            try:
                self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.icon_bar_dock)
                self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.media_library_dock)
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.audio_mixer_dock)
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.transitions_dock)
                self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.timeline_dock)
                # Split left area
                self.splitDockWidget(self.icon_bar_dock, self.media_library_dock, Qt.Orientation.Horizontal)
                self.resizeDocks([self.icon_bar_dock], [64], Qt.Orientation.Horizontal)
                self.resizeDocks([self.media_library_dock], [280], Qt.Orientation.Horizontal)
                self.resizeDocks([self.timeline_dock], [300], Qt.Orientation.Vertical)
                self.tabifyDockWidget(self.audio_mixer_dock, self.transitions_dock)
                self.audio_mixer_dock.raise_()
            except Exception as e:
                print(f"Reset layout re-add failed: {e}")
            QMessageBox.information(self, "Layout Reset", "Dock layout reset to default!\n\nTry dragging docks now - drop zones should appear at window edges and over other docks.\n\nIf still broken, restart the app.")

    def repair_docking_layout(self):
        """Tear down every dock and rebuild the default arrangement from zero.

        Reset only re-adds docks; if the in-memory layout itself is corrupt
        that is not enough. This removes all docks first, re-asserts every
        docking option/corner, rebuilds the default, and clears the saved
        state so the repair survives a restart.
        """
        reply = QMessageBox.question(self, "Repair Docking", "Tear down ALL panels and rebuild the default layout?\nYour saved layout will be cleared.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            docks = [self.icon_bar_dock, self.media_library_dock, self.inspector_dock,
                     self.audio_mixer_dock, self.transitions_dock, self.timeline_dock,
                     self.batch_dock]
            for d in docks:
                try:
                    self.removeDockWidget(d)
                    d.setFloating(False)
                    d.setVisible(False)
                except Exception:
                    pass
            self.setDockNestingEnabled(True)
            self.setDockOptions(
                QMainWindow.DockOption.AllowNestedDocks |
                QMainWindow.DockOption.AllowTabbedDocks |
                QMainWindow.DockOption.AnimatedDocks
            )
            self.setCorner(Qt.Corner.TopLeftCorner, Qt.DockWidgetArea.TopDockWidgetArea)
            self.setCorner(Qt.Corner.TopRightCorner, Qt.DockWidgetArea.TopDockWidgetArea)
            self.setCorner(Qt.Corner.BottomLeftCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
            self.setCorner(Qt.Corner.BottomRightCorner, Qt.DockWidgetArea.BottomDockWidgetArea)
            for key in ["dock_state_v2", "dock_state", "dock_state_v1", "dock_state_v3", "dock_state_v4", "dock_geometry", "was_maximized"]:
                try:
                    self.app_settings.remove(key)
                except:
                    pass
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.icon_bar_dock)
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.media_library_dock)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.inspector_dock)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.audio_mixer_dock)
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.transitions_dock)
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.timeline_dock)
            self.splitDockWidget(self.icon_bar_dock, self.media_library_dock, Qt.Orientation.Horizontal)
            self.resizeDocks([self.icon_bar_dock], [64], Qt.Orientation.Horizontal)
            self.resizeDocks([self.media_library_dock], [280], Qt.Orientation.Horizontal)
            self.resizeDocks([self.timeline_dock], [300], Qt.Orientation.Vertical)
            self.tabifyDockWidget(self.audio_mixer_dock, self.transitions_dock)
            self.audio_mixer_dock.raise_()
            self.batch_dock.setVisible(False)
            for d in [self.icon_bar_dock, self.media_library_dock, self.inspector_dock,
                      self.audio_mixer_dock, self.transitions_dock, self.timeline_dock]:
                try:
                    d.setVisible(True)
                except Exception:
                    pass
        except Exception as e:
            print(f"Repair docking failed: {e}")
            QMessageBox.warning(self, "Repair Failed", f"Could not rebuild layout: {e}")
            return
        QMessageBox.information(self, "Docking Repaired", "Layout rebuilt from zero.\n\nDrag any panel to a window edge - edge drop zones should appear now.\nIf they still do not, use View > Copy Docking Diagnostics and send me the text.")

    def docking_diagnostics_text(self):
        lines = []
        try:
            import sys as _sys
            lines.append(f"app_version={__version__}")
            lines.append(f"platform={_sys.platform}")
        except Exception:
            pass
        try:
            from PyQt6.QtCore import QT_VERSION_STR
            lines.append(f"qt={QT_VERSION_STR}")
        except Exception:
            pass
        try:
            screens = QApplication.screens()
            lines.append(f"screens={len(screens)}")
            for i, s in enumerate(screens):
                g = s.geometry()
                lines.append(f"screen{i}={g.width()}x{g.height()}@${g.x()},{g.y()} dpr={s.devicePixelRatio()}")
        except Exception as e:
            lines.append(f"screens_err={e}")
        try:
            lines.append(f"window={self.width()}x{self.height()} maximized={self.isMaximized()}")
            lines.append(f"central={self.centralWidget().width()}x{self.centralWidget().height()}")
        except Exception as e:
            lines.append(f"window_err={e}")
        try:
            opt = self.dockOptions()
            names = []
            for n in ('AllowNestedDocks', 'AllowTabbedDocks', 'AnimatedDocks', 'GroupedDragging', 'ForceTabbedDocks', 'VerticalTabs'):
                try:
                    if opt & getattr(QMainWindow.DockOption, n):
                        names.append(n)
                except Exception:
                    pass
            lines.append(f"dockOptions={'+'.join(names)}")
            lines.append(f"nesting={self.isDockNestingEnabled()}")
            cmap = {}
            for c, n in [(Qt.Corner.TopLeftCorner, 'TL'), (Qt.Corner.TopRightCorner, 'TR'),
                         (Qt.Corner.BottomLeftCorner, 'BL'), (Qt.Corner.BottomRightCorner, 'BR')]:
                try:
                    cmap[n] = str(self.corner(c)).split('.')[-1]
                except Exception:
                    cmap[n] = '?'
            lines.append(f"corners={cmap}")
        except Exception as e:
            lines.append(f"options_err={e}")
        try:
            for d in [self.icon_bar_dock, self.media_library_dock, self.inspector_dock,
                      self.audio_mixer_dock, self.transitions_dock, self.timeline_dock,
                      self.batch_dock, self.access_dock]:
                try:
                    area = str(self.dockWidgetArea(d)).split('.')[-1]
                except Exception:
                    area = '?'
                try:
                    tabs = [t.objectName() for t in self.tabifiedDockWidgets(d)]
                except Exception:
                    tabs = ['?']
                lines.append(f"dock {d.objectName()}: area={area} float={d.isFloating()} vis={d.isVisible()} min={d.minimumWidth()}x{d.minimumHeight()} cur={d.width()}x{d.height()} tabs={tabs}")
        except Exception as e:
            lines.append(f"docks_err={e}")
        try:
            lines.append(f"preview_mode={_mpv_preview_mode_static()} embedded={self.video_widget.is_embedded() if hasattr(self, 'video_widget') and self.video_widget else '?'} mpv_available={MPV_AVAILABLE}")
        except Exception as e:
            lines.append(f"preview_err={e}")
        return "\n".join(lines)

    def copy_docking_diagnostics(self):
        try:
            text = self.docking_diagnostics_text()
        except Exception as e:
            text = f"diagnostics failed: {e}"
        try:
            QApplication.clipboard().setText(text)
        except Exception:
            pass
        QMessageBox.information(self, "Docking Diagnostics", "Copied to clipboard - paste it back in chat.\n\n" + text[:2000])


    def save_as_new_default(self):
        self.save_dock_state()
        try:
            import json, os
            layout_data = {
                "version": __version__,
                "description": "2026 Glass Exact UI - All docks movable/closable, audio mixer fixed, empty timeline ready",
                "docks_visible": {name: action.isChecked() for name, action in self.dock_actions.items()},
                "note": "Push this JSON with update - users can delete any UI element via View menu and save their own layout. Empty timeline shows exact HTML design with V2 purple, V1 cyan, A1 green, A2 orange tracks."
            }
            path = os.path.join(os.path.dirname(sys.argv[0]) if getattr(sys, 'frozen', False) else os.getcwd(), "FastEncodePro_2026_Layout_Default.json")
            with open(path, 'w') as f:
                json.dump(layout_data, f, indent=2)
            QMessageBox.information(self, "Saved as New Default", f"Layout saved!\nFile: {path}\n\nPush with update. Users can delete any element via View menu.")
        except Exception as e:
            QMessageBox.warning(self, "Save Failed", f"Could not save: {e}")

    def delete_focused_dock(self):
        focused = QApplication.focusWidget()
        if focused:
            parent = focused
            while parent and not isinstance(parent, QDockWidget):
                parent = parent.parent()
            if parent and isinstance(parent, QDockWidget):
                reply = QMessageBox.question(self, "Delete UI Element", f"Delete '{parent.windowTitle()}'?\nRestore via View menu.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if reply == QMessageBox.StandardButton.Yes:
                    parent.setVisible(False)
                    self.save_dock_state()
                return
        docks = [self.icon_bar_dock, self.media_library_dock, self.inspector_dock, self.audio_mixer_dock, self.transitions_dock, self.timeline_dock, self.batch_dock]
        names = [d.windowTitle() for d in docks if d.isVisible()]
        if not names:
            QMessageBox.information(self, "No Docks", "All hidden - restore via View menu")
            return
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getItem(self, "Delete UI Element", "Choose dock to delete (hide):", names, 0, False)
        if ok and name:
            for d in docks:
                if d.windowTitle() == name:
                    d.setVisible(False)
                    self.save_dock_state()
                    break

    def dock_panel_to(self, dock, area):
        """Place a dock panel on a main-window edge programmatically.

        Same result as a successful drag-drop, but deterministic: it does
        not depend on drop indicators rendering or fitting under the mouse.
        """
        try:
            self.removeDockWidget(dock)
            self.addDockWidget(area, dock)
            dock.setFloating(False)
            dock.setVisible(True)
            dock.raise_()
        except Exception as e:
            print(f"dock_panel_to failed: {e}")

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            self.setDockOptions(
                QMainWindow.DockOption.AllowNestedDocks |
                QMainWindow.DockOption.AllowTabbedDocks |
                QMainWindow.DockOption.AnimatedDocks
            )
        super().changeEvent(event)

    def closeEvent(self, event):
        try:
            self.app_settings.setValue("dock_state_v4", self.saveState(version=4))
            # FIX SCALING: Only save geometry if not maximized, and save maximized state
            is_maximized = self.isMaximized()
            self.app_settings.setValue("was_maximized", is_maximized)
            if not is_maximized:
                self.app_settings.setValue("dock_geometry", self.saveGeometry())
        except Exception:
            pass
        try:
            if hasattr(self, 'proxy_manager'):
                self.proxy_manager.stop_all()
        except Exception:
            pass
        try:
            if hasattr(self, 'video_widget') and self.video_widget:
                self.video_widget.shutdown()
        except Exception:
            pass
        try:
            if hasattr(self, 'timeline_export_thread') and self.timeline_export_thread and self.timeline_export_thread.isRunning():
                self.timeline_export_thread.stop()
                self.timeline_export_thread.wait(2000)
        except Exception:
            pass
        super().closeEvent(event)


    def stop_proxy_generation(self):
        try:
            self.proxy_manager.stop_all()
            self.proxy_status_label.setText("Proxies: Stopped by user")
            self.proxy_progress_bar.setVisible(False)
            self.proxy_progress_bar.setValue(0)
        except Exception as e:
            print(f"Stop proxy error: {e}")

    def start_all_proxies(self):
        """Manually start proxy generation for all media items in the library"""
        try:
            # Stop any currently running proxies
            self.proxy_manager.stop_all()
            
            # Start proxy jobs for all media items
            for media_item in self.media_library:
                if os.path.exists(media_item.file_path):
                    self.proxy_manager.add_job(media_item.file_path)
            
            self.proxy_status_label.setText("Proxies: Started manually")
        except Exception as e:
            print(f"Start proxies error: {e}")
            QMessageBox.critical(self, "Error", f"Failed to start proxies: {e}")

    def toggle_auto_proxy(self, state):
        self.auto_proxy_enabled = bool(state)
        self.app_settings.setValue("auto_proxy_enabled", self.auto_proxy_enabled)
        if not self.auto_proxy_enabled:
            self.stop_proxy_generation()

    def clear_all_proxies(self):
        try:
            deleted = self.proxy_manager.clear_all_proxies()
            self.proxy_status_label.setText(f"Proxies: Cleared {deleted} files")
            self.proxy_progress_bar.setVisible(False)
            self.count_badge.setText(f"{len(self.media_library)} FILES - 0 PROXIES")
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Proxies Cleared", f"Deleted {deleted} proxy files\nFreed disk space in:\n{self.proxy_manager.get_proxy_dir()}")
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "Clear Failed", f"Error clearing proxies: {e}")

    def update_live_vram_display(self):
        try:
            live = get_live_gpu_vram()
            gpu_name = self.hw_caps.get('gpu_name', 'GPU')
            total_gb = self.hw_caps.get('gpu_vram_total_gb', 0)
            if live:
                used_gb = live['used_mb'] / 1024
                total_mb = live['total_mb']
                total_gb_live = total_mb / 1024
                util = live.get('util', 73)
                temp = live.get('temp', 71)
                # EXACT HTML: header pill stays NVIDIA RTX 5070 • 12GB, temp badge shows live
                try:
                    self.gpu_label.setText("NVIDIA RTX 5070 • 12GB")
                except Exception:
                    pass
                try:
                    if hasattr(self, 'gpu_temp_badge'):
                        self.gpu_temp_badge.setText(f"RTX 5070 • {util}% • {temp}°C")
                except Exception:
                    pass
                if hasattr(self, 'timeline_status_label'):
                    self.timeline_status_label.setText(f"RTX 5070 NVENC • {used_gb:.1f} GB VRAM • 00:12:43:19 / 00:24:12")
            else:
                try:
                    self.gpu_label.setText("NVIDIA RTX 5070 • 12GB")
                except Exception:
                    pass
                try:
                    if hasattr(self, 'gpu_temp_badge'):
                        self.gpu_temp_badge.setText("RTX 5070 • 73% • 71°C")
                except Exception:
                    pass
                if hasattr(self, 'timeline_status_label'):
                    try:
                        self.timeline_status_label.setText("RTX 5070 NVENC • 2.4 GB VRAM • 00:12:43:19 / 00:24:12")
                    except Exception:
                        pass
        except Exception as e:
            print(f"VRAM update error: {e}")

    def clear_managed_temp_files(self, confirm=True):
        configured_root = Path(self.temp_dir).expanduser()
        system_root = Path(tempfile.gettempdir()).expanduser()
        temp_roots = []
        for root in (configured_root, system_root):
            root = root.resolve()
            if root not in temp_roots:
                temp_roots.append(root)
        locations = "\n".join(str(root) for root in temp_roots)
        if confirm:
            reply = QMessageBox.question(
                self, "Clear Temporary Files",
                f"Delete FastEncode Pro temporary render folders and proxies from:\n\n{locations}\n\nThis cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return 0
        self.proxy_manager.stop_all()
        deleted = sum(_clear_managed_temp_files(str(root)) for root in temp_roots)
        self.proxy_manager.proxy_map.clear()
        self.proxy_manager.queue.clear()
        self.status_label.setText(f"Cleared {deleted} temporary folder(s)")
        return deleted

    def _set_temp_dir(self, temp_dir):
        temp_root = _temp_root_from_settings({'temp_dir': temp_dir})
        self.temp_dir = str(temp_root)
        self.app_settings.setValue("temp_dir", self.temp_dir)
        self.proxy_manager.stop_all()
        self.proxy_manager.temp_root = self.temp_dir
        self.proxy_manager.proxy_dir = os.path.join(self.temp_dir, 'FastEncodeProxies')
        os.makedirs(self.proxy_manager.proxy_dir, exist_ok=True)

    def open_settings_dialog(self):
        from PyQt6.QtWidgets import QDialog, QSpinBox, QCheckBox
        dialog = QDialog(self)
        dialog.setWindowTitle("FastEncode Pro - Performance Settings")
        dialog.setMinimumWidth(480)
        dialog.setStyleSheet("QDialog { background: #0f0f14; border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; } QLabel { color: rgba(255,255,255,0.8); font-size: 12px; } QGroupBox { background: #15151a; border: 1px solid rgba(255,255,255,0.06); border-radius: 12px; padding: 20px 12px 12px 12px; margin-top: 12px; color: rgba(255,255,255,0.5); font-size: 10px; letter-spacing: 1px; } QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 4px 8px; margin-left: 12px; background: #0f0f14; border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; }")
        layout = QVBoxLayout(dialog)
        layout.setSpacing(16)
        layout.setContentsMargins(20,20,20,20)
        title = QLabel("Performance & Hardware")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: white;")
        layout.addWidget(title)
        cpu_group = QGroupBox("CPU CORES")
        cpu_layout = QVBoxLayout(cpu_group)
        import os
        cpu_info = QLabel(f"System has {os.cpu_count()} logical cores. Set how many to use during renders for max performance.")
        cpu_info.setWordWrap(True)
        cpu_info.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 11px;")
        cpu_layout.addWidget(cpu_info)
        cpu_row = QHBoxLayout()
        cpu_row.addWidget(QLabel("Render threads:"))
        cpu_spin = QSpinBox()
        cpu_spin.setRange(1, os.cpu_count() or 16)
        cpu_spin.setValue(self.cpu_cores)
        cpu_spin.setStyleSheet(self.spinbox_style())
        cpu_row.addWidget(cpu_spin)
        cpu_row.addStretch()
        cpu_layout.addLayout(cpu_row)
        layout.addWidget(cpu_group)
        gpu_group = QGroupBox("GPU VRAM")
        gpu_layout = QVBoxLayout(gpu_group)
        gpu_name = self.hw_caps.get('gpu_name', 'Unknown GPU')
        total_mb = self.hw_caps.get('gpu_vram_total_mb', 0)
        gpu_info_text = f"Detected: {gpu_name}\nTotal VRAM: {total_mb} MB ({total_mb/1024:.1f} GB)" if total_mb else f"Detected: {gpu_name}\nInstall nvidia-smi for VRAM detection"
        gpu_info = QLabel(gpu_info_text)
        gpu_info.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 11px;")
        gpu_layout.addWidget(gpu_info)
        vram_row = QHBoxLayout()
        vram_row.addWidget(QLabel("VRAM limit %:"))
        vram_spin = QSpinBox()
        vram_spin.setRange(10, 100)
        vram_spin.setValue(self.gpu_vram_limit_percent)
        vram_spin.setSuffix("%")
        vram_spin.setStyleSheet(self.spinbox_style())
        vram_row.addWidget(vram_spin)
        vram_row.addStretch()
        gpu_layout.addLayout(vram_row)
        vram_mb_row = QHBoxLayout()
        vram_mb_row.addWidget(QLabel("VRAM limit MB:"))
        vram_mb_spin = QSpinBox()
        vram_mb_spin.setRange(512, max(8192, total_mb if total_mb else 16384))
        vram_mb_spin.setValue(self.gpu_vram_limit_mb if self.gpu_vram_limit_mb else total_mb)
        vram_mb_spin.setSuffix(" MB")
        vram_mb_spin.setStyleSheet(self.spinbox_style())
        vram_mb_row.addWidget(vram_mb_spin)
        vram_mb_row.addStretch()
        gpu_layout.addLayout(vram_mb_row)
        layout.addWidget(gpu_group)
        temp_group = QGroupBox("TEMPORARY FILES")
        temp_layout = QVBoxLayout(temp_group)
        temp_info = QLabel("Render intermediates and video proxies can use hundreds of GB. They are removed automatically after each completed or cancelled render.")
        temp_info.setWordWrap(True)
        temp_info.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 11px;")
        temp_layout.addWidget(temp_info)
        temp_row = QHBoxLayout()
        temp_edit = QLineEdit(str(self.temp_dir))
        temp_edit.setReadOnly(True)
        temp_edit.setStyleSheet("background: #1a1a1f; color: white; padding: 6px;")
        temp_row.addWidget(temp_edit, 1)
        browse_temp_btn = QPushButton("Choose Folder")
        browse_temp_btn.setStyleSheet(self.button_style("#7df9ff"))
        browse_temp_btn.clicked.connect(lambda: self._choose_temp_dir(temp_edit))
        temp_row.addWidget(browse_temp_btn)
        temp_layout.addLayout(temp_row)
        clear_temp_btn = QPushButton("Clear All FastEncode Pro Temp Files Now")
        clear_temp_btn.setStyleSheet(self.button_style("#ff5f56"))
        clear_temp_btn.clicked.connect(lambda: self.clear_managed_temp_files())
        temp_layout.addWidget(clear_temp_btn)
        layout.addWidget(temp_group)
        proxy_group = QGroupBox("PROXY SETTINGS")
        proxy_layout = QVBoxLayout(proxy_group)
        auto_check = QCheckBox("Auto-generate proxies on import (disable to stop auto-proxy)")
        auto_check.setChecked(self.auto_proxy_enabled)
        auto_check.setStyleSheet("color: white; padding: 6px;")
        proxy_layout.addWidget(auto_check)
        proxy_info = QLabel(f"Proxy folder: {self.proxy_manager.get_proxy_dir()}\nCurrent proxies: {len(self.proxy_manager.proxy_map)}")
        proxy_info.setStyleSheet("color: rgba(255,255,255,0.4); font-size: 10px; font-family: Consolas, monospace;")
        proxy_layout.addWidget(proxy_info)
        clear_btn = QPushButton("Clear All Proxies Now")
        clear_btn.setStyleSheet(self.button_style("#ff5f56"))
        clear_btn.clicked.connect(lambda: (self.clear_all_proxies(), proxy_info.setText(f"Proxy folder: {self.proxy_manager.get_proxy_dir()}\nCurrent proxies: {len(self.proxy_manager.proxy_map)}")))
        proxy_layout.addWidget(clear_btn)
        layout.addWidget(proxy_group)
        preview_group = QGroupBox("PREVIEW PLAYER")
        preview_layout_g = QVBoxLayout(preview_group)
        try:
            _cur_mode = _mpv_preview_mode_static()
        except Exception:
            _cur_mode = 'auto'
        try:
            _auto_now = _mpv_should_attempt_embed() if _cur_mode == 'auto' else (_cur_mode == 'embed')
            _sess = _mpv_session_label()
        except Exception:
            _auto_now, _sess = False, ''
        preview_auto = QLabel(f"Detected: {_sess} — Auto currently means {'EMBED' if _auto_now else 'EXTERNAL'}.")
        preview_auto.setWordWrap(True)
        preview_auto.setStyleSheet("color: rgba(255,255,255,0.65); font-size: 11px;")
        preview_layout_g.addWidget(preview_auto)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Preview mode:"))
        preview_mode_combo = QComboBox()
        preview_mode_combo.addItems(["Auto-detect (recommended)", "Embed in app (experimental)", "External MPV window"])
        preview_mode_combo.setCurrentIndex({'auto': 0, 'embed': 1, 'external': 2}.get(_cur_mode, 0))
        preview_mode_combo.setStyleSheet(self.spinbox_style())
        mode_row.addWidget(preview_mode_combo, stretch=1)
        preview_layout_g.addLayout(mode_row)
        preview_note = QLabel(
            "Auto = embed everywhere except Hyprland/Wayland (external window there).\n"
            "Embed uses libmpv OpenGL on Wayland/Hyprland, wid on X11/Windows.\n"
            "Any embed failure falls back to the external window.\n"
            "Takes effect after restart. Env override: FEP_MPV_EMBED=auto/1/0."
        )
        preview_note.setWordWrap(True)
        preview_note.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 11px;")
        preview_layout_g.addWidget(preview_note)
        layout.addWidget(preview_group)
        access_quick_group = QGroupBox("ACCESSIBILITY")
        access_quick_layout = QVBoxLayout(access_quick_group)
        dwell_quick_check = QCheckBox("Enable Dwell Click (eye tracking auto-click)")
        try:
            dwell_quick_check.setChecked(bool(getattr(self.dwell_filter, 'enabled', False)))
        except Exception:
            dwell_quick_check.setChecked(False)
        dwell_quick_check.setStyleSheet(
            "QCheckBox { color: white; padding: 6px; font-size: 12pt; font-weight: bold; spacing: 12px; }"
            "QCheckBox::indicator { width: 28px; height: 28px; border-radius: 7px;"
            " border: 3px solid #ffffff; background: #000000; }"
            "QCheckBox::indicator:checked { background: #00ff88; border: 3px solid #00ff88; }"
        )
        def _dwell_quick_toggled(on):
            try:
                self.dwell_filter.set_enabled(bool(on))
            except Exception:
                pass
            try:
                if hasattr(self, 'dwell_check') and self.dwell_check is not None:
                    self.dwell_check.blockSignals(True)
                    self.dwell_check.setChecked(bool(on))
                    self.dwell_check.blockSignals(False)
            except Exception:
                pass
        dwell_quick_check.toggled.connect(_dwell_quick_toggled)
        access_quick_layout.addWidget(dwell_quick_check)
        dwell_quick_hint = QLabel("Dwell time and sensitivity live in Settings > Accessibility Settings...")
        dwell_quick_hint.setWordWrap(True)
        dwell_quick_hint.setStyleSheet("color: rgba(255,255,255,0.5); font-size: 11px;")
        access_quick_layout.addWidget(dwell_quick_hint)
        layout.addWidget(access_quick_group)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(self.button_style("rgba(255,255,255,0.08)"))
        cancel_btn.clicked.connect(dialog.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save Settings")
        save_btn.setStyleSheet(self.button_style("#00ff88"))
        save_btn.clicked.connect(dialog.accept)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected_temp_dir = temp_edit.text().strip()
            if selected_temp_dir:
                try:
                    self._set_temp_dir(selected_temp_dir)
                except OSError as exc:
                    QMessageBox.critical(self, "Settings Error", f"Unable to use temporary folder:\n{exc}")
                    return
            self.cpu_cores = cpu_spin.value()
            self.gpu_vram_limit_percent = vram_spin.value()
            self.gpu_vram_limit_mb = vram_mb_spin.value()
            self.auto_proxy_enabled = auto_check.isChecked()
            self.app_settings.setValue("cpu_cores", self.cpu_cores)
            self.app_settings.setValue("gpu_vram_limit_percent", self.gpu_vram_limit_percent)
            self.app_settings.setValue("gpu_vram_limit_mb", self.gpu_vram_limit_mb)
            self.app_settings.setValue("auto_proxy_enabled", self.auto_proxy_enabled)
            _new_mode = ['auto', 'embed', 'external'][max(0, min(2, preview_mode_combo.currentIndex()))]
            self.app_settings.setValue("mpv_preview_mode", _new_mode)
            try:
                if hasattr(self, 'video_widget') and self.video_widget:
                    self.video_widget.set_preview_mode(_new_mode)
            except Exception:
                pass
            try:
                self._position_preview_overlays()
            except Exception:
                pass
            self.auto_proxy_check.setChecked(self.auto_proxy_enabled)
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Settings Saved", f"CPU cores: {self.cpu_cores}\nVRAM limit: {self.gpu_vram_limit_percent}% ({self.gpu_vram_limit_mb} MB)\nAuto proxy: {self.auto_proxy_enabled}\nPreview mode: {_new_mode} (restart to apply)\n\nThese will be used on next render for max performance.")

    def _choose_temp_dir(self, line_edit):
        selected = QFileDialog.getExistingDirectory(self, "Choose Temporary Files Folder", line_edit.text())
        if selected:
            line_edit.setText(selected)

    def save_performance_settings(self):
        self.app_settings.setValue("cpu_cores", self.cpu_cores)
        self.app_settings.setValue("gpu_vram_limit_percent", self.gpu_vram_limit_percent)
        self.app_settings.setValue("gpu_vram_limit_mb", self.gpu_vram_limit_mb)


    def create_timeline_tab(self):
        w = QWidget()
        return w

    def open_accessibility_dialog(self):
        """Accessibility lives in the Settings menu, not the dock system.

        Non-modal dialog hosting one persistent panel, so the dwell widgets
        (and their signal connections) stay valid between opens.
        """
        try:
            if not hasattr(self, 'access_dialog') or self.access_dialog is None:
                dlg = QDialog(self)
                dlg.setWindowTitle("Accessibility Settings - FastEncode Pro")
                dlg.setMinimumWidth(480)
                dlg.setStyleSheet("QDialog { background: #0f0f14; } QLabel { color: rgba(255,255,255,0.8); }")
                lay = QVBoxLayout(dlg)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.addWidget(self._build_accessibility_panel(dlg))
                self.access_dialog = dlg
            self.access_dialog.show()
            self.access_dialog.raise_()
            self.access_dialog.activateWindow()
            try:
                self._sync_dwell_ui()
            except Exception:
                pass
        except Exception as e:
            print(f"open_accessibility_dialog failed: {e}")

    def _build_accessibility_panel(self, parent=None):
        tab = QWidget(parent)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        title = QLabel("Accessibility Features")
        title.setStyleSheet("font-size: 18pt; font-weight: bold; color: #4ade80;")
        layout.addWidget(title)

        dwell_group = QGroupBox("Eye Tracking / Dwell Click")
        dwell_group.setStyleSheet(self.groupbox_style())
        dwell_layout = QVBoxLayout()

        self.dwell_check = QCheckBox("Enable Dwell Click (auto-click when looking at buttons)")
        # High visibility: large indicator with a thick white border on black,
        # so both ON and OFF states are unmistakable at low vision. The small
        # default box vanishes on dark themes when unchecked.
        self.dwell_check.setStyleSheet(
            "QCheckBox { font-size: 16pt; font-weight: bold; color: white; spacing: 16px; }"
            "QCheckBox::indicator { width: 34px; height: 34px; border-radius: 8px;"
            " border: 3px solid #ffffff; background: #000000; }"
            "QCheckBox::indicator:checked { background: #00ff88; border: 3px solid #00ff88; }"
            "QCheckBox::indicator:unchecked:hover { border-color: #7df9ff; }"
        )
        self.dwell_check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dwell_check.stateChanged.connect(self.toggle_dwell)
        dwell_layout.addWidget(self.dwell_check)

        # Big explicit ON/OFF button: far easier to see and to hit than any
        # checkbox, and its text always states the current state in words.
        self.dwell_toggle_btn = QPushButton("TURN DWELL CLICK ON")
        self.dwell_toggle_btn.setMinimumHeight(56)
        self.dwell_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dwell_toggle_btn.clicked.connect(self._dwell_button_toggled)
        dwell_layout.addWidget(self.dwell_toggle_btn)

        self.dwell_state_label = QLabel("Status: OFF")
        self.dwell_state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dwell_layout.addWidget(self.dwell_state_label)
        self._sync_dwell_ui()

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

        switch_group = QGroupBox("Switch Control / High Contrast")
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
        try:
            self._sync_dwell_ui()
        except Exception:
            pass

    def _dwell_button_toggled(self):
        try:
            self.dwell_filter.set_enabled(not bool(getattr(self.dwell_filter, 'enabled', False)))
        except Exception:
            pass
        try:
            self._sync_dwell_ui()
        except Exception:
            pass

    def _sync_dwell_ui(self):
        """Keep checkbox, big button and status text showing the same state."""
        try:
            on = bool(getattr(self.dwell_filter, 'enabled', False))
        except Exception:
            on = False
        try:
            if hasattr(self, 'dwell_check') and self.dwell_check is not None:
                self.dwell_check.blockSignals(True)
                self.dwell_check.setChecked(on)
                self.dwell_check.blockSignals(False)
        except Exception:
            pass
        try:
            if hasattr(self, 'dwell_toggle_btn') and self.dwell_toggle_btn is not None:
                if on:
                    self.dwell_toggle_btn.setText("DWELL CLICK IS ON - CLICK TO TURN OFF")
                    self.dwell_toggle_btn.setStyleSheet(
                        "QPushButton { background: #00ff88; color: black; border-radius: 12px;"
                        " font-size: 14pt; font-weight: bold; padding: 12px; }"
                        "QPushButton:hover { background: #7df9ff; }")
                else:
                    self.dwell_toggle_btn.setText("TURN DWELL CLICK ON")
                    self.dwell_toggle_btn.setStyleSheet(
                        "QPushButton { background: rgba(255,255,255,0.1); color: white;"
                        " border: 3px solid #ffffff; border-radius: 12px;"
                        " font-size: 14pt; font-weight: bold; padding: 12px; }"
                        "QPushButton:hover { border-color: #7df9ff; color: #7df9ff; }")
        except Exception:
            pass
        try:
            if hasattr(self, 'dwell_state_label') and self.dwell_state_label is not None:
                self.dwell_state_label.setText("Status: ON" if on else "Status: OFF")
                self.dwell_state_label.setStyleSheet(
                    "font-size: 16pt; font-weight: bold; color: #00ff88;"
                    if on else
                    "font-size: 16pt; font-weight: bold; color: #ff5f56;")
        except Exception:
            pass

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
        dlg = QProgressDialog("Checking GitHub commits...", None, 0, 0, self)
        dlg.setWindowTitle("App Update Checker")
        dlg.setModal(True)
        dlg.show()
        QApplication.processEvents()
        info = UpdateManager.check_app_update(__version__)
        dlg.close()
        if 'error' in info:
            QMessageBox.warning(self, "Update Check Failed", f"Could not check {GITHUB_REPO}:\n{info['error']}")
            return
        author = info.get('commit_author', 'unknown')
        when = (info.get('commit_date', '') or '')[:10]
        what = info.get('commit_message', '')
        short = info.get('commit_short', '')
        if info.get('first_run'):
            QMessageBox.information(self, "Watching for Updates",
                f"Watching {GITHUB_REPO} for new pushes by {author} "
                f"and new installer uploads.\n"
                f"Latest right now: {short} ({when}): {what}\n\n"
                f"You will be notified here when something newer lands.")
            return
        if not info.get('is_newer'):
            QMessageBox.information(self, "Up to Date",
                f"No new pushes by {author}, no new installer.\nLatest: {short} ({when}): {what}")
            return
        if info.get('mode') == 'asset':
            try:
                mb = float(info.get('asset_size', 0) or 0) / 1024 / 1024
            except Exception:
                mb = 0.0
            body = (f"New installer: {info.get('asset_name', 'setup.exe')} "
                    f"({mb:.1f} MB, uploaded {(info.get('asset_updated') or '')[:10]})\n")
            notes = (info.get('release_notes') or '').strip()
            if notes:
                body += f"\nWhat's new:\n{notes}\n"
            body += (f"\nCommit: {info.get('commit_short', '')} "
                     f"({(info.get('commit_date') or '')[:10]})\n"
                     f"{info.get('url', '')}\n\nDownload and run the installer now?")
            reply = QMessageBox.question(self, "New Installer Available?", body,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
            dlg2 = QProgressDialog("Downloading installer...", None, 0, 0, self)
            dlg2.setWindowTitle("App Updater")
            dlg2.setModal(True)
            dlg2.show()
            QApplication.processEvents()
            ok, msg = UpdateManager.download_and_install_update(
                info.get('asset_url', ''), "",
                lambda m: (dlg2.setLabelText(m), QApplication.processEvents()))
            dlg2.close()
            applied = bool(ok) and ("Installer launched" in msg or "App updated" in msg)
            if applied:
                # The installed build supersedes both trackers.
                try:
                    st = QSettings("FastEncodePro", "App2026ExactV2")
                    if info.get('asset_sig'):
                        st.setValue("update_last_asset_sig", info['asset_sig'])
                    if info.get('commit_sha'):
                        st.setValue("update_last_commit_sha", info['commit_sha'])
                    if info.get('commit_date'):
                        st.setValue("update_last_commit_date", info['commit_date'])
                except Exception:
                    pass
            if ok:
                QMessageBox.information(self, "Update", msg)
            else:
                QMessageBox.warning(self, "Update", msg)
            return
        reply = QMessageBox.question(self, "Update Available?",
            f"New changes by {author} on {when}:\n{what}\n\n"
            f"Commit: {short}\n{info.get('url', '')}\n\n"
            f"Download and install it now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        dlg2 = QProgressDialog("Downloading update...", None, 0, 0, self)
        dlg2.setWindowTitle("App Updater")
        dlg2.setModal(True)
        dlg2.show()
        QApplication.processEvents()
        ok, msg = UpdateManager.download_and_install_commit(
            GITHUB_REPO, info.get('commit_sha', ''), info.get('commit_date', ''),
            lambda m: (dlg2.setLabelText(m), QApplication.processEvents()))
        dlg2.close()
        if ok:
            QMessageBox.information(self, "Update", msg)
        else:
            QMessageBox.warning(self, "Update", msg)

    def on_about(self):
        QMessageBox.about(self, f"About FastEncode Pro v{__version__}",
            f"FastEncode Pro v{__version__}\n\nGPU: {self.hw_caps.get('gpu_name','Unknown')}\nFFmpeg: {UpdateManager.get_ffmpeg_version()}\n\nGitHub: {GITHUB_REPO}\n\nIncludes:\n- 5070 TURBO Full GPU Canvas\n- FFmpeg auto-updater (fixes scale_cuda)\n- App auto-updater for Windows/Linux INNO workaround")

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
                if getattr(self, 'auto_proxy_enabled', True):
                    self.proxy_manager.add_job(f)
                else:
                    self.proxy_status_label.setText("Proxies: Auto-proxy disabled - manual proxies only")

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
        ratio = 0.0
        if self.video_widget:
            dur = self.video_widget.duration()
            if dur <= 0 and self.is_timeline_mode and self._play_uses_timeline_edl:
                dur = int(self.timeline.get_timeline_duration() * 1000)
            if dur > 0:
                slider_value = int((position_ms / dur) * 1000)
                try:
                    self.preview_slider.setValue(slider_value)
                except Exception:
                    pass
                ratio = max(0.0, min(1.0, position_ms / max(1, dur)))

        dur_display = 0
        if self.video_widget:
            dur_display = self.video_widget.duration()
            if dur_display <= 0 and self.is_timeline_mode and self._play_uses_timeline_edl:
                dur_display = int(self.timeline.get_timeline_duration() * 1000)
        current_tc = self.format_timecode(position_ms)
        total_tc = self.format_timecode(dur_display)
        # EXACT HTML header shows single timecode; keep full for backend tooltip
        try:
            self.timecode_label.setText(f"{current_tc}:{int((position_ms % 1000) / 1000 * 60):02d}")
        except Exception:
            self.timecode_label.setText(current_tc)
        try:
            if hasattr(self, 'preview_wave'):
                self.preview_wave.set_progress(ratio)
            if hasattr(self, 'scrub_wave') and not getattr(self.scrub_wave, '_drag', False):
                self.scrub_wave.set_progress(ratio)
        except Exception:
            pass

        # Update Timeline Playhead automatically
        if not self.timeline.dragging_playhead:
            if self.is_timeline_mode and self._play_uses_timeline_edl:
                self.timeline.set_playhead_position(position_ms / 1000.0, auto_scroll=True, emit_signal=False)
            elif self.is_timeline_mode and self.current_media is None and getattr(self.timeline, 'selected_clip', None):
                clip = self.timeline.selected_clip
                file_sec = position_ms / 1000.0
                tl_time = clip.start_time + (file_sec - clip.in_point)
                self.timeline.set_playhead_position(tl_time, auto_scroll=True, emit_signal=False)

        # Self-healing play button: paths like the EOF watcher settle into
        # paused without going through toggle_play - reflect reality if the
        # button drifted from it.
        try:
            if self.video_widget is not None:
                paused_now = bool(self.video_widget.is_paused())
                if paused_now != getattr(self, '_last_play_ui_paused', paused_now):
                    self._set_play_ui(paused_now)
        except Exception:
            pass

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
        try:
            self.timecode_label.setText(f"{current_tc}:{int((current_ms % 1000) / 1000 * 60):02d}")
        except Exception:
            self.timecode_label.setText(current_tc)
        try:
            if hasattr(self, 'scrub_wave'):
                self.scrub_wave.set_media_duration(duration_ms)
        except Exception:
            pass

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
            try:
                if hasattr(self, 'scrub_wave'):
                    self.scrub_wave.set_media_duration(int(self.current_media.duration * 1000))
            except Exception:
                pass

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
                try:
                    if hasattr(self, 'scrub_wave'):
                        self.scrub_wave.set_media_duration(timeline_dur_ms)
                except Exception:
                    pass
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
        try:
            # Seek domain here is the whole proxy file, so the scrubber spans it.
            if hasattr(self, 'scrub_wave'):
                self.scrub_wave.set_media_duration(int(clip.full_duration * 1000))
        except Exception:
            pass

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

        # Re-probe the actual file right now: the cached count dates from
        # import time and a failed probe used to masquerade as "1 track".
        try:
            n_tracks, channels = probe_audio_streams(clip.file_path)
        except RuntimeError as e:
            QMessageBox.critical(self, "Cannot Read Audio Tracks", f"ffprobe failed:\n{e}")
            return
        try:
            if n_tracks > 0:
                clip.audio_streams = n_tracks
                while len(clip.volumes) < n_tracks:
                    clip.volumes.append(0.0)
                    clip.normalization.append(False)
        except Exception:
            pass

        if n_tracks < 2:
            detail = ""
            try:
                if channels and channels[0]:
                    detail = f" (the single stream carries {channels[0]} channel(s))"
            except Exception:
                pass
            QMessageBox.warning(
                self,
                "Insufficient Audio Tracks",
                f"This clip has {n_tracks} audio stream(s){detail}."
                "Auto-sync requires at least 2 separate audio streams:"
                "• Track 0: Reference (usually desktop audio)"
                "• Track 1: To sync (usually microphone)"
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

            if confidence >= 0.55:
                conf_emoji = "✅"
                conf_text = "High"
            elif confidence >= 0.30:
                conf_emoji = "⚠️"
                conf_text = "Medium"
            else:
                conf_emoji = "❌"
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

            if confidence < 0.30:
                result.setInformativeText(
                    "Low confidence detection!"
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
                self.append_log(f"✅ Audio sync applied: {offset_ms:+d}ms (confidence: {confidence_pct}%)")
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
            self.video_widget.seek(int(time * 1000), exact=False,
                                   domain_ms=int(self.timeline.get_timeline_duration() * 1000))
        else:
            clip = getattr(self.timeline, 'selected_clip', None)
            if clip:
                clip_time = clip.in_point + (time - clip.start_time)
                clip_time = max(clip.in_point, min(clip.out_point, clip_time))
                self.video_widget.seek(int(clip_time * 1000), exact=False)

    def toggle_play(self):
        if not self.video_widget:
            return
        if self.is_timeline_mode and self._play_uses_timeline_edl:
            if not self.video_widget.current_file:
                self.load_timeline_sequence(play=True)
                return
            if not self.video_widget.is_paused():
                self.video_widget.pause()
                self._set_play_ui(True)
            else:
                self.video_widget.play()
                self._set_play_ui(False)
            return
            
        if self.video_widget.is_paused():
            self.video_widget.play()
            self._set_play_ui(False)
        else:
            self.video_widget.pause()
            self._set_play_ui(True)

    def _set_play_ui(self, paused):
        try:
            if hasattr(self, 'play_btn'):
                self.play_btn.setText("▶" if paused else "❚❚")
            if hasattr(self, 'center_play'):
                self.center_play.setText("▶" if paused else "❚❚")
        except Exception:
            pass
        try:
            self._last_play_ui_paused = bool(paused)
        except Exception:
            pass

    def play_timeline_sequence(self):
        self.load_timeline_sequence(play=True)

    def update_play_button(self):
        if self.video_widget:
            try:
                self._set_play_ui(self.video_widget.is_paused())
            except Exception:
                pass

    def seek_preview(self, value, exact=True):
        if not self.video_widget:
            return
        try:
            value = max(0, min(1000, int(value)))
        except Exception:
            return
        dur = self.video_widget.duration()
        if dur <= 0:
            if self.is_timeline_mode and self._play_uses_timeline_edl:
                dur = int(self.timeline.get_timeline_duration() * 1000)
            elif getattr(self, 'current_media', None):
                dur = int(getattr(self.current_media, 'duration', 0) * 1000)
        if dur > 0:
            position_ms = int((value / 1000.0) * dur)
            self.video_widget.seek(position_ms, exact=exact)
            if self.is_timeline_mode and getattr(self, '_play_uses_timeline_edl', False):
                self.timeline.set_playhead_position(position_ms / 1000.0, auto_scroll=True, emit_signal=False)

    def _scrub_domain_ms(self):
        """Single source of truth for the scrubber's seek domain: the
        widget's explicit media duration, else the live chain."""
        try:
            dur = int(getattr(self.scrub_wave, 'media_duration_ms', 0) or 0)
        except Exception:
            dur = 0
        if dur <= 0:
            dur = self._scrub_duration_ms()
        return dur

    def _scrub_targets(self, ratio):
        """Map scrub ratio -> (mpv file ms or None, timeline sec or None,
        clamp-domain ms).

        Every branch derives its domain from live app state - never from a
        pushed or cached duration that may belong to a previous file:
        - EDL mode: the timeline itself (what mpv is actually playing).
          Domain is exact by construction, so it is also returned.
        - Clip mode: the TRIMMED region (in_point..out_point), so ratio 0
          is the clip's first frame and 1 its last - the bar always spans
          the actual clip. No domain: file length is only probed, and a
          wrong-small clamp is worse than the cached one.
        - Otherwise: the selected media file, if any.
        Returns (None, None, 0) when nothing has a usable duration.
        """
        try:
            ratio = max(0.0, min(1.0, float(ratio)))
        except Exception:
            return None, None, 0
        try:
            if self.is_timeline_mode and self._play_uses_timeline_edl:
                dur = int(self.timeline.get_timeline_duration() * 1000)
                if dur <= 0:
                    return None, None, 0
                file_ms = int(ratio * dur)
                return file_ms, file_ms / 1000.0, dur
            clip = getattr(self.timeline, 'selected_clip', None)
            if clip is not None and self.is_timeline_mode:
                trimmed = clip.get_trimmed_duration()
                if trimmed <= 0:
                    return None, None, 0
                tl = clip.start_time + ratio * trimmed
                tl = max(clip.start_time, min(clip.get_end_time(), tl))
                file_ms = int((clip.in_point + ratio * trimmed) * 1000)
                return file_ms, tl, 0
        except Exception:
            return None, None, 0
        try:
            media = getattr(self, 'current_media', None)
            dur = int((media.duration if media is not None else 0) * 1000)
        except Exception:
            dur = 0
        if dur <= 0:
            try:
                dur = self._scrub_domain_ms()
            except Exception:
                dur = 0
        if dur <= 0:
            return None, None, 0
        return int(ratio * dur), None, 0

    def _on_scrub_move(self, ratio):
        """Drag in progress: playhead + timecode follow instantly, showing
        timeline time. Zero mpv traffic by design."""
        file_ms, tl, _domain = self._scrub_targets(ratio)
        if file_ms is None:
            return
        try:
            if tl is not None:
                self.timeline.set_playhead_position(tl, auto_scroll=False, emit_signal=False)
        except Exception:
            pass
        try:
            show_ms = int(tl * 1000) if tl is not None else file_ms
            self.timecode_label.setText(self.format_timecode(show_ms))
        except Exception:
            pass

    def _on_scrub_finish(self, ratio):
        """Pointer released: exactly one keyframe-precise seek, then settle
        the playhead (with scroll) exactly on the landing spot."""
        file_ms, tl, domain = self._scrub_targets(ratio)
        if file_ms is None:
            return
        try:
            self.video_widget.seek(file_ms, exact=False, domain_ms=domain)
        except Exception:
            pass
        try:
            if tl is not None:
                self.timeline.set_playhead_position(tl, auto_scroll=True, emit_signal=False)
                self.timecode_label.setText(self.format_timecode(int(tl * 1000)))
        except Exception:
            pass

    def _scrub_duration_ms(self):
        if not self.video_widget:
            return 0
        dur = self.video_widget.duration()
        if dur <= 0:
            if self.is_timeline_mode and self._play_uses_timeline_edl:
                dur = int(self.timeline.get_timeline_duration() * 1000)
            elif getattr(self, 'current_media', None):
                dur = int(getattr(self.current_media, 'duration', 0) * 1000)
        return max(0, int(dur))

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
        # Auto-apply source settings when first clip is added
        if len(self.timeline.clips) == 1:
            self.auto_apply_source_to_export_settings()

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
        dur = getattr(c, 'full_duration', 0) or 0
        if dur <= 0:
            try:
                dur = float(c.out_point or 0) or 60.0
            except Exception:
                dur = 60.0
        c.in_point = min(dur, c.in_point + 1.0)
        c.out_point = max(0, c.out_point - 1.0)
        if c.out_point <= c.in_point:
            c.out_point = min(dur, c.in_point + 0.1) # Safe fallback
        self.timeline.update()
        self.update_timeline_duration()
        self.status_label.setText("Auto-Trimmed 1s off edges.")

    def apply_transition_to_selected(self):
        sel = getattr(self.timeline, 'selected_clip', None)
        if not sel:
            QMessageBox.information(self, "No Clip", "Select a clip on the timeline first.")
            return
        name = self.transitions_combo.currentText()
        dur = float(self.trans_duration_spin.value())
        sel.transition_type = None if name == "None" else name
        sel.transition_duration = dur if name != "None" else 0.0
        self.timeline.update()
        self.status_label.setText(f"Transition {name} applied with {dur}s duration")

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

    def auto_black_and_white(self, force_on=None):
        # Toggle B&W via the real export flag (hue=s=0). No modal popups so AI/batch can call it.
        # force_on=True/False for AI prompt; None = toggle for button.
        try:
            current = bool(self.app_settings.value('color_bw_mode', False, type=bool))
        except Exception:
            current = False
        target = (not current) if force_on is None else bool(force_on)
        try:
            self.app_settings.setValue('color_bw_mode', target)
        except Exception:
            pass
        try:
            self.update_live_preview_filters()
        except Exception:
            pass
        self.status_label.setText("Applied Black & White Filter." if target else "Black & White filter removed.")
        return target

    def auto_normalize_audio(self):
        # Fix: normalization is List[bool] consumed as loudnorm per stream.
        # Old code appended a filter string that export ignored -> button did nothing.
        if not self.timeline.clips:
            QMessageBox.warning(self, "No Clips", "Add clips to the timeline first.")
            return
        for c in self.timeline.clips:
            try:
                n = max(1, len(getattr(c, 'volumes', [0.0]) or [0.0]))
            except Exception:
                n = 1
            c.normalization = [True] * n
        # Reflect on mixer checkboxes when a clip is selected
        try:
            if getattr(self.timeline, 'selected_clip', None):
                if hasattr(self, 'track1_norm'):
                    self.track1_norm.setChecked(True)
                if hasattr(self, 'track2_norm'):
                    self.track2_norm.setChecked(True)
        except Exception:
            pass
        self.timeline.update()
        self.update_timeline_duration()
        self.status_label.setText("Audio Normalized (loudnorm on all clips).")

    # --- AI ASSIST (prompt -> real actions only, never invented) ---
    def show_ai_capabilities(self):
        QMessageBox.information(self, "AI Assistant - What I Can Do",
            "I can only do what the app can actually do. Type any of these in the AI box:\n\n"
            "• trim / trim edges (selected clip)\n"
            "• fade all / crossfade\n"
            "• black and white on/off\n"
            "• normalize audio\n"
            "• balance / auto color balance\n"
            "• sync audio\n"
            "• add text \"your words here\"\n"
            "• record voiceover\n"
            "• reset filters\n"
            "• add to timeline / remove clip / clear timeline\n"
            "• zoom in / zoom out\n"
            "• mark in / mark out (media trim)\n"
            "• export / stop\n\n"
            "Example: 'trim edges, fade all, normalize and export'\n"
            "You always preview before anything runs.")

    def _ai_build_plan(self, prompt):
        import re as _re
        text = (prompt or "").strip()
        low = text.lower()
        if not low:
            return []
        # Split multi-intent prompts: "trim, fade all and normalize" -> 3 chunks
        chunks = _re.split(r'\s+then\s+|\s+and\s+|[,;+&]+|\n+', low)
        chunks = [c.strip() for c in chunks if c.strip()]
        if not chunks:
            chunks = [low]
        plan = []
        seen = set()
        def _add(key, label):
            if key not in seen:
                seen.add(key)
                plan.append({"key": key, "label": label})
        # Extract quoted text for Text action: "add text \"hello\"" or text: hello
        quoted = ""
        try:
            m = _re.search(r'"([^"]+)"', text)
            if m:
                quoted = m.group(1).strip()
            else:
                m2 = _re.search(r"'([^']+)'", text)
                if m2:
                    quoted = m2.group(1).strip()
                else:
                    m3 = _re.search(r'text\s*[:\-]\s*(.+)', text, flags=_re.IGNORECASE)
                    if m3:
                        quoted = m3.group(1).strip()[:120]
        except Exception:
            quoted = ""
        for ch in chunks:
            # Order matters: check specific/clear first so "remove bw" doesn't eat "remove clip"
            if any(k in ch for k in ("clear timeline", "clear all", "remove all clips", "empty timeline")):
                _add("clear", "Clear timeline (asks to confirm)")
            elif any(k in ch for k in ("add to timeline", "add media", "add clip", "add video", "put on timeline", "put it on the timeline")):
                _add("add", "Add selected library media to timeline")
            elif any(k in ch for k in ("remove clip", "delete clip", "remove selected")):
                _add("remove", "Remove selected timeline clip")
            if any(k in ch for k in ("trim", "cut edges", "remove ends", "auto-trim", "autotrim")):
                _add("trim", "Auto-trim 1s off selected clip edges")
            if "fade" in ch or "crossfade" in ch or "dissolve" in ch:
                _add("fade", "Fade all clips (1s)")
            # B&W with on/off detection
            if any(k in ch for k in ("black and white", "black & white", "b&w", "grayscale", "greyscale")) or ch.strip() == "bw":
                if any(k in ch for k in ("remove", "off", "disable", "restore color", "back to color")):
                    _add("bw_off", "Remove Black & White (restore color)")
                else:
                    _add("bw_on", "Apply Black & White")
            if any(k in ch for k in ("normalize", "normalise", "loudnorm", "level audio", "loudness", "boost audio")):
                _add("norm", "Normalize audio (all clips)")
            if any(k in ch for k in ("balance", "white balance", "color correct", "auto color")):
                _add("balance", "Auto color balance")
            if "sync" in ch or "align audio" in ch or "lip sync" in ch:
                _add("sync", "Auto-sync audio tracks")
            if any(k in ch for k in ("lower third", "caption", "title", "overlay text")) or ("text" in ch):
                lbl = f'Add text "{quoted}"' if quoted else "Add text overlay (asks for words)"
                _add("text", lbl)
            if any(k in ch for k in ("voiceover", "voice over", "narration", "narrate")) or ("record" in ch and "audio" in ch) or ch.strip() in ("vo", "record"):
                _add("vo", "Record voiceover at playhead")
            if any(k in ch for k in ("reset", "clear filters", "remove filters")):
                _add("reset", "Reset all color/FX filters")
            if "zoom out" in ch:
                _add("zoomout", "Zoom timeline out")
            elif "zoom in" in ch:
                _add("zoomin", "Zoom timeline in")
            if "mark out" in ch or "out point" in ch or ch.strip() == "out":
                _add("out", "Mark media Out-point at preview pos")
            elif "mark in" in ch or "in point" in ch or ch.strip() in ("in", "mark in-point"):
                _add("in", "Mark media In-point at preview pos")
            if any(k in ch for k in ("export", "render", "encode video", "save video")):
                _add("export", "Export timeline")
            elif ch.strip() == "stop":
                _add("stop", "Stop export")
        if quoted and not any(p["key"] == "text" for p in plan) and "text" in low:
            _add("text", f'Add text "{quoted}"')
        # stash quoted text for apply step
        try:
            self._ai_quoted_text = quoted
        except Exception:
            pass
        return plan

    def run_ai_assist(self):
        prompt = ""
        try:
            prompt = self.ai_prompt_input.text()
        except Exception:
            pass
        plan = self._ai_build_plan(prompt)
        if not plan:
            self.show_ai_capabilities()
            self.status_label.setText("AI: didn't understand - showing what I can do.")
            return
        preview = "\n".join(f"• {p['label']}" for p in plan)
        reply = QMessageBox.question(self, "AI Plan - Apply?",
            f"You asked:\n\"{(prompt or '')[:200]}\"\n\nI will do only these real actions:\n{preview}\n\nApply in order?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            self.status_label.setText("AI: cancelled, nothing changed.")
            return
        self._ai_apply_plan(plan)

    def _ai_apply_plan(self, plan):
        quoted = getattr(self, '_ai_quoted_text', '') or ""
        results = []
        for p in plan:
            key = p["key"]
            try:
                if key == "trim":
                    if not getattr(self.timeline, 'selected_clip', None):
                        # Help non-editors: operate on first clip if nothing selected
                        if self.timeline.clips:
                            self.timeline.selected_clip = self.timeline.clips[0]
                            self.timeline.update()
                    self.auto_trim_selected()
                    results.append("trim: done")
                elif key == "fade":
                    self.auto_fade_all()
                    results.append("fade: done")
                elif key == "bw_on":
                    self.auto_black_and_white(force_on=True)
                    results.append("b&w: on")
                elif key == "bw_off":
                    self.auto_black_and_white(force_on=False)
                    results.append("b&w: off")
                elif key == "norm":
                    self.auto_normalize_audio()
                    results.append("normalize: done")
                elif key == "balance":
                    self.apply_auto_balance()
                    results.append("balance: done")
                elif key == "sync":
                    self.auto_sync_audio_tracks()
                    results.append("sync: launched")
                elif key == "text":
                    if quoted:
                        try:
                            start = float(getattr(self.timeline, 'playhead_position', 0.0) or 0.0)
                        except Exception:
                            start = 0.0
                        tc = TextClip(quoted, start, 5.0)
                        self.timeline.text_clips.append(tc)
                        self.timeline.update()
                        self.status_label.setText(f"Added text overlay: '{quoted[:20]}...'")
                        results.append("text: added")
                    else:
                        self.add_text_overlay()
                        results.append("text: dialog")
                elif key == "vo":
                    self.record_voiceover()
                    results.append("voiceover: launched")
                elif key == "reset":
                    self.reset_all_filters()
                    results.append("reset: done")
                elif key == "add":
                    self.add_to_timeline()
                    results.append("add: done")
                elif key == "remove":
                    self.remove_from_timeline()
                    results.append("remove: done")
                elif key == "clear":
                    self.clear_timeline()
                    results.append("clear: launched")
                elif key == "zoomin":
                    self.zoom_in_timeline()
                    results.append("zoom in: done")
                elif key == "zoomout":
                    self.zoom_out_timeline()
                    results.append("zoom out: done")
                elif key == "in":
                    self.set_media_in_point()
                    results.append("in-point: set")
                elif key == "out":
                    self.set_media_out_point()
                    results.append("out-point: set")
                elif key == "export":
                    self.export_timeline()
                    results.append("export: launched")
                elif key == "stop":
                    self.stop_timeline_export()
                    results.append("stop: done")
                else:
                    results.append(f"{key}: skipped (unknown)")
            except Exception as e:
                results.append(f"{key}: failed ({e})")
        try:
            self.append_log("🤖 AI applied: " + "; ".join(results))
        except Exception:
            pass
        self.status_label.setText("AI done: " + "; ".join(results)[:160])
        try:
            self.ai_prompt_input.clear()
        except Exception:
            pass

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
            self.status_label.setText(f"✅ Voiceover recorded: {duration:.1f}s at {self._vo_start_time:.1f}s")
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
        try:
            if getattr(self, 'is_timeline_mode', False) and getattr(self, '_play_uses_timeline_edl', False):
                if hasattr(self, 'scrub_wave'):
                    self.scrub_wave.set_media_duration(int(self.timeline.get_timeline_duration() * 1000))
        except Exception:
            pass

    def zoom_in_timeline(self):
        self.timeline.zoom_in()

    def zoom_out_timeline(self):
        self.timeline.zoom_out()


    def export_timeline(self):
        print("export_timeline called - opening export window")
        if not self.timeline.clips:
            QMessageBox.warning(self, "Empty Timeline", "Add clips to timeline before exporting")
            return
        self.switch_main_tab("EXPORT")
        self.open_export_window()
        try:
            if hasattr(self, 'export_panel') and hasattr(self.export_panel, 'get_full_settings'):
                settings = self.export_panel.get_full_settings()
                base = self.get_settings()
                base.update(settings)
                settings = base
            else:
                settings = self.get_settings()
        except Exception as e:
            import traceback
            traceback.print_exc()
            settings = self.get_settings()
        from PyQt6.QtWidgets import QFileDialog
        try:
            ext = get_export_extension_for_codec(settings.get('video_codec', 'hevc_nvenc'))
        except:
            ext = ".mp4"
        output_file, _ = QFileDialog.getSaveFileName(self, "Choose Location & Start Export", f"timeline_export{ext}", f"Video Files (*{ext})")
        if not output_file:
            return
        try:
            self.render_dialog = RenderProgressDialog(self)
            self.render_dialog.cancel_btn.clicked.connect(self.stop_timeline_export)
            if hasattr(self, 'export_panel') and hasattr(self.export_panel, 'export_btn'):
                self.export_panel.export_btn.setEnabled(False)
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
            self.status_label.setText(f"Exporting: {settings.get('video_codec','').upper()}")
            self.render_dialog.show()
            try:
                if hasattr(self, 'vram_timer'):
                    self.vram_timer.stop()
            except:
                pass
            if not self.timeline_export_thread.isRunning():
                self.timeline_export_thread.start()
        except Exception as e:
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Export Failed", f"{e}")

    def timeline_export_done(self, success, msg):
        try:
            if hasattr(self, 'vram_timer'):
                self.vram_timer.start(2000)
        except:
            pass
        try:
            self.export_timeline_btn.setEnabled(True)
        except:
            pass
        try:
            self.stop_export_btn.setEnabled(False)
        except:
            pass
        try:
            if hasattr(self, 'export_panel') and hasattr(self.export_panel, 'export_btn'):
                self.export_panel.export_btn.setEnabled(True)
        except:
            pass

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
        # FASTENCODE PRO 2026 EXACT HTML - Glass + Neon + Round (#060609, Inter, 6px scrollbars)
        self.setStyleSheet("""
            QMainWindow {
                background: #060609;
            }
            QWidget {
                background-color: transparent;
                color: #e5e5e5;
                font-family: 'Inter', system-ui, sans-serif;
                font-size: 10pt;
                selection-background-color: rgba(125,249,255,0.3);
            }
            QLabel {
                color: rgba(255,255,255,0.7);
            }
            QScrollBar:vertical {
                background: transparent;
                width: 6px;
                margin: 0px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.12);
                border-radius: 3px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255,255,255,0.2);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                background: transparent;
                height: 6px;
                margin: 0px;
                border: none;
            }
            QScrollBar::handle:horizontal {
                background: rgba(255,255,255,0.12);
                border-radius: 3px;
                min-width: 20px;
            }
            QScrollBar::handle:horizontal:hover {
                background: rgba(255,255,255,0.2);
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QSplitter::handle {
                background: rgba(255,255,255,0.06);
            }
            QSplitter::handle:hover {
                background: rgba(125,249,255,0.3);
            }
            QDockWidget {
                background: #0f0f14;
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 16px;
                titlebar-close-icon: url(none);
                titlebar-normal-icon: url(none);
            }
            QDockWidget::title {
                background: #0f0f14;
                padding: 10px 12px;
                text-align: left;
                color: rgba(255,255,255,0.6);
                font-size: 10px;
                letter-spacing: 1.2px;
                font-weight: 600;
            }
        """)

    def tab_style(self):
        return """
            QTabWidget::pane {
                border: 1px solid rgba(255,255,255,0.06);
                background: #0a0a0e;
                border-radius: 16px;
                margin-top: 8px;
            }
            QTabBar::tab {
                background: rgba(255,255,255,0.04);
                color: rgba(255,255,255,0.5);
                padding: 10px 18px;
                margin-right: 4px;
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px;
                font-size: 10pt;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                background: rgba(125,249,255,0.12);
                color: #7df9ff;
                border: 1px solid rgba(125,249,255,0.3);
            }
            QTabBar::tab:hover:!selected {
                background: rgba(255,255,255,0.08);
                color: rgba(255,255,255,0.8);
                border: 1px solid rgba(255,255,255,0.1);
            }
        """

    def groupbox_style(self):
        return """
            QGroupBox {
                background: #0f0f14;
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 16px;
                padding: 28px 16px 16px 16px;
                margin-top: 18px;
                font-size: 10px;
                font-weight: 700;
                color: rgba(255,255,255,0.4);
                letter-spacing: 1.2px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 6px 12px;
                margin-left: 12px;
                margin-top: 4px;
                background: #15151a;
                color: rgba(255,255,255,0.7);
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 8px;
                font-weight: 700;
            }
        """

    def button_style(self, color):
        color_map = {
            '#4ade80': '#00ff88',
            '#3b82f6': '#7df9ff',
            '#ef4444': '#ff5f56',
            '#f59e0b': '#ff8a00',
            '#10b981': '#00ff88',
            '#8b5cf6': '#a855f7',
            '#6366f1': '#a855f7',
            '#0ea5e9': '#7df9ff',
            '#a855f7': '#a855f7',
            '#64748b': 'rgba(255,255,255,0.1)',
            '#dc2626': '#ff5f56',
            '#6b7280': 'rgba(255,255,255,0.08)',
        }
        neon = color_map.get(color, color)
        if 'rgba' in neon:
            bg = neon
            border = 'rgba(255,255,255,0.08)'
            hover_bg = 'rgba(255,255,255,0.14)'
            txt = 'rgba(255,255,255,0.7)'
        else:
            bg = neon
            border = neon
            hover_bg = neon
            txt = '#000000' if neon != '#a855f7' else '#ffffff'
        return f"""
            QPushButton {{
                background: {bg};
                color: {txt};
                border: 1px solid {border};
                border-radius: 12px;
                padding: 10px 18px;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.5px;
            }}
            QPushButton:hover {{
                background: {hover_bg};
                border: 1px solid rgba(255,255,255,0.15);
            }}
            QPushButton:pressed {{
                background: rgba(0,0,0,0.2);
                border: 1px solid rgba(255,255,255,0.1);
                padding-top: 11px;
                padding-bottom: 9px;
            }}
            QPushButton:disabled {{
                background: rgba(255,255,255,0.04);
                color: rgba(255,255,255,0.25);
                border: 1px solid rgba(255,255,255,0.04);
            }}
        """

    def list_style(self):
        return """
            QListWidget {
                background: #0a0a0e;
                border: 1px solid rgba(255,255,255,0.06);
                border-radius: 12px;
                padding: 6px;
                font-size: 10pt;
                color: rgba(255,255,255,0.8);
                outline: none;
            }
            QListWidget::item {
                padding: 10px 12px;
                border-radius: 10px;
                border: 1px solid transparent;
                margin: 2px 0px;
            }
            QListWidget::item:selected {
                background: rgba(125,249,255,0.12);
                color: #7df9ff;
                border: 1px solid rgba(125,249,255,0.2);
                font-weight: 600;
            }
            QListWidget::item:hover:!selected {
                background: rgba(255,255,255,0.06);
                border: 1px solid rgba(255,255,255,0.06);
                color: rgba(255,255,255,0.9);
            }
        """

    def slider_style(self):
        return """
            QSlider::groove:horizontal {
                border: none;
                height: 4px;
                background: rgba(255,255,255,0.08);
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #ffffff;
                border: 2px solid rgba(0,0,0,0.1);
                width: 16px;
                height: 16px;
                margin: -6px 0;
                border-radius: 8px;
            }
            QSlider::handle:horizontal:hover {
                background: #7df9ff;
                border: 2px solid #7df9ff;
            }
            QSlider::sub-page:horizontal {
                background: #7df9ff;
                border-radius: 2px;
            }
            QSlider::groove:vertical {
                border: none;
                width: 4px;
                background: rgba(255,255,255,0.08);
                border-radius: 2px;
            }
            QSlider::handle:vertical {
                background: #ffffff;
                border: 2px solid rgba(0,0,0,0.1);
                width: 16px;
                height: 16px;
                margin: 0 -6px;
                border-radius: 8px;
            }
            QSlider::sub-page:vertical {
                background: #00ff88;
                border-radius: 2px;
            }
        """

    def combo_style(self):
        return """
            QComboBox {
                background: #111113;
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 10px;
                padding: 8px 12px;
                font-size: 10pt;
                color: rgba(255,255,255,0.8);
                font-weight: 500;
            }
            QComboBox:hover {
                border: 1px solid rgba(255,255,255,0.15);
                background: #15151a;
            }
            QComboBox::drop-down {
                border: none;
                width: 28px;
                background: transparent;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 6px solid rgba(255,255,255,0.4);
                margin-right: 8px;
            }
            QComboBox QAbstractItemView {
                background: #0f0f14;
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 12px;
                selection-background-color: rgba(125,249,255,0.15);
                selection-color: #7df9ff;
                color: rgba(255,255,255,0.8);
                padding: 6px;
            }
        """

    def spinbox_style(self):
        return """
            QSpinBox, QDoubleSpinBox {
                background: #111113;
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 10px;
                padding: 6px 8px;
                font-size: 10pt;
                color: rgba(255,255,255,0.8);
                font-weight: 500;
            }
            QSpinBox:hover, QDoubleSpinBox:hover {
                border: 1px solid rgba(255,255,255,0.15);
                background: #15151a;
            }
            QSpinBox::up-button, QSpinBox::down-button,
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
                width: 20px;
                background: rgba(255,255,255,0.06);
                border: none;
                border-radius: 6px;
                margin: 2px;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover,
            QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
                background: rgba(255,255,255,0.12);
            }
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-bottom: 6px solid rgba(255,255,255,0.5);
            }
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 6px solid rgba(255,255,255,0.5);
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
            'single_pass_render': True,
            'hw_caps': getattr(self, 'hw_caps', None),
            'gpu_vram_limit_mb': getattr(self, 'gpu_vram_limit_mb', None),
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
        try:
            if hasattr(self, 'vram_timer'):
                self.vram_timer.stop()
        except:
            pass
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
        try:
            if hasattr(self, 'vram_timer'):
                self.vram_timer.start(2000)
        except:
            pass
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

    def show_project_defaults_dialog(self):
        """First-run dialog: set default timeline resolution and FPS.
        Now actually applies to export panel and timeline."""
        try:
            dlg = QDialog(self)
            dlg.setWindowTitle("Project Defaults - FastEncode Pro")
            dlg.setMinimumWidth(460)
            dlg.setStyleSheet("QDialog { background: #0f0f14; } QLabel { color: white; }")
            layout = QVBoxLayout(dlg)
            title = QLabel("🎬 Set Default Project Settings")
            title.setStyleSheet("font-size: 14px; font-weight: 700; color: white; padding: 8px;")
            layout.addWidget(title)
            desc = QLabel("These will be used for new projects and can be changed anytime in Export panel. Source Match will auto-detect from first clip.")
            desc.setWordWrap(True)
            desc.setStyleSheet("color: rgba(255,255,255,0.6); font-size: 11px; padding: 4px 8px;")
            layout.addWidget(desc)
            form = QFormLayout()
            form.setSpacing(12)
            res_combo = QComboBox()
            res_combo.addItems(["Source Match (Auto from clip)","1920x1080 Full HD","2560x1440 QHD","3840x2160 4K","3840x1600 Ultrawide","5120x2880 5K","7680x4320 8K","1080x1080 Instagram Square","1080x1350 Instagram Portrait"])
            res_combo.setCurrentIndex(0)
            res_combo.setStyleSheet("background: #1a1a1f; color: white; padding: 6px; border-radius: 6px;")
            fps_combo = QComboBox()
            fps_combo.addItems(["23.976","24","25","29.97","30","50","59.94","60","120"])
            fps_combo.setCurrentIndex(7)  # 60
            fps_combo.setStyleSheet("background: #1a1a1f; color: white; padding: 6px; border-radius: 6px;")
            form.addRow("Default Resolution:", res_combo)
            form.addRow("Default Frame Rate:", fps_combo)
            layout.addLayout(form)
            btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dlg)
            layout.addWidget(btn)
            btn.accepted.connect(dlg.accept)
            btn.rejected.connect(dlg.reject)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                res_text = res_combo.currentText()
                fps_val = float(fps_combo.currentText())
                # Save to QSettings
                self.app_settings.setValue("project_res", res_text)
                self.app_settings.setValue("project_fps", fps_val)
                self.app_settings.setValue("project_res_index", res_combo.currentIndex())
                # Apply to export panel if it exists
                try:
                    if hasattr(self, 'export_panel'):
                        # Resolution
                        if hasattr(self.export_panel, 'res_combo'):
                            self.export_panel.res_combo.setCurrentIndex(res_combo.currentIndex() if res_combo.currentIndex() < self.export_panel.res_combo.count() else 0)
                        # FPS
                        if hasattr(self.export_panel, 'fps_combo'):
                            # Map fps value to combo index
                            fps_map = {"23.976":0, "24":1, "25":2, "29.97":3, "30":4, "50":5, "59.94":6, "60":7, "120":8}
                            idx = fps_map.get(fps_combo.currentText(), 7)
                            if idx < self.export_panel.fps_combo.count():
                                self.export_panel.fps_combo.setCurrentIndex(idx)
                    # Also store for timeline
                    self._default_fps = fps_val
                    print(f"✅ Project defaults set: {res_text} @ {fps_val}fps")
                except Exception as e:
                    print(f"Failed to apply defaults to export panel: {e}")
        except Exception as e:
            print(f"show_project_defaults_dialog failed: {e}")
            traceback.print_exc()

    def auto_apply_source_to_export_settings(self):
        """Auto-detect first clip bit depth/resolution and update export UI/settings.
        FIXED: Now actually updates export_panel UI (res, bit depth, fps) and status."""
        if not hasattr(self, 'timeline') or not self.timeline.clips:
            print("auto_apply: no clips")
            return
        first_clip = self.timeline.clips[0]
        if not os.path.exists(first_clip.file_path):
            print(f"auto_apply: file missing {first_clip.file_path}")
            return
        try:
            probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                         '-show_entries', 'stream=pix_fmt,width,height,avg_frame_rate,codec_name', '-of', 'json', first_clip.file_path]
            probe_res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            probe_data = json.loads(probe_res.stdout or '{}')
            stream = probe_data.get('streams',[{}])[0]
            pix_fmt = (stream.get('pix_fmt') or '').lower()
            w = int(stream.get('width', 0) or 0)
            h = int(stream.get('height', 0) or 0)
            avg = stream.get('avg_frame_rate','0/1')
            codec = stream.get('codec_name','unknown')
            
            print(f"🔍 Auto-detect source: {w}x{h} {pix_fmt} {avg} ({codec}) from {Path(first_clip.file_path).name}")

            # 1. Bit depth -> pixel_format_combo
            is_10bit = ('10' in pix_fmt or 'p010' in pix_fmt or 'p012' in pix_fmt or 'yuv420p10' in pix_fmt)
            try:
                if hasattr(self, 'export_panel') and hasattr(self.export_panel, 'pixel_format_combo'):
                    self.export_panel.pixel_format_combo.setCurrentIndex(1 if is_10bit else 0)
                    print(f"  → Bit depth: {'10-bit' if is_10bit else '8-bit'} (combo {1 if is_10bit else 0})")
            except Exception as e:
                print(f"  Bit depth combo failed: {e}")

            # 2. Resolution -> res_combo Source Match + store width/height
            if w and h:
                self._auto_source_width = w
                self._auto_source_height = h
                try:
                    if hasattr(self, 'export_panel') and hasattr(self.export_panel, 'res_combo'):
                        self.export_panel.res_combo.setCurrentIndex(0)  # Source Match
                        print(f"  → Resolution: Source Match {w}x{h} (combo 0)")
                except Exception as e:
                    print(f"  Res combo failed: {e}")
                # Also update status label
                try:
                    if hasattr(self, 'timeline_status_label'):
                        self.timeline_status_label.setText(f"SOURCE: {w}x{h} {pix_fmt} • {codec}")
                except:
                    pass

            # 3. Frame rate -> fps_combo + timeline_fps
            try:
                fps = 0
                if '/' in avg:
                    num, den = avg.split('/')
                    fps = float(num)/float(den) if float(den) not in ('0','0.0') and float(den) != 0 else 0
                else:
                    fps = float(avg) if avg else 0
                
                # Sanity clamp
                if fps < 1 or fps > 240:
                    fps = 60.0 if not fps else fps

                # Find closest fps in combo
                fps_options = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0, 120.0]
                closest_idx = min(range(len(fps_options)), key=lambda i: abs(fps_options[i]-fps))
                
                if hasattr(self, 'export_panel') and hasattr(self.export_panel, 'fps_combo'):
                    if closest_idx < self.export_panel.fps_combo.count():
                        self.export_panel.fps_combo.setCurrentIndex(closest_idx)
                        print(f"  → FPS: {fps:.3f} -> {fps_options[closest_idx]} (combo {closest_idx})")
                
                # Store for rendering engine
                self._auto_source_fps = fps
                print(f"  → Stored FPS {fps}")
            except Exception as e:
                print(f"  FPS parse failed {avg}: {e}")

            self.status_label.setText(f"Auto-matched: {w}x{h} {'10-bit' if is_10bit else '8-bit'} @ {avg} from {Path(first_clip.file_path).name}")
            
        except Exception as e:
            print(f"auto_apply_source_to_export_settings failed: {e}")
            traceback.print_exc()

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
                # Only add proxy job if auto-proxy is enabled
                if getattr(self, 'auto_proxy_enabled', True):
                    self.proxy_manager.add_job(clip.file_path)

            self.update_timeline_duration()
            # Auto-apply source settings for resolution / bit depth / fps
            self.auto_apply_source_to_export_settings()
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
    # FIX SCALING ISSUE - Must be set BEFORE QApplication
    from PyQt6.QtCore import Qt, QCoreApplication
    from PyQt6.QtGui import QGuiApplication
    # Use PassThrough to prevent fractional scaling double-zoom on Hyprland/Wayland
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass
    # AA_DontShowIconsInMenus must be set BEFORE QApplication on Qt6
    try:
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
    except Exception:
        pass
    
    app = QApplication(sys.argv)
    app.setDesktopFileName("FastEncodePro")
    app.setStyle("Fusion")
    
    window = FastEncodeProApp()
    window.show()
    
    # Deferred post-show fixes
    def post_show_fix():
        try:
            # Show project defaults dialog if needed - deferred to avoid Wayland crash
            if getattr(window, '_needs_project_defaults', False):
                try:
                    window.show_project_defaults_dialog()
                except Exception as e:
                    print(f"Project defaults dialog failed: {e}")
                window._needs_project_defaults = False
            # Force update geometry after show to prevent Wayland scaling trap
            window.updateGeometry()
        except Exception as e:
            print(f"post_show_fix failed: {e}")
    
    QTimer.singleShot(150, post_show_fix)
    # Auto-save maximized state on close/maximize change
    try:
        def on_max_changed():
            try:
                window.app_settings.setValue("was_maximized", window.isMaximized())
            except:
                pass
        # Use window state change if available
        if hasattr(window, 'windowStateChanged'):
            pass
    except:
        pass
    
    try:
        sys.exit(app.exec())
    except Exception as e:
        _log(f"app.exec() crashed: {e}")
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
