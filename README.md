# 🎞️ Timelapser

A simple **Windows** desktop app for turning a folder of photos into a smooth
timelapse video. Built with **CustomTkinter** and powered by **FFmpeg**.

## ✨ Features

- 🖼️ Image preview grid with EXIF capture times, sorting and reverse
- ⚡ Automatic GPU encoder detection (NVENC / AMF / QSV / VideoToolbox), CPU fallback
- 🎛️ Adjustable FPS, resolution, quality and encoding speed
- 📥 Auto-installs FFmpeg via `winget` if it isn't already on your system
- 📂 Supports JPG, PNG, BMP, TIFF and WebP

## 📦 Installation

**Requirements:** Windows · Python 3.11+

```bash
git clone <repo-url>
cd Timelapser
pip install customtkinter Pillow
```

> FFmpeg is installed automatically on first launch if missing. To install it
> yourself, get it from [ffmpeg.org](https://ffmpeg.org/) and add it to `PATH`.

## 🚀 Usage

```bash
python app.py
```

1. Click **📁 Ordner öffnen** and pick any image — the whole folder loads
2. Sort the frames and open **⚙ Einstellungen** to set FPS, resolution, quality and speed
3. Choose an output file with **💾 Ausgabedatei …**
4. Hit **▶ Timelapse erstellen** ✅

## 🏗️ Building the EXE

Run the included PowerShell script to produce a standalone `dist\Timelapser.exe`:

```powershell
.\build.ps1
```

It installs the build dependencies (`customtkinter`, `Pillow`, `pyinstaller`),
bundles everything into a single windowed executable with PyInstaller, and
launches the app when done.

## 🙏 Credits

The original `timelapse.py` script was written by [LarsKue](https://github.com/LarsKue).

---

<sub>Timelapser v1.0 · Windows</sub>
