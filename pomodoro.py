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
import random
import os
import sys
import time
import queue
import shutil
import tempfile
import platform
import threading
import subprocess

from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.align import Align
from rich.text import Text
from rich.columns import Columns
from rich.progress import Progress, BarColumn, TextColumn
from rich.table import Table
from rich.live import Live
from rich import box

from pyfiglet import Figlet

import hashlib
from pathlib import Path
import numpy as np

CACHE_DIR = Path.home() / ".pomodoro" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
console = Console()

fig = Figlet(font="big")

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

        self._levels = np.zeros(32)

        self._smooth = np.zeros(32)
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

        self.filepath = str(cache_file)

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
                        if data.size:

                            rms = np.sqrt(np.mean(data ** 2))

                            self._level = min(rms * 8.0, 1.0)

                            # stereo -> mono
                            if data.ndim > 1:
                                mono = data.mean(axis=1)
                            else:
                                mono = data

                            # windowing
                            mono = mono * np.hanning(len(mono))

                            # FFT
                            fft = np.abs(np.fft.rfft(mono))

                            # Buang DC
                            fft = fft[1:]

                            # Ambil frekuensi penting
                            fft = fft[:1024]

                            num_bars = 48

                            edges = np.geomspace(
                                1,
                                len(fft),
                                num_bars + 1
                            ).astype(int)

                            levels = []

                            for i in range(num_bars):

                                start = edges[i]
                                end = edges[i + 1]

                                band = fft[start:end]

                                if len(band):
                                    levels.append(np.max(band))
                                else:
                                    levels.append(0)

                            levels = np.array(levels)
                            # 32 band
                            # bands = np.array_split(fft, 32)

                            # levels = np.array([
                            #     band.mean() if len(band) else 0
                            #     for band in bands
                            # ])
                            edges = np.geomspace(
                                1,
                                len(fft),
                                33
                            ).astype(int)

                            levels = []

                            for i in range(32):

                                start = edges[i]

                                end = edges[i + 1]

                                band = fft[start:end]

                                levels.append(
                                    band.mean() if len(band) else 0
                                )

                            levels = np.array(levels)

                            # Log scale
                            levels = np.log1p(levels * 25)

                            mx = np.percentile(levels, 95)

                            if mx > 0:
                                levels /= mx

                            levels = np.clip(levels, 0, 1)
                            levels = np.maximum(levels, self._smooth * 0.85)
                            # Smooth
                            self._smooth = (
                                self._smooth * 0.80
                                +
                                levels * 0.20
                            )

                        else:

                            self._level = 0
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
    
    def get_levels(self):
        return self._smooth.tolist()
    
    def _cache_file(self):

        name = hashlib.md5(
            self.youtube_url.encode("utf-8")
        ).hexdigest()

        return CACHE_DIR / f"{name}.wav"



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


def build_header(title, music, phase):

    table = Table.grid(expand=True)

    table.add_column(justify="left")
    table.add_column(justify="center")
    table.add_column(justify="right")

    phase_color = {
        "FOCUS": "red",
        "BREAK": "green"
    }.get(phase.upper(), "cyan")

    table.add_row(
        "[bold red]🍅 Pomodoro[/]",
        f"[bold cyan]{title}[/]",
        f"[bold {phase_color}]🎵 {music}[/]"
    )

    return Panel(
        table,
        border_style="bright_blue",
        box=box.ROUNDED,
    )

def build_footer():

    table = Table.grid(expand=True)

    table.add_column()

    table.add_row(
        "[bold cyan][P][/bold cyan] Pause    "
        "[bold yellow][S][/bold yellow] Skip    "
        "[bold red][Q][/bold red] Quit"
    )

    return Panel(
        Align.center(table),
        border_style="bright_blue",
        box=box.ROUNDED
    )

def build_big_timer(mins, secs):

    timer = f"{mins:02}:{secs:02}"

    txt = fig.renderText(timer)

    return Panel(
        Align.center(
            f"[bold bright_cyan]{txt}[/]"
        ),
        border_style="cyan",
        box=box.ROUNDED
    )

def build_progress(elapsed, total):

    progress = Progress(

        TextColumn("[bold cyan]Progress"),

        BarColumn(bar_width=None),

        TextColumn(
            "[bold]{task.percentage:>3.0f}%"
        ),

        expand=True,

    )

    task = progress.add_task("", total=total)

    progress.update(task, completed=elapsed)

    return progress

def build_layout(
    mins,
    secs,
    elapsed,
    total,
    session,
    total_sessions,
    task_name,
    phase,
    visual_levels=0.0,
    music="No Music",
):

    layout = Layout()

    layout.split_column(

        Layout(name="header", size=3),

        Layout(name="body"),

        Layout(name="footer", size=3),

    )

    layout["body"].split_column(

        Layout(name="timer", size=11),

        Layout(name="progress", size=4),

        Layout(name="info", size=5),

        Layout(name="visualizer"),

    )

    layout["header"].update(

        build_header(

            task_name,

            music,

            phase,

        )

    )

    layout["timer"].update(

        build_big_timer(

            mins,

            secs,

        )

    )

    layout["progress"].update(

        Panel(

            build_progress(

                elapsed,

                total,

            ),

            title="[bold green]Progress[/]",

            border_style="green",

        )

    )

    info = Table.grid(expand=True)

    info.add_column()

    info.add_column()

    info.add_row(

        f"[bold]Session[/]\n{session}/{total_sessions}",

        f"[bold]Mode[/]\n[cyan]{phase}[/]",

    )

    layout["info"].update(

        Panel(

            info,

            border_style="yellow",

        )

    )
    height = console.size.height - 18
    layout["visualizer"].update(
        Panel(
            Align.center(
                build_visualizer(
                    visual_levels,
                    height
                ),
                vertical="middle",
            ),

            border_style="magenta",

            title="[bold]Audio Visualizer[/]",

        )

    )

    layout["footer"].update(

        build_footer()

    )

    return layout

# ---------------------------------------------------------------------------
# Loop utama satu fase (Focus / Break)
# ---------------------------------------------------------------------------
def run_phase(phase_label, minutes, task_name, session_num, total_sessions,
              audio_player, notifier, key_listener, show_audio):
    remaining = minutes * 60
    last_tick = time.monotonic()
    paused = False
    with Live(

        build_layout(

            mins=minutes,
            secs=0,

            elapsed=0,

            total=minutes * 60,

            session=session_num,

            total_sessions=total_sessions,

            task_name=task_name,

            phase=phase_label,

            visual_levels=[0] * 32,

        ),

        refresh_per_second=30,

        screen=True,

    ) as live:
        while remaining >= 0:
            now = time.monotonic()
            key = key_listener.get_key()
            if key == "q":
                raise QuitPomodoro()
            elif key == "s":
                break
            elif key == "p":
                paused = not paused
                if audio_player and show_audio:
                    audio_player.pause() if paused else audio_player.resume()

            elapsed = (minutes * 60) - remaining
            if not paused:

                if now - last_tick >= 1:

                    remaining -= 1

                    last_tick = now

            mins, secs = divmod(
                max(remaining, 0),
                60
            )

            elapsed = (minutes * 60) - remaining
            
            live.update(

                build_layout(

                    mins=mins,

                    secs=secs,

                    elapsed=elapsed,

                    total=minutes * 60,

                    session=session_num,

                    total_sessions=total_sessions,

                    task_name=task_name,

                    phase=phase_label,

                    visual_levels=audio_player.get_levels(),

                )

            )


            
            time.sleep(1 / 30)



_history = [0.0] * 32


def build_visualizer(levels, height=16):

    if levels is None:
        levels = [0] * 32

    rows = []

    for y in reversed(range(height)):

        line = Text()

        for value in levels:

            filled = int(value * height)

            if filled > y:

                ratio = y / height

                if ratio < 0.35:
                    style = "green"

                elif ratio < 0.7:
                    style = "yellow"

                else:
                    style = "red"

                line.append("█ ", style=style)

            else:

                line.append("  ")

        rows.append(line)

    return Group(*rows)

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
