#!/usr/bin/env python3
"""
FastEncode Pro - Accessibility Edition v0.07.1
GPU-Accelerated Video Editor with Native Eye-Tracking & Switch Support

v0.7.1 Changes:
- STABILITY: Rollback to QtMultimedia (fixes Wayland crash).
- FIX: Force GStreamer backend to detect gst-libav plugins.
- FIX: Render Crash Protection (Broken Pipe handling).
- FIX: GPU Scaling (scale_cuda).
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

# Force Qt to use GStreamer (Fixes some black screen issues on Arch)
os.environ["QT_MEDIA_BACKEND"] = "gstreamer"

from PyQt6.QtWidgets import *
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSettings, QUrl, QPointF, QTimer, QEvent, QPoint, QRectF, QObject
from PyQt6.QtGui import QFont, QPalette, QColor, QPainter, QBrush, QPen, QCursor, QAction, QPainterPath, QMouseEvent
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QVideoWidget

__version__ = "0.11"
__author__ = "cpgplays"

# --- ACCESSIBILITY CLASSES ---

class DwellClickOverlay(QWidget):
    """Visual indicator for Dwell Clicking"""
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
        if not self.active or self.progress <= 0: return
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(0, 0, 0, 100)); p.drawEllipse(5, 5, 50, 50)
        pen = QPen(QColor("#4ade80")); pen.setWidth(6); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(10, 10, 40, 40, 90 * 16, int(-self.progress * 360 * 16))

class DwellClickFilter(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer(); self.timer.setInterval(50); self.timer.timeout.connect(self.check_dwell)
        self.enabled = False
        self.last_pos = QPoint(0, 0); self.dwell_start = 0; self.dur = 1.2; self.thresh = 10
        self.overlay = DwellClickOverlay()
        
    def set_enabled(self, e):
        self.enabled = e
        if e: self.timer.start(); self.overlay.show()
        else: self.timer.stop(); self.overlay.hide()
            
    def set_params(self, d, t): self.dur = d; self.thresh = t

    def check_dwell(self):
        if not self.enabled: return
        cur = QCursor.pos(); dist = (cur - self.last_pos).manhattanLength()
        if dist > self.thresh:
            self.last_pos = cur; self.dwell_start = time.time()
            self.overlay.active = False; self.overlay.update_progress(0)
            self.overlay.move(cur.x() - 30, cur.y() - 30)
        else:
            el = time.time() - self.dwell_start; prog = min(1.0, el / self.dur)
            self.overlay.move(cur.x() - 30, cur.y() - 30)
            self.overlay.active = True; self.overlay.update_progress(prog)
            if el >= self.dur:
                self.dwell_start = time.time(); self.overlay.update_progress(0); self.perform_click(cur)

    def perform_click(self, pos):
        self.overlay.hide()
        w = QApplication.widgetAt(pos)
        if w:
            lp = w.mapFromGlobal(pos)
            QApplication.sendEvent(w, QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(lp), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
            QApplication.sendEvent(w, QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(lp), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
        QTimer.singleShot(100, self.overlay.show)

# --- CORE CLASSES ---

class TimelineClip:
    def __init__(self, file_path, track, start_time, in_point=0, out_point=None, duration=None):
        self.file_path = file_path; self.track = track; self.start_time = start_time; self.in_point = in_point
        self.name = Path(file_path).name
        self.full_duration = duration if duration else self.get_dur()
        self.out_point = out_point if out_point else self.full_duration
    def get_dur(self):
        try: return float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.file_path], capture_output=True).stdout)
        except: return 60.0
    def get_trimmed_duration(self): return self.out_point - self.in_point
    def get_end_time(self): return self.start_time + self.get_trimmed_duration()
    def to_dict(self): return vars(self)
    @staticmethod
    def from_dict(d): return TimelineClip(d['file_path'], d['track'], d['start_time'], d['in_point'], d['out_point'], d['duration'])

class TimelineWidget(QWidget):
    clip_selected = pyqtSignal(object); playhead_moved = pyqtSignal(float)
    def __init__(self, parent=None):
        super().__init__(parent); self.clips = []; self.selected_clip = None; self.zoom = 10.0; self.scroll = 0; self.playhead = 0
        self.setMinimumHeight(250); self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    def paintEvent(self, e):
        p = QPainter(self); p.fillRect(self.rect(), QColor("#111827")); p.fillRect(0, 0, self.width(), 40, QColor("#1f2937"))
        # Ruler
        for s in range(int(self.scroll), int(self.scroll + self.width()/self.zoom) + 1, 5):
            x = int((s - self.scroll) * self.zoom)
            p.setPen(QColor("#9ca3af")); p.drawLine(x, 30, x, 40); p.drawText(x+2, 25, f"{s}s")
        # Tracks
        for t in range(4):
            y = 40 + t * 60; p.setPen(QColor("#374151")); p.fillRect(0, y, self.width(), 60, QColor("#1f2937" if t%2==0 else "#374151"))
        # Clips
        for c in self.clips:
            x = int((c.start_time - self.scroll) * self.zoom); w = int(c.get_trimmed_duration() * self.zoom)
            y = 40 + c.track * 60 + 5
            p.setBrush(QColor("#3b82f6") if c == self.selected_clip else QColor("#10b981"))
            p.setPen(QPen(Qt.GlobalColor.white)); p.drawRoundedRect(x, y, w, 50, 5, 5); p.drawText(x+5, y+25, c.name)
        # Playhead
        px = int((self.playhead - self.scroll) * self.zoom)
        p.setPen(QPen(QColor("#ef4444"), 2)); p.drawLine(px, 0, px, self.height())
        if self.hasFocus(): p.setPen(QPen(QColor("#f59e0b"), 4)); p.setBrush(Qt.BrushStyle.NoBrush); p.drawRect(self.rect().adjusted(2,2,-2,-2))
    def mousePressEvent(self, e):
        t = (e.position().x() / self.zoom) + self.scroll
        if e.position().y() < 40: self.playhead = max(0, t); self.playhead_moved.emit(self.playhead); self.update(); return
        tr = (int(e.position().y()) - 40) // 60
        for c in self.clips:
            if c.track == tr and c.start_time <= t <= c.get_end_time():
                self.selected_clip = c; self.clip_selected.emit(c); self.update(); return
        self.selected_clip = None; self.update()
    def add_clip(self, c): self.clips.append(c); self.update()
    def clear_timeline(self): self.clips = []; self.update()
    def set_playhead_position(self, t): self.playhead = t; self.update()

class MediaLibraryItem:
    def __init__(self, path):
        self.file_path = path; self.name = Path(path).name; self.duration = 60.0; self.in_point = 0; self.out_point = 60.0
        self.get_duration()
    def get_duration(self):
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.file_path], capture_output=True, text=True)
            self.duration = float(r.stdout.strip()); self.out_point = self.duration
        except: pass

class TimelineRenderingEngine:
    def __init__(self, timeline, settings, output_path, log, progress, status):
        self.timeline = timeline; self.settings = settings; self.output_path = output_path
        self.log = log; self.progress = progress; self.status = status; self.should_stop = False; self.encoder_process = None

    def get_meta(self, path):
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', path], capture_output=True, text=True)
            d = json.loads(r.stdout); return d['streams'][0]['width'], d['streams'][0]['height']
        except: return 1920, 1080

    def render(self):
        t_vid = self.output_path + ".temp.mov"; t_aud = self.output_path + ".temp.wav"
        try:
            self.log("=== STARTING RENDER v0.11 ===")
            if not self.timeline.clips: return False, "No clips"
            
            clips = sorted(self.timeline.clips, key=lambda c: c.start_time)
            fps = self.settings.get('timeline_fps', 60.0)
            w, h = self.get_meta(clips[0].file_path)
            
            # --- VIDEO PASS ---
            self.log("Phase 1: Video Render")
            cmd = ['ffmpeg', '-y', '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-s', f'{w}x{h}', '-r', str(fps), '-i', '-']
            
            # Encoder settings
            codec = self.settings.get('video_codec', 'hevc_nvenc')
            cmd.extend(['-c:v', codec])
            if 'nvenc' in codec:
                br = int(self.settings.get('bitrate_mbps', 20) * 1000)
                cmd.extend(['-preset', 'p7', '-rc', 'cbr', '-b:v', f'{br}k'])
            else:
                cmd.extend(['-preset', 'fast'])
            
            cmd.append(t_vid)
            self.encoder_process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            
            # Frame loop
            curr_frame = 0
            # We process clip by clip (simple concat logic for rendering)
            for c in clips:
                # Handle gaps (black frames)
                gap = int((c.start_time * fps) - curr_frame)
                if gap > 0:
                    black = bytes([16] * (w*h) + [128] * (w*h//2))
                    for _ in range(gap): self._safe_write(black); curr_frame += 1
                
                # Decode Clip
                dur = int(c.get_trimmed_duration() * fps)
                self.log(f"Encoding {c.name} ({dur} frames)")
                
                d_cmd = ['ffmpeg']
                if self.settings.get('use_gpu_decode', False): d_cmd.extend(['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'])
                d_cmd.extend(['-ss', str(c.in_point), '-i', c.file_path, '-vframes', str(dur)])
                
                # GPU Scaling Logic
                if self.settings.get('use_gpu_decode', False):
                    vf = f"scale_cuda={w}:{h},hwdownload,format=nv12,fps={fps},format=yuv420p"
                else:
                    vf = f"scale={w}:{h},fps={fps},format=yuv420p"
                
                d_cmd.extend(['-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'])
                
                dec = subprocess.Popen(d_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                f_size = int(w * h * 1.5)
                
                while True:
                    if self.should_stop: dec.kill(); break
                    raw = dec.stdout.read(f_size)
                    if not raw or len(raw) != f_size: break
                    if not self._safe_write(raw): break # Pipe broken?
                    curr_frame += 1
                    if curr_frame % 30 == 0: self.status(f"Rendering frame {curr_frame}")
                dec.wait()
                if self.should_stop: break

            if self.encoder_process:
                self.encoder_process.stdin.close()
                self.encoder_process.wait()
            
            if self.should_stop: return False, "Cancelled"

            # --- AUDIO PASS (Simple Extract/Concat) ---
            self.log("Phase 2: Audio Render")
            # Build complex filter for audio
            inputs = []; fil = ""
            for i, c in enumerate(clips):
                inputs.extend(['-ss', str(c.in_point), '-t', str(c.get_trimmed_duration()), '-i', c.file_path])
                fil += f"[{i}:a]"
            fil += f"concat=n={len(clips)}:v=0:a=1[out]"
            subprocess.run(['ffmpeg', '-y', *inputs, '-filter_complex', fil, '-map', '[out]', t_aud], stderr=subprocess.DEVNULL)

            # --- MERGE ---
            self.log("Phase 3: Final Merge")
            subprocess.run(['ffmpeg', '-y', '-i', t_vid, '-i', t_aud, '-c', 'copy', '-shortest', self.output_path], stderr=subprocess.DEVNULL)
            
            if os.path.exists(t_vid): os.remove(t_vid)
            if os.path.exists(t_aud): os.remove(t_aud)
            
            return True, "Render Complete!"

        except Exception as e:
            return False, str(e)

    def _safe_write(self, data):
        # Prevents crash if ffmpeg dies early
        if not self.encoder_process: return False
        try:
            self.encoder_process.stdin.write(data)
            return True
        except BrokenPipeError:
            return False
        except Exception:
            return False

class TimelineExportThread(QThread):
    progress = pyqtSignal(int); status = pyqtSignal(str); log_message = pyqtSignal(str); finished = pyqtSignal(bool, str)
    def __init__(self, timeline, out, settings):
        super().__init__(); self.timeline = timeline; self.out = out; self.settings = settings; self.engine = None
    def run(self):
        self.engine = TimelineRenderingEngine(self.timeline, self.settings, self.out, self.log_message.emit, self.progress.emit, self.status.emit)
        s, m = self.engine.render()
        self.finished.emit(s, m)
    def stop(self):
        if self.engine: self.engine.should_stop = True

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

class FastEncodeProApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"FastEncode Pro v{__version__} - Accessible Video Editor")
        self.setGeometry(100, 100, 1400, 900)
        
        # Settings
        self.settings_store = QSettings("FastEncode", "Pro")
        self.output_folder = self.settings_store.value("output_folder", "")
        
        # UI
        central = QWidget(); self.setCentralWidget(central); self.layout = QVBoxLayout(central)
        self.tabs = QTabWidget(); self.layout.addWidget(self.tabs)
        
        # Tools
        self.player = QMediaPlayer(); self.audio = QAudioOutput(); self.player.setAudioOutput(self.audio)
        self.dwell = DwellClickFilter(self)
        
        self.lib_items = []
        self.cur_media = None
        self.export_thread = None

        self.setup_timeline_tab()
        self.setup_settings_tab()
        self.setup_access_tab()
        self.apply_theme()

        # Connect player output now that widgets exist
        self.player.setVideoOutput(self.vid_wid)

    def setup_timeline_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        
        # Project Controls
        p_layout = QHBoxLayout()
        self.btn_save = QPushButton("Save Project"); self.btn_save.clicked.connect(self.save_project)
        self.btn_load = QPushButton("Load Project"); self.btn_load.clicked.connect(self.load_project)
        p_layout.addWidget(self.btn_save); p_layout.addWidget(self.btn_load)
        layout.addLayout(p_layout)

        # Top: Lib + Player
        top = QHBoxLayout()
        # Lib
        lib_grp = QGroupBox("Library"); lib_l = QVBoxLayout(lib_grp)
        self.lib_list = QListWidget(); self.lib_list.itemClicked.connect(self.preview_media)
        btn_add = QPushButton("Add Media"); btn_add.clicked.connect(self.add_media)
        lib_l.addWidget(self.lib_list); lib_l.addWidget(btn_add)
        top.addWidget(lib_grp)
        
        # Player
        play_grp = QGroupBox("Preview"); play_l = QVBoxLayout(play_grp)
        self.vid_wid = QVideoWidget(); self.vid_wid.setMinimumSize(400, 225); self.vid_wid.setStyleSheet("background:black;")
        play_l.addWidget(self.vid_wid)
        
        ctrls = QHBoxLayout()
        self.btn_play = QPushButton("Play"); self.btn_play.clicked.connect(self.toggle_play)
        self.btn_in = QPushButton("In"); self.btn_in.clicked.connect(self.set_in)
        self.btn_out = QPushButton("Out"); self.btn_out.clicked.connect(self.set_out)
        self.btn_fs = QPushButton("Fullscreen"); self.btn_fs.clicked.connect(self.enter_fs)
        self.btn_append = QPushButton("Add to Timeline"); self.btn_append.clicked.connect(self.add_to_tl)
        for b in [self.btn_play, self.btn_in, self.btn_out, self.btn_fs, self.btn_append]: ctrls.addWidget(b)
        play_l.addLayout(ctrls)
        top.addWidget(play_grp)
        
        layout.addLayout(top)
        
        # Timeline
        self.timeline = TimelineWidget()
        layout.addWidget(self.timeline)
        
        # Export
        bot = QHBoxLayout()
        self.btn_export = QPushButton("EXPORT"); self.btn_export.clicked.connect(self.export)
        self.btn_stop = QPushButton("STOP"); self.btn_stop.clicked.connect(self.stop_export); self.btn_stop.setEnabled(False)
        bot.addWidget(self.btn_export); bot.addWidget(self.btn_stop)
        layout.addLayout(bot)
        
        self.tabs.addTab(tab, "Timeline")

    def setup_settings_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        
        self.combo_codec = QComboBox(); self.combo_codec.addItems(["hevc_nvenc", "h264_nvenc", "libx264"])
        layout.addWidget(QLabel("Codec:")); layout.addWidget(self.combo_codec)
        
        self.spin_bitrate = QSpinBox(); self.spin_bitrate.setRange(1, 500); self.spin_bitrate.setValue(20)
        layout.addWidget(QLabel("Bitrate (Mbps):")); layout.addWidget(self.spin_bitrate)
        
        self.chk_gpu_dec = QCheckBox("Enable GPU Hardware Decode"); self.chk_gpu_dec.setChecked(True)
        layout.addWidget(self.chk_gpu_dec)
        
        layout.addStretch()
        self.tabs.addTab(tab, "Settings")

    def setup_access_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        
        # DWELL
        grp = QGroupBox("Eye Tracking / Dwell"); gl = QVBoxLayout(grp)
        chk = QCheckBox("Enable Dwell Click"); chk.stateChanged.connect(lambda s: self.dwell.set_enabled(s==2))
        gl.addWidget(chk)
        layout.addWidget(grp)
        
        layout.addStretch()
        self.tabs.addTab(tab, "Accessibility")

    def apply_theme(self):
        self.setStyleSheet("QMainWindow { background: #111827; } QLabel { color: white; } *:focus { border: 4px solid #f59e0b; }")

    # Logic
    def add_media(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Open Video")
        for f in files:
            m = MediaLibraryItem(f)
            self.lib_items.append(m)
            self.lib_list.addItem(m.name)

    def preview_media(self, item):
        idx = self.lib_list.row(item)
        self.cur_media = self.lib_items[idx]
        self.player.setSource(QUrl.fromLocalFile(self.cur_media.file_path))
        self.player.play()

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState: self.player.pause()
        else: self.player.play()

    def set_in(self):
        if self.cur_media: self.cur_media.in_point = self.player.position() / 1000.0
    def set_out(self):
        if self.cur_media: self.cur_media.out_point = self.player.position() / 1000.0

    def enter_fs(self):
        if self.cur_media or self.player.source().isValid():
            self.fullscreen_player = FullscreenVideoPlayer(self.player, self)
            self.fullscreen_player.show()

    def add_to_tl(self):
        if self.cur_media:
            st = max([c.get_end_time() for c in self.timeline.clips], default=0)
            c = TimelineClip(self.cur_media.file_path, 0, st, self.cur_media.in_point, self.cur_media.out_point, self.cur_media.duration)
            self.timeline.add_clip(c)

    def export(self):
        if not self.timeline.clips: return
        out, _ = QFileDialog.getSaveFileName(self, "Save", "video.mp4")
        if not out: return
        
        s = {
            'video_codec': self.combo_codec.currentText(),
            'bitrate_mbps': self.spin_bitrate.value(),
            'use_gpu_decode': self.chk_gpu_dec.isChecked(),
            'timeline_fps': 60.0
        }
        
        self.btn_export.setEnabled(False); self.btn_stop.setEnabled(True)
        self.export_thread = TimelineExportThread(self.timeline, out, s)
        self.export_thread.finished.connect(self.export_done)
        self.export_thread.start()

    def stop_export(self):
        if self.export_thread: self.export_thread.stop()

    def export_done(self, success, msg):
        self.btn_export.setEnabled(True); self.btn_stop.setEnabled(False)
        QMessageBox.information(self, "Export", msg)

    def save_project(self):
        if not self.timeline.clips: return
        f, _ = QFileDialog.getSaveFileName(self, "Save Project", "project.fep")
        if f:
            data = {"clips": [c.to_dict() for c in self.timeline.clips]}
            with open(f, 'w') as file: json.dump(data, file)

    def load_project(self):
        f, _ = QFileDialog.getOpenFileName(self, "Load Project", "", "*.fep")
        if f:
            with open(f, 'r') as file: data = json.load(file)
            self.timeline.clear_timeline()
            for cd in data['clips']:
                self.timeline.add_clip(TimelineClip.from_dict(cd))

def main():
    app = QApplication(sys.argv)
    app.setDesktopFileName("FastEncodePro")
    app.setStyle("Fusion")
    w = FastEncodeProApp()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
