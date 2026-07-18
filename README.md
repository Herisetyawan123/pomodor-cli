# Pomodoro Timer Interaktif (CLI)

![Preview](/images/preview.png)
Pomodoro timer berbasis command line dengan countdown, pause/resume,
musik latar dari YouTube (audio only) lengkap dengan visualizer level
suara, dan notifikasi native macOS saat sesi selesai.

## Fitur

- Input interaktif per tugas: **Task Name**, **Focus Minutes**, **Break Minutes**,
  **Sessions**, dan **Music** (link YouTube).
- Countdown timer untuk fase **Focus** dan **Break**.
- **Pause / Resume** kapan saja tanpa menghentikan aplikasi.
- Musik latar diambil langsung dari YouTube (hanya audio-nya) dan diputar
  di background, lengkap dengan **bar visualizer** yang merefleksikan
  level suara secara real-time.
- **Notifikasi native macOS** (via `osascript`) ketika sebuah fase Focus/Break
  selesai. Jika dijalankan di OS selain macOS, notifikasi otomatis
  dinonaktifkan dan diganti pesan biasa di terminal - tidak akan error.

## Prasyarat

Selain dependency Python di `requirements.txt`, aplikasi ini butuh:

1. **Python 3.9+**
2. **ffmpeg** terpasang dan bisa diakses dari PATH (dipakai `yt-dlp` untuk
   mengekstrak audio ke format wav).
   - macOS (Homebrew): `brew install ffmpeg`
   - Ubuntu/Debian: `sudo apt install ffmpeg`
   - Windows (choco): `choco install ffmpeg`
3. **PortAudio** (dibutuhkan oleh `sounddevice` untuk memutar audio).
   - macOS: biasanya sudah tersedia; jika belum, `brew install portaudio`
   - Ubuntu/Debian: `sudo apt install libportaudio2`
   - Windows: sudah termasuk dalam wheel `sounddevice`, tidak perlu instalasi tambahan.

Jika `music` dikosongkan saat input, semua library audio di atas tidak
diperlukan - aplikasi tetap berjalan normal sebagai timer biasa.

## Instalasi

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Menjalankan

```bash
python3 pomodoro.py
```

Kamu akan diminta mengisi:

```
Task Name: Belajar Machine Learning
Focus Minutes [25]: 25
Break Minutes [5]: 5
Sessions (jumlah pengulangan sesi) [4]: 4
Music (link YouTube, kosongkan jika tidak perlu): https://www.youtube.com/watch?v=xxxxxxxxxxx
```

Tekan Enter tanpa mengetik apa pun untuk memakai nilai default di dalam `[ ]`.

## Kontrol saat Timer Berjalan

| Tombol | Fungsi                          |
|--------|----------------------------------|
| `p`    | Pause / Resume (termasuk musik) |
| `s`    | Skip ke fase berikutnya          |
| `q`    | Keluar dari aplikasi             |

Tombol langsung terbaca tanpa perlu menekan Enter.

## Catatan tentang Musik & Visualizer

- Audio diunduh sekali ke folder temporary saat sesi dimulai, lalu diputar
  looping selama fase **Focus** berlangsung, dan otomatis di-pause saat
  masuk fase **Break** (agar telinga istirahat), lalu resume lagi di sesi
  Focus berikutnya.
- Bar visualizer (`[████░░░░░░]`) dihitung dari nilai RMS (root-mean-square)
  amplitudo audio secara real-time, bukan simulasi acak.
- Jika link YouTube gagal diunduh (private, region-locked, dsb), aplikasi
  akan menampilkan pesan error dan **tetap melanjutkan** timer tanpa musik.

## Catatan tentang Notifikasi

- Di **macOS**: notifikasi native akan muncul di Notification Center
  setiap kali fase Focus atau Break selesai.
- Di **Linux/Windows**: notifikasi otomatis dinonaktifkan (karena
  `osascript` hanya tersedia di macOS) dan diganti dengan pesan teks
  biasa di terminal, sehingga aplikasi tidak akan crash.

## Troubleshooting

- **`ffmpeg tidak ditemukan di PATH`** → install ffmpeg sesuai OS di atas.
- **Tidak ada suara / error PortAudio** → pastikan `libportaudio2`
  (Linux) atau PortAudio (macOS via brew) sudah terpasang, lalu cek
  device audio default di sistem.
- **Tombol pause/skip tidak terbaca** → pastikan dijalankan langsung di
  terminal interaktif (bukan lewat pipe/redirect), karena aplikasi
  membaca keypress langsung dari `stdin`.

## Struktur File

```
pomodoro.py       # Script utama (timer, audio player, notifier, key listener)
requirements.txt  # Dependency Python
README.md         # Dokumentasi ini
```
