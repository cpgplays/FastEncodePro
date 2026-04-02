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

MPV_AVAILABLE = False
try:
    import mpv
    MPV_AVAILABLE = True
    print("✅ python-mpv available")
except ImportError:
    print("⚠️  python-mpv not installed - install with: sudo pacman -S python-mpv")

from PyQt6.QtWidgets import *
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSettings, QUrl, QPointF, QTimer, QEvent, QPoint, QRectF, QObject, QSize
from PyQt6.QtGui import QFont, QPalette, QColor, QPainter, QBrush, QPen, QCursor, QAction, QPainterPath, QMouseEvent, QImage, QPixmap

__version__ = "0.9.0"
__author__ = "cpgplays"

# --- HELPER FUNCTIONS ---

def get_audio_stream_count_static(filepath):
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index', '-of', 'csv=p=0', filepath]
        out = subprocess.check_output(cmd).decode().strip()
        if not out: return 0
        return len(out.splitlines())
    except:
        return 1

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

            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        if not MPV_AVAILABLE:
            layout = QVBoxLayout(self)
            error_label = QLabel("⚠️ MPV Not Available\n\nInstall: sudo pacman -S python-mpv")
            error_label.setStyleSheet("color: #ef4444; font-size: 14pt; font-weight: bold;")
            error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(error_label)
            self.mpv = None
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

        self.current_file = None
        self._is_paused = True
        self._duration_ms = 0
        self._position_ms = 0
        self._pending_audio_filter = None

        self.position_timer = QTimer(self)
        self.position_timer.timeout.connect(self._update_position)
        self.position_timer.setInterval(100)

        self.setStyleSheet("background-color: #0f172a;")
        self.setMinimumSize(640, 360)

        self.mpv = None
        self._init_mpv()

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
                    self._duration_ms = int(value * 1000)
                    self.durationChanged.emit(self._duration_ms)

            @self.mpv.property_observer('time-pos')
            def position_observer(_name, value):
                if value is not None:
                    self._position_ms = int(value * 1000)

            @self.mpv.property_observer('pause')
            def pause_observer(_name, value):
                self._is_paused = value

            @self.mpv.event_callback('file-loaded')
            def file_loaded_handler(event):
                if self._pending_audio_filter:
                    try:
                        self.mpv.lavfi_complex = self._pending_audio_filter
                    except Exception as e:
                        pass
                    self._pending_audio_filter = None

        except Exception as e:
            self.mpv = None

    def load_file(self, file_path):
        if not self.mpv:
            return False
        try:
            self.mpv.lavfi_complex = ""
            self._pending_audio_filter = None
            self.current_file = file_path
            self.mpv.loadfile(file_path)
            self.mpv.pause = True
            self._is_paused = True
            return True
        except Exception as e:
            return False

    def play(self):
        if not self.mpv or not self.current_file: return
        self.mpv.pause = False
        self._is_paused = False
        self.position_timer.start()

    def pause(self):
        if not self.mpv: return
        self.mpv.pause = True
        self._is_paused = True
        self.position_timer.stop()

    def is_paused(self):
        return self._is_paused

    def seek(self, position_ms):
        if not self.mpv: return
        try:
            self.mpv.seek(position_ms / 1000.0, reference='absolute')
            self._position_ms = position_ms
        except:
            pass

    def position(self):
        return self._position_ms

    def duration(self):
        return self._duration_ms

    def _update_position(self):
        self.positionChanged.emit(self._position_ms)

    def stop(self):
        if not self.mpv: return
        try:
            self.mpv.command('stop')
            self._is_paused = True
            self._position_ms = 0
            self.position_timer.stop()
        except:
            pass

    def set_audio_complex_filter(self, filter_string):
        if not self.mpv: return
        self._pending_audio_filter = filter_string
        try:
            if not self.mpv.core_idle:
                self.mpv.lavfi_complex = filter_string
        except Exception as e:
            pass

    def shutdown(self):
        self.position_timer.stop()
        if self.mpv:
            try:
                self.mpv.terminate()
            except:
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
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.file_path], capture_output=True, text=True, )
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

    def set_playhead_position(self, time, auto_scroll=True):
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
        self.timeline_clicked.emit()
        if event.button() == Qt.MouseButton.LeftButton:
            click_x = event.position().x()
            click_y = event.position().y()
            raw_time = self.x_to_time(click_x)
            click_time = self.get_snap_time(raw_time)

            if click_y < 40:
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
            self.selected_clip = None
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
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.file_path], capture_output=True, text=True, )
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
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=5)
        audio_tracks = [int(x) for x in result.stdout.strip().split('\n') if x]

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

        result = subprocess.run(extract1_cmd, capture_output=True, timeout=60)
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

        result = subprocess.run(extract2_cmd, capture_output=True, timeout=60)
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


class TimelineRenderingEngine:
    """
    MASTER CANVAS COMPOSITOR ENGINE (v0.9.0)
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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            data = json.loads(result.stdout)
            stream = data['streams'][0]
            return stream['width'], stream['height']
        except:
            return 1920, 1080

    def _get_video_codec(self, file_path):
        try:
            cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries', 'stream=codec_name', '-of', 'json', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            data = json.loads(result.stdout)
            return data['streams'][0]['codec_name']
        except:
            return 'unknown'

    def _build_video_filters(self):
        filters = []
        denoise = self.settings.get('denoise_level', 0)
        if denoise > 0:
            vals = ['', 'hqdn3d=1.5:1.5:6:6', 'hqdn3d=2:2:8:8', 'hqdn3d=3:3:10:10',
                    'hqdn3d=4:4:12:12', 'hqdn3d=6:6:15:15', 'hqdn3d=8:8:18:18']
            if denoise < len(vals): filters.append(vals[denoise])
        deflicker = self.settings.get('deflicker_level', 0)
        if deflicker > 0:
            vals = ['', 'deflicker=mode=pm:size=5', 'deflicker=mode=pm:size=10',
                    'deflicker=mode=pm:size=15', 'deflicker=mode=am:size=20', 'deflicker=mode=am:size=30']
            if deflicker < len(vals): filters.append(vals[deflicker])
        exposure = self.settings.get('exposure_level', 0)
        if exposure > 0:
            exp_map = {
                1: 'eq=brightness=0.05:saturation=1.1',   2: 'eq=brightness=0.1:saturation=1.15',
                3: 'eq=brightness=0.15:saturation=1.2',   4: 'eq=brightness=0.2:saturation=1.25',
                5: 'eq=brightness=0.3:saturation=1.3',    6: 'eq=brightness=0.4:saturation=1.35',
                7: 'eq=brightness=-0.05:saturation=0.95', 8: 'eq=brightness=-0.1:saturation=0.9',
                9: 'eq=brightness=-0.15:saturation=0.85', 10: 'eq=brightness=-0.2:saturation=0.8',
                11: 'eq=brightness=-0.3:saturation=0.75', 12: 'eq=brightness=-0.4:saturation=0.7',
            }
            if exposure in exp_map: filters.append(exp_map[exposure])
        temporal = self.settings.get('temporal_level', 0)
        if temporal > 0:
            vals = ['', 'tmix=frames=3:weights="1 1 1"', 'tmix=frames=5:weights="1 1 2 1 1"',
                    'tmix=frames=7:weights="1 1 2 2 2 1 1"', 'tmix=frames=9:weights="1 1 2 3 3 3 2 1 1"',
                    'tmix=frames=11:weights="1 2 2 3 4 4 4 3 2 2 1"']
            if temporal < len(vals): filters.append(vals[temporal])
        sharpness = self.settings.get('sharpness_level', 0)
        if sharpness > 0:
            vals = ['', 'unsharp=3:3:0.3:3:3:0', 'unsharp=5:5:0.5:5:5:0', 'unsharp=5:5:0.8:5:5:0.4',
                    'unsharp=5:5:1.2:5:5:0.6', 'unsharp=7:7:1.5:7:7:0.8', 'unsharp=7:7:2.0:7:7:1.0']
            if sharpness < len(vals): filters.append(vals[sharpness])
        return filters

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

            cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats']

            # Global Hardware Decoding if requested
            use_gpu = self.settings.get('use_gpu_decode', False)

            # 1. ADD INPUTS
            for clip in sorted_clips:
                codec = self._get_video_codec(clip.file_path)
                # Don't try to HW Decode AV1 on RTX 20 series
                if use_gpu and codec != 'av1':
                    cmd.extend(['-hwaccel', 'cuda'])
                cmd.extend(['-i', clip.file_path])

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

                # Trim the clip to its in/out points and reset timestamps to 0
                filter_complex.append(f"{v_in}trim=start={clip.in_point}:end={clip.out_point},setpts=PTS-STARTPTS{v_trimmed}")

                # Scale the clip perfectly into the canvas dimensions, padding with black if aspect ratio mismatches
                scale_str = f"scale={export_width}:{export_height}:force_original_aspect_ratio=decrease,pad={export_width}:{export_height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={timeline_fps}"
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

            # Apply global user filters
            user_filters = self._build_video_filters()
            if user_filters:
                filter_complex.append(f"{last_v}{','.join(user_filters)}[out_v]")
                map_v = "[out_v]"
            else:
                map_v = last_v

            # Mix Audio
            if audio_inputs:
                inputs_str = "".join(audio_inputs)
                filter_complex.append(f"{inputs_str}amix=inputs={len(audio_inputs)}:duration=first:dropout_transition=0[out_a]")
                map_a = "[out_a]"
            else:
                # Generate silent audio track if completely muted
                filter_complex.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={timeline_duration}[out_a]")
                map_a = "[out_a]"

            # Append graph to command
            cmd.extend(['-filter_complex', ';'.join(filter_complex)])
            cmd.extend(['-map', map_v, '-map', map_a])

            # 3. ENCODER SETTINGS
            codec = self.settings.get('video_codec', 'hevc_nvenc')
            cmd.extend(['-c:v', codec])
            if 'nvenc' in codec:
                bitrate_kbps = int(self.settings.get('bitrate_mbps', 100) * 1000)
                pixel_format = self.settings.get('pixel_format', 0)
                pix_fmt = 'yuv420p' if pixel_format == 0 else 'p010le'
                cmd.extend([
                    '-preset', 'p7', '-tune', 'hq', '-rc', 'cbr',
                    '-b:v', f'{bitrate_kbps}k', '-maxrate', f'{bitrate_kbps}k',
                    '-bufsize', f'{int(bitrate_kbps * 2)}k',
                    '-g', str(int(timeline_fps * 2)),
                    '-pix_fmt', pix_fmt,
                ])

            # Set audio encode and explicitly clamp total duration
            cmd.extend([
                '-c:a', 'aac', '-b:a', '320k',
                '-movflags', '+faststart',
                '-t', f"{timeline_duration:.6f}",
                self.output_path
            ])

            self.log(f"Compositing execution started...")

            start_time = time.time()
            self.encoder_process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True
            )

            # 4. MONITOR PROGRESS
            for line in iter(self.encoder_process.stderr.readline, ''):
                if self.should_stop:
                    self.encoder_process.kill()
                    return False, "Render cancelled by user"

                t = _parse_ffmpeg_time(line)
                if t is not None and timeline_duration > 0:
                    pct = min(99, int((t / timeline_duration) * 100))
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
            self.process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, universal_newlines=True, bufsize=1)
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
        if self.settings.get('use_gpu_decode', True):
            cmd.extend(['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'])
        cmd.extend(['-i', self.input_file])
        filter_complex = []

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
        codec = self.settings['video_codec']
        cmd.extend(['-c:v', codec])
        if codec == 'prores_ks':
            profile = self.settings['prores_profile']
            target_bitrate_mbps = self.settings.get('bitrate_mbps', 500)
            qscale = 9 if target_bitrate_mbps >= 500 else 11 if target_bitrate_mbps >= 300 else 13 if target_bitrate_mbps >= 150 else 15
            cmd.extend(['-profile:v', str(profile), '-vendor', 'apl0', '-qscale:v', str(qscale)])
        elif 'nvenc' in codec:
            if self.settings['use_gpu']:
                target_bitrate_mbps = self.settings.get('bitrate_mbps', 100)
                target_bitrate_kbps = int(target_bitrate_mbps * 1000)
                cmd.extend(['-rc', 'cbr', '-b:v', f'{target_bitrate_kbps}k', '-maxrate', f'{target_bitrate_kbps}k',
                            '-bufsize', f'{int(target_bitrate_kbps * 2)}k', '-preset', 'p7', '-tune', 'hq',
                            '-g', '60', '-bf', '3', '-b_ref_mode', 'middle'])
                if self.settings['pixel_format'] == 0: cmd.extend(['-pix_fmt', 'yuv420p'])
                else: cmd.extend(['-pix_fmt', 'p010le'])
            else: cmd.extend(['-preset', 'medium'])
        cmd.extend(['-c:a', self.settings['audio_codec']])
        if self.settings['audio_codec'] == 'aac': cmd.extend(['-b:a', '320k'])
        if self.settings['threads'] > 0: cmd.extend(['-threads', str(self.settings['threads'])])
        cmd.append(self.output_file)
        return cmd

    def get_duration(self):
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.input_file], capture_output=True, text=True, )
            return float(result.stdout.strip())
        except:
            return 0

    def stop(self):
        self.should_stop = True
        if self.process:
            try: self.process.kill()
            except: pass


class FastEncodeProApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"FastEncode Pro v{__version__} - Accessible Video Editor")
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

        self.dwell_filter = DwellClickFilter(self)

        self.app_settings = QSettings("FastEncodePro", "App")
        self.output_folder = self.app_settings.value("output_folder", "")

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
        save_proj_btn = QPushButton("💾 Save Project")
        save_proj_btn.setStyleSheet(self.button_style("#3b82f6"))
        save_proj_btn.setMinimumHeight(40)
        save_proj_btn.clicked.connect(self.save_project)
        project_controls.addWidget(save_proj_btn)

        load_proj_btn = QPushButton("📂 Load Project")
        load_proj_btn.setStyleSheet(self.button_style("#f59e0b"))
        load_proj_btn.setMinimumHeight(40)
        load_proj_btn.clicked.connect(self.load_project)
        project_controls.addWidget(load_proj_btn)
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
        add_media_btn = QPushButton("➕ Add Media")
        add_media_btn.setStyleSheet(self.button_style("#4ade80"))
        add_media_btn.setMinimumHeight(50)
        add_media_btn.clicked.connect(self.add_media_to_library)
        lib_buttons.addWidget(add_media_btn)
        remove_media_btn = QPushButton("➖ Remove")
        remove_media_btn.setStyleSheet(self.button_style("#ef4444"))
        remove_media_btn.setMinimumHeight(50)
        remove_media_btn.clicked.connect(self.remove_from_library)
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
        self.play_btn = QPushButton("▶️ Play")
        self.play_btn.setStyleSheet(self.button_style("#3b82f6"))
        self.play_btn.setMinimumHeight(50)
        self.play_btn.clicked.connect(self.toggle_play)
        controls_row.addWidget(self.play_btn)
        self.fullscreen_btn = QPushButton("⛶ Fullscreen")
        self.fullscreen_btn.setStyleSheet(self.button_style("#8b5cf6"))
        self.fullscreen_btn.setMinimumHeight(50)
        self.fullscreen_btn.clicked.connect(self.enter_fullscreen)
        controls_row.addWidget(self.fullscreen_btn)
        preview_layout.addLayout(controls_row)

        trim_panel = QWidget()
        trim_layout = QHBoxLayout(trim_panel)
        trim_layout.setContentsMargins(0, 5, 0, 5)

        trim_box = QGroupBox("✂️ Trim")
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

        mixer_box = QGroupBox("🎚️ Audio Mixer")
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
        self.auto_sync_btn = QPushButton("🎯 Auto-Sync Audio")
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
        layout.addWidget(top_section, stretch=3)
        timeline_section = QWidget()
        timeline_layout = QVBoxLayout(timeline_section)
        timeline_layout.setContentsMargins(5, 5, 5, 5)
        timeline_header = QHBoxLayout()
        timeline_title = QLabel("🎞️ TIMELINE")
        timeline_title.setStyleSheet("font-size: 14pt; font-weight: bold; color: #f59e0b; padding: 5px;")
        timeline_header.addWidget(timeline_title)
        timeline_header.addStretch()
        zoom_in_btn = QPushButton("🔍+")
        zoom_in_btn.setStyleSheet(self.button_style("#6366f1"))
        zoom_in_btn.setFixedSize(60, 40)
        zoom_in_btn.clicked.connect(self.zoom_in_timeline)
        timeline_header.addWidget(zoom_in_btn)
        zoom_out_btn = QPushButton("🔍−")
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
        add_to_timeline_btn = QPushButton("➕ Add to Timeline")
        add_to_timeline_btn.setStyleSheet(self.button_style("#4ade80"))
        add_to_timeline_btn.setMinimumHeight(50)
        add_to_timeline_btn.clicked.connect(self.add_to_timeline)
        timeline_controls.addWidget(add_to_timeline_btn)
        remove_from_timeline_btn = QPushButton("➖ Remove Clip")
        remove_from_timeline_btn.setStyleSheet(self.button_style("#ef4444"))
        remove_from_timeline_btn.setMinimumHeight(50)
        remove_from_timeline_btn.clicked.connect(self.remove_from_timeline)
        timeline_controls.addWidget(remove_from_timeline_btn)
        clear_timeline_btn = QPushButton("🗑️ Clear All")
        clear_timeline_btn.setStyleSheet(self.button_style("#dc2626"))
        clear_timeline_btn.setMinimumHeight(50)
        clear_timeline_btn.clicked.connect(self.clear_timeline)
        timeline_controls.addWidget(clear_timeline_btn)
        self.export_timeline_btn = QPushButton("💾 EXPORT TIMELINE")
        self.export_timeline_btn.setStyleSheet(self.button_style("#8b5cf6"))
        self.export_timeline_btn.setMinimumHeight(50)
        self.export_timeline_btn.clicked.connect(self.export_timeline)
        timeline_controls.addWidget(self.export_timeline_btn)
        self.stop_export_btn = QPushButton("⏹️ STOP RENDER")
        self.stop_export_btn.setStyleSheet(self.button_style("#ef4444"))
        self.stop_export_btn.setMinimumHeight(50)
        self.stop_export_btn.setEnabled(False)
        self.stop_export_btn.clicked.connect(self.stop_timeline_export)
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
        codec_row = QHBoxLayout()
        codec_row.addWidget(QLabel("Codec:"))
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["ProRes", "H.264 (NVENC)", "H.265/HEVC (NVENC)"])
        self.codec_combo.setCurrentIndex(0)
        self.codec_combo.setStyleSheet(self.combo_style())
        self.codec_combo.currentIndexChanged.connect(self.on_codec_changed)
        codec_row.addWidget(self.codec_combo)
        codec_layout.addLayout(codec_row)

        prores_row = QHBoxLayout()
        self.prores_label = QLabel("ProRes Profile:")
        prores_row.addWidget(self.prores_label)
        self.prores_combo = QComboBox()
        self.prores_combo.addItems(["Proxy", "LT", "Standard", "HQ", "4444", "4444 XQ"])
        self.prores_combo.setCurrentIndex(5)
        self.prores_combo.setStyleSheet(self.combo_style())
        self.prores_combo.currentIndexChanged.connect(self.update_estimated_size)
        prores_row.addWidget(self.prores_combo)
        codec_layout.addLayout(prores_row)

        nvenc_row = QHBoxLayout()
        self.nvenc_label = QLabel("Pixel Format:")
        nvenc_row.addWidget(self.nvenc_label)
        self.pixel_combo = QComboBox()
        self.pixel_combo.addItems(["8-bit (yuv420p)", "10-bit (p010le)"])
        self.pixel_combo.setCurrentIndex(1)
        self.pixel_combo.setStyleSheet(self.combo_style())
        nvenc_row.addWidget(self.pixel_combo)
        codec_layout.addLayout(nvenc_row)

        codec_group.setLayout(codec_layout)
        scroll_layout.addWidget(codec_group)

        timeline_group = QGroupBox("🎞️ Timeline Settings")
        timeline_group.setStyleSheet(self.groupbox_style())
        timeline_layout = QVBoxLayout()

        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("Timeline FPS:"))
        self.timeline_fps_combo = QComboBox()
        self.timeline_fps_combo.addItems(["23.976", "24", "25", "29.97", "30", "50", "60", "120"])
        self.timeline_fps_combo.setCurrentIndex(6)
        self.timeline_fps_combo.setStyleSheet(self.combo_style())
        fps_row.addWidget(self.timeline_fps_combo)
        timeline_layout.addLayout(fps_row)

        res_row = QHBoxLayout()
        res_row.addWidget(QLabel("Export Resolution:"))
        self.export_res_combo = QComboBox()
        self.export_res_combo.addItems(["Source", "1920x1080", "2560x1440", "3840x2160", "5120x2880", "7680x4320"])
        self.export_res_combo.setCurrentIndex(0)
        self.export_res_combo.setStyleSheet(self.combo_style())
        res_row.addWidget(self.export_res_combo)
        timeline_layout.addLayout(res_row)

        scale_row = QHBoxLayout()
        scale_row.addWidget(QLabel("Upscale Quality:"))
        self.scale_algo_combo = QComboBox()
        self.scale_algo_combo.addItems(["Bilinear", "Bicubic", "Lanczos", "Spline"])
        self.scale_algo_combo.setCurrentIndex(2)
        self.scale_algo_combo.setStyleSheet(self.combo_style())
        scale_row.addWidget(self.scale_algo_combo)
        timeline_layout.addLayout(scale_row)

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
        audio_row = QHBoxLayout()
        audio_row.addWidget(QLabel("Audio Codec:"))
        self.audio_combo = QComboBox()
        self.audio_combo.addItems(["PCM 24-bit", "PCM 16-bit", "AAC 320kbps", "Copy Stream"])
        self.audio_combo.setStyleSheet(self.combo_style())
        audio_row.addWidget(self.audio_combo)
        audio_layout.addLayout(audio_row)
        audio_group.setLayout(audio_layout)
        scroll_layout.addWidget(audio_group)

        filters_group = QGroupBox("🎨 Filters (Optional)")
        filters_group.setStyleSheet(self.groupbox_style())
        filters_layout = QVBoxLayout()

        denoise_row = QHBoxLayout()
        denoise_row.addWidget(QLabel("Denoise:"))
        self.denoise_combo = QComboBox()
        self.denoise_combo.addItems(["Off", "Light", "Medium", "Heavy", "Very Heavy", "Nuclear", "Ultra Nuclear"])
        self.denoise_combo.setStyleSheet(self.combo_style())
        denoise_row.addWidget(self.denoise_combo)
        filters_layout.addLayout(denoise_row)

        deflicker_row = QHBoxLayout()
        deflicker_row.addWidget(QLabel("Deflicker:"))
        self.deflicker_combo = QComboBox()
        self.deflicker_combo.addItems(["Off", "Light", "Medium", "Strong", "Very Strong", "Maximum"])
        self.deflicker_combo.setStyleSheet(self.combo_style())
        deflicker_row.addWidget(self.deflicker_combo)
        filters_layout.addLayout(deflicker_row)

        exposure_row = QHBoxLayout()
        exposure_row.addWidget(QLabel("Exposure:"))
        self.exposure_combo = QComboBox()
        self.exposure_combo.addItems(["Off", "+0.25 EV", "+0.5 EV", "+0.75 EV", "+1.0 EV", "+1.5 EV", "+2.0 EV",
                                       "-0.25 EV", "-0.5 EV", "-0.75 EV", "-1.0 EV", "-1.5 EV", "-2.0 EV"])
        self.exposure_combo.setStyleSheet(self.combo_style())
        exposure_row.addWidget(self.exposure_combo)
        filters_layout.addLayout(exposure_row)

        temporal_row = QHBoxLayout()
        temporal_row.addWidget(QLabel("Temporal Smoothing:"))
        self.temporal_combo = QComboBox()
        self.temporal_combo.addItems(["Off", "Light", "Medium", "Strong", "Very Strong", "Maximum"])
        self.temporal_combo.setStyleSheet(self.combo_style())
        temporal_row.addWidget(self.temporal_combo)
        filters_layout.addLayout(temporal_row)

        sharpness_row = QHBoxLayout()
        sharpness_row.addWidget(QLabel("Sharpness:"))
        self.sharpness_combo = QComboBox()
        self.sharpness_combo.addItems(["Off", "Subtle", "Light", "Medium", "Strong", "Very Strong", "Ultra Sharp"])
        self.sharpness_combo.setStyleSheet(self.combo_style())
        sharpness_row.addWidget(self.sharpness_combo)
        filters_layout.addLayout(sharpness_row)

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

        gpu_decode_info = QLabel(
            "ℹ️ AV1 hardware decode requires RTX 30-series or newer\n"
            "   RTX 20-series: Keep OFF for AV1 files (use CPU decode)\n"
            "   RTX 30+: Can enable for faster AV1 preview"
        )
        gpu_decode_info.setStyleSheet("font-size: 9pt; color: #94a3b8; padding: 5px 20px;")
        gpu_decode_info.setWordWrap(True)
        perf_layout.addWidget(gpu_decode_info)

        threads_row = QHBoxLayout()
        threads_row.addWidget(QLabel("CPU Threads (0=auto):"))
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 64)
        self.threads_spin.setValue(0)
        self.threads_spin.setStyleSheet(self.spinbox_style())
        threads_row.addWidget(self.threads_spin)
        perf_layout.addLayout(threads_row)
        self.gpu_info = QLabel("✅ ProRes 4444 XQ (~500 Mbps)")
        self.gpu_info.setStyleSheet("font-size: 10pt; color: #4ade80; font-weight: bold; padding: 5px;")
        perf_layout.addWidget(self.gpu_info)
        perf_group.setLayout(perf_layout)
        scroll_layout.addWidget(perf_group)

        reset_btn = QPushButton("🔄 Reset All Settings")
        reset_btn.setStyleSheet(self.button_style("#ef4444"))
        reset_btn.setMinimumHeight(50)
        reset_btn.clicked.connect(self.reset_all)
        scroll_layout.addWidget(reset_btn)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
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
        add_btn = QPushButton("➕ Add Files")
        add_btn.setStyleSheet(self.button_style("#4ade80"))
        add_btn.setMinimumHeight(50)
        add_btn.clicked.connect(self.add_files)
        file_buttons.addWidget(add_btn)
        remove_btn = QPushButton("➖ Remove")
        remove_btn.setStyleSheet(self.button_style("#ef4444"))
        remove_btn.setMinimumHeight(50)
        remove_btn.clicked.connect(self.remove_selected)
        file_buttons.addWidget(remove_btn)
        clear_btn = QPushButton("🗑️ Clear All")
        clear_btn.setStyleSheet(self.button_style("#dc2626"))
        clear_btn.setMinimumHeight(50)
        clear_btn.clicked.connect(self.clear_files)
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
        browse_btn = QPushButton("📂 Browse")
        browse_btn.setStyleSheet(self.button_style("#3b82f6"))
        browse_btn.setMinimumHeight(40)
        browse_btn.clicked.connect(self.select_output)
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
        self.start_btn = QPushButton("▶️ START ENCODING")
        self.start_btn.setStyleSheet(self.button_style("#4ade80"))
        self.start_btn.setMinimumHeight(60)
        self.start_btn.clicked.connect(self.start_encoding)
        control_buttons.addWidget(self.start_btn)
        self.stop_btn = QPushButton("⏹️ STOP")
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
        if self.video_widget and self.video_widget.duration() > 0:
            slider_value = int((position_ms / self.video_widget.duration()) * 1000)
            self.preview_slider.setValue(slider_value)

        current_tc = self.format_timecode(position_ms)
        total_tc = self.format_timecode(self.video_widget.duration() if self.video_widget else 0)
        self.timecode_label.setText(f"{current_tc} / {total_tc}")

    def _on_duration_changed(self, duration_ms):
        """Handle video duration updates"""
        current_tc = self.format_timecode(self.video_widget.position() if self.video_widget else 0)
        total_tc = self.format_timecode(duration_ms)
        self.timecode_label.setText(f"{current_tc} / {total_tc}")

    def on_media_selected(self, item):
        self.is_timeline_mode = False
        row = self.media_list.row(item)
        if 0 <= row < len(self.media_library):
            self.current_media = self.media_library[row]
            file_path = self.current_media.file_path

            if self.video_widget and self.video_widget.load_file(file_path):
                self.video_widget.pause()

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
        self.is_timeline_mode = True
        self.trim_info.setText("Timeline Mode Active - Click Play to Preview Sequence")

    def on_timeline_clip_selected(self, clip):
        self.is_timeline_mode = True

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

        if self.video_widget.load_file(clip.file_path):
            self.video_widget.seek(int(clip.in_point * 1000))
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

        reply = QMessageBox.question(
            self,
            "Auto-Sync Audio",
            f"Analyze audio sync for: {clip.name}\n\n"
            "This will analyze the first 30 seconds to detect\n"
            "the sync offset between audio tracks.\n\n"
            "Track 0 (desktop) will be used as reference.\n"
            "Track 1 (mic) will be synchronized.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
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
            progress.hide()
            progress.deleteLater()
            QApplication.processEvents()
            QMessageBox.critical(
                self,
                "Auto-Sync Failed",
                f"Failed to analyze audio sync:\n\n{str(e)}\n\n"
                "Make sure the clip has multiple audio tracks\n"
                "and that FFmpeg is installed."
            )
            self.append_log(f"❌ Auto-sync failed: {e}")

    def on_timeline_playhead_moved(self, time):
        pass

    def toggle_play(self):
        if not self.video_widget:
            return

        if self.is_timeline_mode and self.video_widget.is_paused():
            self.play_timeline_sequence()
        elif self.video_widget.is_paused():
            self.video_widget.play()
            self.play_btn.setText("⏸️ Pause")
        else:
            self.video_widget.pause()
            self.play_btn.setText("▶️ Play")

    def play_timeline_sequence(self):
        if not self.timeline.clips:
            return

        edl_content = "# mpv EDL v0\n"
        sorted_clips = sorted(self.timeline.clips, key=lambda c: c.start_time)

        for clip in sorted_clips:
            length = clip.get_trimmed_duration()
            edl_content += f"{clip.file_path},{clip.in_point},{length}\n"

        try:
            fd, path = tempfile.mkstemp(suffix='.edl', text=True)
            with os.fdopen(fd, 'w') as f:
                f.write(edl_content)

            self.video_widget.set_audio_complex_filter("")

            if self.video_widget.load_file(path):
                if sorted_clips:
                    first_clip = sorted_clips[0]
                    self.apply_audio_mix_preview(first_clip.file_path, first_clip.volumes, first_clip.normalization)

                self.video_widget.seek(int(self.timeline.playhead_position * 1000))
                self.video_widget.play()
                self.play_btn.setText("⏸️ Pause")
        except Exception as e:
            pass

    def update_play_button(self):
        if self.video_widget:
            if not self.video_widget.is_paused():
                self.play_btn.setText("⏸️ Pause")
            else:
                self.play_btn.setText("▶️ Play")

    def seek_preview(self, value):
        if self.video_widget and self.video_widget.duration() > 0:
            position_ms = int((value / 1000.0) * self.video_widget.duration())
            self.video_widget.seek(position_ms)

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

    def update_timeline_duration(self):
        if self.timeline.clips:
            self.timeline_duration = sum(clip.get_trimmed_duration() for clip in self.timeline.clips)
        else:
            self.timeline_duration = 0
        self.update_estimated_size()

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
            profile_names = ["Proxy", "LT", "Standard", "HQ", "4444", "4444 XQ"]
            self.gpu_info.setText(f"✅ ProRes {profile_names[self.prores_combo.currentIndex()]} (~{value} Mbps CBR) | {decode_status}")
        else:
            self.gpu_info.setText(f"✅ GPU: NVENC ({value} Mbps CBR) | {decode_status}")

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
        reply = QMessageBox.question(self, "Export Timeline", f"Export {len(self.timeline.clips)} clips?\n\nCodec: {settings['video_codec'].upper()}\nBitrate: {bitrate} Mbps (CBR - Constant)\nContainer: MOV\n\n✅ This will maintain consistent quality throughout!", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
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
        self.audio_combo.setCurrentIndex(0)
        self.gpu_check.setChecked(True)
        self.gpu_decode_check.setChecked(False)
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
        codec_map = {0: "prores_ks", 1: "h264_nvenc", 2: "hevc_nvenc"}
        audio_map = {0: "pcm_s24le", 1: "pcm_s16le", 2: "aac", 3: "copy"}

        fps_values = [23.976, 24, 25, 29.97, 30, 50, 60, 120]
        timeline_fps = fps_values[self.timeline_fps_combo.currentIndex()]

        export_res_index = self.export_res_combo.currentIndex()

        scale_algos = ['bilinear', 'bicubic', 'lanczos', 'spline']
        scale_algo = scale_algos[self.scale_algo_combo.currentIndex()]

        settings = {
            'video_codec': codec_map[self.codec_combo.currentIndex()],
            'prores_profile': self.prores_combo.currentIndex(),
            'pixel_format': self.pixel_combo.currentIndex(),
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


def main():
    app = QApplication(sys.argv)
    app.setDesktopFileName("FastEncodePro")
    app.setStyle("Fusion")
    window = FastEncodeProApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
