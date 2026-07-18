#!/usr/bin/env python3
"""
Pomodoro Timer Interaktif berbasis CLI
========================================

Fitur:
- Input interaktif: Task Name, Focus Minutes, Break Minutes, Sessions, Music (YouTube)
- Countdown timer untuk fase Focus & Break
- Pause / Resume dengan tombol 'p' (tanpa perlu tekan Enter)
- Musik latar dari YouTube (audio only) + visualizer level suara (bar) real-time
- Notifikasi native macOS ketika sesi Focus/Break selesai
  (otomatis nonaktif jika dijalankan di OS selain macOS)

Kontrol saat berjalan:
  [p] pause / resume
  [s] skip fase saat ini
  [q] keluar dari aplikasi
"""

import os
import sys
import time
import queue
import shutil
import tempfile
import platform
import threading
import subprocess

from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.align import Align
from rich.text import Text
from rich.table import Table
from rich.progress import Progress, BarColumn, TextColumn
from rich.layout import Layout
import hashlib
from pathlib import Path
import numpy as np

CACHE_DIR = Path.home() / ".pomodoro" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
console = Console()

try:
    import sounddevice as sd
    import soundfile as sf
    AUDIO_LIBS_AVAILABLE = True
except ImportError:
    AUDIO_LIBS_AVAILABLE = False

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False


class QuitPomodoro(Exception):
    """Dilempar ketika user menekan 'q' untuk keluar dari aplikasi."""
    pass


# ---------------------------------------------------------------------------
# Key Listener - baca 1 tombol tanpa perlu Enter, cross-platform
# ---------------------------------------------------------------------------
class KeyListener:
    def __init__(self):
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._is_windows = os.name == "nt"
        self._old_settings = None
        self._fd = None

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        if not self._is_windows and self._old_settings is not None:
            import termios
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass

    def _listen(self):
        if self._is_windows:
            import msvcrt
            while not self._stop.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getch().decode(errors="ignore").lower()
                    self._q.put(ch)
                else:
                    time.sleep(0.05)
        else:
            import termios
            import tty
            import select

            if not sys.stdin.isatty():
                # Tidak ada terminal interaktif (mis. dijalankan lewat pipe)
                return

            self._fd = sys.stdin.fileno()
            self._old_settings = termios.tcgetattr(self._fd)
            try:
                tty.setcbreak(self._fd)
                while not self._stop.is_set():
                    r, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if r:
                        ch = sys.stdin.read(1).lower()
                        self._q.put(ch)
            finally:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)

    def get_key(self):
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None


# ---------------------------------------------------------------------------
# Notifier - notifikasi native macOS, otomatis nonaktif di OS lain
# ---------------------------------------------------------------------------
class Notifier:
    def __init__(self):
        self.is_macos = platform.system() == "Darwin"

    def notify(self, title: str, message: str):
        if not self.is_macos:
            print(f"\n(Notifikasi dinonaktifkan - bukan macOS) {title}: {message}")
            return
        safe_title = title.replace('"', "'")
        safe_message = message.replace('"', "'")
        script = f'display notification "{safe_message}" with title "{safe_title}" sound name "Glass"'
        try:
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            print(f"\n{title}: {message}")


# ---------------------------------------------------------------------------
# Audio Player + Visualizer
# Unduh audio dari YouTube (via yt-dlp) lalu mainkan sambil menghitung
# level RMS tiap blok sample untuk ditampilkan sebagai bar visualizer.
# ---------------------------------------------------------------------------
class AudioVisualizerPlayer:
    def __init__(self, youtube_url: str):
        self.youtube_url = youtube_url
        self.filepath = None
        self.tempdir = None
        self._level = 0.0
        self._pause_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread = None
        self.ready = False
        self.error = None
        self.download_percent = 0

    def prepare(self) -> bool:
        """Unduh audio dari YouTube sebagai wav. Return True jika sukses."""
        if not YTDLP_AVAILABLE or not AUDIO_LIBS_AVAILABLE:
            self.error = (
                "Library audio (yt-dlp/sounddevice/soundfile) belum terpasang. "
                "Jalankan: pip install -r requirements.txt"
            )
            return False
        if shutil.which("ffmpeg") is None:
            self.error = "ffmpeg tidak ditemukan di PATH. Install ffmpeg terlebih dahulu."
            return False

        cache_file = self._cache_file()

        if cache_file.exists():
            self.filepath = str(cache_file)
            self.ready = True
            return True

        if cache_file.exists():
            console.print("[bold green]✓ Music ditemukan di cache[/]")
            self.filepath = str(cache_file)
            self.ready = True
            return True

        console.print("[bold yellow]↓ Downloading audio dari YouTube...[/]")
        
        self.tempdir = tempfile.mkdtemp(prefix="pomodoro_audio_")

        out_template = os.path.join(
            self.tempdir,
            "track.%(ext)s"
        )
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_template,
            "progress_hooks": [self._progress_hook],
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "192",
            }],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self.youtube_url])
        except Exception as e:
            self.error = f"Gagal mengunduh audio: {e}"
            return False

        expected = os.path.join(self.tempdir, "track.wav")
        cache_file = self._cache_file()

        shutil.copy2(
            expected,
            cache_file
        )

        self.filepath = str(cache_file)
        if not os.path.exists(expected):
            self.error = "File audio hasil unduhan tidak ditemukan."
            return False
        console.print("[bold green]✓ Audio berhasil disimpan ke cache[/]")
        self.filepath = expected
        self.ready = True
        return True

    def play(self):
        if not self.ready:
            return
        self._thread = threading.Thread(target=self._play_loop, daemon=True)
        self._thread.start()

    def _play_loop(self):
        try:
            with sf.SoundFile(self.filepath) as f:
                samplerate = f.samplerate
                blocksize = 1024
                with sd.OutputStream(samplerate=samplerate, channels=f.channels) as stream:
                    while not self._stop_event.is_set():
                        if self._pause_event.is_set():
                            time.sleep(0.1)
                            continue
                        data = f.read(blocksize, dtype="float32")
                        if len(data) == 0:
                            f.seek(0)  # loop lagu jika sesi lebih panjang dari durasi lagu
                            continue
                        stream.write(data)
                        rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
                        self._level = min(rms * 8.0, 1.0)
        except Exception:
            self._level = 0.0

    def _progress_hook(self, d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")

            if total:
                downloaded = d.get("downloaded_bytes", 0)
                self.download_percent = downloaded / total * 100

        elif d["status"] == "finished":
            self.download_percent = 100

    def pause(self):
        self._pause_event.set()

    def resume(self):
        self._pause_event.clear()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self.tempdir and os.path.exists(self.tempdir):
            shutil.rmtree(self.tempdir, ignore_errors=True)

    def get_level(self) -> float:
        return self._level

    def _cache_file(self):

        name = hashlib.md5(
            self.youtube_url.encode("utf-8")
        ).hexdigest()

        return CACHE_DIR / f"{name}.wav"


# ---------------------------------------------------------------------------
# Helper tampilan
# ---------------------------------------------------------------------------
def render_bar(level: float, width: int = 30) -> str:
    filled = int(level * width)
    filled = max(0, min(width, filled))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def clear_screen():
    console.clear()

def clear_cache():

    if CACHE_DIR.exists():

        shutil.rmtree(CACHE_DIR)

    CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

def prompt_int(label: str, default: int) -> int:
    raw = input(f"{label} [{default}]: ").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        print("Input tidak valid, menggunakan nilai default.")
        return default


# ---------------------------------------------------------------------------
# Loop utama satu fase (Focus / Break)
# ---------------------------------------------------------------------------
def run_phase(phase_label, minutes, task_name, session_num, total_sessions,
              audio_player, notifier, key_listener, show_audio):
    remaining = minutes * 60
    paused = False

    while remaining >= 0:
        key = key_listener.get_key()
        if key == "q":
            raise QuitPomodoro()
        elif key == "s":
            break
        elif key == "p":
            paused = not paused
            if audio_player and show_audio:
                audio_player.pause() if paused else audio_player.resume()

        clear_screen()
        elapsed = (minutes * 60) - remaining
        mins, secs = divmod(remaining, 60)
        layout = Table.grid(expand=True)

        layout.add_row(
            f"[bold red]🍅 {task_name}[/]"
        )

        layout.add_row("")

        layout.add_row(
            Align.center(
                f"[bold bright_cyan]{mins:02}:{secs:02}[/]"
            )
        )

        layout.add_row("")

        layout.add_row(
            progress_bar(
                elapsed,
                minutes * 60
            )
        )

        layout.add_row("")

        layout.add_row(
            f"🎯 Session : [green]{session_num}/{total_sessions}[/]"
        )

        layout.add_row(
            f"⚡ Phase : [yellow]{phase_label}[/]"
        )

        if paused:
            layout.add_row(
                "[bold red]⏸ PAUSED[/]"
            )

        if show_audio:

            layout.add_row("")

            layout.add_row(
                Align.center(
                    render_visualizer(
                        audio_player.get_level()
                    )
                )
            )

        layout.add_row("")

        layout.add_row(
            "[cyan]P[/] Pause    "
            "[yellow]S[/] Skip    "
            "[red]Q[/] Quit"
        )

        console.clear()

        console.print(

            Panel(
                layout,
                title="[bold green]Pomodoro[/]",
                border_style="green",
                padding=(1, 2),
            )

        )

        if not paused:
            time.sleep(1)
            remaining -= 1
        else:
            time.sleep(0.1)

import random

_visual_cache = [1] * 24


def render_visualizer(level):

    global _visual_cache

    height = int(level * 49)

    _visual_cache.pop(0)

    _visual_cache.append(
        max(1, height + random.randint(-1, 1))
    )

    blocks = "▁▂▃▄▅▆▇█"

    result = ""

    for h in _visual_cache:

        h = max(1, min(8, h))

        color = (
            "green"
            if h < 4
            else "yellow"
            if h < 6
            else "red"
        )

        result += f"[{color}]{blocks[h-1]}[/] "

    return result

def progress_bar(current, total):

    p = Progress(
        TextColumn("[cyan]Progress"),
        BarColumn(bar_width=40),
        TextColumn("{task.percentage:>3.0f}%"),
    )

    task = p.add_task("", total=total)

    p.update(task, completed=current)

    return p

def show_banner():
    console.clear()

    banner = """
██████╗  ██████╗ ███╗   ███╗
██╔══██╗██╔═══██╗████╗ ████║
██████╔╝██║   ██║██╔████╔██║
██╔═══╝ ██║   ██║██║╚██╔╝██║
██║     ╚██████╔╝██║ ╚═╝ ██║
╚═╝      ╚═════╝ ╚═╝     ╚═╝
"""

    console.print(
        Panel.fit(
            f"[bold red]{banner}[/]\n"
            "[bold yellow]🍅 Pomodoro Terminal[/]",
            border_style="bright_red"
        )
    )

def main():
    show_banner()

    task_name = input("Task Name: ").strip() or "Tanpa Nama"
    focus_minutes = prompt_int("Focus Minutes", 25)
    break_minutes = prompt_int("Break Minutes", 5)
    sessions = prompt_int("Sessions (jumlah pengulangan sesi)", 4)
    music_url = input("Music (link YouTube, kosongkan jika tidak perlu): ").strip()

    notifier = Notifier()
    audio_player = None
    show_audio = False

    if music_url:
        ok = False
        with console.status(
            "[bold green]Preparing Music..."
        ):

            audio_player = AudioVisualizerPlayer(
                music_url
            )

            ok = audio_player.prepare()
        if ok:
            audio_player.play()
            show_audio = True
        else:
            print(f"Audio tidak dapat diputar: {audio_player.error}")
            print("Melanjutkan tanpa musik.\n")
            audio_player = None
            input()

    key_listener = KeyListener()
    key_listener.start()

    try:
        for session_num in range(1, sessions + 1):
            if audio_player and show_audio:
                audio_player.resume()
            run_phase("FOCUS", focus_minutes, task_name, session_num, sessions,
                      audio_player, notifier, key_listener, show_audio)
            notifier.notify("Pomodoro", f"Sesi Focus {session_num} selesai - saatnya istirahat!")

            if break_minutes > 0:
                if audio_player and show_audio:
                    audio_player.pause()
                run_phase("BREAK", break_minutes, task_name, session_num, sessions,
                          audio_player, notifier, key_listener, show_audio)
                notifier.notify("Pomodoro", "Istirahat selesai, waktunya kembali fokus!")

        clear_screen()

        console.print(
            Panel.fit(
                f"""

        [bold green]🎉 Semua sesi selesai![/]

        Task
        [cyan]{task_name}[/]

        Session
        [yellow]{sessions}[/]

        Good Job 🔥

        """,
                border_style="green"
            )
        )
        notifier.notify("Pomodoro Selesai", f"Semua sesi untuk '{task_name}' telah selesai!")
        time.sleep(2)

    except QuitPomodoro:
        clear_screen()
        print("Pomodoro dihentikan oleh user. Sampai jumpa!")
    except KeyboardInterrupt:
        clear_screen()
        print("\nPomodoro dihentikan (Ctrl+C).")
    finally:
        key_listener.stop()
        if audio_player:
            audio_player.stop()


if __name__ == "__main__":
    main()
