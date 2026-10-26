# Bilibili Downloader

A local web app (runs on your own PC) to download Bilibili videos and whole channels, with optional crop,
background-music removal, Chinese-to-English captions and an English voice.

> **Use responsibly.** This tool is for videos you own or have permission to download, change and republish.
> Respect Bilibili's terms of service and the rights of the creators. It is not affiliated with Bilibili, and the
> authors are not responsible for how it is used.

## Run it (one command)

1. Download **`bilibili-downloader.zip`** from the [latest release](../../releases/latest) (or clone this
   repository) and unzip it anywhere.
2. Double-click **`run.bat`** (or type `run.bat` in a terminal opened in the folder).

The first run sets everything up, then opens the app in your browser at http://127.0.0.1:5000.
Later runs just start it. Press **Ctrl+C** in the window to stop.

| Option | What it does |
|---|---|
| `run.bat -Ai yes` | install the AI add-ons without asking |
| `run.bat -Ai no` | never install them (plain downloader only) |
| `run.bat -NoOpen` | do not open the browser |
| `run.bat -Port 5001` | use another port |
| `run.bat -DryRun` | show what would be installed, change nothing |

## What the first run does

- Finds Python 3.10 or newer (offers to install it with winget if missing).
- Creates a private environment (in `%USERPROFILE%\bili-audio\app-venv`) and installs Flask and yt-dlp
  (small, about a minute).
- Looks for **ffmpeg** (offers to install it with winget). Without it you only get basic downloads.
- Asks whether to install the **AI add-ons**: music removal, English captions, English voice.
  They need about **7 GB** of disk and a long first download (PyTorch, speech and translation models).
  An NVIDIA graphics card makes them roughly 10 times faster; without one they run on the processor.
  They are installed outside the app folder, in `%USERPROFILE%\bili-audio` (override with the
  `BILI_AUDIO_HOME` environment variable). Say no and the app still works without them.

It is safe to run again at any time: whatever is already installed is skipped.

## Where things are

| | |
|---|---|
| Downloaded videos | `downloads\<channel name>\` |
| Channels, settings, library | `data\app.db` (copy it to keep your setup) |
| App environment, AI add-ons and models | `%USERPROFILE%\bili-audio` |

## Notes

- **Windows only.** Needs an internet connection.
- **Bilibili blocks some connections** (error 412), including most VPN exit addresses. With a VPN on, the app
  sends its small lookup requests from your normal connection and the video download through the VPN
  (Settings, "VPN"). If channel listing is blocked, wait a few minutes, or add a cookies file in Settings.
- Translations and the English voice are automatic and will contain mistakes. Check the `.srt` file saved next
  to each video before publishing.
- Only download, change or republish videos that you own or have permission to use. Respect Bilibili's
  terms and the creators' rights.
- Third-party software and models and their licences: see `THIRD_PARTY.md`.
- This project is released under the MIT licence (see `LICENSE`).

## Troubleshooting

- *The window closes or shows an error:* run `run.bat` from a terminal to read the message.
- *The page says a tool is "not installed":* run `run.bat -Ai yes`.
- *"The folder path is too long":* Windows struggles with very long paths. Move the app folder somewhere short
  (for example `C:\bili-downloader`), or set `BILI_AUDIO_HOME=C:\bili` before running.
- *Start from scratch / uninstall:* delete the app folder and `%USERPROFILE%\bili-audio`.
