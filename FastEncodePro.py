#!/usr/bin/env python3
"""
FastEncode Pro - Timeline Edition v0.08
GPU-Accelerated Video Editor with Full MKV Support

v0.08 Features:
- MKV Video Preview (MPV Integration)
- AV1 Codec Support (CPU Decode Fallback)
- Fixed Audio Rendering (Proper Error Handling)
- Fixed Merge Crashes (Non-Blocking Merge)
- Dwell Clicking & Eye Tracking Support
"""

import sys
import shutil
import os
import subprocess
import tempfile
import json
import time
import math
from pathlib import Path

# Try to import MPV for MKV support
print("Checking for python-mpv library...")
try:
    import mpv
    MPV_AVAILABLE = True
    print(f"✅ python-mpv found! Version: {mpv.__version__ if hasattr(mpv, '__version__') else 'unknown'}")
except ImportError as e:
    MPV_AVAILABLE = False
    print("=" * 60)
    print("❌ WARNING: python-mpv not installed. MKV preview disabled.")
    print(f"   Import error: {e}")
    print("=" * 60)
    print("Install instructions:")
    print("")
    print("Arch/Manjaro/CachyOS:")
    print("  sudo pacman -S mpv python-mpv")
    print("")
    print("Debian/Ubuntu:")
    print("  sudo apt install libmpv-dev mpv")
    print("  pip install python-mpv --break-system-packages")
    print("")
    print("Fedora:")
    print("  sudo dnf install mpv python3-mpv")
    print("=" * 60)
    print("")

from PyQt6.QtWidgets import *
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSettings, QUrl, QPointF, QTimer, QEvent, QPoint, QRectF, QObject
from PyQt6.QtGui import QFont, QPalette, QColor, QPainter, QBrush, QPen, QCursor, QAction, QPainterPath, QMouseEvent
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

__version__ = "0.08"
__author__ = "cpgplays"

# --- ACCESSIBILITY CLASSES ---

class DwellClickOverlay(QWidget):
    """Visual indicator for Dwell Clicking (Eye Tracking)"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(60, 60)
        self.progress = 0.0  # 0.0 to 1.0
        self.active = False
        
    def update_progress(self, value):
        self.progress = value
        self.update()
        
    def paintEvent(self, event):
        if not self.active or self.progress <= 0:
            return
            
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Draw background circle (transparent grey)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 100))
        painter.drawEllipse(5, 5, 50, 50)
        
        # Draw progress arc (Green)
        pen = QPen(QColor("#4ade80"))
        pen.setWidth(6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        
        # 360 * 16 (qt uses 1/16th degrees)
        span_angle = int(-self.progress * 360 * 16)
        painter.drawArc(10, 10, 40, 40, 90 * 16, span_angle)

class DwellClickFilter(QObject):
    """
    Global event filter to detect lack of mouse movement.
    Simulates a click if mouse hovers in same spot for 'dwell_time'.
    """
    click_triggered = pyqtSignal(QPoint)
    progress_update = pyqtSignal(float)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer()
        self.timer.setInterval(50) # Check every 50ms
        self.timer.timeout.connect(self.check_dwell)
        self.enabled = False
        
        self.last_pos = QPoint(0, 0)
        self.dwell_start_time = 0
        self.dwell_duration = 1.2 # seconds
        self.jitter_threshold = 10 # pixels radius
        
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
            # Mouse moved too much, reset
            self.last_pos = current_pos
            self.dwell_start_time = time.time()
            self.overlay.active = False
            self.overlay.update_progress(0)
            self.overlay.move(current_pos.x() - 30, current_pos.y() - 30)
        else:
            # Mouse is stationary (dwelling)
            elapsed = time.time() - self.dwell_start_time
            progress = min(1.0, elapsed / self.dwell_duration)
            
            self.overlay.move(current_pos.x() - 30, current_pos.y() - 30)
            self.overlay.active = True
            self.overlay.update_progress(progress)
            
            if elapsed >= self.dwell_duration:
                # Trigger Click
                self.dwell_start_time = time.time() # Reset immediately to prevent double clicks
                self.overlay.update_progress(0)
                self.perform_click(current_pos)

    def perform_click(self, pos):
        # We need to temporarily disable the overlay so we don't click IT
        self.overlay.hide()
        
        # Get the widget at the position
        widget = QApplication.widgetAt(pos)
        if widget:
            # Create a localized click event
            local_pos = widget.mapFromGlobal(pos)
            QTest_click = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(local_pos), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(widget, QTest_click)
            QTest_release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(local_pos), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(widget, QTest_release)
            
        # Restore overlay
        QTimer.singleShot(100, self.overlay.show)

# --- END ACCESSIBILITY CLASSES ---

class FullscreenVideoPlayer(QWidget):
    """Fullscreen video player with always-visible overlay controls"""

    def __init__(self, player, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.player = player
        self.setWindowState(Qt.WindowState.WindowFullScreen)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet("background-color: black;")
        self.original_video_output = self.player.videoOutput()
        self.was_playing = (self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.video_widget = QVideoWidget()
        self.video_widget.setStyleSheet("background-color: black;")
        main_layout.addWidget(self.video_widget, stretch=1)
        controls_panel = QWidget()
        controls_panel.setStyleSheet("""
            QWidget {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(0,0,0,200), stop:1 rgba(0,0,0,240));
                padding: 20px;
            }
        """)
        controls_layout = QVBoxLayout(controls_panel)
        controls_layout.setSpacing(15)
        controls_layout.setContentsMargins(40, 20, 40, 20)
        self.timecode_label = QLabel("00:00:00 / 00:00:00")
        self.timecode_label.setStyleSheet("""
            QLabel {
                color: white;
                font-size: 28pt;
                font-weight: bold;
                background: transparent;
            }
        """)
        self.timecode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        controls_layout.addWidget(self.timecode_label)
        self.scrubber = QSlider(Qt.Orientation.Horizontal)
        self.scrubber.setMinimum(0)
        self.scrubber.setMaximum(1000)
        self.scrubber.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.scrubber.setStyleSheet("""
            QSlider {
                min-height: 60px;
                max-height: 60px;
                background: transparent;
            }
            QSlider::groove:horizontal {
                border: none;
                height: 30px;
                background: rgba(100, 100, 100, 255);
                border-radius: 15px;
            }
            QSlider::handle:horizontal {
                background: #3b82f6;
                border: 4px solid white;
                width: 50px;
                height: 50px;
                margin: -10px 0;
                border-radius: 25px;
            }
            QSlider::sub-page:horizontal {
                background: #3b82f6;
                border-radius: 15px;
            }
        """)
        controls_layout.addWidget(self.scrubber)
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(20)
        buttons_row.addStretch()
        self.play_pause_btn = QPushButton("⏸️ PAUSE")
        self.play_pause_btn.setMinimumSize(300, 80)
        self.play_pause_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus) 
        self.play_pause_btn.setStyleSheet("""
            QPushButton {
                background-color: #3b82f6;
                color: white;
                font-size: 24pt;
                font-weight: bold;
                border-radius: 15px;
                border: 4px solid white;
            }
            QPushButton:hover {
                background-color: #2563eb;
            }
            QPushButton:focus {
                border: 6px solid #f59e0b; /* Orange focus for switches */
            }
            QPushButton:pressed {
                background-color: #1d4ed8;
            }
        """)
        self.play_pause_btn.clicked.connect(self.toggle_playback)
        buttons_row.addWidget(self.play_pause_btn)
        self.exit_btn = QPushButton("✕ EXIT")
        self.exit_btn.setMinimumSize(200, 80)
        self.exit_btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.exit_btn.setStyleSheet("""
            QPushButton {
                background-color: #ef4444;
                color: white;
                font-size: 20pt;
                font-weight: bold;
                border-radius: 15px;
                border: 4px solid white;
            }
            QPushButton:hover {
                background-color: #dc2626;
            }
            QPushButton:focus {
                border: 6px solid #f59e0b;
            }
            QPushButton:pressed {
                background-color: #b91c1c;
            }
        """)
        self.exit_btn.clicked.connect(self.exit_fullscreen)
        buttons_row.addWidget(self.exit_btn)
        buttons_row.addStretch()
        controls_layout.addLayout(buttons_row)
        main_layout.addWidget(controls_panel, stretch=0)
        try:
            self.player.setVideoOutput(self.video_widget)
            if self.was_playing:
                self.player.play()
        except Exception as e:
            print(f"Error setting video output: {e}")
        self.scrubber.sliderMoved.connect(self.seek)
        self.scrubber.sliderPressed.connect(self.on_scrubber_pressed)
        self.scrubber.sliderReleased.connect(self.on_scrubber_released)
        self.player.positionChanged.connect(self.update_position)
        self.player.durationChanged.connect(self.update_duration)
        self.player.playbackStateChanged.connect(self.update_play_button)
        self.user_dragging = False
        self.update_duration(self.player.duration())
        self.update_position(self.player.position())
        self.update_play_button()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.exit_fullscreen()
        elif event.key() == Qt.Key.Key_Space:
            self.toggle_playback()
        super().keyPressEvent(event)

    def toggle_playback(self):
        try:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()
        except Exception as e:
            print(f"Playback toggle error: {e}")

    def update_play_button(self):
        try:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.play_pause_btn.setText("⏸️ PAUSE")
            else:
                self.play_pause_btn.setText("▶️ PLAY")
        except:
            pass

    def on_scrubber_pressed(self):
        self.user_dragging = True

    def on_scrubber_released(self):
        self.user_dragging = False
        self.seek(self.scrubber.value())

    def seek(self, value):
        if self.player.duration() > 0:
            position = int((value / 1000.0) * self.player.duration())
            self.player.setPosition(position)

    def update_position(self, position):
        if not self.user_dragging and self.player.duration() > 0:
            value = int((position / self.player.duration()) * 1000)
            self.scrubber.setValue(value)
        self.timecode_label.setText(f"{self.format_time(position)} / {self.format_time(self.player.duration())}")

    def update_duration(self, duration):
        self.scrubber.setMaximum(1000)
        self.timecode_label.setText(f"{self.format_time(self.player.position())} / {self.format_time(duration)}")

    def format_time(self, ms):
        s = ms // 1000
        h = s // 3600
        m = (s % 3600) // 60
        s = s % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def exit_fullscreen(self):
        try:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            self.player.setVideoOutput(self.original_video_output)
        except Exception as e:
            print(f"Error restoring video output: {e}")
        self.close()


# --- MPV VIDEO WIDGET FOR MKV SUPPORT ---

class MPVVideoWidget(QWidget):
    """MPV-based video widget for MKV/AV1/VP9 playback support"""
    
    positionChanged = pyqtSignal(int)  # Emits position in milliseconds
    durationChanged = pyqtSignal(int)  # Emits duration in milliseconds
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # CRITICAL: Make this a native window BEFORE MPV tries to embed
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        
        self.mpv_player = None
        self.current_file = None
        self._is_paused = True
        self._duration_ms = 0
        self._position_ms = 0
        self._mpv_initialized = False
        
        # Timer to update position
        self.position_timer = QTimer(self)
        self.position_timer.timeout.connect(self._update_position)
        self.position_timer.setInterval(100)
        
        # Set black background
        self.setStyleSheet("background-color: black;")
        self.setMinimumSize(640, 360)
        
        print("MPV widget created (MPV will initialize on first file load)")
    
    def _initialize_mpv(self):
        """Initialize MPV player (called when first file is loaded)"""
        if self._mpv_initialized:
            print("MPV already initialized")
            return True
            
        if not MPV_AVAILABLE:
            print("MPV_AVAILABLE is False - python-mpv not imported")
            return False
        
        try:
            print("Starting MPV initialization...")
            
            # Detect if running on Wayland
            import os as os_check
            session_type = os_check.environ.get('XDG_SESSION_TYPE', '').lower()
            wayland_display = os_check.environ.get('WAYLAND_DISPLAY', '')
            is_wayland = session_type == 'wayland' or wayland_display
            
            print(f"Session type: {session_type}")
            print(f"Wayland display: {wayland_display}")
            print(f"Detected Wayland: {is_wayland}")
            
            # CRITICAL: Always use CPU decode for MPV preview
            # Hardware decode causes issues with AV1 on RTX 20-series
            # The main rendering engine will still use GPU for encode
            hwdec_mode = 'no'  # Force CPU decode - ALWAYS
            print("MPV Preview: Hardware decode DISABLED (CPU decode)")
            print("(Note: Main rendering still uses GPU encode)")
            
            if is_wayland:
                print("=" * 60)
                print("Wayland detected - Using MPV without window embedding")
                print("Note: Video will render in MPV's own context")
                print("=" * 60)
                
                # On Wayland, don't use wid (window embedding doesn't work)
                self.mpv_player = mpv.MPV(
                    # NO wid parameter on Wayland!
                    vo='gpu',
                    hwdec=hwdec_mode,  # Always CPU decode
                    keep_open='yes',
                    idle='yes',
                    osc='no',
                    input_default_bindings='no',
                    input_vo_keyboard='no',
                    log_handler=print,
                    loglevel='info',
                    # Wayland-specific: embed in current window without wid
                    force_window='yes',
                    ontop='no',
                    border='no',
                    geometry=f'{self.width()}x{self.height()}+0+0'
                )
            else:
                print("X11 detected - Using window embedding with wid")
                
                # Force Qt to fully realize the widget
                self.show()
                from PyQt6.QtWidgets import QApplication
                QApplication.processEvents()
                
                # Get window ID after widget is fully shown
                wid = int(self.winId())
                print(f"Got window ID: {wid}")
                
                # On X11, use traditional wid embedding
                self.mpv_player = mpv.MPV(
                    wid=str(wid),
                    vo='gpu',
                    hwdec=hwdec_mode,  # Always CPU decode
                    keep_open='yes',
                    idle='yes',
                    osc='no',
                    input_default_bindings='no',
                    input_vo_keyboard='no',
                    log_handler=print,
                    loglevel='info'
                )
            
            print("MPV instance created successfully!")
            print(f"MPV hwdec mode: {hwdec_mode} (CPU decode forced for preview)")
            
            # Set up event observers
            @self.mpv_player.property_observer('duration')
            def duration_observer(_name, value):
                if value:
                    self._duration_ms = int(value * 1000)
                    self.durationChanged.emit(self._duration_ms)
            
            @self.mpv_player.property_observer('time-pos')
            def time_observer(_name, value):
                if value is not None:
                    self._position_ms = int(value * 1000)
            
            print("Event observers registered")
            
            self._mpv_initialized = True
            return True
            
        except Exception as e:
            print(f"MPV initialization FAILED with exception:")
            print(f"  Error type: {type(e).__name__}")
            print(f"  Error message: {e}")
            import traceback
            traceback.print_exc()
            self.mpv_player = None
            return False
    
    def load_file(self, file_path):
        """Load a video file"""
        print(f"MPV load_file called with: {file_path}")
        print(f"File exists: {os.path.exists(file_path)}")
        
        # Initialize MPV on first file load (lazy initialization)
        if not self._mpv_initialized:
            if not self._initialize_mpv():
                return False
        
        if not self.mpv_player:
            return False
        
        try:
            self.current_file = file_path
            print(f"Calling mpv_player.loadfile({file_path})")
            self.mpv_player.loadfile(file_path)
            self.mpv_player.pause = True  # Start paused
            self._is_paused = True
            print("MPV loadfile succeeded")
            return True
        except Exception as e:
            print(f"MPV load error: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def play(self):
        """Start playback"""
        if self.mpv_player and self.current_file:
            self.mpv_player.pause = False
            self._is_paused = False
            self.position_timer.start()
    
    def pause(self):
        """Pause playback"""
        if self.mpv_player:
            self.mpv_player.pause = True
            self._is_paused = True
            self.position_timer.stop()
    
    def is_paused(self):
        """Check if playback is paused"""
        return self._is_paused
    
    def seek(self, position_ms):
        """Seek to position in milliseconds"""
        if self.mpv_player:
            try:
                self.mpv_player.seek(position_ms / 1000.0, reference='absolute')
                self._position_ms = position_ms
            except:
                pass
    
    def position(self):
        """Get current position in milliseconds"""
        return self._position_ms
    
    def duration(self):
        """Get duration in milliseconds"""
        return self._duration_ms
    
    def _update_position(self):
        """Emit position update signal"""
        self.positionChanged.emit(self._position_ms)
    
    def stop(self):
        """Stop playback"""
        if self.mpv_player:
            self.mpv_player.command('stop')
            self._is_paused = True
            self._position_ms = 0
            self.position_timer.stop()
    
    def shutdown(self):
        """Cleanup MPV player"""
        if self.mpv_player:
            try:
                self.position_timer.stop()
                self.mpv_player.terminate()
            except:
                pass


class TimelineClip:
    """Represents a clip on the timeline"""
    def __init__(self, file_path, track, start_time, in_point=0, out_point=None, duration=None):
        self.file_path = file_path
        self.track = track
        self.start_time = start_time
        self.in_point = in_point
        self.name = Path(file_path).name
        self.full_duration = duration if duration is not None else self.get_video_duration()
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
        """Convert timeline time to source file time"""
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
            "duration": self.full_duration
        }

    @staticmethod
    def from_dict(data):
        return TimelineClip(
            data["file_path"],
            data["track"],
            data["start_time"],
            data["in_point"],
            data["out_point"],
            data["duration"]
        )


class TimelineWidget(QWidget):
    """Visual timeline editor with drag-and-drop clips"""
    clip_selected = pyqtSignal(object)
    playhead_moved = pyqtSignal(float)

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
        self.track_height = 60
        self.num_tracks = 4
        self.playhead_position = 0
        self.dragging_playhead = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

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
        # Draw time markers based on scroll offset
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
        # Calculate playhead position relative to scroll offset
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
        painter.setPen(QColor("white"))
        font = QFont("Arial", 9, QFont.Weight.Bold)
        painter.setFont(font)
        text_rect = painter.boundingRect(x + 5, y + 5, width - 10, height - 10, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, clip.name)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, clip.name)
        duration_text = f"{clip.get_trimmed_duration():.1f}s"
        duration_rect = painter.boundingRect(x + 5, y + height - 20, width - 10, 15, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, duration_text)
        painter.drawText(duration_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, duration_text)

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
            left_margin = self.width() * 0.2
            right_margin = self.width() * 0.8
            if playhead_x > right_margin:
                self.scroll_offset = self.playhead_position - (right_margin / self.zoom_level)
            elif playhead_x < left_margin and self.scroll_offset > 0:
                self.scroll_offset = max(0, self.playhead_position - (left_margin / self.zoom_level))
        self.update()
        self.playhead_moved.emit(self.playhead_position)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            click_x = event.position().x()
            click_y = event.position().y()
            click_time = self.x_to_time(click_x)
            if click_y < 40:
                self.dragging_playhead = True
                self.set_playhead_position(click_time, auto_scroll=False)
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
        click_time = self.x_to_time(click_x)
        if self.dragging_playhead:
            self.set_playhead_position(click_time, auto_scroll=False)
            return
        if self.dragging_clip:
            new_time = click_time - self.drag_offset
            new_track = self.y_to_track(event.position().y())
            if new_track >= 0:
                self.dragging_clip.start_time = max(0, new_time)
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
    """Represents a media file in the library"""
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


class TimelineRenderingEngine:
    """
    Fixed engine: Uses local temporary folder for raw streams to prevent
    External Drive (USB) bottlenecks, broken pipes, and SIGKILL errors.
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
                if self.encoder_process.stdin:
                    self.encoder_process.stdin.close()
                self.encoder_process.kill()
            except:
                pass
    
    def get_timeline_duration(self):
        if not self.timeline.clips:
            return 0
        return max(clip.get_end_time() for clip in self.timeline.clips)

    def get_clip_at_timeline_time(self, timeline_time):
        candidates = []
        for clip in self.timeline.clips:
            if clip.start_time <= timeline_time < clip.get_end_time():
                candidates.append(clip)
        if not candidates:
            return None
        candidates.sort(key=lambda x: x.track, reverse=True)
        return candidates[0]
    
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
        """Detect video codec (needed for AV1 handling)"""
        try:
            cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries', 'stream=codec_name', '-of', 'json', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            data = json.loads(result.stdout)
            return data['streams'][0]['codec_name']
        except:
            return 'unknown'

    def _build_render_plan(self, total_frames, timeline_fps):
        self.log("Building render plan...")
        segments = []
        if total_frames == 0:
            return segments
        current_clip = None
        segment_start_frame = 0
        frames_in_segment = 0
        for i in range(total_frames):
            time = i / timeline_fps
            visible_clip = self.get_clip_at_timeline_time(time)
            if visible_clip != current_clip:
                if frames_in_segment > 0:
                    segments.append({
                        'type': 'clip' if current_clip else 'blank',
                        'clip': current_clip,
                        'start_frame': segment_start_frame,
                        'count': frames_in_segment,
                        'timeline_start': segment_start_frame / timeline_fps
                    })
                current_clip = visible_clip
                segment_start_frame = i
                frames_in_segment = 0
            frames_in_segment += 1
        if frames_in_segment > 0:
            segments.append({
                'type': 'clip' if current_clip else 'blank',
                'clip': current_clip,
                'start_frame': segment_start_frame,
                'count': frames_in_segment,
                'timeline_start': segment_start_frame / timeline_fps
            })
        return segments

    def render(self):
        # SMART TEMP DIRECTORY SELECTION:
        # 1. Try output directory (user's selected drive) if it has space
        # 2. Fall back to /tmp/ (boot drive) if output drive is problematic
        # 3. This ensures we don't fill up boot drive on small SSDs
        
        output_dir = os.path.dirname(self.output_path)
        temp_dir = None
        
        # Check if output directory has enough free space (estimate 15GB needed for 5K render)
        try:
            stat = shutil.disk_usage(output_dir)
            free_gb = stat.free / (1024**3)
            
            if free_gb > 15:  # At least 15GB free
                # Use output directory for temp files (same drive = faster final copy)
                temp_dir = output_dir
                self.log(f"Using output drive for temp files ({free_gb:.1f}GB free)")
            else:
                # Not enough space on output drive, use /tmp/
                temp_dir = tempfile.gettempdir()
                self.log(f"Output drive low on space ({free_gb:.1f}GB), using {temp_dir}")
        except:
            # Can't check output directory (permissions?), use /tmp/
            temp_dir = tempfile.gettempdir()
            self.log(f"Using system temp: {temp_dir}")
        
        # Check /tmp/ space if we're using it
        if temp_dir == tempfile.gettempdir():
            try:
                stat = shutil.disk_usage(temp_dir)
                free_gb = stat.free / (1024**3)
                if free_gb < 10:
                    self.log(f"WARNING: Low disk space on {temp_dir} ({free_gb:.1f}GB free)")
                    return False, f"Insufficient disk space: {free_gb:.1f}GB free (need 10GB+)"
            except:
                pass
        
        ts = int(time.time())
        temp_video = os.path.join(temp_dir, f"fep_video_stream_{ts}.mov")
        temp_audio = os.path.join(temp_dir, f"fep_audio_stream_{ts}.wav")
        
        try:
            self.log("=== HIGH-PERFORMANCE STREAM RENDERING v0.7.2 ===")
            self.log(f"Temp storage: {temp_dir} (Faster & Safer than USB)")
            
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

            total_frames = int(timeline_duration * timeline_fps)
            self.log(f"Resolution: {export_width}x{export_height} @ {timeline_fps} FPS")
            self.log(f"Total Frames: {total_frames}")
            
            segments = self._build_render_plan(total_frames, timeline_fps)
            self.log(f"Render Plan: {len(segments)} segments optimized.")

            self.log("Phase 1/3: Rendering Video Stream...")
            if not self._start_encoder(timeline_fps, export_width, export_height, temp_video):
                return False, "Failed to start encoder"

            frames_processed = 0
            start_time = time.time()
            
            for seg in segments:
                if self.should_stop: return False, "Cancelled"
                count = seg['count']
                if seg['type'] == 'blank':
                    # Generate black frames for YUV420P
                    # Y = 16 (Black), U = 128 (Neutral), V = 128 (Neutral)
                    black_frame = bytes([16] * (export_width * export_height)) + bytes([128] * (export_width * export_height // 2))
                    for _ in range(count):
                        if self.should_stop: break
                        try:
                            self.encoder_process.stdin.write(black_frame)
                        except (BrokenPipeError, IOError):
                            return False, "Encoder pipe broken (Disk full?)"
                        frames_processed += 1
                        self._update_progress(frames_processed, total_frames, start_time, timeline_fps)
                elif seg['type'] == 'clip':
                    clip = seg['clip']
                    offset_into_clip = seg['timeline_start'] - clip.start_time
                    source_seek_time = clip.in_point + offset_into_clip
                    self.log(f"Encoding Video: {clip.name} ({count} frames)")
                    success = self._stream_segment_to_encoder(
                        clip.file_path, source_seek_time, count, export_width, export_height, timeline_fps,
                        frames_processed, total_frames, start_time
                    )
                    
                    # FIX: 99% Stall Fix. If clip ended early or errored, pad with black to satisfy encoder.
                    if not success or success < count:
                        missing = count - (success if isinstance(success, int) else 0)
                        self.log(f"Warning: Clip ended early. Padding {missing} frames.")
                        black_frame = bytes([16] * (export_width * export_height)) + bytes([128] * (export_width * export_height // 2))
                        for _ in range(missing):
                            try:
                                self.encoder_process.stdin.write(black_frame)
                            except:
                                break
                    
                    frames_processed += count

            if self.encoder_process.stdin:
                self.encoder_process.stdin.close()
            
            # FIX: Wait with timeout for encoder to finish
            # Long renders (78 min) need more time to finalize
            self.log("Waiting for encoder to finalize video file...")
            try:
                self.encoder_process.wait(timeout=60)  # 60 seconds for long renders
                self.log("Encoder finished successfully")
            except subprocess.TimeoutExpired:
                self.log("WARNING: Encoder timeout after 60s - forcing termination")
                self.encoder_process.kill()
                self.encoder_process.wait()
                # Check if file was created anyway
                if not os.path.exists(temp_video) or os.path.getsize(temp_video) < 1000:
                    return False, "Encoder timeout - file not created"
            
            # Verify encoder completed successfully
            if self.encoder_process.returncode != 0:
                return False, f"Encoder failed with exit code {self.encoder_process.returncode}"
            
            self.log("Phase 2/3: Rendering Audio Stream...")
            audio_success = self._render_audio(segments, timeline_fps, temp_audio)
            
            if not audio_success:
                self.log("ERROR: Audio rendering failed")
                # Clean up temp video
                if os.path.exists(temp_video): os.remove(temp_video)
                return False, "Audio rendering failed - check logs for details"
            
            self.log("Phase 3/3: Merging Audio and Video to Destination...")
            self.status("Finalizing...")
            
            # Merge logic - reading from local temp, writing to final destination
            # Add +faststart here (rewrites once at end, not during encoding)
            merge_cmd = [
                'ffmpeg', '-y', '-v', 'error', '-stats',
                '-i', temp_video, 
                '-i', temp_audio,
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '320k', 
                '-movflags', '+faststart',  # Optimize for web streaming
                '-shortest', self.output_path
            ]
            
            # CRITICAL FIX: Use Popen instead of run() to prevent UI freeze on large files
            # subprocess.run() blocks for 5-10 min on 60GB files, causing Qt to kill the app
            self.log("Merging 60GB+ files - this may take 5-10 minutes...")
            merge_process = subprocess.Popen(
                merge_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            
            # Monitor merge progress and keep UI responsive
            while merge_process.poll() is None:
                if self.should_stop:
                    merge_process.kill()
                    return False, "Merge cancelled"
                
                # Read stderr to check for errors (FFmpeg outputs to stderr)
                try:
                    import select
                    if select.select([merge_process.stderr], [], [], 0.5)[0]:
                        line = merge_process.stderr.readline()
                        if line and 'error' in line.lower():
                            self.log(f"Merge warning: {line.strip()}")
                except:
                    pass
                
                # Update status every second to keep UI alive
                time.sleep(1)
                self.status("Finalizing (merging audio/video)...")
            
            # Check merge completed successfully
            if merge_process.returncode != 0:
                stderr_output = merge_process.stderr.read()
                self.log(f"Merge failed: {stderr_output}")
                return False, f"Merge failed with exit code {merge_process.returncode}"
            
            self.log("Merge complete!")
            
            # Cleanup
            if os.path.exists(temp_video): os.remove(temp_video)
            if os.path.exists(temp_audio): os.remove(temp_audio)
            
            elapsed = time.time() - start_time
            return True, f"Render Complete! {elapsed:.1f}s"

        except Exception as e:
            import traceback
            self.log(f"Critical Error: {e}")
            self.log(traceback.format_exc())
            if os.path.exists(temp_video): os.remove(temp_video)
            if os.path.exists(temp_audio): os.remove(temp_audio)
            return False, str(e)
        finally:
            self.stop()

    def _stream_segment_to_encoder(self, input_file, start_time, frame_count, width, height, fps, 
                                 current_total_frames, target_total_frames, job_start_time):
        scale_algo = self.settings.get('scale_algo', 'lanczos')
        # YUV420P frame size = W * H * 1.5
        frame_size = int(width * height * 1.5)
        
        # Detect codec - AV1 needs software decode on RTX 20-series
        codec = self._get_video_codec(input_file)
        use_gpu = self.settings.get('use_gpu_decode', False)
        
        # Disable GPU decode for AV1 (not supported on RTX 20/30-series)
        if codec == 'av1':
            use_gpu = False
            if current_total_frames == 0:  # Log once per file
                self.log(f"Detected AV1 codec - using CPU decode (GPU AV1 decode requires RTX 30+)")
        
        cmd = ['ffmpeg']
        if use_gpu:
            cmd.extend(['-hwaccel', 'cuda'])
        cmd.extend([
            '-ss', f"{start_time:.6f}", '-i', input_file, '-vframes', str(frame_count),
            '-vf', f'scale={width}:{height}:flags={scale_algo},fps={fps},format=yuv420p',
            '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'
        ])
        
        decoder = None
        frames_read = 0
        try:
            # Capture stderr to see decoder errors (especially for MKV files)
            decoder = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=10**7)
            
            while frames_read < frame_count:
                if self.should_stop:
                    decoder.kill()
                    return frames_read
                
                raw_data = decoder.stdout.read(frame_size)
                if not raw_data or len(raw_data) != frame_size:
                    # Decoder failed - check stderr
                    if frames_read == 0:  # Failed immediately
                        try:
                            stderr_output = decoder.stderr.read().decode('utf-8', errors='ignore')
                            if stderr_output:
                                self.log(f"Decoder failed on {os.path.basename(input_file)}: {stderr_output[-500:]}")
                        except:
                            pass
                    break
                    
                try:
                    self.encoder_process.stdin.write(raw_data)
                except IOError:
                    # Pipe broken
                    return frames_read
                
                frames_read += 1
                if frames_read % 5 == 0:
                    self._update_progress(current_total_frames + frames_read, target_total_frames, job_start_time, fps)
            
            decoder.wait()
            return frames_read
        except Exception as e:
            self.log(f"Stream error: {e}")
            if decoder: decoder.kill()
            return frames_read

    def _update_progress(self, current, total, start_time, fps):
        if total == 0: return
        pct = int((current / total) * 100)
        self.progress(pct)
        if current % int(fps) == 0:
            elapsed = time.time() - start_time
            actual_fps = current / elapsed if elapsed > 0 else 0
            timeline_time = current / fps
            self.status(f"Rendering: {pct}% ({actual_fps:.1f} fps)")
            if self.playhead:
                self.playhead(timeline_time)

    def _start_encoder(self, fps, width, height, output_file):
        try:
            cmd = ['ffmpeg', '-y', '-v', 'warning', '-stats', '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
                   '-s', f'{width}x{height}', '-r', str(fps), '-i', 'pipe:0']
            codec = self.settings.get('video_codec', 'hevc_nvenc')
            cmd.extend(['-c:v', codec])
            if 'nvenc' in codec:
                bitrate_kbps = int(self.settings.get('bitrate_mbps', 100) * 1000)
                cmd.extend(['-preset', 'p7', '-tune', 'hq', '-rc', 'cbr',
                            '-b:v', f'{bitrate_kbps}k', '-maxrate', f'{bitrate_kbps}k',
                            '-bufsize', f'{int(bitrate_kbps * 2)}k', '-g', str(int(fps * 2))])
                pixel_format = self.settings.get('pixel_format', 0)
                cmd.extend(['-pix_fmt', 'yuv420p' if pixel_format == 0 else 'p010le'])
            # DON'T use +faststart here - it rewrites entire 60GB file (takes 30-40 min on HDD)
            # We'll add it in the final merge step instead
            cmd.extend(['-an', output_file])
            self.encoder_process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=10**7)
            return True
        except Exception as e:
            self.log(f"Encoder start failed: {e}")
            return False
            
    def _render_audio(self, segments, timeline_fps, audio_output_path):
        sample_rate = 48000
        channels = 2
        bytes_per_sample = 2
        
        cmd_enc = ['ffmpeg', '-y', '-v', 'error', '-f', 's16le', '-ar', str(sample_rate), '-ac', str(channels),
                   '-i', 'pipe:0', '-c:a', 'pcm_s16le', audio_output_path]
        
        encoder = None
        try:
            # Capture stderr to see audio encoding errors
            encoder = subprocess.Popen(cmd_enc, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            
            for seg in segments:
                if self.should_stop: 
                    break
                    
                num_samples = int((seg['count'] / timeline_fps) * sample_rate)
                
                if seg['type'] == 'blank':
                    num_bytes = num_samples * channels * bytes_per_sample
                    try:
                        encoder.stdin.write(bytes([0] * num_bytes))
                    except (BrokenPipeError, IOError) as e:
                        self.log(f"Audio encoder pipe broken: {e}")
                        return False
                        
                elif seg['type'] == 'clip':
                    clip = seg['clip']
                    offset = seg['timeline_start'] - clip.start_time
                    seek_time = clip.in_point + offset
                    duration = seg['count'] / timeline_fps
                    
                    self.log(f"Encoding Audio: {clip.name}")
                    
                    cmd_dec = ['ffmpeg', '-ss', f"{seek_time:.6f}", '-i', clip.file_path, '-t', f"{duration:.6f}",
                               '-vn', '-f', 's16le', '-ar', str(sample_rate), '-ac', str(channels), '-']
                    
                    decoder = subprocess.Popen(cmd_dec, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    
                    while True:
                        chunk = decoder.stdout.read(4096)
                        if not chunk: 
                            break
                        try:
                            encoder.stdin.write(chunk)
                        except (BrokenPipeError, IOError) as e:
                            self.log(f"Audio encoder pipe broken while writing: {e}")
                            decoder.kill()
                            return False
                    
                    decoder.wait()
            
            # Close encoder stdin and wait with timeout
            encoder.stdin.close()
            
            self.log("Waiting for audio encoder to finish...")
            try:
                encoder.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.log("WARNING: Audio encoder timeout")
                encoder.kill()
                encoder.wait()
                return False
            
            # Check if encoder completed successfully
            if encoder.returncode != 0:
                stderr_output = encoder.stderr.read().decode('utf-8', errors='ignore')
                self.log(f"Audio encoder failed: {stderr_output}")
                return False
            
            # Verify audio file was created
            if not os.path.exists(audio_output_path):
                self.log(f"ERROR: Audio file was not created at {audio_output_path}")
                return False
            
            file_size = os.path.getsize(audio_output_path)
            if file_size < 1000:
                self.log(f"ERROR: Audio file is too small ({file_size} bytes) - likely corrupted")
                return False
            
            self.log(f"Audio rendering complete ({file_size / (1024*1024):.1f} MB)")
            return True
            
        except Exception as e:
            self.log(f"Audio render error: {e}")
            import traceback
            self.log(traceback.format_exc())
            if encoder: 
                encoder.kill()
            return False


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
        """Force immediate log display instead of buffering"""
        self.log_message.emit(message)
        # Force Qt to process the signal immediately
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()

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
        denoise = self.settings['denoise_level']
        if denoise > 0:
            denoise_values = ['', 'hqdn3d=1.5:1.5:6:6', 'hqdn3d=2:2:8:8', 'hqdn3d=3:3:10:10', 'hqdn3d=4:4:12:12', 'hqdn3d=6:6:15:15', 'hqdn3d=8:8:18:18']
            if denoise < len(denoise_values): filter_complex.append(denoise_values[denoise])
        deflicker = self.settings['deflicker_level']
        if deflicker > 0:
            deflicker_values = ['', 'deflicker=mode=pm:size=5', 'deflicker=mode=pm:size=10', 'deflicker=mode=pm:size=15', 'deflicker=mode=am:size=20', 'deflicker=mode=am:size=30']
            if deflicker < len(deflicker_values): filter_complex.append(deflicker_values[deflicker])
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
        
        # Dual player system: QMediaPlayer for MP4/MOV, MPV for MKV/AV1
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.mpv_widget = None  # Will be created when needed
        self.using_mpv = False  # Track which player is active
        
        self.fullscreen_player = None
        self.timeline_duration = 0
        
        # --- ACCESSIBILITY INIT ---
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
        
        # DWELL CLICK
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
        
        # SWITCH CONTROL
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
        
        # Container for switchable video widgets (QVideoWidget or MPVVideoWidget)
        self.video_container = QWidget()
        self.video_container_layout = QVBoxLayout(self.video_container)
        self.video_container_layout.setContentsMargins(0, 0, 0, 0)
        
        # Create QVideoWidget (for MP4, MOV, etc.)
        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumSize(640, 360)
        self.video_widget.setStyleSheet("background-color: black; border: 2px solid #4b5563; border-radius: 8px;")
        self.player.setVideoOutput(self.video_widget)
        self.video_container_layout.addWidget(self.video_widget)
        self.video_widget.show()
        
        # MPV widget will be created on-demand for MKV files
        # (saves resources if user never loads MKV)
        
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
        trim_layout = QVBoxLayout(trim_panel)
        trim_layout.setContentsMargins(5, 5, 5, 5)
        trim_title = QLabel("✂️ TRIM POINTS")
        trim_title.setStyleSheet("font-size: 12pt; font-weight: bold; color: #fbbf24; padding: 5px;")
        trim_layout.addWidget(trim_title)
        trim_buttons = QHBoxLayout()
        set_in_btn = QPushButton("[ Set IN")
        set_in_btn.setStyleSheet(self.button_style("#10b981"))
        set_in_btn.setMinimumHeight(45)
        set_in_btn.clicked.connect(self.set_media_in_point)
        trim_buttons.addWidget(set_in_btn)
        set_out_btn = QPushButton("Set OUT ]")
        set_out_btn.setStyleSheet(self.button_style("#10b981"))
        set_out_btn.setMinimumHeight(45)
        set_out_btn.clicked.connect(self.set_media_out_point)
        trim_buttons.addWidget(set_out_btn)
        trim_layout.addLayout(trim_buttons)
        self.trim_info = QLabel("In: 00:00:00 | Out: 00:00:00 | Duration: 00:00:00")
        self.trim_info.setStyleSheet("font-size: 10pt; color: #9ca3af; padding: 5px;")
        self.trim_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trim_layout.addWidget(self.trim_info)
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
        self.player.positionChanged.connect(self.update_preview_position)
        self.player.durationChanged.connect(self.update_preview_duration)
        self.player.playbackStateChanged.connect(self.update_play_button)
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
        self.gpu_decode_check.setChecked(False)  # Default OFF - safer, especially for AV1 on RTX 20-series
        self.gpu_decode_check.setStyleSheet("font-size: 11pt; color: white;")
        self.gpu_decode_check.stateChanged.connect(lambda: self.update_quality_label(self.quality_slider.value()))
        perf_layout.addWidget(self.gpu_decode_check)
        
        # Add info about GPU decode compatibility
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
                self.player.stop()
    
    def _should_use_mpv(self, file_path):
        """Determine if file should use MPV player (MKV, AV1, VP9)"""
        if not MPV_AVAILABLE:
            return False
        
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.mkv', '.webm']:
            return True
        
        # Check codec for AV1/VP9 in other containers
        try:
            codec = self.timeline.rendering_engine._get_video_codec(file_path) if hasattr(self, 'timeline') else None
            if codec in ['av1', 'vp9', 'vp8']:
                return True
        except:
            pass
        
        return False
    
    def _switch_to_mpv(self):
        """Switch from QVideoWidget to MPV widget"""
        if self.using_mpv:
            return  # Already using MPV
        
        try:
            # Hide QVideoWidget
            self.video_widget.hide()
            
            # Create MPV widget if it doesn't exist
            if not self.mpv_widget:
                print("Creating MPV widget...")
                self.mpv_widget = MPVVideoWidget()
                
                if not self.mpv_widget.mpv_player:
                    # MPV failed to initialize
                    error_msg = "MPV player failed to initialize. MKV preview not available.\n\n"
                    error_msg += "Installation instructions:\n\n"
                    error_msg += "Arch/Manjaro/CachyOS:\n"
                    error_msg += "  sudo pacman -S mpv python-mpv\n\n"
                    error_msg += "Debian/Ubuntu:\n"
                    error_msg += "  sudo apt install libmpv-dev mpv\n"
                    error_msg += "  pip install python-mpv --break-system-packages\n\n"
                    error_msg += "Fedora:\n"
                    error_msg += "  sudo dnf install mpv python3-mpv\n\n"
                    error_msg += "Rendering will still work (uses FFmpeg directly)."
                    
                    QMessageBox.warning(self, "MPV Not Available", error_msg)
                    
                    # Fall back to showing QVideoWidget (won't play, but won't crash)
                    self.video_widget.show()
                    self.using_mpv = False
                    return
                
                self.mpv_widget.setMinimumSize(640, 360)
                self.mpv_widget.setStyleSheet("background-color: black; border: 2px solid #4b5563; border-radius: 8px;")
                self.video_container_layout.addWidget(self.mpv_widget)
                
                # Connect MPV signals to UI
                self.mpv_widget.positionChanged.connect(self._on_mpv_position_changed)
                self.mpv_widget.durationChanged.connect(self._on_mpv_duration_changed)
                
                print("MPV widget created successfully")
            
            self.mpv_widget.show()
            self.using_mpv = True
            print("Switched to MPV player for MKV/AV1 support")
            
        except Exception as e:
            print(f"ERROR creating MPV widget: {e}")
            import traceback
            traceback.print_exc()
            
            # Show error to user
            QMessageBox.critical(self, "MPV Error", f"Failed to initialize MPV player:\n\n{str(e)}\n\nMKV preview will not work, but rendering will still function.")
            
            # Fall back to QVideoWidget
            self.video_widget.show()
            self.using_mpv = False
    
    def _switch_to_qmediaplayer(self):
        """Switch from MPV to QVideoWidget"""
        if not self.using_mpv:
            return  # Already using QMediaPlayer
        
        # Hide MPV widget
        if self.mpv_widget:
            self.mpv_widget.hide()
        
        # Show QVideoWidget
        self.video_widget.show()
        self.using_mpv = False
        print("Switched to QMediaPlayer")
    
    def _on_mpv_position_changed(self, position_ms):
        """Handle MPV position updates"""
        # Update slider
        if self.mpv_widget and self.mpv_widget.duration() > 0:
            slider_value = int((position_ms / self.mpv_widget.duration()) * 1000)
            self.preview_slider.setValue(slider_value)
        
        # Update timecode
        current_tc = self.format_timecode(position_ms)
        total_tc = self.format_timecode(self.mpv_widget.duration() if self.mpv_widget else 0)
        self.timecode_label.setText(f"{current_tc} / {total_tc}")
    
    def _on_mpv_duration_changed(self, duration_ms):
        """Handle MPV duration updates"""
        # Update timecode display
        current_tc = self.format_timecode(self.mpv_widget.position() if self.mpv_widget else 0)
        total_tc = self.format_timecode(duration_ms)
        self.timecode_label.setText(f"{current_tc} / {total_tc}")

    def on_media_selected(self, item):
        row = self.media_list.row(item)
        if 0 <= row < len(self.media_library):
            self.current_media = self.media_library[row]
            file_path = self.current_media.file_path
            
            # Debug: Check the file path being used
            print("=" * 60)
            print(f"Media selected: {self.current_media.name if hasattr(self.current_media, 'name') else 'unknown'}")
            print(f"File path: {file_path}")
            print(f"File exists: {os.path.exists(file_path)}")
            print(f"File extension: {os.path.splitext(file_path)[1]}")
            print(f"MPV_AVAILABLE: {MPV_AVAILABLE}")
            print("=" * 60)
            
            # Determine which player to use
            if self._should_use_mpv(file_path):
                print(f"File requires MPV (detected as MKV/AV1)")
                self._switch_to_mpv()
                
                # Only try to load if MPV actually initialized
                if self.mpv_widget and self.mpv_widget.mpv_player:
                    print("MPV player available, loading file...")
                    self.mpv_widget.load_file(file_path)
                    self.mpv_widget.pause()
                else:
                    print("MPV player not available, falling back to QMediaPlayer")
                    # MPV failed - use QMediaPlayer instead (won't preview but won't crash)
                    self._switch_to_qmediaplayer()
                    self.player.setSource(QUrl.fromLocalFile(file_path))
                    self.player.pause()
            else:
                print("Using QMediaPlayer")
                self._switch_to_qmediaplayer()
                self.player.setSource(QUrl.fromLocalFile(file_path))
                self.player.pause()
            
            self.update_trim_info()

    def on_timeline_clip_selected(self, clip):
        file_path = clip.file_path
        
        # Determine which player to use
        if self._should_use_mpv(file_path):
            self._switch_to_mpv()
            
            # Only try to load if MPV actually initialized
            if self.mpv_widget and self.mpv_widget.mpv_player:
                self.mpv_widget.load_file(file_path)
                self.mpv_widget.seek(int(clip.in_point * 1000))
                self.mpv_widget.pause()
            else:
                # MPV failed - use QMediaPlayer instead
                self._switch_to_qmediaplayer()
                self.player.setSource(QUrl.fromLocalFile(file_path))
                self.player.setPosition(int(clip.in_point * 1000))
                self.player.pause()
        else:
            self._switch_to_qmediaplayer()
            self.player.setSource(QUrl.fromLocalFile(file_path))
            self.player.setPosition(int(clip.in_point * 1000))
            self.player.pause()
        
        in_tc = self.format_timecode(int(clip.in_point * 1000))
        out_tc = self.format_timecode(int(clip.out_point * 1000))
        dur_tc = self.format_timecode(int(clip.get_trimmed_duration() * 1000))
        self.trim_info.setText(f"Timeline Clip | In: {in_tc} | Out: {out_tc} | Duration: {dur_tc}")

    def on_timeline_playhead_moved(self, time):
        pass

    def toggle_play(self):
        if self.using_mpv and self.mpv_widget:
            # MPV player control
            if self.mpv_widget.is_paused():
                self.mpv_widget.play()
                self.play_btn.setText("⏸️ Pause")
            else:
                self.mpv_widget.pause()
                self.play_btn.setText("▶️ Play")
        else:
            # QMediaPlayer control
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()

    def update_play_button(self):
        if self.using_mpv and self.mpv_widget:
            if not self.mpv_widget.is_paused():
                self.play_btn.setText("⏸️ Pause")
            else:
                self.play_btn.setText("▶️ Play")
        else:
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.play_btn.setText("⏸️ Pause")
            else:
                self.play_btn.setText("▶️ Play")

    def seek_preview(self, value):
        if self.using_mpv and self.mpv_widget:
            # MPV seek
            if self.mpv_widget.duration() > 0:
                position_ms = int((value / 1000.0) * self.mpv_widget.duration())
                self.mpv_widget.seek(position_ms)
        else:
            # QMediaPlayer seek
            if self.player.duration() > 0:
                position = int((value / 1000.0) * self.player.duration())
                self.player.setPosition(position)

    def update_preview_position(self, position):
        if self.player.duration() > 0:
            value = int((position / self.player.duration()) * 1000)
            self.preview_slider.setValue(value)
        self.timecode_label.setText(f"{self.format_timecode(position)} / {self.format_timecode(self.player.duration())}")

    def update_preview_duration(self, duration):
        self.preview_slider.setMaximum(1000)
        self.timecode_label.setText(f"{self.format_timecode(self.player.position())} / {self.format_timecode(duration)}")

    def format_timecode(self, ms):
        s = ms // 1000
        h = s // 3600
        m = (s % 3600) // 60
        s = s % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    def enter_fullscreen(self):
        if self.current_media or self.player.source().isValid():
            self.fullscreen_player = FullscreenVideoPlayer(self.player, self)
            self.fullscreen_player.show()

    def set_media_in_point(self):
        if self.current_media:
            self.current_media.in_point = self.player.position() / 1000.0
            if self.current_media.out_point <= self.current_media.in_point:
                self.current_media.out_point = self.current_media.duration
            self.update_trim_info()

    def set_media_out_point(self):
        if self.current_media:
            self.current_media.out_point = self.player.position() / 1000.0
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
            # ProRes typical bitrates
            self.quality_slider.setMinimum(50)
            self.quality_slider.setMaximum(1000)
            self.quality_slider.setValue(500)
            self.update_quality_label(500)
        else:
            # NVENC typical bitrates
            self.quality_slider.setMinimum(5)
            self.quality_slider.setMaximum(500)
            self.quality_slider.setValue(100)
            self.update_quality_label(100)

        self.update_estimated_size()

    def update_quality_label(self, value):
        self.quality_value_label.setText(f"{value} Mbps")
        self.update_estimated_size()

        # Update GPU info
        codec_idx = self.codec_combo.currentIndex()
        decode_status = "HW Decode ON" if self.gpu_decode_check.isChecked() else "SW Decode"
        if codec_idx == 0:  # ProRes
            profile_names = ["Proxy", "LT", "Standard", "HQ", "4444", "4444 XQ"]
            self.gpu_info.setText(f"✅ ProRes {profile_names[self.prores_combo.currentIndex()]} (~{value} Mbps CBR) | {decode_status}")
        else:  # NVENC
            self.gpu_info.setText(f"✅ GPU: NVENC ({value} Mbps CBR) | {decode_status}")

    def update_estimated_size(self):
        if self.timeline_duration > 0:
            duration = self.timeline_duration
        else:
            duration = 60  # Default estimate for 1 minute

        bitrate_mbps = self.quality_slider.value()
        video_size_mb = (bitrate_mbps * duration) / 8

        # Add audio size estimate (320kbps AAC or 2304kbps PCM 24-bit)
        audio_codec_idx = self.audio_combo.currentIndex()
        if audio_codec_idx == 2:  # AAC
            audio_bitrate_kbps = 320
        elif audio_codec_idx in [0, 1]:  # PCM
            audio_bitrate_kbps = 2304  # 48kHz * 24bit * 2 channels
        else:
            audio_bitrate_kbps = 320  # Default estimate

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
        """Safely stop timeline rendering with timeout"""
        if self.timeline_export_thread and self.timeline_export_thread.isRunning():
            self.status_label.setText("Stopping render...")
            self.stop_export_btn.setEnabled(False)
            
            # Signal stop to the rendering engine
            self.timeline_export_thread.stop()
            
            # Wait with timeout (5 seconds)
            if not self.timeline_export_thread.wait(5000):
                # Force termination if not stopped gracefully
                self.timeline_export_thread.terminate()
                self.timeline_export_thread.wait()
            
            # Reset UI
            self.export_timeline_btn.setEnabled(True)
            self.stop_export_btn.setEnabled(False)
            self.status_label.setText("Render stopped")
            self.log_text.append("\n=== Render cancelled by user ===\n")

    def apply_theme(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #111827; }
            QWidget { background-color: #111827; color: white; font-size: 10pt; }
            QLabel { color: white; }
            QGroupBox { font-weight: bold; }
            
            /* High Contrast Focus for Switch Users */
            *:focus {
                border: 4px solid #f59e0b; /* Bright Orange Focus Ring */
                outline: none;
            }
        """)

    def tab_style(self):
        return """
            QTabWidget::pane { border: 2px solid #4b5563; background-color: #111827; border-radius: 8px; }
            QTabBar::tab { background-color: #1f2937; color: white; padding: 12px 24px; margin: 2px;
                border-top-left-radius: 8px; border-top-right-radius: 8px; font-size: 11pt; font-weight: bold; }
            QTabBar::tab:selected { background-color: #3b82f6; color: white; }
            QTabBar::tab:hover { background-color: #374151; }
            QTabBar::tab:focus { border: 4px solid #f59e0b; }
        """

    def groupbox_style(self):
        return """
            QGroupBox { background-color: #1f2937; border: 2px solid #4b5563; border-radius: 10px;
                padding: 15px; margin-top: 10px; font-size: 11pt; font-weight: bold; color: #4ade80; }
            QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; padding: 5px 10px;
                background-color: #111827; border-radius: 5px; }
        """

    def button_style(self, color):
        hover = self.brighten(color, 1.2)
        pressed = self.brighten(color, 0.8)
        return f"""
            QPushButton {{ background-color: {color}; color: white; border: none; border-radius: 10px;
                padding: 8px 16px; font-size: 11pt; font-weight: bold; }}
            QPushButton:hover {{ background-color: {hover}; }}
            QPushButton:pressed {{ background-color: {pressed}; }}
            QPushButton:disabled {{ background-color: #4b5563; color: #9ca3af; }}
            QPushButton:focus {{ border: 4px solid #f59e0b; }}
        """

    def list_style(self):
        return """
            QListWidget { background-color: #1f2937; border: 2px solid #4b5563; border-radius: 8px;
                padding: 5px; font-size: 10pt; color: white; }
            QListWidget::item { padding: 8px; border-radius: 5px; }
            QListWidget::item:selected { background-color: #3b82f6; color: white; }
            QListWidget::item:hover { background-color: #374151; }
            QListWidget:focus { border: 4px solid #f59e0b; }
        """

    def slider_style(self):
        return """
            QSlider::groove:horizontal { border: none; height: 12px; background: #4b5563; border-radius: 6px; }
            QSlider::handle:horizontal { background: #4ade80; border: 3px solid white; width: 24px;
                height: 24px; margin: -6px 0; border-radius: 12px; }
            QSlider::sub-page:horizontal { background: #4ade80; border-radius: 6px; }
            QSlider::handle:horizontal:focus { border: 4px solid #f59e0b; width: 28px; height: 28px; margin: -8px 0; }
        """

    def combo_style(self):
        return """
            QComboBox { background-color: #1f2937; border: 2px solid #4b5563; border-radius: 8px;
                padding: 6px; font-size: 10pt; color: white; }
            QComboBox::drop-down { border: none; width: 30px; }
            QComboBox::down-arrow { image: none; border-left: 5px solid transparent; border-right: 5px solid transparent;
                border-top: 8px solid white; margin-right: 8px; }
            QComboBox QAbstractItemView { background-color: #1f2937; border: 2px solid #4b5563;
                selection-background-color: #4ade80; selection-color: black; color: white; padding: 5px; }
            QComboBox:focus { border: 4px solid #f59e0b; }
        """

    def spinbox_style(self):
        return """
            QSpinBox, QDoubleSpinBox { background-color: #1f2937; border: 2px solid #4b5563; border-radius: 8px;
                padding: 6px; font-size: 10pt; color: white; }
            QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 20px; background-color: #4b5563; }
            QSpinBox:focus, QDoubleSpinBox:focus { border: 4px solid #f59e0b; }
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
        self.gpu_decode_check.setChecked(False)  # Default OFF - safer for all GPUs
        self.threads_spin.setValue(0)
        self.quality_slider.setValue(500 if self.codec_combo.currentIndex() == 0 else 100)
        self.denoise_combo.setCurrentIndex(0)
        self.deflicker_combo.setCurrentIndex(0)
        self.on_codec_changed()
        self.save_settings()

    def get_settings(self):
        codec_map = {0: "prores_ks", 1: "h264_nvenc", 2: "hevc_nvenc"}
        audio_map = {0: "pcm_s24le", 1: "pcm_s16le", 2: "aac", 3: "copy"}
        
        # Parse timeline FPS
        fps_values = [23.976, 24, 25, 29.97, 30, 50, 60, 120]
        timeline_fps = fps_values[self.timeline_fps_combo.currentIndex()]
        
        # Parse export resolution
        export_res_index = self.export_res_combo.currentIndex()
        
        # Parse scale algorithm
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
        # We can expand this to save other settings if desired

    def load_settings(self):
        # We did output_folder in __init__, but could add more here
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
            
            # Load clips
            for clip_data in project_data.get("clips", []):
                clip = TimelineClip.from_dict(clip_data)
                # Verify file still exists
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
        
        # Cleanup MPV player
        if self.mpv_widget:
            try:
                self.mpv_widget.shutdown()
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
    
    # --- FIX FOR ARCH/HYPRLAND ICONS ---
    app.setDesktopFileName("FastEncodePro") 
    # -----------------------------------
    
    app.setStyle("Fusion")
    window = FastEncodeProApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
