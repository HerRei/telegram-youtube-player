# Telegram YouTube Player

A Telegram bot that opens YouTube links or search results fullscreen on a selected monitor. It supports Linux, Windows, and macOS.

## Requirements

- Python 3.11 or a packaged release.
- Firefox, Chromium, Google Chrome, or Brave.
- [Ollama](https://ollama.com/download) with `qwen3:0.6b-q4_K_M`. The setup links to the download if Ollama is missing.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- An [Ollama API key](https://ollama.com/settings/keys) for web search. See the [official guide](https://docs.ollama.com/capabilities/web-search).

Minimum hardware:

- 64-bit x86-64 or ARM64 CPU with 2 logical cores.
- 4 GB RAM; 8 GB is recommended for high-resolution playback.
- 2 GB of free disk space in addition to the browser.

## Setup

Download the installer for your operating system from the [latest release](https://github.com/HerRei/telegram-youtube-player/releases/latest):

- Windows x86-64: `.exe`
- Ubuntu/Debian x86-64: `.deb`
- Other x86-64 Linux: portable executable
- macOS: `.dmg` for Apple Silicon or Intel

Open the installer, send `/start` to the bot, and use **Detect from /start**. The setup provides Ollama download and API-key links. Select a browser and monitor, choose whether to create a desktop shortcut and start the app when you sign in, then select **Install and start**.

From source:

```bash
git clone https://github.com/HerRei/telegram-youtube-player.git
cd telegram-youtube-player
python player.py
```

The installer detects active monitors and supported browsers and downloads the Ollama model if needed. Startup is optional:

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

Send one or more YouTube links or describe a page to find. New YouTube links are added to the queue without interrupting the current video. The queue survives service restarts. Direct non-YouTube links are rejected.

```text
/queue   Show the current video and waiting items.
/skip    Play the next item.
/skipall Skip directly to the final item in the queue.
/clear   Remove waiting items but keep the current video.
/status  Show the browser, monitor, and queue size.
/stop    Close playback and clear the queue.
/help    Show all commands.
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
python player.py queue-smoke-test
```

CI tests the source, queue server, player script, and packaged apps on Linux, Windows, Apple Silicon macOS, and Intel macOS. Physical second-monitor playback still needs testing on the target machine.
