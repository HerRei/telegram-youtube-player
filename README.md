# Telegram YouTube Player

A Telegram bot that opens YouTube links or search results fullscreen on a selected monitor. It supports Linux, Windows, and macOS.

## Requirements

- Python 3.11 or a packaged release.
- Firefox, Chromium, Google Chrome, or Brave.
- [Ollama](https://ollama.com/download) with `qwen3:0.6b-q4_K_M`.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- An [Ollama API key](https://ollama.com/settings/keys) for web search.

Minimum hardware:

- 64-bit x86-64 or ARM64 CPU with 2 logical cores.
- 4 GB RAM; 8 GB is recommended for high-resolution playback.
- 2 GB of free disk space in addition to the browser.
- A graphics adapter that can drive the selected monitor.

## Setup

Download the installer for your operating system from the [latest release](https://github.com/HerRei/telegram-youtube-player/releases/latest):

- Windows x86-64: `.exe`
- Ubuntu/Debian x86-64: `.deb`
- Other x86-64 Linux: portable executable
- macOS: `.dmg` for Apple Silicon or Intel

Open the installer, send `/start` to the bot, and use **Detect from /start**. Select a browser and monitor, then choose **Install and start**.

From source:

```bash
git clone https://github.com/HerRei/telegram-youtube-player.git
cd telegram-youtube-player
python player.py
```

The installer detects active monitors and supported browsers, downloads the Ollama model if needed, and adds the app to startup:

- Linux: systemd user service.
- Windows: user Startup folder.
- macOS: user LaunchAgent.

On macOS, Firefox needs Accessibility permission for monitor placement and fullscreen control.

## Browsers

| Browser | Content blocking |
| --- | --- |
| Firefox | uBlock Origin is installed in the playback profile. |
| Chromium | uBlock Origin Lite is installed in the playback profile. |
| Brave | Uses built-in Shields. |
| Google Chrome | Uses Chrome content controls. |

Playback uses a separate profile and does not change the user's normal browser profile.

## Use

Send a YouTube link or describe a page to find. Direct non-YouTube links are rejected.

```text
/status  Show the browser and target monitor.
/stop    Close the playback window.
/help    Show bot help.
```

Source installs also support:

```bash
python player.py configure
python player.py install
python player.py check
```

Configuration is kept in the current user's application-data directory. Real tokens and keys must not be committed. See [`config.example.json`](config.example.json).

## Tests

```bash
python -m unittest discover -s tests -v
python player.py smoke-test
```

CI tests the source and packaged apps on Linux, Windows, Apple Silicon macOS, and Intel macOS. Physical second-monitor playback still needs testing on the target machine.
