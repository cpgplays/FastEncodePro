#!/usr/bin/env python3
"""
FastEncode Pro - Timeline Edition v0.9.0
GPU-Accelerated Video Editor with Native Wayland MPV Support

v0.9.0 Features:
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
print("✅ Locale set to C for MPV")

import sys
import shutil
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
    print("✅ python-mpv available")
except (ImportError, OSError) as _mpv_err:
    print(f"⚠️  python-mpv / libmpv unavailable: {_mpv_err}")
    print("   Dev: pip install mpv   |   EXE: hidden-import=mpv + bundle libmpv-2.dll next to the app / in _MEIPASS.")
except Exception as _mpv_err:
    print(f"⚠️  python-mpv failed to load: {_mpv_err}")

from PyQt6.QtWidgets import *
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSettings, QUrl, QPointF, QTimer, QEvent, QPoint, QRectF, QObject, QSize
from PyQt6.QtGui import QFont, QPalette, QColor, QPainter, QBrush, QPen, QCursor, QAction, QPainterPath, QMouseEvent, QImage, QPixmap

__version__ = "0.9.0"
__author__ = "cpgplays"

# --- SHARED CONSTANTS ---

SUBPROCESS_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0

TIMELINE_FPS_VALUES = [23.976, 24, 25, 29.97, 30, 50, 60, 120]
TIMELINE_FPS_LABELS = ["23.976", "24", "25", "29.97", "30", "50", "60", "120"]

# Index 0 means "Source" (keep the resolution of the first clip).
EXPORT_RESOLUTIONS = [None, (1920, 1080), (2560, 1440), (3840, 2160), (5120, 2880), (7680, 4320)]
EXPORT_RESOLUTION_LABELS = ["Source"] + [f"{w}x{h}" for w, h in EXPORT_RESOLUTIONS[1:]]

PRORES_PROFILE_NAMES = ["Proxy", "LT", "Standard", "HQ", "4444", "4444 XQ"]
SCALE_ALGOS = ['bilinear', 'bicubic', 'lanczos', 'spline']
SCALE_ALGO_LABELS = ["Bilinear", "Bicubic", "Lanczos", "Spline"]

DENOISE_FILTERS = ['', 'hqdn3d=1.5:1.5:6:6', 'hqdn3d=2:2:8:8', 'hqdn3d=3:3:10:10',
                   'hqdn3d=4:4:12:12', 'hqdn3d=6:6:15:15', 'hqdn3d=8:8:18:18']
DEFLICKER_FILTERS = ['', 'deflicker=mode=pm:size=5', 'deflicker=mode=pm:size=10',
                     'deflicker=mode=pm:size=15', 'deflicker=mode=am:size=20', 'deflicker=mode=am:size=30']
EXPOSURE_FILTERS = {
    1: 'eq=brightness=0.05:saturation=1.1',   2: 'eq=brightness=0.1:saturation=1.15',
    3: 'eq=brightness=0.15:saturation=1.2',   4: 'eq=brightness=0.2:saturation=1.25',
    5: 'eq=brightness=0.3:saturation=1.3',    6: 'eq=brightness=0.4:saturation=1.35',
    7: 'eq=brightness=-0.05:saturation=0.95', 8: 'eq=brightness=-0.1:saturation=0.9',
    9: 'eq=brightness=-0.15:saturation=0.85', 10: 'eq=brightness=-0.2:saturation=0.8',
    11: 'eq=brightness=-0.3:saturation=0.75', 12: 'eq=brightness=-0.4:saturation=0.7',
}
TEMPORAL_FILTERS = ['', 'tmix=frames=3:weights="1 1 1"', 'tmix=frames=5:weights="1 1 2 1 1"',
                    'tmix=frames=7:weights="1 1 2 2 2 1 1"', 'tmix=frames=9:weights="1 1 2 3 3 3 2 1 1"',
                    'tmix=frames=11:weights="1 2 2 3 4 4 4 3 2 2 1"']
SHARPNESS_FILTERS = ['', 'unsharp=3:3:0.3:3:3:0', 'unsharp=5:5:0.5:5:5:0', 'unsharp=5:5:0.8:5:5:0.4',
                     'unsharp=5:5:1.2:5:5:0.6', 'unsharp=7:7:1.5:7:7:0.8', 'unsharp=7:7:2.0:7:7:1.0']

FFMPEG_BASE_ARGS = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-stats_period', '0.5']

# --- SUBPROCESS / FFPROBE HELPERS ---

def run_process(cmd, timeout=None, text=True, check=False):
    """Run ``cmd`` capturing output, without spawning a console window on Windows."""
    return subprocess.run(cmd, capture_output=True, text=text, timeout=timeout,
                          check=check, creationflags=SUBPROCESS_NO_WINDOW)


def start_streaming_process(cmd, bufsize=-1):
    """Start ``cmd`` with a line-readable stderr pipe used for FFmpeg progress parsing."""
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            universal_newlines=True, bufsize=bufsize,
                            creationflags=SUBPROCESS_NO_WINDOW)


def probe_media_duration(file_path, default=0.0):
    try:
        result = run_process(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'default=noprint_wrappers=1:nokey=1', file_path])
        return float(result.stdout.strip())
    except Exception:
        return default


def probe_video_stream(file_path, entries):
    """Return the first video stream as a dict of the requested ``entries``."""
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
           '-show_entries', f'stream={entries}', '-of', 'json', file_path]
    result = run_process(cmd, timeout=5)
    return json.loads(result.stdout)['streams'][0]


def probe_audio_stream_indices(file_path):
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index',
           '-of', 'csv=p=0', file_path]
    result = run_process(cmd, timeout=5, check=True)
    return [int(x) for x in result.stdout.strip().splitlines() if x.strip()]


# --- HELPER FUNCTIONS ---

def get_audio_stream_count_static(filepath):
    try:
        return len(probe_audio_stream_indices(filepath))
    except Exception:
        return 1


def get_export_target_labels():
    return [
        "Master / color grading (NVENC P7)",
        "YouTube - long form (NVENC P5, same CBR)",
        "YouTube Shorts (NVENC P5, same CBR)",
        "TikTok / Reels / Shorts vertical (NVENC P5, same CBR)",
        "Instagram - feed / square (NVENC P5, same CBR)",
        "X (Twitter) / General social (NVENC P5, same CBR)",
    ]


def get_nvenc_preset_for_target(export_target_index):
    return "p7" if export_target_index == 0 else "p5"


def build_nvenc_cbr_args(settings, fps_value=None):
    bitrate_kbps = int(settings.get('bitrate_mbps', 100) * 1000)
    pixel_format = settings.get('pixel_format', 0)
    pix_fmt = 'yuv420p' if pixel_format == 0 else 'p010le'
    export_target_index = settings.get('export_target_index', 0)
    preset = get_nvenc_preset_for_target(export_target_index)
    gop = str(int((fps_value or 30) * 2))

    return [
        '-preset', preset, '-tune', 'hq', '-rc', 'cbr',
        '-b:v', f'{bitrate_kbps}k', '-maxrate', f'{bitrate_kbps}k',
        '-bufsize', f'{int(bitrate_kbps * 2)}k',
        '-g', gop, '-bf', '3', '-b_ref_mode', 'middle',
        '-pix_fmt', pix_fmt,
    ]


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


def build_hw_decode_input_args(file_path, codec_name, use_gpu_decode):
    if not use_gpu_decode:
        return ['-i', file_path]
    return ['-hwaccel', 'cuda', '-i', file_path]


def has_optional_video_filters(settings):
    return any(
        settings.get(key, 0) > 0
        for key in ('denoise_level', 'deflicker_level', 'exposure_level', 'temporal_level', 'sharpness_level')
    )


def build_video_filter_chain(settings):
    """Optional codec-tab filters, in the order they are applied to the video."""
    filters = []
    for key, table in (('denoise_level', DENOISE_FILTERS),
                       ('deflicker_level', DEFLICKER_FILTERS),
                       ('exposure_level', EXPOSURE_FILTERS),
                       ('temporal_level', TEMPORAL_FILTERS),
                       ('sharpness_level', SHARPNESS_FILTERS)):
        level = settings.get(key, 0)
        if level <= 0:
            continue
        if isinstance(table, dict):
            if level in table:
                filters.append(table[level])
        elif level < len(table):
            filters.append(table[level])
    return filters


def detect_hardware_capabilities():
    caps = {
        'nvidia_smi': False,
        'gpu_name': 'Unknown GPU',
        'nvenc_h264': False,
        'nvenc_hevc': False,
        'nvdec': False,
    }

    try:
        if shutil.which('nvidia-smi'):
            caps['nvidia_smi'] = True
            smi = run_process(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], timeout=2)
            name = (smi.stdout or '').strip().splitlines()
            if name and name[0]:
                caps['gpu_name'] = name[0]
    except Exception:
        pass

    try:
        enc = run_process(['ffmpeg', '-hide_banner', '-encoders'], timeout=3)
        enc_text = (enc.stdout or '') + (enc.stderr or '')
        caps['nvenc_h264'] = 'h264_nvenc' in enc_text
        caps['nvenc_hevc'] = 'hevc_nvenc' in enc_text
    except Exception:
        pass

    try:
        dec = run_process(['ffmpeg', '-hide_banner', '-decoders'], timeout=3)
        dec_text = (dec.stdout or '') + (dec.stderr or '')
        caps['nvdec'] = ('h264_cuvid' in dec_text) or ('hevc_cuvid' in dec_text) or ('av1_cuvid' in dec_text)
    except Exception:
        pass

    return caps

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

            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=SUBPROCESS_NO_WINDOW)

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
                "⚠️ MPV preview unavailable\n\n"
                "• Development: pip install mpv\n"
                "• Linux: install python-mpv and mpv (distro packages)\n"
                "• Frozen EXE (Auto-py-to-exe): Advanced → add --hidden-import=mpv\n"
                "  and Add Binary: libmpv-2.dll (plus any DLLs your MPV build needs)"
            )
            error_label.setStyleSheet("color: #ef4444; font-size: 12pt; font-weight: bold;")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            error_label.setWordWrap(True)
            layout.addWidget(error_label)
            return

        layout = QVBoxLayout(self)
        info_label = QLabel(
            "🎬 Video Preview\n\n"
            "Video plays in a separate MPV window\n"
            "(Native Wayland support - no XWayland needed)\n\n"
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

        if out_point is None or out_point <= 0:
            self.out_point = self.full_duration
        else:
            self.out_point = out_point
        if self.out_point <= self.in_point:
            self.out_point = self.full_duration

    def get_video_duration(self):
        return probe_media_duration(self.file_path, default=60.0)

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


class TimelineWidget(QWidget):
    clip_selected = pyqtSignal(object)
    playhead_moved = pyqtSignal(float)
    timeline_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.clips = []
        self.selected_clip = None
        self.dragging_clip = None
        self.drag_start_pos = None
        self.drag_offset = 0
        self.zoom_level = 10.0
        self.scroll_offset = 0
        self.setMinimumHeight(250)
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

        info_text = f"{clip.get_trimmed_duration():.1f}s | {clip.audio_streams} Tracks"
        duration_rect = painter.boundingRect(x + 5, y + height - 20, width - 10, 15, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, info_text)
        painter.drawText(duration_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, info_text)

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
            self.selected_clip = None
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
        return probe_media_duration(self.file_path, default=60.0)

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


def iter_ffmpeg_progress(process, duration):
    """Yield ``(line, seconds, percent)`` per stderr line; ``percent`` is None until FFmpeg reports a time."""
    for line in iter(process.stderr.readline, ''):
        seconds = _parse_ffmpeg_time(line)
        percent = None
        if seconds is not None and duration > 0:
            percent = min(99, int((seconds / duration) * 100))
        yield line, seconds, percent


def extract_audio_track_pcm(video_file, track, output_path, sample_duration, sample_rate):
    """Decode one audio track to raw mono 16-bit PCM for correlation analysis."""
    cmd = [
        'ffmpeg', '-y', '-v', 'error',
        '-i', video_file,
        '-map', f'0:a:{track}',
        '-t', str(sample_duration),
        '-ac', '1',
        '-ar', str(sample_rate),
        '-f', 's16le',
        output_path
    ]
    result = run_process(cmd, timeout=60, text=False)
    if result.returncode != 0:
        raise Exception(f"Failed to extract track {track}: {result.stderr.decode()}")


def auto_sync_audio(video_file, track1=0, track2=1, sample_duration=30, progress_callback=None):
    def log(msg):
        if progress_callback:
            progress_callback(msg)

    log("Extracting audio tracks...")

    try:
        audio_tracks = probe_audio_stream_indices(video_file)

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
        extract_audio_track_pcm(video_file, track1, tmp1_path, sample_duration, sample_rate)

        log(f"Extracting track {track2} (to sync)...")
        extract_audio_track_pcm(video_file, track2, tmp2_path, sample_duration, sample_rate)

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

def get_timeline_hash(timeline, settings):
    state = []
    if not timeline.clips:
        return "empty"
    for c in timeline.clips:
        vol_str = ','.join(map(str, getattr(c, 'volumes', [])))
        norm_str = ','.join(map(str, getattr(c, 'normalization', [])))
        so = getattr(c, 'sync_offset', 0)
        state.append(f"{c.file_path}|{c.start_time}|{c.in_point}|{c.out_point}|{vol_str}|{norm_str}|{so}")
    
    keys = ['denoise_level', 'deflicker_level', 'exposure_level', 'temporal_level', 'sharpness_level', 'export_res_index', 'timeline_fps']
    for k in keys:
        state.append(f"{k}:{settings.get(k, 0)}")
        
    return hashlib.md5("\n".join(state).encode('utf-8')).hexdigest()

class BackgroundCacheThread(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal(bool, str, str)

    def __init__(self, timeline_copy, settings_copy, cache_path, current_hash):
        super().__init__()
        self.timeline = timeline_copy
        self.settings = settings_copy
        self.cache_path = cache_path
        self.current_hash = current_hash
        self.engine = None

    def run(self):
        self.engine = TimelineRenderingEngine(self.timeline, self.settings, self.cache_path,
            log_callback=lambda m: None, progress_callback=self.progress.emit,
            status_callback=self.status.emit, playhead_callback=lambda t: None,
            is_cache_render=True)
        success, message = self.engine.render()
        self.finished.emit(success, message, self.current_hash)

    def stop(self):
        if self.engine:
            self.engine.should_stop = True

class TimelineCacheManager(QObject):
    status_changed = pyqtSignal(str)
    
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.cache_thread = None
        self.debounce_timer = QTimer()
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(1500)
        self.debounce_timer.timeout.connect(self._start_cache_render)
        
        self.last_hash = ""
        self.ready_hash = ""
        self.cache_file = os.path.join(tempfile.gettempdir(), 'fep_timeline_cache.mp4')
        
    def trigger_update(self):
        settings = self.app.get_settings()
        if not has_optional_video_filters(settings):
            self.status_changed.emit("Cache: Inactive")
            self.ready_hash = ""
            return
            
        current_hash = get_timeline_hash(self.app.timeline, settings)
        if current_hash == self.ready_hash:
            self.status_changed.emit("Cache: Ready ⚡")
            return
            
        if self.last_hash != current_hash:
            self.last_hash = current_hash
            self.status_changed.emit("Cache: Waiting...")
            self.debounce_timer.start()

    def _start_cache_render(self):
        if not self.app.timeline.clips:
            self.status_changed.emit("")
            return
            
        if self.cache_thread and self.cache_thread.isRunning():
            self.cache_thread.stop()
            self.cache_thread.wait()
            
        self.status_changed.emit("Cache: Rendering (0%)")
        
        import copy
        class DummyTimeline: pass
        tl_copy = DummyTimeline()
        tl_copy.clips = copy.deepcopy(self.app.timeline.clips)
        
        settings = self.app.get_settings()
        self.cache_thread = BackgroundCacheThread(tl_copy, settings, self.cache_file, self.last_hash)
        self.cache_thread.progress.connect(self._on_progress)
        self.cache_thread.finished.connect(self._on_finished)
        self.cache_thread.start()
        
    def _on_progress(self, pct):
        self.status_changed.emit(f"Cache: Rendering ({pct}%)")
        
    def _on_finished(self, success, msg, finished_hash):
        if success and finished_hash == self.last_hash:
            self.ready_hash = finished_hash
            self.status_changed.emit("Cache: Ready ⚡")
        elif not success and finished_hash == self.last_hash:
            self.status_changed.emit("Cache: Error")
            
    def get_valid_cache_path(self):
        settings = self.app.get_settings()
        current_hash = get_timeline_hash(self.app.timeline, settings)
        if current_hash == self.ready_hash and os.path.exists(self.cache_file):
            return self.cache_file
        return None

class TimelineRenderingEngine:
    """
    MASTER CANVAS COMPOSITOR ENGINE (v0.9.0)
    This entirely replaces the Python-pipe transcoder with a true NLE FFmpeg graph.
    All clips are overlaid onto a blank hardware canvas natively.
    No temp files. No System RAM bottlenecks. 100% GPU utilization.
    """
    def __init__(self, timeline, settings, output_path,
                 log_callback, progress_callback, status_callback, playhead_callback=None,
                 is_cache_render=False, valid_cache_path=None):
        self.timeline = timeline
        self.settings = settings
        self.output_path = output_path
        self.log = log_callback
        self.progress = progress_callback
        self.status = status_callback
        self.playhead = playhead_callback
        self.is_cache_render = is_cache_render
        self.valid_cache_path = valid_cache_path
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
            stream = probe_video_stream(file_path, 'width,height,codec_name')
            return stream['width'], stream['height']
        except Exception:
            return 1920, 1080

    def _get_video_codec(self, file_path):
        try:
            return probe_video_stream(file_path, 'codec_name')['codec_name']
        except Exception:
            return 'unknown'

    def render(self):
        try:
            self.log("=== HIGH-PERFORMANCE MASTER CANVAS ENGINE v0.9.0 ===")
            self.log("Compiling Timeline NLE Graph...")

            if not self.timeline.clips:
                return False, "No clips on timeline"

            timeline_duration = self.get_timeline_duration()
            timeline_fps = self.settings.get('timeline_fps', 60.0)
            sorted_clips = sorted(self.timeline.clips, key=lambda c: c.start_time)

            source_width, source_height = self.get_video_metadata(sorted_clips[0].file_path)
            export_res_index = self.settings.get('export_res_index', 0)
            export_resolution = EXPORT_RESOLUTIONS[export_res_index]
            if export_resolution is None:
                export_width, export_height = source_width, source_height
            else:
                export_width, export_height = export_resolution

            # ENSURE EVEN DIMENSIONS (Prevents NVENC padding crash)
            if export_width % 2 != 0: export_width -= 1
            if export_height % 2 != 0: export_height -= 1

            self.log(f"Resolution: {export_width}x{export_height} @ {timeline_fps} FPS")
            self.log(f"Total Duration: {timeline_duration:.2f}s")
            
            if not getattr(self, 'is_cache_render', False) and getattr(self, 'valid_cache_path', None) and os.path.exists(self.valid_cache_path):
                self.log("💎 Found valid background render cache! Bypassing CPU filters for maximum speed...")
                cmd = list(FFMPEG_BASE_ARGS)
                cmd.extend(['-hwaccel', 'auto', '-i', self.valid_cache_path])
                
                codec = self.settings.get('video_codec', 'hevc_nvenc')
                cmd.extend(['-c:v', codec])
                if 'nvenc' in codec:
                    cmd.extend(build_nvenc_cbr_args(self.settings, timeline_fps))
                
                cmd.extend(['-c:a', 'copy', '-movflags', '+faststart', self.output_path])
            else:
                optional_filters_enabled = has_optional_video_filters(self.settings)
                self.log(f"Optional codec-tab filters: {'ON' if optional_filters_enabled else 'OFF'}")
                self.log("Compositor path: decode -> software filter graph -> NVENC encode")

                cmd = list(FFMPEG_BASE_ARGS)
                use_gpu_decode = self.settings.get('use_gpu_decode', False)

                # 1. ADD INPUTS
                for clip in sorted_clips:
                    codec = self._get_video_codec(clip.file_path)
                    cmd.extend(build_hw_decode_input_args(clip.file_path, codec, use_gpu_decode))

                # 2. BUILD THE COMPOSITING GRAPH
                filter_complex = []

                # Create the master blank canvas at exact output specs
                filter_complex.append(f"color=c=black:s={export_width}x{export_height}:r={timeline_fps}:d={timeline_duration}[bg0]")

                audio_inputs = []

                for i, clip in enumerate(sorted_clips):
                    # --- VIDEO GRAPH ---
                    v_in = f"[{i}:v]"
                    v_trimmed = f"[v{i}_trim]"
                    v_scaled = f"[v{i}_scale]"

                    st = clip.start_time
                    filter_complex.append(
                        f"{v_in}trim=start={clip.in_point}:end={clip.out_point},"
                        f"setpts=PTS-STARTPTS+{st:.6f}/TB{v_trimmed}"
                    )

                    scale_str = f"scale={export_width}:{export_height}:force_original_aspect_ratio=decrease,pad={export_width}:{export_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
                    filter_complex.append(f"{v_trimmed}{scale_str}{v_scaled}")

                    # Overlay the scaled clip onto the running background canvas
                    bg_in = f"[bg{i}]"
                    bg_out = f"[bg{i+1}]"
                    end_time = clip.start_time + clip.get_trimmed_duration()
                    filter_complex.append(f"{bg_in}{v_scaled}overlay=enable='between(t,{clip.start_time},{end_time})':eof_action=pass{bg_out}")

                    # --- AUDIO GRAPH ---
                    n_streams = clip.audio_streams
                    for a_idx in range(n_streams):
                        a_in = f"[{i}:a:{a_idx}]"
                        a_trimmed = f"[a{i}_{a_idx}_trim]"

                        filter_complex.append(f"{a_in}atrim=start={clip.in_point}:end={clip.out_point},asetpts=PTS-STARTPTS{a_trimmed}")

                        # Compute delay: Base timeline placement + Track sync offset
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
                        # Apply delay if it exists
                        if base_delay_ms > 0:
                            chain += f"adelay={base_delay_ms}|{base_delay_ms},"

                        chain += f"volume={vol_db}dB"

                        if norm:
                            chain += ",loudnorm"

                        filter_complex.append(f"{a_trimmed}{chain}{a_ready}")
                        audio_inputs.append(a_ready)

                # --- FINAL OUTPUT MAPPING ---
                last_v = f"[bg{len(sorted_clips)}]"

                user_filters = build_video_filter_chain(self.settings)
                if user_filters:
                    filter_complex.append(f"{last_v}{','.join(user_filters)}[v_filtered]")
                    pre_fps_v = "[v_filtered]"
                else:
                    pre_fps_v = last_v

                filter_complex.append(f"{pre_fps_v}fps={timeline_fps}[out_v]")
                map_v = "[out_v]"

                # Mix Audio
                if audio_inputs:
                    inputs_str = "".join(audio_inputs)
                    filter_complex.append(f"{inputs_str}amix=inputs={len(audio_inputs)}:duration=longest:dropout_transition=0[out_a]")
                    map_a = "[out_a]"
                else:
                    # Generate silent audio track if completely muted
                    filter_complex.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={timeline_duration}[out_a]")
                    map_a = "[out_a]"

                cmd.extend(['-filter_complex', ';'.join(filter_complex)])
                cmd.extend(['-map', map_v, '-map', map_a])

                # 3. ENCODER SETTINGS
                if getattr(self, 'is_cache_render', False):
                    # For cache render, use visually lossless fast NVENC
                    codec = 'hevc_nvenc'
                    cmd.extend(['-c:v', codec, '-preset', 'p6', '-tune', 'hq', '-cq', '15', '-b:v', '0'])
                    cmd.extend(['-c:a', 'aac', '-b:a', '320k', '-movflags', '+faststart', '-y', self.output_path])
                else:
                    codec = self.settings.get('video_codec', 'hevc_nvenc')
                    cmd.extend(['-c:v', codec])
                    if 'nvenc' in codec:
                        cmd.extend(build_nvenc_cbr_args(self.settings, timeline_fps))
                    cmd.extend([
                        '-c:a', 'aac', '-b:a', '320k',
                        '-movflags', '+faststart',
                        '-t', f"{timeline_duration:.6f}",
                        self.output_path
                    ])

            self.log(f"Compositing execution started...")

            start_time = time.time()
            self.encoder_process = start_streaming_process(cmd)

            # 4. MONITOR PROGRESS
            for _line, t, pct in iter_ffmpeg_progress(self.encoder_process, timeline_duration):
                if self.should_stop:
                    self.encoder_process.kill()
                    return False, "Render cancelled by user"

                if pct is not None:
                    self.progress(pct)
                    elapsed = time.time() - start_time
                    fps_actual = (t * timeline_fps) / elapsed if elapsed > 0 else 0
                    self.status(f"Rendering: {pct}% — {fps_actual:.1f} fps")
                    if self.playhead:
                        self.playhead(t)

            self.encoder_process.wait()

            if self.encoder_process.returncode != 0:
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
            self.process = start_streaming_process(cmd, bufsize=1)
            duration = self.get_duration()
            for line, _seconds, pct in iter_ffmpeg_progress(self.process, duration):
                if self.should_stop:
                    self.process.kill()
                    self.finished.emit(False, "Stopped")
                    return
                self.log_message.emit(line.strip())
                if pct is not None:
                    self.progress.emit(pct)
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
        cmd = list(FFMPEG_BASE_ARGS)
        use_gpu_decode = self.settings.get('use_gpu_decode', False)
        codec_name = None
        try:
            codec_name = self.settings.get('input_codec_name')
        except Exception:
            codec_name = None
        cmd.extend(build_hw_decode_input_args(self.input_file, codec_name, use_gpu_decode))

        video_filters = build_video_filter_chain(self.settings)
        if video_filters: cmd.extend(['-vf', ','.join(video_filters)])
        codec = self.settings['video_codec']
        
        # New Remux Support
        if codec == 'copy':
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
        cmd.append(self.output_file)
        return cmd

    def get_duration(self):
        return probe_media_duration(self.input_file)

    def stop(self):
        self.should_stop = True
        if self.process:
            try: self.process.kill()
            except: pass


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
        
        self.cache_status_label = QLabel("Cache: Inactive")
        self.cache_status_label.setStyleSheet("color: #94a3b8; font-weight: bold; font-size: 14px;")
        self.cache_manager = TimelineCacheManager(self)
        self.cache_manager.status_changed.connect(self.cache_status_label.setText)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(self.tab_style())
        main_layout.addWidget(self.tabs)

        self.timeline_tab = self.create_timeline_tab()
        self.codec_tab = self.create_codec_tab()
        self.batch_tab = self.create_batch_tab()
        self.access_tab = self.create_accessibility_tab()

        self.tabs.addTab(self.timeline_tab, "📽️ Timeline")
        self.tabs.addTab(self.codec_tab, "⚙️ Codec")
        self.tabs.addTab(self.batch_tab, "📦 Batch")
        self.tabs.addTab(self.access_tab, "♿ Accessibility")

        self.apply_theme()
        self.load_settings()

    def apply_auto_hardware_settings(self, update_status=False):
        nvenc_available = self.hw_caps.get('nvenc_h264', False) or self.hw_caps.get('nvenc_hevc', False)
        nvdec_available = self.hw_caps.get('nvdec', False)

        self.gpu_check.setEnabled(nvenc_available)
        self.gpu_decode_check.setEnabled(nvdec_available)
        self.gpu_check.setChecked(nvenc_available)
        self.gpu_decode_check.setChecked(nvdec_available)

        gpu_name = self.hw_caps.get('gpu_name', 'Unknown GPU')
        self.hw_detect_label.setText(
            f"Auto hardware detect: {gpu_name} | NVENC: {'YES' if nvenc_available else 'NO'} | NVDEC: {'YES' if nvdec_available else 'NO'}"
        )

        if update_status:
            self.status_label.setText("Applied automatic hardware settings")

    # --- WIDGET FACTORIES ---

    def make_button(self, text, color, height=None, size=None, on_click=None, tooltip=None, enabled=True):
        button = QPushButton(text)
        button.setStyleSheet(self.button_style(color))
        if size is not None:
            button.setFixedSize(*size)
        if height is not None:
            button.setMinimumHeight(height)
        if tooltip:
            button.setToolTip(tooltip)
        if not enabled:
            button.setEnabled(False)
        if on_click is not None:
            button.clicked.connect(on_click)
        return button

    def add_combo_row(self, layout, label_text, items, current_index=None, on_change=None):
        """Append a ``label + combo box`` row to ``layout`` and return both widgets."""
        row = QHBoxLayout()
        label = QLabel(label_text)
        row.addWidget(label)
        combo = QComboBox()
        combo.addItems(items)
        if current_index is not None:
            combo.setCurrentIndex(current_index)
        combo.setStyleSheet(self.combo_style())
        if on_change is not None:
            combo.currentIndexChanged.connect(on_change)
        row.addWidget(combo)
        layout.addLayout(row)
        return label, combo

    def add_spinbox_row(self, layout, label_text, spinbox, minimum, maximum, value, step=None, on_change=None):
        row = QHBoxLayout()
        row.addWidget(QLabel(label_text))
        spinbox.setRange(minimum, maximum)
        spinbox.setValue(value)
        if step is not None:
            spinbox.setSingleStep(step)
        spinbox.setStyleSheet(self.spinbox_style())
        if on_change is not None:
            spinbox.valueChanged.connect(on_change)
        row.addWidget(spinbox)
        layout.addLayout(row)
        return spinbox

    def add_audio_track_row(self, layout, label_text):
        """Append the ``normalize`` + ``volume slider`` controls for one audio track."""
        header = QHBoxLayout()
        header.addWidget(QLabel(label_text))
        norm_check = QCheckBox("Normalize")
        norm_check.setStyleSheet("color: #4ade80;")
        norm_check.stateChanged.connect(self.update_clip_volume)
        header.addWidget(norm_check)
        layout.addLayout(header)

        slider_row = QHBoxLayout()
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(-60, 30)
        slider.setValue(0)
        slider.setStyleSheet(self.slider_style())
        slider.valueChanged.connect(self.update_clip_volume)
        slider_row.addWidget(slider)
        value_label = QLabel("0 dB")
        slider_row.addWidget(value_label)
        layout.addLayout(slider_row)
        return norm_check, slider, value_label

    def confirm(self, title, question):
        reply = QMessageBox.question(self, title, question,
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return reply == QMessageBox.StandardButton.Yes

    def create_accessibility_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(20)

        title = QLabel("♿ Accessibility Features")
        title.setStyleSheet("font-size: 18pt; font-weight: bold; color: #4ade80;")
        layout.addWidget(title)

        dwell_group = QGroupBox("👁️ Eye Tracking / Dwell Click")
        dwell_group.setStyleSheet(self.groupbox_style())
        dwell_layout = QVBoxLayout()

        self.dwell_check = QCheckBox("Enable Dwell Click (Auto-click when looking at buttons)")
        self.dwell_check.setStyleSheet("font-size: 14pt; font-weight: bold; color: white;")
        self.dwell_check.stateChanged.connect(self.toggle_dwell)
        dwell_layout.addWidget(self.dwell_check)

        self.dwell_time_spin = self.add_spinbox_row(
            dwell_layout, "Dwell Time (seconds):", QDoubleSpinBox(), 0.2, 5.0, 1.2,
            step=0.1, on_change=self.update_dwell_params)
        self.dwell_thresh_spin = self.add_spinbox_row(
            dwell_layout, "Movement Threshold (Sensitivity):", QSpinBox(), 5, 50, 15,
            on_change=self.update_dwell_params)

        dwell_group.setLayout(dwell_layout)
        layout.addWidget(dwell_group)

        switch_group = QGroupBox("🔘 Switch Control / High Contrast")
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

    def create_timeline_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        project_controls = QHBoxLayout()
        save_proj_btn = self.make_button("💾 Save Project", "#3b82f6", height=40, on_click=self.save_project)
        project_controls.addWidget(save_proj_btn)

        load_proj_btn = self.make_button("📂 Load Project", "#f59e0b", height=40, on_click=self.load_project)
        project_controls.addWidget(load_proj_btn)
        
        project_controls.addStretch()
        project_controls.addWidget(self.cache_status_label)
        
        layout.addLayout(project_controls)

        top_section = QWidget()
        top_layout = QHBoxLayout(top_section)
        top_layout.setSpacing(10)
        library_panel = QWidget()
        library_layout = QVBoxLayout(library_panel)
        library_layout.setContentsMargins(5, 5, 5, 5)
        lib_title = QLabel("📚 MEDIA LIBRARY")
        lib_title.setStyleSheet("font-size: 14pt; font-weight: bold; color: #4ade80; padding: 5px;")
        library_layout.addWidget(lib_title)
        self.media_list = QListWidget()
        self.media_list.setStyleSheet(self.list_style())
        self.media_list.itemClicked.connect(self.on_media_selected)
        library_layout.addWidget(self.media_list)
        lib_buttons = QHBoxLayout()
        add_media_btn = self.make_button("➕ Add Media", "#4ade80", height=50, on_click=self.add_media_to_library)
        lib_buttons.addWidget(add_media_btn)
        remove_media_btn = self.make_button("➖ Remove", "#ef4444", height=50, on_click=self.remove_from_library)
        lib_buttons.addWidget(remove_media_btn)
        library_layout.addLayout(lib_buttons)
        top_layout.addWidget(library_panel, stretch=1)
        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(5, 5, 5, 5)
        preview_title = QLabel("🎬 PREVIEW")
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
        self.play_btn = self.make_button("▶️ Play", "#3b82f6", height=50, on_click=self.toggle_play)
        controls_row.addWidget(self.play_btn)
        self.fullscreen_btn = self.make_button("⛶ Fullscreen", "#8b5cf6", height=50, on_click=self.enter_fullscreen)
        controls_row.addWidget(self.fullscreen_btn)
        preview_layout.addLayout(controls_row)

        trim_panel = QWidget()
        trim_layout = QHBoxLayout(trim_panel)
        trim_layout.setContentsMargins(0, 5, 0, 5)

        trim_box = QGroupBox("✂️ Trim")
        trim_box.setStyleSheet(self.groupbox_style())
        trim_box_layout = QVBoxLayout(trim_box)
        trim_buttons = QHBoxLayout()
        set_in_btn = self.make_button("[ Set IN", "#10b981", height=35, on_click=self.set_media_in_point)
        trim_buttons.addWidget(set_in_btn)
        set_out_btn = self.make_button("Set OUT ]", "#10b981", height=35, on_click=self.set_media_out_point)
        trim_buttons.addWidget(set_out_btn)
        trim_box_layout.addLayout(trim_buttons)
        self.trim_info = QLabel("In: 00:00:00 | Out: 00:00:00 | Duration: 00:00:00")
        self.trim_info.setStyleSheet("font-size: 9pt; color: #9ca3af; padding: 2px;")
        self.trim_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trim_box_layout.addWidget(self.trim_info)
        trim_layout.addWidget(trim_box)

        mixer_box = QGroupBox("🎚️ Audio Mixer")
        mixer_box.setStyleSheet(self.groupbox_style())
        mixer_box_layout = QVBoxLayout(mixer_box)

        self.track1_norm, self.track1_slider, self.t1_val = self.add_audio_track_row(mixer_box_layout, "Audio Track 1")
        self.track2_norm, self.track2_slider, self.t2_val = self.add_audio_track_row(mixer_box_layout, "Audio Track 2")

        sync_layout = QHBoxLayout()
        self.auto_sync_btn = self.make_button(
            "🎯 Auto-Sync Audio", "#8b5cf6", height=35, on_click=self.auto_sync_audio_tracks,
            tooltip="Automatically detect and fix audio sync offset between tracks")
        sync_layout.addWidget(self.auto_sync_btn)

        self.sync_status_label = QLabel("")
        self.sync_status_label.setStyleSheet("color: #60a5fa; font-size: 10pt;")
        sync_layout.addWidget(self.sync_status_label)
        sync_layout.addStretch()
        mixer_box_layout.addLayout(sync_layout)

        trim_layout.addWidget(mixer_box)

        preview_layout.addWidget(trim_panel)
        top_layout.addWidget(preview_panel, stretch=2)
        layout.addWidget(top_section, stretch=3)
        timeline_section = QWidget()
        timeline_layout = QVBoxLayout(timeline_section)
        timeline_layout.setContentsMargins(5, 5, 5, 5)
        timeline_header = QHBoxLayout()
        timeline_title = QLabel("🎞️ TIMELINE")
        timeline_title.setStyleSheet("font-size: 14pt; font-weight: bold; color: #f59e0b; padding: 5px;")
        timeline_header.addWidget(timeline_title)
        timeline_header.addStretch()
        zoom_in_btn = self.make_button("🔍+", "#6366f1", size=(60, 40), on_click=self.zoom_in_timeline)
        timeline_header.addWidget(zoom_in_btn)
        zoom_out_btn = self.make_button("🔍−", "#6366f1", size=(60, 40), on_click=self.zoom_out_timeline)
        timeline_header.addWidget(zoom_out_btn)
        timeline_layout.addLayout(timeline_header)
        self.timeline = TimelineWidget()
        self.timeline.setStyleSheet("background-color: #111827; border: 2px solid #4b5563; border-radius: 8px;")
        self.timeline.clip_selected.connect(self.on_timeline_clip_selected)
        self.timeline.playhead_moved.connect(self.on_timeline_playhead_moved)
        self.timeline.timeline_clicked.connect(self.activate_timeline_mode)
        timeline_layout.addWidget(self.timeline, stretch=1)
        timeline_controls = QHBoxLayout()
        timeline_controls.addWidget(self.make_button("➕ Add to Timeline", "#4ade80", height=50, on_click=self.add_to_timeline))
        timeline_controls.addWidget(self.make_button("➖ Remove Clip", "#ef4444", height=50, on_click=self.remove_from_timeline))
        timeline_controls.addWidget(self.make_button("🗑️ Clear All", "#dc2626", height=50, on_click=self.clear_timeline))
        self.export_timeline_btn = self.make_button("💾 EXPORT TIMELINE", "#8b5cf6", height=50, on_click=self.export_timeline)
        timeline_controls.addWidget(self.export_timeline_btn)
        self.stop_export_btn = self.make_button("⏹️ STOP RENDER", "#ef4444", height=50,
                                                on_click=self.stop_timeline_export, enabled=False)
        timeline_controls.addWidget(self.stop_export_btn)
        timeline_layout.addLayout(timeline_controls)
        layout.addWidget(timeline_section, stretch=2)
        return tab

    def create_codec_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background-color: #111827; }")
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(15)

        codec_group = QGroupBox("🎥 Video Codec")
        codec_group.setStyleSheet(self.groupbox_style())
        codec_layout = QVBoxLayout()
        # ADDED REMUX (COPY) OPTION HERE
        _, self.codec_combo = self.add_combo_row(
            codec_layout, "Codec:",
            ["ProRes", "H.264 (NVENC)", "H.265/HEVC (NVENC)", "Remux (Copy Stream)"],
            current_index=2, on_change=self.on_codec_changed)
        self.prores_label, self.prores_combo = self.add_combo_row(
            codec_layout, "ProRes Profile:", PRORES_PROFILE_NAMES,
            current_index=5, on_change=self.update_estimated_size)
        self.nvenc_label, self.pixel_combo = self.add_combo_row(
            codec_layout, "Pixel Format:", ["8-bit (yuv420p)", "10-bit (p010le)"], current_index=1)
        self.export_target_label, self.export_target_combo = self.add_combo_row(
            codec_layout, "Export target:", get_export_target_labels(), current_index=0,
            on_change=lambda _: self.update_quality_label(self.quality_slider.value()))

        codec_group.setLayout(codec_layout)
        scroll_layout.addWidget(codec_group)

        timeline_group = QGroupBox("🎞️ Timeline Settings")
        timeline_group.setStyleSheet(self.groupbox_style())
        timeline_layout = QVBoxLayout()

        _, self.timeline_fps_combo = self.add_combo_row(
            timeline_layout, "Timeline FPS:", TIMELINE_FPS_LABELS, current_index=6)
        _, self.export_res_combo = self.add_combo_row(
            timeline_layout, "Export Resolution:", EXPORT_RESOLUTION_LABELS, current_index=0)
        _, self.scale_algo_combo = self.add_combo_row(
            timeline_layout, "Upscale Quality:", SCALE_ALGO_LABELS, current_index=2)

        timeline_group.setLayout(timeline_layout)
        scroll_layout.addWidget(timeline_group)

        quality_group = QGroupBox("🎯 Quality Bitrate Slider (CBR)")
        quality_group.setStyleSheet(self.groupbox_style())
        quality_layout = QVBoxLayout()

        quality_info = QLabel("Drag slider to set constant bitrate (Mbps). Higher = better quality & larger files.")
        quality_info.setStyleSheet("font-size: 9pt; color: #9ca3af; padding: 5px;")
        quality_info.setWordWrap(True)
        quality_layout.addWidget(quality_info)

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Bitrate:"))

        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setMinimum(1)
        self.quality_slider.setMaximum(1000)
        self.quality_slider.setValue(100)
        self.quality_slider.setStyleSheet(self.slider_style())
        self.quality_slider.valueChanged.connect(self.update_quality_label)
        slider_row.addWidget(self.quality_slider)

        self.quality_value_label = QLabel("100 Mbps")
        self.quality_value_label.setStyleSheet("font-size: 11pt; font-weight: bold; color: #4ade80; min-width: 100px;")
        slider_row.addWidget(self.quality_value_label)

        quality_layout.addLayout(slider_row)

        self.estimated_size_label = QLabel("Estimated Size: Calculating...")
        self.estimated_size_label.setStyleSheet("font-size: 12pt; font-weight: bold; color: #fbbf24; padding: 10px; background-color: #1f2937; border-radius: 5px;")
        self.estimated_size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        quality_layout.addWidget(self.estimated_size_label)

        quality_group.setLayout(quality_layout)
        scroll_layout.addWidget(quality_group)

        audio_group = QGroupBox("🔊 Audio")
        audio_group.setStyleSheet(self.groupbox_style())
        audio_layout = QVBoxLayout()
        _, self.audio_combo = self.add_combo_row(
            audio_layout, "Audio Codec:", ["PCM 24-bit", "PCM 16-bit", "AAC 320kbps", "Copy Stream"])
        audio_group.setLayout(audio_layout)
        scroll_layout.addWidget(audio_group)

        filters_group = QGroupBox("🎨 Filters (Optional)")
        filters_group.setStyleSheet(self.groupbox_style())
        filters_layout = QVBoxLayout()

        _, self.denoise_combo = self.add_combo_row(
            filters_layout, "Denoise:",
            ["Off", "Light", "Medium", "Heavy", "Very Heavy", "Nuclear", "Ultra Nuclear"])
        _, self.deflicker_combo = self.add_combo_row(
            filters_layout, "Deflicker:", ["Off", "Light", "Medium", "Strong", "Very Strong", "Maximum"])
        _, self.exposure_combo = self.add_combo_row(
            filters_layout, "Exposure:",
            ["Off", "+0.25 EV", "+0.5 EV", "+0.75 EV", "+1.0 EV", "+1.5 EV", "+2.0 EV",
             "-0.25 EV", "-0.5 EV", "-0.75 EV", "-1.0 EV", "-1.5 EV", "-2.0 EV"])
        _, self.temporal_combo = self.add_combo_row(
            filters_layout, "Temporal Smoothing:", ["Off", "Light", "Medium", "Strong", "Very Strong", "Maximum"])
        _, self.sharpness_combo = self.add_combo_row(
            filters_layout, "Sharpness:",
            ["Off", "Subtle", "Light", "Medium", "Strong", "Very Strong", "Ultra Sharp"])

        filters_group.setLayout(filters_layout)
        scroll_layout.addWidget(filters_group)

        perf_group = QGroupBox("⚡ Performance")
        perf_group.setStyleSheet(self.groupbox_style())
        perf_layout = QVBoxLayout()
        self.gpu_check = QCheckBox("Enable GPU Acceleration (NVENC Encode)")
        self.gpu_check.setChecked(True)
        self.gpu_check.setStyleSheet("font-size: 11pt; color: white;")
        perf_layout.addWidget(self.gpu_check)

        self.gpu_decode_check = QCheckBox("Enable GPU Hardware Decode (CUDA/NVDEC)")
        self.gpu_decode_check.setChecked(False)
        self.gpu_decode_check.setStyleSheet("font-size: 11pt; color: white;")
        self.gpu_decode_check.stateChanged.connect(lambda: self.update_quality_label(self.quality_slider.value()))
        perf_layout.addWidget(self.gpu_decode_check)

        auto_hw_btn = self.make_button("🔍 Auto Detect Hardware", "#3b82f6", height=36,
                                       on_click=lambda: self.apply_auto_hardware_settings(update_status=True))
        perf_layout.addWidget(auto_hw_btn)

        self.hw_detect_label = QLabel("Auto hardware detect: pending")
        self.hw_detect_label.setStyleSheet("font-size: 9pt; color: #93c5fd; padding: 2px 5px;")
        self.hw_detect_label.setWordWrap(True)
        perf_layout.addWidget(self.hw_detect_label)

        gpu_decode_info = QLabel(
            "ℹ️ AV1 hardware decode requires RTX 30-series or newer\n"
            "   RTX 20-series: Keep OFF for AV1 files (use CPU decode)\n"
            "   RTX 30+: Can enable for faster AV1 preview"
        )
        gpu_decode_info.setStyleSheet("font-size: 9pt; color: #94a3b8; padding: 5px 20px;")
        gpu_decode_info.setWordWrap(True)
        perf_layout.addWidget(gpu_decode_info)

        self.threads_spin = self.add_spinbox_row(perf_layout, "CPU Threads (0=auto):", QSpinBox(), 0, 64, 0)
        self.gpu_info = QLabel("✅ ProRes 4444 XQ (~500 Mbps)")
        self.gpu_info.setStyleSheet("font-size: 10pt; color: #4ade80; font-weight: bold; padding: 5px;")
        perf_layout.addWidget(self.gpu_info)
        perf_group.setLayout(perf_layout)
        scroll_layout.addWidget(perf_group)

        scroll_layout.addWidget(self.make_button("🔄 Reset All Settings", "#ef4444", height=50, on_click=self.reset_all))

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        self.apply_auto_hardware_settings()
        self.on_codec_changed()
        self.update_quality_label(100)
        return tab

    def create_batch_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(15)
        files_group = QGroupBox("📁 Files")
        files_group.setStyleSheet(self.groupbox_style())
        files_layout = QVBoxLayout()
        self.file_list = QListWidget()
        self.file_list.setStyleSheet(self.list_style())
        files_layout.addWidget(self.file_list)
        file_buttons = QHBoxLayout()
        file_buttons.addWidget(self.make_button("➕ Add Files", "#4ade80", height=50, on_click=self.add_files))
        file_buttons.addWidget(self.make_button("➖ Remove", "#ef4444", height=50, on_click=self.remove_selected))
        clear_btn = self.make_button("🗑️ Clear All", "#dc2626", height=50, on_click=self.clear_files)
        file_buttons.addWidget(clear_btn)
        files_layout.addLayout(file_buttons)
        files_group.setLayout(files_layout)
        layout.addWidget(files_group)
        output_group = QGroupBox("💾 Output")
        output_group.setStyleSheet(self.groupbox_style())
        output_layout = QVBoxLayout()
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Folder:"))
        self.output_label = QLabel(self.output_folder if self.output_folder else "Not selected")
        self.output_label.setStyleSheet("color: #9ca3af; padding: 5px;")
        output_row.addWidget(self.output_label, stretch=1)
        browse_btn = self.make_button("📂 Browse", "#3b82f6", height=40, on_click=self.select_output)
        output_row.addWidget(browse_btn)
        output_layout.addLayout(output_row)
        output_group.setLayout(output_layout)
        layout.addWidget(output_group)
        progress_group = QGroupBox("📊 Progress")
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
        log_group = QGroupBox("📝 Log")
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
        self.start_btn = self.make_button("▶️ START ENCODING", "#4ade80", height=60, on_click=self.start_encoding)
        control_buttons.addWidget(self.start_btn)
        self.stop_btn = self.make_button("⏹️ STOP", "#ef4444", height=60,
                                         on_click=self.stop_encoding, enabled=False)
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

    def remove_from_library(self):
        row = self.media_list.currentRow()
        if row >= 0:
            self.media_list.takeItem(row)
            del self.media_library[row]
            if self.current_media and row == self.media_library.index(self.current_media) if self.current_media in self.media_library else False:
                self.current_media = None
                if self.video_widget:
                    self.video_widget.stop()

    def preview_duration_ms(self):
        """Player duration, falling back to the timeline length while previewing an EDL."""
        if not self.video_widget:
            return 0
        duration = self.video_widget.duration()
        if duration <= 0 and self.is_timeline_mode and self._play_uses_timeline_edl:
            duration = int(self.timeline.get_timeline_duration() * 1000)
        return duration

    def _on_position_changed(self, position_ms):
        """Handle video position updates"""
        dur = self.preview_duration_ms()
        if dur > 0:
            slider_value = int((position_ms / dur) * 1000)
            self.preview_slider.setValue(slider_value)

        current_tc = self.format_timecode(position_ms)
        total_tc = self.format_timecode(dur)
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
        if duration_ms <= 0 and self.is_timeline_mode and self._play_uses_timeline_edl:
            duration_ms = int(self.timeline.get_timeline_duration() * 1000)
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

            if self.video_widget and self.video_widget.load_file(file_path):
                self.video_widget.pause()

                if get_audio_stream_count_static(file_path) > 1:
                    self.apply_audio_mix_preview(file_path, [])

            self.update_trim_info()

    def activate_timeline_mode(self):
        self.is_timeline_mode = True
        self._play_uses_timeline_edl = True
        self.trim_info.setText("Timeline Mode Active - Click Play to Preview Sequence")
        self.load_timeline_sequence(play=False)

    def load_timeline_sequence(self, play=False):
        if not self.timeline.clips:
            if self.video_widget:
                self.video_widget.stop()
            return

        sorted_clips = sorted(self.timeline.clips, key=lambda c: c.start_time)
        edl_content = "# mpv EDL v0\n"
        for clip in sorted_clips:
            length = clip.get_trimmed_duration()
            fp = clip.file_path.replace('\\', '/')
            fp_bytes = fp.encode('utf-8')
            edl_content += f"%{len(fp_bytes)}%{fp},{clip.in_point},{length}\n"

        try:
            fd, path = tempfile.mkstemp(suffix='.edl')
            os.close(fd)
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(edl_content)

            self.video_widget.set_audio_complex_filter("")

            edl_path = path.replace('\\', '/')
            seek_ms = int(self.timeline.playhead_position * 1000)
            
            if self.video_widget.load_file(edl_path, seek_ms=seek_ms):
                # Set timeline-derived duration immediately as fallback
                # so scrubber/timecode work before MPV's async observer fires
                timeline_dur_ms = int(self.timeline.get_timeline_duration() * 1000)
                if self.video_widget._duration_ms <= 0:
                    self.video_widget._duration_ms = timeline_dur_ms
                    self.video_widget.durationChanged.emit(timeline_dur_ms)
                if play:
                    self.video_widget.play()
                    self.play_btn.setText("⏸️ Pause")
                else:
                    self.video_widget.pause()
                    self.play_btn.setText("▶️ Play")
        except Exception as e:
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
        if self.video_widget.load_file(clip.file_path, seek_ms=seek_ms):
            self.video_widget.pause()
            self.apply_audio_mix_preview(clip.file_path, clip.volumes, clip.normalization)

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
                f"This clip only has {clip.audio_streams} audio track(s).\n\n"
                "Auto-sync requires at least 2 audio tracks:\n"
                "• Track 0: Reference (usually desktop audio)\n"
                "• Track 1: To sync (usually microphone)"
            )
            return

        if not self.confirm(
            "Auto-Sync Audio",
            f"Analyze audio sync for: {clip.name}\n\n"
            "This will analyze the first 30 seconds to detect\n"
            "the sync offset between audio tracks.\n\n"
            "Track 0 (desktop) will be used as reference.\n"
            "Track 1 (mic) will be synchronized.\n\n"
            "Continue?"
        ):
            return

        # FIX: Use QProgressDialog instead of QMessageBox to prevent Wayland ghost-window freeze
        progress = QProgressDialog("Extracting audio tracks...\n\nThis may take 10-30 seconds.", None, 0, 0, self)
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
            self._destroy_progress_dialog(progress)
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
                conf_emoji = "✅"
                conf_text = "High"
            elif confidence >= 0.4:
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

            if confidence < 0.4:
                result.setInformativeText(
                    "⚠️ Low confidence detection!\n\n"
                    "The audio tracks may not have enough overlap,\n"
                    "or the sync offset might be inaccurate.\n\n"
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
            self._destroy_progress_dialog(progress)
            QMessageBox.critical(
                self,
                "Auto-Sync Failed",
                f"Failed to analyze audio sync:\n\n{str(e)}\n\n"
                "Make sure the clip has multiple audio tracks\n"
                "and that FFmpeg is installed."
            )
            self.append_log(f"❌ Auto-sync failed: {e}")

    def _destroy_progress_dialog(self, progress):
        progress.hide()
        progress.deleteLater()
        QApplication.processEvents()

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
                self.play_btn.setText("▶️ Play")
            else:
                self.video_widget.play()
                self.play_btn.setText("⏸️ Pause")
            return
            
        if self.video_widget.is_paused():
            self.video_widget.play()
            self.play_btn.setText("⏸️ Pause")
        else:
            self.video_widget.pause()
            self.play_btn.setText("▶️ Play")

    def play_timeline_sequence(self):
        self.load_timeline_sequence(play=True)

    def update_play_button(self):
        if self.video_widget:
            if not self.video_widget.is_paused():
                self.play_btn.setText("⏸️ Pause")
            else:
                self.play_btn.setText("▶️ Play")

    def seek_preview(self, value):
        if not self.video_widget:
            return
        dur = self.preview_duration_ms()
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
        if self.confirm("Clear Timeline", "Remove all clips from timeline?"):
            self.timeline.clear_timeline()
            self.update_timeline_duration()

    def update_timeline_duration(self):
        if self.timeline.clips:
            self.timeline_duration = sum(clip.get_trimmed_duration() for clip in self.timeline.clips)
        else:
            self.timeline_duration = 0
        self.update_estimated_size()
        
        # Keep EDL synced if we are currently looking at the full timeline
        if getattr(self, 'is_timeline_mode', False) and getattr(self, '_play_uses_timeline_edl', False):
            self.load_timeline_sequence(play=False)

    def zoom_in_timeline(self):
        self.timeline.zoom_in()

    def zoom_out_timeline(self):
        self.timeline.zoom_out()

    def on_codec_changed(self):
        idx = self.codec_combo.currentIndex()
        is_prores = idx == 0
        is_nvenc = idx in [1, 2]
        self.prores_label.setVisible(is_prores)
        self.prores_combo.setVisible(is_prores)
        self.nvenc_label.setVisible(is_nvenc)
        self.pixel_combo.setVisible(is_nvenc)
        self.export_target_label.setVisible(is_nvenc)
        self.export_target_combo.setVisible(is_nvenc)

        if is_prores:
            self.quality_slider.setMinimum(50)
            self.quality_slider.setMaximum(1000)
            self.quality_slider.setValue(500)
            self.update_quality_label(500)
        else:
            self.quality_slider.setMinimum(5)
            self.quality_slider.setMaximum(500)
            self.quality_slider.setValue(100)
            self.update_quality_label(100)

        self.update_estimated_size()

    def update_quality_label(self, value):
        self.quality_value_label.setText(f"{value} Mbps")
        self.update_estimated_size()

        codec_idx = self.codec_combo.currentIndex()
        decode_status = "HW Decode ON" if self.gpu_decode_check.isChecked() else "SW Decode"
        if codec_idx == 0:
            self.gpu_info.setText(f"✅ ProRes {PRORES_PROFILE_NAMES[self.prores_combo.currentIndex()]} (~{value} Mbps CBR) | {decode_status}")
        elif codec_idx == 3:
            self.gpu_info.setText("✅ Remux (Copy Stream) | No encoding processing")
        else:
            target_idx = self.export_target_combo.currentIndex()
            target_name = get_export_target_labels()[target_idx]
            preset = get_nvenc_preset_for_target(target_idx).upper()
            self.gpu_info.setText(f"✅ GPU: NVENC {preset} ({value} Mbps CBR) | {target_name} | {decode_status}")

    def update_estimated_size(self):
        if self.timeline_duration > 0:
            duration = self.timeline_duration
        else:
            duration = 60

        bitrate_mbps = self.quality_slider.value()
        video_size_mb = (bitrate_mbps * duration) / 8

        audio_codec_idx = self.audio_combo.currentIndex()
        if audio_codec_idx == 2:
            audio_bitrate_kbps = 320
        elif audio_codec_idx in [0, 1]:
            audio_bitrate_kbps = 2304
        else:
            audio_bitrate_kbps = 320

        audio_size_mb = (audio_bitrate_kbps * duration) / (8 * 1024)

        total_size_mb = video_size_mb + audio_size_mb
        total_size_gb = total_size_mb / 1024

        if self.timeline_duration > 0:
            self.estimated_size_label.setText(f"Estimated Size: {total_size_gb:.2f} GB ({total_size_mb:.0f} MB) for {duration:.1f}s timeline")
        else:
            self.estimated_size_label.setText(f"Estimated Size: ~{total_size_gb:.2f} GB per minute")

    def export_timeline(self):
        if not self.timeline.clips:
            QMessageBox.warning(self, "Empty Timeline", "Add clips to timeline before exporting")
            return
        settings = self.get_settings()
        ext = ".mov"
        output_file, _ = QFileDialog.getSaveFileName(self, "Export Timeline As", f"timeline_export{ext}", f"Video Files (*{ext})")
        if not output_file:
            return

        bitrate = self.quality_slider.value()
        export_target = get_export_target_labels()[self.export_target_combo.currentIndex()]
        filters_state = "ON" if settings.get('has_optional_filters') else "OFF"
        if not self.confirm("Export Timeline", f"Export {len(self.timeline.clips)} clips?\n\nCodec: {settings['video_codec'].upper()}\nExport target: {export_target}\nOptional filters: {filters_state}\nBitrate: {bitrate} Mbps (CBR - Constant)\nContainer: MOV\n\n✅ This will maintain consistent quality throughout!"):
            return
        self.export_timeline_btn.setEnabled(False)
        self.stop_export_btn.setEnabled(True)
        self.timeline_export_thread = TimelineExportThread(self.timeline, output_file, settings)
        self.timeline_export_thread.progress.connect(self.progress_bar.setValue)
        self.timeline_export_thread.status.connect(self.status_label.setText)
        self.timeline_export_thread.log_message.connect(self.append_log)
        self.timeline_export_thread.finished.connect(self.timeline_export_done)
        self.timeline_export_thread.playhead_update.connect(self.timeline.set_playhead_position)
        self.progress_bar.setValue(0)
        self.status_label.setText("Exporting with CBR...")
        self.timeline_export_thread.start()

    def timeline_export_done(self, success, msg):
        self.export_timeline_btn.setEnabled(True)
        self.stop_export_btn.setEnabled(False)
        if success:
            QMessageBox.information(self, "Export Complete", msg)
            self.progress_bar.setValue(100)
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
            self.log_text.append("\n=== Render cancelled by user ===\n")

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
        self.codec_combo.setCurrentIndex(0)
        self.prores_combo.setCurrentIndex(5)
        self.pixel_combo.setCurrentIndex(1)
        self.export_target_combo.setCurrentIndex(0)
        self.audio_combo.setCurrentIndex(0)
        self.apply_auto_hardware_settings()
        self.threads_spin.setValue(0)
        self.quality_slider.setValue(500 if self.codec_combo.currentIndex() == 0 else 100)
        self.denoise_combo.setCurrentIndex(0)
        self.deflicker_combo.setCurrentIndex(0)
        self.exposure_combo.setCurrentIndex(0)
        self.temporal_combo.setCurrentIndex(0)
        self.sharpness_combo.setCurrentIndex(0)
        self.on_codec_changed()
        self.save_settings()

    def get_settings(self):
        codec_map = {0: "prores_ks", 1: "h264_nvenc", 2: "hevc_nvenc", 3: "copy"}
        audio_map = {0: "pcm_s24le", 1: "pcm_s16le", 2: "aac", 3: "copy"}

        timeline_fps = TIMELINE_FPS_VALUES[self.timeline_fps_combo.currentIndex()]
        export_res_index = self.export_res_combo.currentIndex()
        scale_algo = SCALE_ALGOS[self.scale_algo_combo.currentIndex()]

        settings = {
            'video_codec': codec_map[self.codec_combo.currentIndex()],
            'prores_profile': self.prores_combo.currentIndex(),
            'pixel_format': self.pixel_combo.currentIndex(),
            'export_target_index': self.export_target_combo.currentIndex(),
            'audio_codec': audio_map[self.audio_combo.currentIndex()],
            'use_gpu': self.gpu_check.isChecked(),
            'use_gpu_decode': self.gpu_decode_check.isChecked(),
            'threads': self.threads_spin.value(),
            'bitrate_mbps': self.quality_slider.value(),
            'denoise_level': self.denoise_combo.currentIndex(),
            'deflicker_level': self.deflicker_combo.currentIndex(),
            'exposure_level': self.exposure_combo.currentIndex(),
            'temporal_level': self.temporal_combo.currentIndex(),
            'sharpness_level': self.sharpness_combo.currentIndex(),
            'timeline_fps': timeline_fps,
            'export_res_index': export_res_index,
            'scale_algo': scale_algo,
        }
        settings['has_optional_filters'] = has_optional_video_filters(settings)
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
        ext = ".mov"
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

            for clip_data in project_data.get("clips", []):
                clip = TimelineClip.from_dict(clip_data)
                if not os.path.exists(clip.file_path):
                    QMessageBox.warning(self, "Missing Media", f"Could not find media: {clip.file_path}")
                    continue
                self.timeline.add_clip(clip)

            self.update_timeline_duration()
            self.status_label.setText(f"Project loaded: {Path(file_path).name}")

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load project: {e}")

    def closeEvent(self, event):
        self.save_settings()

        if self.video_widget:
            try:
                self.video_widget.shutdown()
            except:
                pass

        if self.encoding_thread and self.encoding_thread.isRunning():
            if not self.confirm("Active", "Stop and quit?"):
                event.ignore()
                return
            self.encoding_thread.stop()
            self.encoding_thread.wait()
        if self.timeline_export_thread and self.timeline_export_thread.isRunning():
            if not self.confirm("Export Active", "Stop export and quit?"):
                event.ignore()
                return
            self.timeline_export_thread.stop()
            self.timeline_export_thread.wait()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setDesktopFileName("FastEncodePro")
    app.setStyle("Fusion")
    window = FastEncodeProApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
