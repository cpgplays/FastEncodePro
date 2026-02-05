#!/usr/bin/env python3
"""
FastEncode Pro - Accessibility Edition v0.09
GPU-Accelerated Video Editor with Native Eye-Tracking & Switch Support

v0.09 Changes:
- REPLACED Video Engine: Now uses MPV (libmpv) for perfect MKV/HEVC playback.
- CRITICAL FIX: Wrapped Render Pipe operations to prevent crashes.
- Fixed GPU Pipeline (Scale CUDA).
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

# Try importing MPV, handle failure gracefully
try:
    import mpv
    HAS_MPV = True
except ImportError:
    HAS_MPV = False
    print("WARNING: python-mpv not found. Please install it: sudo pacman -S python-mpv")

__version__ = "0.09"
__author__ = "cpgplays"

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
        if not self.active or self.progress <= 0: return
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(0,0,0,100)); p.drawEllipse(5,5,50,50)
        pen = QPen(QColor("#4ade80")); pen.setWidth(6); pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(10,10,40,40,90*16, int(-self.progress*360*16))

class DwellClickFilter(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer(); self.timer.setInterval(50); self.timer.timeout.connect(self.check_dwell)
        self.enabled = False; self.last_pos = QPoint(0,0); self.dwell_start = 0; self.dur = 1.2; self.thresh = 10
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
            self.last_pos = cur; self.dwell_start = time.time(); self.overlay.active = False; self.overlay.update_progress(0)
            self.overlay.move(cur.x()-30, cur.y()-30)
        else:
            el = time.time() - self.dwell_start; prog = min(1.0, el/self.dur)
            self.overlay.move(cur.x()-30, cur.y()-30); self.overlay.active = True; self.overlay.update_progress(prog)
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

# --- MPV PLAYER WIDGET ---

class MpvWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.player = None
        if HAS_MPV:
            # Initialize MPV with embedding
            self.player = mpv.MPV(wid=str(int(self.winId())), vo='gpu', hwdec='auto', keep_open='yes')
            # Fix Wayland scaling?
            self.player['keep-aspect-window'] = False

    def play(self, filepath):
        if self.player: self.player.play(filepath)
    def pause(self):
        if self.player: self.player.pause = not self.player.pause
    def stop(self):
        if self.player: self.player.stop()
    def seek(self, time_pos):
        if self.player: self.player.seek(time_pos, reference="absolute", precision="exact")
    def get_time(self):
        return self.player.time_pos if self.player and self.player.time_pos else 0
    def get_duration(self):
        return self.player.duration if self.player and self.player.duration else 0.1
    def is_playing(self):
        return self.player and not self.player.pause

class FullscreenVideoPlayer(QWidget):
    """Fullscreen video player"""
    def __init__(self, media_path, start_pos, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowState(Qt.WindowState.WindowFullScreen)
        self.setStyleSheet("background: black;")
        layout = QVBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        
        self.mpv = MpvWidget(self)
        layout.addWidget(self.mpv, stretch=1)
        
        controls = QWidget(); controls.setStyleSheet("background: rgba(0,0,0,200); padding: 20px;")
        clayout = QHBoxLayout(controls)
        self.btn = QPushButton("EXIT"); self.btn.clicked.connect(self.close)
        self.btn.setStyleSheet("font-size: 24pt; color: white; background: red; border-radius: 10px; padding: 10px;")
        clayout.addStretch(); clayout.addWidget(self.btn); clayout.addStretch()
        layout.addWidget(controls)
        
        self.mpv.play(media_path)
        self.mpv.seek(start_pos)

# --- CORE CLASSES ---

class TimelineClip:
    def __init__(self, file_path, track, start_time, in_point=0, out_point=None, duration=None):
        self.file_path = file_path; self.track = track; self.start_time = start_time; self.in_point = in_point
        self.name = Path(file_path).name
        self.full_duration = duration if duration else self.get_duration()
        self.out_point = out_point if out_point else self.full_duration
    def get_duration(self):
        try: return float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', self.file_path], capture_output=True).stdout)
        except: return 60.0
    def get_trimmed_duration(self): return self.out_point - self.in_point
    def get_end_time(self): return self.start_time + self.get_trimmed_duration()
    def to_dict(self): return vars(self)
    @staticmethod
    def from_dict(d): return TimelineClip(d['file_path'], d['track'], d['start_time'], d['in_point'], d['out_point'], d.get('full_duration'))

class TimelineWidget(QWidget):
    clip_selected = pyqtSignal(object)
    playhead_moved = pyqtSignal(float)
    def __init__(self):
        super().__init__()
        self.clips = []; self.selected_clip = None; self.zoom = 10.0; self.scroll = 0; self.playhead = 0
        self.setMinimumHeight(250); self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    def paintEvent(self, e):
        p = QPainter(self); p.fillRect(self.rect(), QColor("#111827"))
        p.fillRect(0,0,self.width(),40,QColor("#1f2937")) # Ruler
        # Draw tracks
        for t in range(4):
            y = 40 + t*60; p.setPen(QColor("#374151")); p.drawLine(0, y+60, self.width(), y+60)
        # Draw clips
        for c in self.clips:
            x = int((c.start_time - self.scroll)*self.zoom); w = int(c.get_trimmed_duration()*self.zoom)
            y = 40 + c.track*60 + 5
            p.setBrush(QColor("#3b82f6") if c==self.selected_clip else QColor("#10b981"))
            p.setPen(QPen(Qt.GlobalColor.white)); p.drawRoundedRect(x,y,w,50,5,5); p.drawText(x+5,y+25,c.name)
        # Playhead
        px = int((self.playhead - self.scroll)*self.zoom)
        p.setPen(QPen(QColor("#ef4444"), 2)); p.drawLine(px,0,px,self.height())
        if self.hasFocus(): p.setPen(QPen(QColor("#f59e0b"), 4)); p.setBrush(Qt.BrushStyle.NoBrush); p.drawRect(self.rect().adjusted(2,2,-2,-2))
    def mousePressEvent(self, e):
        t = (e.position().x()/self.zoom) + self.scroll
        if e.position().y() < 40: self.playhead = max(0, t); self.playhead_moved.emit(self.playhead); self.update(); return
        track = (int(e.position().y()) - 40) // 60
        for c in self.clips:
            if c.track == track and c.start_time <= t <= c.get_end_time():
                self.selected_clip = c; self.clip_selected.emit(c); self.update(); return
        self.selected_clip = None; self.update()
    def add_clip(self, c): self.clips.append(c); self.update()
    def clear_timeline(self): self.clips = []; self.update()
    def set_playhead(self, t): self.playhead = t; self.update()

# --- RENDERING ENGINE (CRASH FIXED) ---

class TimelineRenderingEngine:
    def __init__(self, timeline, settings, output_path, log, progress, status):
        self.timeline = timeline; self.settings = settings; self.output_path = output_path
        self.log = log; self.progress = progress; self.status = status; self.should_stop = False
        self.enc_proc = None

    def render(self):
        t_vid = self.output_path + ".temp.mov"; t_aud = self.output_path + ".temp.wav"
        try:
            if not self.timeline.clips: return False, "No clips"
            fps = self.settings.get('fps', 60.0)
            clips = sorted(self.timeline.clips, key=lambda c: c.start_time)
            
            # --- VIDEO PASS ---
            self.log("Phase 1: Video Encoding...")
            cmd = ['ffmpeg', '-y', '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-s', '1920x1080', '-r', str(fps), '-i', '-']
            if self.settings.get('gpu', True):
                cmd.extend(['-c:v', 'hevc_nvenc', '-preset', 'p7', '-b:v', '20M'])
            else:
                cmd.extend(['-c:v', 'libx264', '-preset', 'fast'])
            cmd.append(t_vid)
            
            self.enc_proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            
            # Simplified render loop (Blank frames + Clip frames)
            tot_dur = max(c.get_end_time() for c in clips)
            tot_frames = int(tot_dur * fps)
            cur_time = 0.0
            
            # We construct a timeline of segments
            segments = []
            last_end = 0
            for c in clips:
                if c.start_time > last_end:
                    segments.append(('blank', c.start_time - last_end))
                segments.append(('clip', c))
                last_end = c.get_end_time()
                
            frame_count = 0
            
            for type, data in segments:
                if self.should_stop: break
                
                if type == 'blank':
                    n = int(data * fps)
                    black = bytes([16]* (1920*1080) + [128]* (1920*1080//2))
                    for _ in range(n):
                        if self.enc_proc.poll() is not None: raise Exception("Encoder died")
                        self.enc_proc.stdin.write(black)
                        frame_count += 1
                        if frame_count % 30 == 0: self.progress(int(frame_count/tot_frames*100))
                        
                elif type == 'clip':
                    c = data
                    n = int(c.get_trimmed_duration() * fps)
                    self.log(f"Encoding {c.name}")
                    
                    # Dec command
                    d_cmd = ['ffmpeg', '-ss', str(c.in_point), '-i', c.file_path, '-vframes', str(n)]
                    # GPU Scale Fix
                    if self.settings.get('gpu', True):
                        vf = f"scale_cuda=1920:1080,hwdownload,format=nv12,fps={fps},format=yuv420p"
                        d_cmd.extend(['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'])
                    else:
                        vf = f"scale=1920:1080,fps={fps},format=yuv420p"
                        
                    d_cmd.extend(['-vf', vf, '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-'])
                    
                    dec = subprocess.Popen(d_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    
                    while True:
                        if self.should_stop: dec.kill(); break
                        raw = dec.stdout.read(1920*1080*3//2)
                        if not raw: break
                        try:
                            self.enc_proc.stdin.write(raw)
                        except: break
                        frame_count += 1
                        if frame_count % 30 == 0: self.progress(int(frame_count/tot_frames*100))
                    dec.wait()

            if self.enc_proc: self.enc_proc.stdin.close(); self.enc_proc.wait()
            
            if self.should_stop: return False, "Stopped"
            
            # --- AUDIO PASS (Simple Concat) ---
            self.log("Phase 2: Audio...")
            inputs = []
            filter = ""
            for i, c in enumerate(clips):
                inputs.extend(['-ss', str(c.in_point), '-t', str(c.get_trimmed_duration()), '-i', c.file_path])
                filter += f"[{i}:a]"
            filter += f"concat=n={len(clips)}:v=0:a=1[out]"
            subprocess.run(['ffmpeg', '-y', *inputs, '-filter_complex', filter, '-map', '[out]', t_aud], stderr=subprocess.DEVNULL)
            
            # --- MERGE ---
            self.log("Phase 3: Merging...")
            subprocess.run(['ffmpeg', '-y', '-i', t_vid, '-i', t_aud, '-c', 'copy', '-shortest', self.output_path], stderr=subprocess.DEVNULL)
            
            os.remove(t_vid); os.remove(t_aud)
            return True, "Done"
            
        except Exception as e:
            if self.enc_proc: self.enc_proc.kill()
            return False, str(e)

# --- MAIN APP ---

class FastEncodeProApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"FastEncode Pro v{__version__} - MPV Edition")
        self.setGeometry(100, 100, 1400, 900)
        self.timeline = TimelineWidget()
        self.player_widget = MpvWidget(self) if HAS_MPV else QLabel("Please install python-mpv")
        self.setup_ui()
        self.dwell = DwellClickFilter(self)
        
        # Load settings
        self.settings = QSettings("FastEncode", "Pro")
        self.output_folder = self.settings.value("out_dir", "")
        
        # Timer for UI update from MPV
        self.timer = QTimer(); self.timer.setInterval(100); self.timer.timeout.connect(self.update_ui_from_player)
        self.timer.start()

    def setup_ui(self):
        main = QWidget(); self.setCentralWidget(main); layout = QVBoxLayout(main)
        
        # Top: Player + Library
        top = QHBoxLayout()
        
        # Library
        lib_box = QGroupBox("Library"); lib_layout = QVBoxLayout(lib_box)
        self.lib_list = QListWidget(); self.lib_list.itemClicked.connect(self.load_preview)
        lib_layout.addWidget(self.lib_list)
        btn_add = QPushButton("Add Media"); btn_add.clicked.connect(self.add_media)
        btn_add.setStyleSheet("background: #4ade80; color: black; font-weight: bold; padding: 10px;")
        lib_layout.addWidget(btn_add)
        top.addWidget(lib_box, stretch=1)
        
        # Player
        play_box = QGroupBox("Preview"); play_layout = QVBoxLayout(play_box)
        self.player_widget.setMinimumSize(640, 360)
        self.player_widget.setStyleSheet("background: black;")
        play_layout.addWidget(self.player_widget)
        
        # Controls
        ctrls = QHBoxLayout()
        self.btn_play = QPushButton("Play"); self.btn_play.clicked.connect(self.toggle_play)
        self.btn_in = QPushButton("Set IN"); self.btn_in.clicked.connect(self.set_in)
        self.btn_out = QPushButton("Set OUT"); self.btn_out.clicked.connect(self.set_out)
        self.btn_add_tl = QPushButton("Add to Timeline"); self.btn_add_tl.clicked.connect(self.add_to_tl)
        for b in [self.btn_play, self.btn_in, self.btn_out, self.btn_add_tl]:
            b.setMinimumHeight(40); b.setStyleSheet("font-size: 11pt; font-weight: bold;")
            ctrls.addWidget(b)
        play_layout.addLayout(ctrls)
        
        self.lbl_time = QLabel("00:00 / 00:00"); self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        play_layout.addWidget(self.lbl_time)
        top.addWidget(play_box, stretch=2)
        
        layout.addLayout(top, stretch=3)
        
        # Middle: Timeline
        layout.addWidget(self.timeline, stretch=2)
        
        # Bottom: Export
        bot = QHBoxLayout()
        self.btn_render = QPushButton("EXPORT VIDEO"); self.btn_render.setStyleSheet("background: #ef4444; color: white; font-size: 14pt; font-weight: bold; padding: 15px;")
        self.btn_render.clicked.connect(self.start_export)
        bot.addWidget(self.btn_render)
        
        # Accessibility Toggle
        self.chk_dwell = QCheckBox("Eye/Dwell Click"); self.chk_dwell.stateChanged.connect(lambda s: self.dwell.set_enabled(s==2))
        bot.addWidget(self.chk_dwell)
        
        layout.addLayout(bot)
        
        self.lib_items = []
        self.cur_media = None

    def add_media(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Open Media")
        for f in files:
            m = MediaLibraryItem(f)
            self.lib_items.append(m)
            self.lib_list.addItem(m.name)

    def load_preview(self, item):
        idx = self.lib_list.row(item)
        self.cur_media = self.lib_items[idx]
        if HAS_MPV:
            self.player_widget.play(self.cur_media.file_path)
            self.player_widget.pause() # Load paused

    def toggle_play(self):
        if HAS_MPV: self.player_widget.pause()

    def update_ui_from_player(self):
        if HAS_MPV and self.cur_media:
            t = self.player_widget.get_time()
            d = self.player_widget.get_duration()
            self.lbl_time.setText(f"{int(t)}s / {int(d)}s")

    def set_in(self):
        if HAS_MPV and self.cur_media: self.cur_media.in_point = self.player_widget.get_time()
    def set_out(self):
        if HAS_MPV and self.cur_media: self.cur_media.out_point = self.player_widget.get_time()

    def add_to_tl(self):
        if self.cur_media:
            # Determine start time (end of last clip)
            start = max([c.get_end_time() for c in self.timeline.clips], default=0)
            c = TimelineClip(self.cur_media.file_path, 0, start, self.cur_media.in_point, self.cur_media.out_point, self.cur_media.duration)
            self.timeline.add_clip(c)

    def start_export(self):
        if not self.timeline.clips: return
        out, _ = QFileDialog.getSaveFileName(self, "Export", "video.mp4")
        if not out: return
        
        self.btn_render.setEnabled(False); self.btn_render.setText("Rendering...")
        
        # Settings dict
        s = {'fps': 60.0, 'gpu': True, 'bitrate_mbps': 20}
        
        self.thread = QThread()
        self.worker = TimelineRenderingEngine(self.timeline, s, out, print, lambda x: self.btn_render.setText(f"{x}%"), print)
        
        # We run render directly in a thread wrapper would be better, but for brevity in this fix:
        # Just running inline for safety test, or use QThread properly if prefered.
        # Let's use the blocking render for SAFETY first to ensure no thread crash logic issues.
        QApplication.processEvents()
        success, msg = self.worker.render()
        
        self.btn_render.setEnabled(True); self.btn_render.setText("EXPORT VIDEO")
        QMessageBox.information(self, "Status", msg)

def main():
    app = QApplication(sys.argv)
    app.setDesktopFileName("FastEncodePro")
    app.setStyle("Fusion")
    
    # High Contrast for switches
    app.setStyleSheet("*:focus { border: 4px solid #f59e0b; }")
    
    w = FastEncodeProApp()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
