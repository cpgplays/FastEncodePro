#!/usr/bin/env python3
"""
FastEncode Pro - Accessibility Edition v0.08.2
GPU-Accelerated Video Editor with Native Eye-Tracking & Switch Support

v0.08.2 Changes:
- CRITICAL FIX: Wrapped Render Pipe operations in try/except to prevent hard crashes.
- Fixed GPU Pipeline: Uses 'scale_cuda' for 100% GPU usage.
- Added MKV/GStreamer diagnostic check on startup.
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
from PyQt6.QtWidgets import *
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSettings, QUrl, QPointF, QTimer, QEvent, QPoint, QRectF, QObject
from PyQt6.QtGui import QFont, QPalette, QColor, QPainter, QBrush, QPen, QCursor, QAction, QPainterPath, QMouseEvent
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

__version__ = "0.08.2"
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

# --- END ACCESSIBILITY CLASSES ---

class FullscreenVideoPlayer(QWidget):
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
        controls_panel.setStyleSheet("background: rgba(0,0,0,200); padding: 20px;")
        controls_layout = QVBoxLayout(controls_panel)
        self.timecode_label = QLabel("00:00:00 / 00:00:00")
        self.timecode_label.setStyleSheet("color: white; font-size: 24pt; font-weight: bold;")
        self.timecode_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        controls_layout.addWidget(self.timecode_label)
        self.scrubber = QSlider(Qt.Orientation.Horizontal)
        self.scrubber.setRange(0, 1000)
        self.scrubber.setStyleSheet("QSlider::handle:horizontal { background: #3b82f6; width: 40px; height: 40px; border-radius: 20px; }")
        controls_layout.addWidget(self.scrubber)
        btn_layout = QHBoxLayout()
        self.play_btn = QPushButton("⏯️ PAUSE")
        self.play_btn.setStyleSheet("font-size: 20pt; padding: 20px; background: #3b82f6; color: white; border-radius: 10px;")
        self.play_btn.clicked.connect(self.toggle_playback)
        self.exit_btn = QPushButton("✕ EXIT")
        self.exit_btn.setStyleSheet("font-size: 20pt; padding: 20px; background: #ef4444; color: white; border-radius: 10px;")
        self.exit_btn.clicked.connect(self.exit_fullscreen)
        btn_layout.addWidget(self.play_btn)
        btn_layout.addWidget(self.exit_btn)
        controls_layout.addLayout(btn_layout)
        main_layout.addWidget(controls_panel)
        self.player.setVideoOutput(self.video_widget)
        if self.was_playing: self.player.play()
        self.scrubber.sliderMoved.connect(self.seek)
        self.player.positionChanged.connect(self.update_pos)
        self.player.durationChanged.connect(self.update_dur)
        
    def toggle_playback(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_btn.setText("▶️ PLAY")
        else:
            self.player.play()
            self.play_btn.setText("⏸️ PAUSE")
            
    def seek(self, val):
        if self.player.duration() > 0:
            self.player.setPosition(int((val/1000)*self.player.duration()))
            
    def update_pos(self, pos):
        if self.player.duration() > 0:
            self.scrubber.setValue(int((pos/self.player.duration())*1000))
        self.update_lbl()
        
    def update_dur(self, dur):
        self.update_lbl()
        
    def update_lbl(self):
        def fmt(ms):
            s=ms//1000; m=(s%3600)//60; h=s//3600; s=s%60
            return f"{h:02}:{m:02}:{s:02}"
        self.timecode_label.setText(f"{fmt(self.player.position())} / {fmt(self.player.duration())}")
        
    def exit_fullscreen(self):
        self.player.setVideoOutput(self.original_video_output)
        self.close()

class TimelineClip:
    def __init__(self, file_path, track, start_time, in_point=0, out_point=None, duration=None):
        self.file_path = file_path
        self.track = track
        self.start_time = start_time
        self.in_point = in_point
        self.name = Path(file_path).name
        self.full_duration = duration if duration is not None else 60.0
        if out_point is None or out_point <= 0: self.out_point = self.full_duration
        else: self.out_point = out_point
    
    def get_trimmed_duration(self): return self.out_point - self.in_point
    def get_end_time(self): return self.start_time + self.get_trimmed_duration()
    
    def to_dict(self):
        return {"file_path": self.file_path, "track": self.track, "start_time": self.start_time,
                "in_point": self.in_point, "out_point": self.out_point, "duration": self.full_duration}
    
    @staticmethod
    def from_dict(d):
        return TimelineClip(d["file_path"], d["track"], d["start_time"], d["in_point"], d["out_point"], d["duration"])

class TimelineWidget(QWidget):
    clip_selected = pyqtSignal(object)
    playhead_moved = pyqtSignal(float)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.clips = []
        self.selected_clip = None
        self.zoom_level = 10.0
        self.scroll_offset = 0
        self.playhead_position = 0
        self.track_height = 60
        self.num_tracks = 4
        self.setMinimumHeight(250)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#111827"))
        p.fillRect(0, 0, self.width(), 40, QColor("#1f2937"))
        # Draw ruler
        visible_start = self.scroll_offset
        visible_end = self.scroll_offset + (self.width()/self.zoom_level)
        p.setPen(QColor("#9ca3af"))
        for sec in range(int(visible_start), int(visible_end)+1, 5):
            x = int((sec - self.scroll_offset) * self.zoom_level)
            p.drawLine(x, 30, x, 40)
            p.drawText(x+2, 25, f"{sec}s")
        # Draw tracks
        for t in range(self.num_tracks):
            y = 40 + t*self.track_height
            p.setPen(QColor("#374151"))
            p.drawLine(0, y+self.track_height, self.width(), y+self.track_height)
        # Draw clips
        for c in self.clips:
            x = int((c.start_time - self.scroll_offset) * self.zoom_level)
            w = int(c.get_trimmed_duration() * self.zoom_level)
            y = 40 + c.track * self.track_height + 5
            if c == self.selected_clip: p.setBrush(QColor("#3b82f6"))
            else: p.setBrush(QColor("#10b981"))
            p.setPen(QPen(Qt.GlobalColor.white))
            p.drawRoundedRect(x, y, w, self.track_height-10, 5, 5)
            p.drawText(x+5, y+20, c.name)
        # Playhead
        px = int((self.playhead_position - self.scroll_offset) * self.zoom_level)
        p.setPen(QPen(QColor("#ef4444"), 2))
        p.drawLine(px, 0, px, self.height())
        
        if self.hasFocus():
            p.setPen(QPen(QColor("#f59e0b"), 4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.rect().adjusted(2,2,-2,-2))

    def mousePressEvent(self, e):
        t = (e.position().x() / self.zoom_level) + self.scroll_offset
        if e.position().y() < 40:
            self.playhead_position = max(0, t)
            self.playhead_moved.emit(self.playhead_position)
            self.update()
            return
        clicked_track = (int(e.position().y()) - 40) // self.track_height
        for c in self.clips:
            if c.track == clicked_track and c.start_time <= t <= c.get_end_time():
                self.selected_clip = c
                self.clip_selected.emit(c)
                self.update()
                return
        self.selected_clip = None
        self.update()

    def add_clip(self, c):
        self.clips.append(c)
        self.update()
        
    def clear_timeline(self):
        self.clips.clear()
        self.update()

    def set_playhead_position(self, t):
        self.playhead_position = t
        self.update()

class MediaLibraryItem:
    def __init__(self, path):
        self.file_path = path
        self.name = Path(path).name
        self.duration = 60.0
        self.in_point = 0
        self.out_point = 60.0
        self.get_duration()
        
    def get_duration(self):
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.file_path], capture_output=True, text=True)
            self.duration = float(r.stdout.strip())
            self.out_point = self.duration
        except: pass

class TimelineRenderingEngine:
    def __init__(self, timeline, settings, output_path, log, progress, status):
        self.timeline = timeline
        self.settings = settings
        self.output_path = output_path
        self.log = log
        self.progress = progress
        self.status = status
        self.should_stop = False
        self.encoder_process = None

    def get_video_metadata(self, path):
        try:
            cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', path]
            r = subprocess.run(cmd, capture_output=True, text=True)
            d = json.loads(r.stdout)
            return d['streams'][0]['width'], d['streams'][0]['height']
        except: return 1920, 1080

    def render(self):
        temp_video = self.output_path + ".temp.mov"
        temp_audio = self.output_path + ".temp.wav"
        
        try:
            self.log("=== STARTING RENDER (v0.08.2) ===")
            if not self.timeline.clips: return False, "Empty timeline"
            
            # Setup
            clips = sorted(self.timeline.clips, key=lambda c: c.start_time)
            fps = self.settings.get('timeline_fps', 60.0)
            width, height = self.get_video_metadata(clips[0].file_path)
            
            # Override res if set
            res_idx = self.settings.get('export_res_index', 0)
            if res_idx == 1: width, height = 1920, 1080
            elif res_idx == 2: width, height = 2560, 1440
            elif res_idx == 3: width, height = 3840, 2160
            
            total_frames = int(max(c.get_end_time() for c in clips) * fps)
            
            # --- VIDEO PASS ---
            self.log(f"Phase 1: Video ({width}x{height} @ {fps}fps)")
            
            # Encode command
            cmd = ['ffmpeg', '-y', '-v', 'warning', '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
                   '-s', f'{width}x{height}', '-r', str(fps), '-i', '-']
            
            # Codec settings
            if 'nvenc' in self.settings.get('video_codec', ''):
                cmd.extend(['-c:v', self.settings['video_codec'], '-preset', 'p7', '-rc', 'cbr',
                            '-b:v', f"{self.settings.get('bitrate_mbps', 100)*1000}k"])
            else:
                cmd.extend(['-c:v', 'libx264', '-preset', 'ultrafast'])
            
            cmd.append(temp_video)
            
            self.encoder_process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Render Loop
            current_frame = 0
            for clip in clips:
                # Fill gaps
                gap = int((clip.start_time * fps) - current_frame)
                if gap > 0:
                    self.log(f"Rendering gap: {gap} frames")
                    black = bytes([16]*(width*height) + [128]*(width*height//2))
                    for _ in range(gap):
                        self._safe_write(black)
                        current_frame += 1
                        
                # Render Clip
                dur_frames = int(clip.get_trimmed_duration() * fps)
                self.log(f"Rendering Clip: {clip.name} ({dur_frames} frames)")
                
                # FIXED: Scale CUDA Logic
                use_gpu = self.settings.get('use_gpu_decode', False)
                dec_cmd = ['ffmpeg']
                if use_gpu: dec_cmd.extend(['-hwaccel', 'cuda'])
                dec_cmd.extend(['-ss', str(clip.in_point), '-i', clip.file_path,
                                '-vframes', str(dur_frames)])
                                
                if use_gpu:
                    vf = f"scale_cuda={width}:{height},hwdownload,format=nv12,fps={fps},format=yuv420p"
                else:
                    vf = f"scale={width}:{height},fps={fps},format=yuv420p"
                    
                dec_cmd.extend(['-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'])
                
                decoder = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                frame_size = int(width * height * 1.5)
                
                while True:
                    if self.should_stop: break
                    raw = decoder.stdout.read(frame_size)
                    if not raw or len(raw) != frame_size: break
                    if not self._safe_write(raw): break
                    current_frame += 1
                    if current_frame % 30 == 0:
                        self.progress(int(current_frame/total_frames*100))
                
                decoder.wait()
                if self.should_stop: break

            if self.encoder_process:
                self.encoder_process.stdin.close()
                self.encoder_process.wait()

            if self.should_stop: return False, "Stopped"

            # --- AUDIO PASS ---
            self.log("Phase 2: Audio")
            # Simple concat for audio to prevent complexity crash
            # (Ideally we mix properly, but for v0.08 we just extract)
            # Creating a complex filter string for audio concatenation
            filter_str = ""
            inputs = []
            for i, clip in enumerate(clips):
                inputs.extend(['-ss', str(clip.in_point), '-t', str(clip.get_trimmed_duration()), '-i', clip.file_path])
                filter_str += f"[{i}:a]"
            
            filter_str += f"concat=n={len(clips)}:v=0:a=1[out]"
            cmd_aud = ['ffmpeg', '-y'] + inputs + ['-filter_complex', filter_str, '-map', '[out]', temp_audio]
            subprocess.run(cmd_aud, stderr=subprocess.DEVNULL)

            # --- MERGE ---
            self.log("Phase 3: Merge")
            subprocess.run(['ffmpeg', '-y', '-i', temp_video, '-i', temp_audio, '-c', 'copy', '-shortest', self.output_path])
            
            # Cleanup
            if os.path.exists(temp_video): os.remove(temp_video)
            if os.path.exists(temp_audio): os.remove(temp_audio)
            
            return True, "Render Complete!"

        except Exception as e:
            return False, str(e)

    def _safe_write(self, data):
        if not self.encoder_process: return False
        try:
            self.encoder_process.stdin.write(data)
            return True
        except: return False

class TimelineExportThread(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    log_message = pyqtSignal(str)
    finished = pyqtSignal(bool, str)

    def __init__(self, timeline, output_path, settings):
        super().__init__()
        self.timeline = timeline
        self.output_path = output_path
        self.settings = settings
        self.engine = None

    def run(self):
        self.engine = TimelineRenderingEngine(self.timeline, self.settings, self.output_path,
            self.log_message.emit, self.progress.emit, self.status.emit)
        success, msg = self.engine.render()
        self.finished.emit(success, msg)

    def stop(self):
        if self.engine: self.engine.should_stop = True

def main():
    app = QApplication(sys.argv)
    app.setDesktopFileName("FastEncodePro")
    window = FastEncodeProApp()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
