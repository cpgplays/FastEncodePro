# FastEncodePro
GPU-accelerated video encoder with advanced noise reduction.
# 🎬 FastEncode Pro

**GPU-accelerated video encoder with advanced noise reduction for GoPro and action camera footage.**

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/Python-3.8+-green.svg)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Linux-orange.svg)](https://linux.org)

---

## ✨ Features

- **🚀 GPU Accelerated** - NVIDIA NVENC encoding (H.264/H.265)
- **🎨 ProRes Support** - Apple ProRes 422/4444 encoding
- **🔇 Advanced Denoise** - 6 levels from Light to NUCLEAR
- **💡 Deflicker** - Remove LED/fluorescent light flicker
- **☀️ Exposure Control** - -5 to +5 stops adjustment
- **🔪 Sharpening** - Recover detail after denoising
- **📊 Quality Control** - Adjustable output quality slider
- **🎞️ Temporal Smoothing** - Frame blending for smooth footage
- **📁 Batch Processing** - Encode multiple files
- **🌙 Dark Theme** - Easy on the eyes

---

## 📦 Installation

### Requirements

- Linux (Ubuntu, Arch, Fedora, etc.)
- Python 3.8+
- PyQt6
- FFmpeg with NVENC support
- NVIDIA GPU (for GPU acceleration)

### Arch Linux

```bash
sudo pacman -S python python-pyqt6 ffmpeg

Ubuntu/Debian
Bash

sudo apt install python3 python3-pyqt6 ffmpeg
Run
Bash

git clone https://github.com/cpgplays/FastEncodePro.git
cd FastEncodePro
python3 fastencode_pro.py
📄 License
Apache License 2.0

🙏 Acknowledgments
Built with assistance from Claude (Anthropic)
Powered by FFmpeg and NVIDIA NVENC
UI built with PyQt6
Made with ❤️ by cpgplays
