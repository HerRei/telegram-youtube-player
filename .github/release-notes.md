Installers for Linux, Windows, and macOS.

## Setup

The setup now links directly to the Ollama download, the web-search guide, and API-key creation. Startup and desktop shortcuts can be enabled or disabled independently.

## Playback

Saved browser state no longer prevents playback when Windows or macOS returns no process details. `/skipall` skips directly to the final video while keeping it in the queue.

## Install

- **Windows x86-64:** download and run `TelegramYouTubePlayer-Setup-Windows-x86_64.exe`.
- **Ubuntu/Debian x86-64:** download and open `TelegramYouTubePlayer-Setup-Linux-x86_64.deb`.
- **Other x86-64 Linux:** download `TelegramYouTubePlayer-Setup-Linux-x86_64`, make it executable, and run it.
- **Apple Silicon Mac:** download and open `TelegramYouTubePlayer-Setup-macos-arm64.dmg`.
- **Intel Mac:** download and open `TelegramYouTubePlayer-Setup-macos-x86_64.dmg`.

Install a supported browser first. The setup links to [Ollama](https://ollama.com/download) if it is missing. Enter the bot token and [Ollama API key](https://ollama.com/settings/keys), send `/start` to the bot, and use **Detect from /start**. Select a browser, monitor, and optional launch items, then choose **Install and start**.

The builds are unsigned. Windows may show a SmartScreen prompt. On macOS, right-click the app and choose **Open**, or allow it under **System Settings > Privacy & Security**. Firefox also needs Accessibility permission on macOS.

`SHA256SUMS.txt` contains checksums for every download.
