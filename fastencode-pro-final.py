#!/usr/bin/env python3
"""
FastEncode Pro - GPU-Accelerated Video Encoder
Version: 0.0.3
Author: cpgplays
License: Apache 2.0

Fast, GPU-accelerated video encoding with advanced noise reduction,
deflicker, and exposure controls. Designed for GoPro and action camera footage.
"""

import sys
import os
import subprocess
from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QListWidget, QComboBox, QCheckBox,
    QProgressBar, QFileDialog, QSpinBox, QGroupBox, QMessageBox, 
    QSplitter, QTextEdit, QFrame, QTabWidget, QScrollArea, QSlider
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSettings
from PyQt6.QtGui import QFont, QPalette, QColor

__version__ = "0.0.3"
__author__ = "cpgplays"


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
        self._stop_requested = False
        
    def run(self):
        try:
            cmd = self.build_ffmpeg_command()
            cmd_str = ' '.join(cmd)
            self.log_message.emit(f"\n{'='*60}")
            self.log_message.emit(f"COMMAND:")
            self.log_message.emit(cmd_str)
            self.log_message.emit(f"{'='*60}\n")
            self.status.emit(f"Starting: {Path(self.input_file).name}")
            
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            self.process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                universal_newlines=True, 
                bufsize=1,
                startupinfo=startupinfo
            )
            
            duration = None
            for line in self.process.stderr:
                if self._stop_requested:
                    break
                
                line = line.strip()
                if line:
                    self.log_message.emit(line)
                
                if "Duration:" in line and duration is None:
                    try:
                        time_str = line.split("Duration:")[1].split(",")[0].strip()
                        h, m, s = time_str.split(":")
                        duration = float(h) * 3600 + float(m) * 60 + float(s)
                        self.log_message.emit(f">>> Duration detected: {duration:.1f} seconds")
                    except:
                        pass
                
                if "time=" in line and duration:
                    try:
                        time_str = line.split("time=")[1].split()[0]
                        if time_str != "N/A":
                            h, m, s = time_str.split(":")
                            current = float(h) * 3600 + float(m) * 60 + float(s)
                            progress = int((current / duration) * 100)
                            self.progress.emit(min(progress, 100))
                    except:
                        pass
                
                if "speed=" in line:
                    try:
                        speed = line.split("speed=")[1].split()[0]
                        self.status.emit(f"Encoding: {speed}")
                    except:
                        pass
                
                if "error" in line.lower() or "failed" in line.lower():
                    self.log_message.emit(f"⚠️ {line}")
            
            self.process.wait()
            
            if self._stop_requested:
                self.finished.emit(False, "Encoding cancelled by user")
            elif self.process.returncode == 0:
                self.finished.emit(True, "Encoding completed successfully!")
            else:
                self.finished.emit(False, f"Encoding failed (exit code: {self.process.returncode})")
                
        except FileNotFoundError:
            self.finished.emit(False, "FFmpeg not found. Please install FFmpeg.")
        except Exception as e:
            self.finished.emit(False, f"Error: {str(e)}")
    
    def build_ffmpeg_command(self):
        cmd = ["ffmpeg", "-y", "-hide_banner"]
        
        codec = self.settings['video_codec']
        use_gpu = self.settings['use_gpu']
        has_filters = self.has_any_filters()
        
        if use_gpu and codec in ["h264_nvenc", "hevc_nvenc"]:
            if has_filters:
                cmd.extend(["-hwaccel", "cuda", "-c:v", "hevc_cuvid"])
            else:
                cmd.extend(["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"])
        
        cmd.extend(["-i", self.input_file])
        
        filters = self.build_video_filters()
        if filters:
            cmd.extend(["-vf", filters])
        
        cq_value = self.settings.get('quality_cq', 20)
        
        if codec == "prores_ks":
            profile = self.settings['prores_profile']
            pix_fmt = "yuv444p10le" if profile >= 4 else "yuv422p10le"
            cmd.extend([
                "-c:v", "prores_ks",
                "-profile:v", str(profile),
                "-pix_fmt", pix_fmt,
                "-vendor", "apl0",
                "-bits_per_mb", "8000"
            ])
        elif codec == "h264_nvenc":
            if use_gpu:
                cmd.extend([
                    "-c:v", "h264_nvenc",
                    "-preset", "p7",
                    "-tune", "hq",
                    "-rc", "vbr",
                    "-cq", str(cq_value),
                    "-b:v", "0",
                    "-pix_fmt", "yuv420p"
                ])
            else:
                cmd.extend([
                    "-c:v", "libx264",
                    "-preset", "slow",
                    "-crf", str(cq_value),
                    "-pix_fmt", "yuv420p"
                ])
        elif codec == "hevc_nvenc":
            if use_gpu:
                cmd.extend([
                    "-c:v", "hevc_nvenc",
                    "-preset", "p7",
                    "-tune", "hq",
                    "-rc", "vbr",
                    "-cq", str(cq_value),
                    "-b:v", "0",
                    "-pix_fmt", "p010le" if self.settings.get('pixel_format', 0) == 1 else "yuv420p"
                ])
            else:
                cmd.extend([
                    "-c:v", "libx265",
                    "-preset", "slow",
                    "-crf", str(cq_value),
                    "-pix_fmt", "yuv420p"
                ])
        
        cmd.extend(["-c:a", self.settings['audio_codec']])
        if self.settings['audio_codec'] == "aac":
            cmd.extend(["-b:a", "320k"])
        
        if self.settings['threads'] > 0:
            cmd.extend(["-threads", str(self.settings['threads'])])
        
        cmd.append(self.output_file)
        return cmd
    
    def has_any_filters(self):
        return (
            self.settings.get('denoise_level', 0) > 0 or
            self.settings.get('deflicker_level', 0) > 0 or
            self.settings.get('temporal_level', 0) > 0 or
            self.settings.get('exposure_level', 0) != 0 or
            self.settings.get('sharpen_level', 0) > 0
        )
    
    def build_video_filters(self):
        filters = []
        
        denoise_level = self.settings.get('denoise_level', 0)
        denoise_presets = {
            0: None,
            1: "hqdn3d=2:1.5:3:2.5",           # Light
            2: "hqdn3d=4:3:6:4.5",              # Medium
            3: "hqdn3d=6:4.5:9:6.5",            # Heavy
            4: "hqdn3d=8:6:12:9",               # Extreme
            5: "hqdn3d=10:8:15:12",             # Nuclear
            6: "hqdn3d=10:8:3:2",               # Nuclear (No Swim) - high spatial, low temporal
        }
        if denoise_level > 0 and denoise_presets.get(denoise_level):
            filters.append(denoise_presets[denoise_level])
        
        deflicker_level = self.settings.get('deflicker_level', 0)
        deflicker_presets = {
            0: None,
            1: "deflicker=size=5:mode=pm",
            2: "deflicker=size=7:mode=pm",
            3: "deflicker=size=10:mode=am",
            4: "deflicker=size=15:mode=am",
            5: "deflicker=size=20:mode=am",
        }
        if deflicker_level > 0 and deflicker_presets.get(deflicker_level):
            filters.append(deflicker_presets[deflicker_level])
        
        temporal_level = self.settings.get('temporal_level', 0)
        temporal_presets = {
            0: None,
            1: "tmix=frames=3:weights='1 2 1'",
            2: "tmix=frames=5:weights='1 2 3 2 1'",
            3: "tmix=frames=5:weights='1 3 4 3 1'",
        }
        if temporal_level > 0 and temporal_presets.get(temporal_level):
            filters.append(temporal_presets[temporal_level])
        
        exposure_level = self.settings.get('exposure_level', 0)
        if exposure_level != 0:
            if exposure_level > 0:
                brightness = exposure_level * 0.03
                gamma = 1.0 + (exposure_level * 0.06)
                contrast = 1.0 + (exposure_level * 0.015)
            else:
                brightness = exposure_level * 0.03
                gamma = 1.0 + (exposure_level * 0.05)
                contrast = 1.0
            filters.append(f"eq=brightness={brightness:.3f}:contrast={contrast:.2f}:gamma={gamma:.2f}")
        
        sharpen_level = self.settings.get('sharpen_level', 0)
        sharpen_presets = {
            0: None,
            1: "unsharp=3:3:0.3:3:3:0",
            2: "unsharp=3:3:0.5:3:3:0",
            3: "unsharp=5:5:0.6:3:3:0",
            4: "unsharp=5:5:0.8:3:3:0",
            5: "unsharp=5:5:1.0:3:3:0",
        }
        if sharpen_level > 0 and sharpen_presets.get(sharpen_level):
            filters.append(sharpen_presets[sharpen_level])
        
        return ','.join(filters) if filters else ""
    
    def stop(self):
        self._stop_requested = True
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except:
                self.process.kill()


class FastEncodeProApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings_store = QSettings("FastEncodePro", "Settings")
        self.input_files = []
        self.output_folder = ""
        self.encoding_thread = None
        self.current_file_index = 0
        self.init_ui()
        self.load_settings()
        self.on_codec_changed()
        
    def init_ui(self):
        self.setWindowTitle(f"FastEncode Pro v{__version__} - GPU Video Encoder")
        self.setGeometry(100, 100, 1450, 980)
        self.set_dark_theme()
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)
        
        header = QLabel("🎬 FastEncode Pro")
        header.setFont(QFont("Arial", 28, QFont.Weight.Bold))
        header.setStyleSheet("color: #4ade80;")
        main_layout.addWidget(header)
        
        subtitle = QLabel(f"v{__version__} • GPU Encoding + GoPro Cleanup • by {__author__}")
        subtitle.setFont(QFont("Arial", 11))
        subtitle.setStyleSheet("color: #9ca3af; margin-bottom: 10px;")
        main_layout.addWidget(subtitle)
        
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left Panel
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(10)
        
        # Input files
        input_group = QGroupBox("📁 Input Files")
        input_group.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        input_layout = QVBoxLayout()
        
        self.file_list = QListWidget()
        self.file_list.setMinimumHeight(150)
        self.file_list.setStyleSheet("""
            QListWidget { 
                background-color: #1f2937; 
                border: 2px solid #374151; 
                border-radius: 8px; 
                padding: 5px; 
                font-size: 10pt; 
            }
            QListWidget::item:selected { background-color: #4ade80; color: black; }
        """)
        input_layout.addWidget(self.file_list)
        
        file_buttons = QHBoxLayout()
        
        add_btn = QPushButton("➕ Add")
        add_btn.setMinimumHeight(40)
        add_btn.setStyleSheet(self.button_style("#10b981"))
        add_btn.clicked.connect(self.add_files)
        file_buttons.addWidget(add_btn)
        
        remove_btn = QPushButton("➖ Remove")
        remove_btn.setMinimumHeight(40)
        remove_btn.setStyleSheet(self.button_style("#f59e0b"))
        remove_btn.clicked.connect(self.remove_selected)
        file_buttons.addWidget(remove_btn)
        
        clear_btn = QPushButton("🗑️ Clear")
        clear_btn.setMinimumHeight(40)
        clear_btn.setStyleSheet(self.button_style("#ef4444"))
        clear_btn.clicked.connect(self.clear_files)
        file_buttons.addWidget(clear_btn)
        
        input_layout.addLayout(file_buttons)
        input_group.setLayout(input_layout)
        left_layout.addWidget(input_group)
        
        # Output folder
        output_group = QGroupBox("📂 Output Folder")
        output_group.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        output_layout = QVBoxLayout()
        
        self.output_label = QLabel("No folder selected")
        self.output_label.setStyleSheet("""
            padding: 10px; 
            background-color: #1f2937; 
            border: 2px solid #374151; 
            border-radius: 8px; 
            font-size: 10pt;
        """)
        self.output_label.setWordWrap(True)
        output_layout.addWidget(self.output_label)
        
        output_btn = QPushButton("📁 Select Output Folder")
        output_btn.setMinimumHeight(40)
        output_btn.setStyleSheet(self.button_style("#3b82f6"))
        output_btn.clicked.connect(self.select_output)
        output_layout.addWidget(output_btn)
        
        output_group.setLayout(output_layout)
        left_layout.addWidget(output_group)
        
        # Progress
        progress_group = QGroupBox("⏳ Encoding Progress")
        progress_group.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        progress_layout = QVBoxLayout()
        
        self.status_label = QLabel("Ready to encode")
        self.status_label.setStyleSheet("font-size: 12pt; color: #4ade80; font-weight: bold;")
        progress_layout.addWidget(self.status_label)
        
        self.file_label = QLabel("")
        self.file_label.setStyleSheet("font-size: 9pt; color: #6b7280;")
        progress_layout.addWidget(self.file_label)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumHeight(35)
        self.progress_bar.setStyleSheet("""
            QProgressBar { 
                border: 2px solid #374151; 
                border-radius: 8px; 
                text-align: center; 
                font-weight: bold; 
                font-size: 11pt;
                background-color: #1f2937; 
            }
            QProgressBar::chunk { 
                background-color: #10b981; 
                border-radius: 6px; 
            }
        """)
        progress_layout.addWidget(self.progress_bar)
        
        btn_layout = QHBoxLayout()
        
        self.start_btn = QPushButton("▶️ START ENCODING")
        self.start_btn.setMinimumHeight(60)
        self.start_btn.setFont(QFont("Arial", 13, QFont.Weight.Bold))
        self.start_btn.setStyleSheet(self.button_style("#10b981", large=True))
        self.start_btn.clicked.connect(self.start_encoding)
        btn_layout.addWidget(self.start_btn)
        
        self.stop_btn = QPushButton("⏹️ STOP")
        self.stop_btn.setMinimumHeight(60)
        self.stop_btn.setFont(QFont("Arial", 13, QFont.Weight.Bold))
        self.stop_btn.setStyleSheet(self.button_style("#ef4444", large=True))
        self.stop_btn.clicked.connect(self.stop_encoding)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.stop_btn)
        
        progress_layout.addLayout(btn_layout)
        progress_group.setLayout(progress_layout)
        left_layout.addWidget(progress_group)
        
        # Log
        log_group = QGroupBox("📋 FFmpeg Output")
        log_group.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        log_layout = QVBoxLayout()
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(150)
        self.log_text.setStyleSheet("""
            QTextEdit { 
                background-color: #0f172a; 
                border: 2px solid #374151; 
                border-radius: 8px; 
                font-family: 'Consolas', 'Monaco', monospace; 
                font-size: 9pt; 
                color: #22d3ee; 
                padding: 8px; 
            }
        """)
        log_layout.addWidget(self.log_text)
        
        clear_log_btn = QPushButton("Clear Log")
        clear_log_btn.setStyleSheet(self.button_style("#374151"))
        clear_log_btn.clicked.connect(lambda: self.log_text.clear())
        log_layout.addWidget(clear_log_btn)
        
        log_group.setLayout(log_layout)
        left_layout.addWidget(log_group)
        
        splitter.addWidget(left_widget)
        
        # Right Panel
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setSpacing(10)
        
        tabs = QTabWidget()
        tabs.setStyleSheet("""
            QTabWidget::pane { 
                border: 2px solid #374151; 
                border-radius: 8px; 
                background-color: #1f2937; 
            }
            QTabBar::tab { 
                background-color: #374151; 
                color: white; 
                padding: 12px 25px; 
                margin-right: 2px;
                border-top-left-radius: 8px; 
                border-top-right-radius: 8px; 
                font-weight: bold; 
                font-size: 11pt;
            }
            QTabBar::tab:selected { 
                background-color: #4ade80; 
                color: black; 
            }
            QTabBar::tab:hover { 
                background-color: #4b5563; 
            }
        """)
        
        # Codec Tab
        codec_tab = QWidget()
        codec_scroll = QScrollArea()
        codec_scroll.setWidgetResizable(True)
        codec_scroll.setStyleSheet("QScrollArea { border: none; }")
        
        codec_content = QWidget()
        codec_layout = QVBoxLayout(codec_content)
        codec_layout.setSpacing(15)
        
        codec_layout.addWidget(self.make_label("Video Codec:", bold=True, size=12))
        self.codec_combo = QComboBox()
        self.codec_combo.addItems([
            "Apple ProRes (CPU Only)", 
            "H.264 NVENC (GPU)", 
            "H.265/HEVC NVENC (GPU)"
        ])
        self.codec_combo.setCurrentIndex(2)
        self.codec_combo.setMinimumHeight(45)
        self.codec_combo.setStyleSheet(self.combo_style())
        self.codec_combo.currentIndexChanged.connect(self.on_codec_changed)
        codec_layout.addWidget(self.codec_combo)
        
        # ProRes profile
        self.prores_label = self.make_label("ProRes Profile:", bold=True)
        codec_layout.addWidget(self.prores_label)
        
        self.prores_combo = QComboBox()
        self.prores_combo.addItems([
            "0 - Proxy", "1 - LT", "2 - 422", 
            "3 - 422 HQ", "4 - 4444", "5 - 4444 XQ"
        ])
        self.prores_combo.setCurrentIndex(5)
        self.prores_combo.setMinimumHeight(45)
        self.prores_combo.setStyleSheet(self.combo_style())
        codec_layout.addWidget(self.prores_combo)
        
        # Bit depth
        self.nvenc_label = self.make_label("Bit Depth:", bold=True)
        codec_layout.addWidget(self.nvenc_label)
        
        self.pixel_combo = QComboBox()
        self.pixel_combo.addItems(["8-bit (Faster)", "10-bit HDR"])
        self.pixel_combo.setMinimumHeight(45)
        self.pixel_combo.setStyleSheet(self.combo_style())
        codec_layout.addWidget(self.pixel_combo)
        
        # Quality Slider
        self.quality_frame = QFrame()
        self.quality_frame.setStyleSheet("""
            QFrame { 
                background-color: #1e3a5f; 
                border: 2px solid #3b82f6; 
                border-radius: 10px; 
                padding: 15px; 
            }
        """)
        quality_layout = QVBoxLayout(self.quality_frame)
        
        quality_header = QLabel("📊 OUTPUT QUALITY")
        quality_header.setStyleSheet("font-size: 13pt; font-weight: bold; color: #60a5fa;")
        quality_layout.addWidget(quality_header)
        
        self.quality_label = QLabel("CQ 18 - High Quality (~3-4 GB for 6 min)")
        self.quality_label.setStyleSheet("font-size: 11pt; color: #93c5fd;")
        quality_layout.addWidget(self.quality_label)
        
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setMinimum(10)
        self.quality_slider.setMaximum(32)
        self.quality_slider.setValue(18)
        self.quality_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.quality_slider.setTickInterval(2)
        self.quality_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: 1px solid #374151;
                height: 10px;
                background: #1f2937;
                border-radius: 5px;
            }
            QSlider::handle:horizontal {
                background: #3b82f6;
                border: 2px solid #60a5fa;
                width: 24px;
                margin: -8px 0;
                border-radius: 12px;
            }
            QSlider::handle:horizontal:hover {
                background: #60a5fa;
            }
            QSlider::sub-page:horizontal {
                background: #3b82f6;
                border-radius: 5px;
            }
        """)
        self.quality_slider.valueChanged.connect(self.update_quality_label)
        quality_layout.addWidget(self.quality_slider)
        
        quality_hint = QLabel("◀ Larger file, better quality    Smaller file, less quality ▶")
        quality_hint.setStyleSheet("font-size: 9pt; color: #6b7280;")
        quality_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        quality_layout.addWidget(quality_hint)
        
        codec_layout.addWidget(self.quality_frame)
        
        # Audio
        codec_layout.addWidget(self.make_label("Audio:", bold=True))
        self.audio_combo = QComboBox()
        self.audio_combo.addItems(["PCM 24-bit", "PCM 16-bit", "AAC 320k", "Copy"])
        self.audio_combo.setMinimumHeight(45)
        self.audio_combo.setStyleSheet(self.combo_style())
        codec_layout.addWidget(self.audio_combo)
        
        # GPU settings
        gpu_frame = QFrame()
        gpu_frame.setStyleSheet("""
            QFrame { 
                background-color: #374151; 
                border-radius: 10px; 
                padding: 15px; 
            }
        """)
        gpu_inner = QVBoxLayout(gpu_frame)
        
        self.gpu_check = QCheckBox("Enable GPU Acceleration")
        self.gpu_check.setChecked(True)
        self.gpu_check.setStyleSheet("font-size: 12pt; font-weight: bold;")
        self.gpu_check.stateChanged.connect(self.on_codec_changed)
        gpu_inner.addWidget(self.gpu_check)
        
        self.gpu_info = QLabel("")
        self.gpu_info.setStyleSheet("font-size: 10pt; color: #4ade80;")
        self.gpu_info.setWordWrap(True)
        gpu_inner.addWidget(self.gpu_info)
        
        threads_row = QHBoxLayout()
        threads_row.addWidget(QLabel("CPU Threads (0=Auto):"))
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, 32)
        self.threads_spin.setValue(0)
        self.threads_spin.setMinimumHeight(35)
        self.threads_spin.setMinimumWidth(80)
        self.threads_spin.setStyleSheet(self.spinbox_style())
        threads_row.addWidget(self.threads_spin)
        threads_row.addStretch()
        gpu_inner.addLayout(threads_row)
        
        codec_layout.addWidget(gpu_frame)
        codec_layout.addStretch()
        
        codec_scroll.setWidget(codec_content)
        codec_tab_layout = QVBoxLayout(codec_tab)
        codec_tab_layout.setContentsMargins(0, 0, 0, 0)
        codec_tab_layout.addWidget(codec_scroll)
        
        tabs.addTab(codec_tab, "🎬 Codec")
        
        # Filters Tab
        filters_tab = QWidget()
        filters_scroll = QScrollArea()
        filters_scroll.setWidgetResizable(True)
        filters_scroll.setStyleSheet("QScrollArea { border: none; }")
        
        filters_content = QWidget()
        filters_layout = QVBoxLayout(filters_content)
        filters_layout.setSpacing(15)
        
        info_frame = QFrame()
        info_frame.setStyleSheet("""
            QFrame { 
                background-color: #172554; 
                border: 2px solid #3b82f6; 
                border-radius: 10px; 
                padding: 12px; 
            }
        """)
        info_layout = QVBoxLayout(info_frame)
        info_title = QLabel("ℹ️ Filter Info")
        info_title.setStyleSheet("font-size: 11pt; font-weight: bold; color: #60a5fa;")
        info_layout.addWidget(info_title)
        info_text = QLabel(
            "• GPU decodes → CPU filters → GPU encodes\n"
            "• All levels optimized for 4K/5.3K footage\n"
            "• Use sharpen to recover detail after denoise\n"
            "• 'No Swim' option prevents warping artifacts"
        )
        info_text.setStyleSheet("font-size: 9pt; color: #93c5fd;")
        info_text.setWordWrap(True)
        info_layout.addWidget(info_text)
        filters_layout.addWidget(info_frame)
        
        # Denoise
        denoise_frame = self.make_filter_frame("🔇 NOISE REDUCTION", "#4ade80", 
            "Remove grain/noise (hqdn3d)")
        dl = denoise_frame.layout()
        self.denoise_combo = QComboBox()
        self.denoise_combo.addItems([
            "⭕ Off", 
            "🟢 Light", 
            "🟡 Medium", 
            "🟠 Heavy", 
            "🔴 Extreme", 
            "☢️ Nuclear",
            "☢️ Nuclear (No Swim)"
        ])
        self.denoise_combo.setMinimumHeight(50)
        self.denoise_combo.setStyleSheet(self.combo_style())
        dl.addWidget(self.denoise_combo)
        
        # Add explanation for No Swim
        no_swim_info = QLabel("💡 'No Swim' = Strong denoise without warping/swirling artifacts")
        no_swim_info.setStyleSheet("font-size: 9pt; color: #9ca3af;")
        no_swim_info.setWordWrap(True)
        dl.addWidget(no_swim_info)
        
        filters_layout.addWidget(denoise_frame)
        
        # Deflicker
        deflicker_frame = self.make_filter_frame("💡 DEFLICKER", "#f59e0b",
            "Remove flicker from LED/fluorescent lights")
        dfl = deflicker_frame.layout()
        self.deflicker_combo = QComboBox()
        self.deflicker_combo.addItems([
            "⭕ Off", "🟢 Light", "🟡 Medium",
            "🟠 Heavy", "🔴 Extreme", "☢️ Nuclear"
        ])
        self.deflicker_combo.setMinimumHeight(50)
        self.deflicker_combo.setStyleSheet(self.combo_style())
        dfl.addWidget(self.deflicker_combo)
        filters_layout.addWidget(deflicker_frame)
        
        # Temporal
        temporal_frame = self.make_filter_frame("🎞️ TEMPORAL SMOOTHING", "#a78bfa",
            "Blend frames (may cause ghosting)")
        tl = temporal_frame.layout()
        self.temporal_combo = QComboBox()
        self.temporal_combo.addItems([
            "⭕ Off", "🟢 Light (3 frames)",
            "🟡 Medium (5 frames)", "🟠 Heavy (5 frames)"
        ])
        self.temporal_combo.setMinimumHeight(50)
        self.temporal_combo.setStyleSheet(self.combo_style())
        tl.addWidget(self.temporal_combo)
        filters_layout.addWidget(temporal_frame)
        
        # Exposure
        exposure_frame = self.make_filter_frame("☀️ EXPOSURE", "#fbbf24",
            "Adjust brightness/gamma")
        el = exposure_frame.layout()
        self.exposure_combo = QComboBox()
        self.exposure_combo.addItems([
            "🌑 -5", "🌒 -4", "🌓 -3", "🌔 -2", "🌕 -1",
            "⚪ 0 No Change",
            "🌕 +1", "🌞 +2", "☀️ +3", "🔆 +4", "💥 +5"
        ])
        self.exposure_combo.setCurrentIndex(5)
        self.exposure_combo.setMinimumHeight(50)
        self.exposure_combo.setStyleSheet(self.combo_style())
        el.addWidget(self.exposure_combo)
        filters_layout.addWidget(exposure_frame)
        
        # Sharpen
        sharpen_frame = self.make_filter_frame("🔪 SHARPENING", "#ec4899",
            "Restore detail after denoising")
        sl = sharpen_frame.layout()
        self.sharpen_combo = QComboBox()
        self.sharpen_combo.addItems([
            "⭕ Off", "🟢 Light", "🟡 Medium",
            "🟠 Heavy", "🔴 Extreme", "☢️ Nuclear"
        ])
        self.sharpen_combo.setMinimumHeight(50)
        self.sharpen_combo.setStyleSheet(self.combo_style())
        sl.addWidget(self.sharpen_combo)
        filters_layout.addWidget(sharpen_frame)
        
        filters_layout.addStretch()
        filters_scroll.setWidget(filters_content)
        
        ftl = QVBoxLayout(filters_tab)
        ftl.setContentsMargins(0,0,0,0)
        ftl.addWidget(filters_scroll)
        
        tabs.addTab(filters_tab, "🔧 Filters")
        
        # Presets Tab
        presets_tab = QWidget()
        presets_scroll = QScrollArea()
        presets_scroll.setWidgetResizable(True)
        presets_scroll.setStyleSheet("QScrollArea { border: none; }")
        
        presets_content = QWidget()
        presets_layout = QVBoxLayout(presets_content)
        presets_layout.setSpacing(12)
        
        presets_layout.addWidget(self.make_label("⚡ QUICK PRESETS", size=14, bold=True, color="#4ade80"))
        presets_layout.addWidget(self.make_label("Output Format:", bold=True))
        
        for text, preset in [
            ("🎯 ProRes 4444 XQ (Best)", "prores_xq"),
            ("⚡ ProRes 422 HQ", "prores_hq"),
            ("🚀 H.265 High Quality", "hevc_hq"),
            ("📱 H.265 Balanced", "hevc_balanced"),
            ("📤 H.265 Small File", "hevc_small"),
        ]:
            btn = QPushButton(text)
            btn.setMinimumHeight(50)
            btn.setStyleSheet(self.button_style("#6366f1"))
            btn.clicked.connect(lambda c, p=preset: self.apply_preset(p))
            presets_layout.addWidget(btn)
        
        presets_layout.addWidget(self.make_label("GoPro Cleanup:", bold=True))
        
        for text, preset, color in [
            ("☀️ Outdoor (Light)", "gopro_outdoor", "#06b6d4"),
            ("🏠 Indoor (Deflicker)", "gopro_indoor", "#0891b2"),
            ("🌙 Low Light (Heavy)", "gopro_lowlight", "#0e7490"),
            ("☢️ Nuclear (Max)", "gopro_nuclear", "#dc2626"),
            ("☢️ Nuclear No Swim (Max, No Warp)", "gopro_nuclear_noswim", "#7c3aed"),
        ]:
            btn = QPushButton(text)
            btn.setMinimumHeight(50)
            btn.setStyleSheet(self.button_style(color))
            btn.clicked.connect(lambda c, p=preset: self.apply_preset(p))
            presets_layout.addWidget(btn)
        
        reset_btn = QPushButton("↺ Reset All")
        reset_btn.setMinimumHeight(45)
        reset_btn.setStyleSheet(self.button_style("#6b7280"))
        reset_btn.clicked.connect(self.reset_all)
        presets_layout.addWidget(reset_btn)
        
        presets_layout.addStretch()
        presets_scroll.setWidget(presets_content)
        
        ptl = QVBoxLayout(presets_tab)
        ptl.setContentsMargins(0,0,0,0)
        ptl.addWidget(presets_scroll)
        
        tabs.addTab(presets_tab, "⚡ Presets")
        
        right_layout.addWidget(tabs)
        splitter.addWidget(right_widget)
        
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        
        main_layout.addWidget(splitter)
    
    def update_quality_label(self, value):
        quality_info = {
            10: ("Maximum", "~8+ GB", "#22c55e"),
            11: ("Maximum", "~7 GB", "#22c55e"),
            12: ("Very High", "~6 GB", "#22c55e"),
            13: ("Very High", "~5.5 GB", "#22c55e"),
            14: ("Very High", "~5 GB", "#22c55e"),
            15: ("High", "~4.5 GB", "#3b82f6"),
            16: ("High", "~4 GB", "#3b82f6"),
            17: ("High", "~3.5 GB", "#3b82f6"),
            18: ("High", "~3 GB", "#3b82f6"),
            19: ("Good", "~2.5 GB", "#3b82f6"),
            20: ("Good", "~2 GB", "#f59e0b"),
            21: ("Good", "~1.8 GB", "#f59e0b"),
            22: ("Balanced", "~1.5 GB", "#f59e0b"),
            23: ("Balanced", "~1.3 GB", "#f59e0b"),
            24: ("Balanced", "~1.1 GB", "#f59e0b"),
            25: ("Smaller", "~1 GB", "#ef4444"),
            26: ("Smaller", "~900 MB", "#ef4444"),
            27: ("Smaller", "~800 MB", "#ef4444"),
            28: ("Small", "~700 MB", "#ef4444"),
            29: ("Small", "~600 MB", "#ef4444"),
            30: ("Smallest", "~500 MB", "#ef4444"),
            31: ("Smallest", "~450 MB", "#ef4444"),
            32: ("Smallest", "~400 MB", "#ef4444"),
        }
        
        info = quality_info.get(value, ("Custom", "varies", "#9ca3af"))
        quality_name, size_estimate, color = info
        
        self.quality_label.setText(f"CQ {value} - {quality_name} ({size_estimate} for 6 min)")
        self.quality_label.setStyleSheet(f"font-size: 11pt; color: {color};")
    
    def make_label(self, text, size=11, bold=False, color=None):
        label = QLabel(text)
        weight = QFont.Weight.Bold if bold else QFont.Weight.Normal
        label.setFont(QFont("Arial", size, weight))
        style = ""
        if color:
            style = f"color: {color};"
        label.setStyleSheet(style)
        return label
    
    def make_filter_frame(self, title, color, desc):
        frame = QFrame()
        frame.setStyleSheet(f"""
            QFrame {{ 
                background-color: #374151; 
                border-radius: 10px; 
                border-left: 5px solid {color};
                padding: 12px; 
            }}
        """)
        layout = QVBoxLayout(frame)
        layout.setSpacing(8)
        
        header = QLabel(title)
        header.setStyleSheet(f"font-size: 13pt; font-weight: bold; color: {color};")
        layout.addWidget(header)
        
        desc_label = QLabel(desc)
        desc_label.setStyleSheet("font-size: 9pt; color: #9ca3af;")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        return frame
    
    def set_dark_theme(self):
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(17, 24, 39))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Base, QColor(31, 41, 55))
        palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Button, QColor(55, 65, 81))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
        self.setPalette(palette)
    
    def button_style(self, color, large=False):
        return f"""
            QPushButton {{ 
                background-color: {color}; 
                color: white; 
                border: none; 
                border-radius: 8px; 
                padding: {'15px' if large else '10px'}; 
                font-size: {'13pt' if large else '11pt'}; 
                font-weight: bold; 
            }} 
            QPushButton:hover {{ background-color: {self.brighten(color, 1.15)}; }}
            QPushButton:pressed {{ background-color: {self.brighten(color, 0.85)}; }}
            QPushButton:disabled {{ background-color: #374151; color: #6b7280; }}
        """
    
    def combo_style(self):
        return """
            QComboBox { 
                background-color: #1f2937; 
                border: 2px solid #4b5563; 
                border-radius: 8px; 
                padding: 12px; 
                font-size: 11pt; 
                color: white; 
            } 
            QComboBox:hover { border-color: #4ade80; }
            QComboBox::drop-down { border: none; width: 35px; }
            QComboBox::down-arrow { 
                border-left: 6px solid transparent; 
                border-right: 6px solid transparent; 
                border-top: 8px solid #9ca3af; 
                margin-right: 12px; 
            }
            QComboBox QAbstractItemView { 
                background-color: #1f2937; 
                border: 2px solid #4b5563; 
                selection-background-color: #4ade80; 
                selection-color: black;
                color: white; 
                padding: 5px;
            }
        """
    
    def spinbox_style(self):
        return """
            QSpinBox { 
                background-color: #1f2937; 
                border: 2px solid #4b5563; 
                border-radius: 8px; 
                padding: 8px; 
                font-size: 11pt; 
                color: white; 
            }
            QSpinBox::up-button, QSpinBox::down-button { 
                width: 25px; 
                background-color: #4b5563; 
            }
        """
    
    def brighten(self, hex_color, factor):
        hex_color = hex_color.lstrip('#')
        r, g, b = [int(hex_color[i:i+2], 16) for i in (0, 2, 4)]
        r, g, b = [min(255, max(0, int(c * factor))) for c in (r, g, b)]
        return f"#{r:02x}{g:02x}{b:02x}"
    
    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Video Files", "",
            "Video Files (*.mp4 *.mov *.avi *.mkv *.mts *.m2ts *.MP4 *.MOV *.MTS);;All Files (*.*)"
        )
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
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_folder = folder
            self.output_label.setText(folder)
    
    def on_codec_changed(self):
        idx = self.codec_combo.currentIndex()
        is_prores = idx == 0
        is_nvenc = idx in [1, 2]
        
        self.prores_label.setVisible(is_prores)
        self.prores_combo.setVisible(is_prores)
        self.nvenc_label.setVisible(is_nvenc)
        self.pixel_combo.setVisible(is_nvenc)
        self.quality_frame.setVisible(is_nvenc)
        
        if is_prores:
            self.gpu_info.setText("⚠️ ProRes uses CPU only")
            self.gpu_info.setStyleSheet("font-size: 10pt; color: #fbbf24;")
        elif self.gpu_check.isChecked():
            codec_name = "H.264" if idx == 1 else "H.265/HEVC"
            self.gpu_info.setText(f"✅ GPU: NVENC {codec_name}")
            self.gpu_info.setStyleSheet("font-size: 10pt; color: #4ade80;")
        else:
            self.gpu_info.setText("⚠️ GPU disabled - using CPU")
            self.gpu_info.setStyleSheet("font-size: 10pt; color: #fbbf24;")
    
    def apply_preset(self, name):
        presets = {
            "prores_xq": {"codec": 0, "prores": 5, "audio": 0},
            "prores_hq": {"codec": 0, "prores": 3, "audio": 0},
            "hevc_hq": {"codec": 2, "pixel": 0, "audio": 0, "gpu": True, "quality": 14},
            "hevc_balanced": {"codec": 2, "pixel": 0, "audio": 2, "gpu": True, "quality": 20},
            "hevc_small": {"codec": 2, "pixel": 0, "audio": 2, "gpu": True, "quality": 26},
            "gopro_outdoor": {"denoise": 1, "deflicker": 0, "temporal": 0, "exposure": 5, "sharpen": 1},
            "gopro_indoor": {"denoise": 2, "deflicker": 2, "temporal": 1, "exposure": 6, "sharpen": 2},
            "gopro_lowlight": {"denoise": 4, "deflicker": 2, "temporal": 1, "exposure": 7, "sharpen": 2},
            "gopro_nuclear": {"denoise": 5, "deflicker": 5, "temporal": 2, "exposure": 7, "sharpen": 3},
            "gopro_nuclear_noswim": {"denoise": 6, "deflicker": 5, "temporal": 0, "exposure": 7, "sharpen": 3},
        }
        
        p = presets.get(name, {})
        
        if "codec" in p: self.codec_combo.setCurrentIndex(p["codec"])
        if "prores" in p: self.prores_combo.setCurrentIndex(p["prores"])
        if "pixel" in p: self.pixel_combo.setCurrentIndex(p["pixel"])
        if "audio" in p: self.audio_combo.setCurrentIndex(p["audio"])
        if "gpu" in p: self.gpu_check.setChecked(p["gpu"])
        if "quality" in p: self.quality_slider.setValue(p["quality"])
        if "denoise" in p: self.denoise_combo.setCurrentIndex(p["denoise"])
        if "deflicker" in p: self.deflicker_combo.setCurrentIndex(p["deflicker"])
        if "temporal" in p: self.temporal_combo.setCurrentIndex(p["temporal"])
        if "exposure" in p: self.exposure_combo.setCurrentIndex(p["exposure"])
        if "sharpen" in p: self.sharpen_combo.setCurrentIndex(p["sharpen"])
        
        self.on_codec_changed()
    
    def reset_all(self):
        self.codec_combo.setCurrentIndex(2)
        self.prores_combo.setCurrentIndex(5)
        self.pixel_combo.setCurrentIndex(0)
        self.audio_combo.setCurrentIndex(0)
        self.gpu_check.setChecked(True)
        self.threads_spin.setValue(0)
        self.quality_slider.setValue(18)
        self.denoise_combo.setCurrentIndex(0)
        self.deflicker_combo.setCurrentIndex(0)
        self.temporal_combo.setCurrentIndex(0)
        self.exposure_combo.setCurrentIndex(5)
        self.sharpen_combo.setCurrentIndex(0)
        self.on_codec_changed()
    
    def get_settings(self):
        codec_map = {0: "prores_ks", 1: "h264_nvenc", 2: "hevc_nvenc"}
        audio_map = {0: "pcm_s24le", 1: "pcm_s16le", 2: "aac", 3: "copy"}
        
        return {
            'video_codec': codec_map[self.codec_combo.currentIndex()],
            'prores_profile': self.prores_combo.currentIndex(),
            'pixel_format': self.pixel_combo.currentIndex(),
            'audio_codec': audio_map[self.audio_combo.currentIndex()],
            'use_gpu': self.gpu_check.isChecked(),
            'threads': self.threads_spin.value(),
            'quality_cq': self.quality_slider.value(),
            'denoise_level': self.denoise_combo.currentIndex(),
            'deflicker_level': self.deflicker_combo.currentIndex(),
            'temporal_level': self.temporal_combo.currentIndex(),
            'exposure_level': self.exposure_combo.currentIndex() - 5,
            'sharpen_level': self.sharpen_combo.currentIndex(),
        }
    
    def get_output_ext(self, codec):
        return ".mov" if codec == "prores_ks" else ".mp4"
    
    def start_encoding(self):
        if not self.input_files:
            QMessageBox.warning(self, "No Files", "Please add video files first.")
            return
        if not self.output_folder:
            QMessageBox.warning(self, "No Output", "Please select an output folder.")
            return
        
        try:
            result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10)
            version = result.stdout.decode().split('\n')[0]
            self.log_text.append(f"✅ {version}\n")
        except FileNotFoundError:
            QMessageBox.critical(self, "FFmpeg Not Found", 
                "FFmpeg is not installed.\n\nInstall with:\nsudo pacman -S ffmpeg")
            return
        except Exception as e:
            QMessageBox.critical(self, "Error", f"FFmpeg check failed: {e}")
            return
        
        settings = self.get_settings()
        if settings['use_gpu'] and settings['video_codec'] in ['h264_nvenc', 'hevc_nvenc']:
            try:
                result = subprocess.run(
                    ["ffmpeg", "-hide_banner", "-encoders"], 
                    capture_output=True, text=True, timeout=10
                )
                if "hevc_nvenc" not in result.stdout:
                    QMessageBox.warning(self, "NVENC Not Found",
                        "NVENC encoder not found!\n\n"
                        "Make sure you have:\n"
                        "• NVIDIA GPU\n"
                        "• nvidia drivers installed\n"
                        "• ffmpeg with NVENC support")
                    return
                else:
                    self.log_text.append("✅ NVENC available\n")
            except:
                pass
        
        self.current_file_index = 0
        self.encode_next()
    
    def encode_next(self):
        if self.current_file_index >= len(self.input_files):
            self.encoding_done(True, f"✅ All {len(self.input_files)} files completed!")
            return
        
        inp = self.input_files[self.current_file_index]
        settings = self.get_settings()
        ext = self.get_output_ext(settings['video_codec'])
        
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
            self.log_text.append(f"\n✅ Done: {Path(self.input_files[self.current_file_index]).name}\n")
            self.current_file_index += 1
            self.encode_next()
        else:
            self.log_text.append(f"\n❌ {msg}\n")
            self.encoding_done(False, msg)
    
    def stop_encoding(self):
        if self.encoding_thread:
            self.encoding_thread.stop()
            self.encoding_thread.wait()
        self.encoding_done(False, "Stopped by user")
    
    def append_log(self, text):
        doc = self.log_text.document()
        if doc.blockCount() > 500:
            cursor = self.log_text.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.movePosition(cursor.MoveOperation.Down, cursor.MoveMode.KeepAnchor, 100)
            cursor.removeSelectedText()
        
        self.log_text.append(text)
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())
    
    def encoding_done(self, success, msg):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        
        if success:
            QMessageBox.information(self, "Complete!", msg)
            self.progress_bar.setValue(100)
        elif "stopped" not in msg.lower():
            QMessageBox.warning(self, "Issue", msg)
        
        self.status_label.setText("Ready")
        self.file_label.setText("")
        self.current_file_index = 0
    
    def save_settings(self):
        s = self.settings_store
        s.setValue("codec", self.codec_combo.currentIndex())
        s.setValue("prores", self.prores_combo.currentIndex())
        s.setValue("pixel", self.pixel_combo.currentIndex())
        s.setValue("audio", self.audio_combo.currentIndex())
        s.setValue("gpu", self.gpu_check.isChecked())
        s.setValue("threads", self.threads_spin.value())
        s.setValue("quality", self.quality_slider.value())
        s.setValue("output", self.output_folder)
        s.setValue("denoise", self.denoise_combo.currentIndex())
        s.setValue("deflicker", self.deflicker_combo.currentIndex())
        s.setValue("temporal", self.temporal_combo.currentIndex())
        s.setValue("exposure", self.exposure_combo.currentIndex())
        s.setValue("sharpen", self.sharpen_combo.currentIndex())
    
    def load_settings(self):
        s = self.settings_store
        self.codec_combo.setCurrentIndex(s.value("codec", 2, type=int))
        self.prores_combo.setCurrentIndex(s.value("prores", 5, type=int))
        self.pixel_combo.setCurrentIndex(s.value("pixel", 0, type=int))
        self.audio_combo.setCurrentIndex(s.value("audio", 0, type=int))
        self.gpu_check.setChecked(s.value("gpu", True, type=bool))
        self.threads_spin.setValue(s.value("threads", 0, type=int))
        self.quality_slider.setValue(s.value("quality", 18, type=int))
        
        out = s.value("output", "")
        if out:
            self.output_folder = out
            self.output_label.setText(out)
        
        self.denoise_combo.setCurrentIndex(s.value("denoise", 0, type=int))
        self.deflicker_combo.setCurrentIndex(s.value("deflicker", 0, type=int))
        self.temporal_combo.setCurrentIndex(s.value("temporal", 0, type=int))
        self.exposure_combo.setCurrentIndex(s.value("exposure", 5, type=int))
        self.sharpen_combo.setCurrentIndex(s.value("sharpen", 0, type=int))
        
        self.update_quality_label(self.quality_slider.value())
    
    def closeEvent(self, event):
        if self.encoding_thread and self.encoding_thread.isRunning():
            reply = QMessageBox.question(
                self, "Encoding Active",
                "Encoding in progress. Stop and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self.encoding_thread.stop()
            self.encoding_thread.wait()
        
        self.save_settings()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = FastEncodeProApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
