#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import getpass
import http.server
import ipaddress
import json
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import link_search
import platform_support as native


APP_NAME = native.APP_NAME
PROJECT_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_PATHS = native.application_paths()
CONFIG_DIR = APP_PATHS.config_dir
STATE_DIR = APP_PATHS.state_dir
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = STATE_DIR / "state.json"
PLAYER_PID_FILE = STATE_DIR / "browser-player.json"
LEGACY_PLAYER_PID_FILE = STATE_DIR / "firefox-player.json"
QUEUE_FILE = STATE_DIR / "queue.json"
PROFILE_DIR = CONFIG_DIR / "firefox-profile"
MONITORS_FILE = Path.home() / ".config/monitors.xml"
SERVICE_FILE = APP_PATHS.service_file
DEFAULT_FIREFOX = Path(native.browser_specs()[0].common_paths[0])
DEFAULT_OLLAMA_MODEL = "qwen3:0.6b-q4_K_M"
BUNDLED_FIND_LINK_SCRIPT = PROJECT_DIR / "link_search.py"
DEFAULT_FIND_LINK_SCRIPT = "bundled"
UBLOCK_ORIGIN_URL = "https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi"
UBLOCK_LITE_RELEASE_API = "https://api.github.com/repos/uBlockOrigin/uBOL-home/releases/latest"
UBLOCK_LITE_RELEASE_PREFIX = "https://github.com/uBlockOrigin/uBOL-home/releases/download/"
UBLOCK_LITE_MAX_DOWNLOAD = 50 * 1024 * 1024
UBLOCK_LITE_MAX_UNPACKED = 100 * 1024 * 1024
LOG = logging.getLogger(APP_NAME)

URL_PATTERN = re.compile(
    r"(?i)\b(?:(?:https?://|www\.)[^\s<>]+|"
    r"(?:[a-z0-9-]+\.)*(?:youtube\.com|youtube-nocookie\.com)/[^\s<>]+|youtu\.be/[^\s<>]+)"
)
TRAILING_URL_PUNCTUATION = ".,!?;:)]}'\""
YOUTUBE_HOSTS = {"youtube.com", "youtu.be", "youtube-nocookie.com"}


BrowserSpec = native.BrowserSpec
Monitor = native.Monitor
BROWSER_SPECS = native.browser_specs()


def browser_spec(key: str) -> BrowserSpec:
    return native.browser_spec(key, BROWSER_SPECS)


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_chat_id: int
    allowed_user_id: int
    target_monitor_product: str = ""
    target_monitor_connector: str = ""
    browser_type: str = "firefox"
    browser_path: str = str(DEFAULT_FIREFOX)
    ollama_api_key: str = ""
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    find_link_script: str = DEFAULT_FIND_LINK_SCRIPT

    @classmethod
    def load(cls) -> "Config":
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            browser_type = str(raw.get("browser_type", "firefox"))
            browser_path = str(raw.get("browser_path", raw.get("firefox_path", DEFAULT_FIREFOX)))
            browser_spec(browser_type)
            return cls(
                bot_token=str(raw["bot_token"]),
                allowed_chat_id=int(raw["allowed_chat_id"]),
                allowed_user_id=int(raw["allowed_user_id"]),
                target_monitor_product=str(raw["target_monitor_product"]),
                target_monitor_connector=str(raw.get("target_monitor_connector", "")),
                browser_type=browser_type,
                browser_path=browser_path,
                ollama_api_key=str(raw.get("ollama_api_key", "")),
                ollama_model=str(raw.get("ollama_model", DEFAULT_OLLAMA_MODEL)),
                find_link_script=str(raw.get("find_link_script", DEFAULT_FIND_LINK_SCRIPT)),
            )
        except FileNotFoundError as error:
            raise RuntimeError(f"Configuration not found. Run: {PROJECT_DIR / 'player.py'} configure") from error
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Invalid configuration in {CONFIG_FILE}: {error}") from error

class _XClientMessageData(ctypes.Union):
    _fields_ = [
        ("b", ctypes.c_char * 20),
        ("s", ctypes.c_short * 10),
        ("l", ctypes.c_long * 5),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _XClientMessageData),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("xclient", _XClientMessageEvent), ("pad", ctypes.c_long * 24)]


class X11WindowManager:
    CLIENT_MESSAGE = 33
    SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
    SUBSTRUCTURE_REDIRECT_MASK = 1 << 20

    def __init__(self) -> None:
        library = ctypes.util.find_library("X11")
        if not library:
            raise RuntimeError("libX11 is required for monitor placement")
        self.x11 = ctypes.CDLL(library)
        self._declare_functions()
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError(f"Cannot connect to X display {os.environ.get('DISPLAY', '(unset)')}")
        self.root = int(self.x11.XDefaultRootWindow(self.display))

    def _declare_functions(self) -> None:
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.x11.XInternAtom.restype = ctypes.c_ulong
        self.x11.XGetWindowProperty.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        self.x11.XGetWindowProperty.restype = ctypes.c_int
        self.x11.XFree.argtypes = [ctypes.c_void_p]
        self.x11.XFree.restype = ctypes.c_int
        self.x11.XSendEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.POINTER(_XEvent),
        ]
        self.x11.XSendEvent.restype = ctypes.c_int
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.x11.XFlush.restype = ctypes.c_int
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x11.XCloseDisplay.restype = ctypes.c_int

    def close(self) -> None:
        if self.display:
            self.x11.XCloseDisplay(self.display)
            self.display = None

    def __enter__(self) -> "X11WindowManager":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def atom(self, name: str) -> int:
        return int(self.x11.XInternAtom(self.display, name.encode("ascii"), False))

    def property_longs(self, window: int, property_name: str) -> list[int]:
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        item_count = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.x11.XGetWindowProperty(
            self.display,
            window,
            self.atom(property_name),
            0,
            4096,
            False,
            0,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(item_count),
            ctypes.byref(bytes_after),
            ctypes.byref(data),
        )
        if status != 0 or not data:
            return []
        try:
            if actual_format.value != 32:
                return []
            values = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))
            return [int(values[index]) for index in range(item_count.value)]
        finally:
            self.x11.XFree(data)

    def find_window_for_pid(self, pid: int) -> int | None:
        for window in self.property_longs(self.root, "_NET_CLIENT_LIST"):
            window_pids = self.property_longs(window, "_NET_WM_PID")
            if window_pids and window_pids[0] == pid:
                return window
        return None

    def find_window_for_process_group(self, process_group: int) -> int | None:
        for window in self.property_longs(self.root, "_NET_CLIENT_LIST"):
            window_pids = self.property_longs(window, "_NET_WM_PID")
            if not window_pids:
                continue
            try:
                if os.getpgid(window_pids[0]) == process_group:
                    return window
            except ProcessLookupError:
                continue
        return None

    def send_client_message(self, window: int, message_type: str, values: list[int]) -> None:
        event = _XEvent()
        event.xclient.type = self.CLIENT_MESSAGE
        event.xclient.serial = 0
        event.xclient.send_event = True
        event.xclient.display = self.display
        event.xclient.window = window
        event.xclient.message_type = self.atom(message_type)
        event.xclient.format = 32
        for index, value in enumerate(values[:5]):
            event.xclient.data.l[index] = value
        sent = self.x11.XSendEvent(
            self.display,
            self.root,
            False,
            self.SUBSTRUCTURE_NOTIFY_MASK | self.SUBSTRUCTURE_REDIRECT_MASK,
            ctypes.byref(event),
        )
        self.x11.XFlush(self.display)
        if not sent:
            raise RuntimeError(f"The window manager rejected {message_type}")

    def set_state(self, window: int, action: int, first: str, second: str | None = None) -> None:
        self.send_client_message(
            window,
            "_NET_WM_STATE",
            [action, self.atom(first), self.atom(second) if second else 0, 1, 0],
        )

    def place_fullscreen(self, window: int, monitor: Monitor) -> None:
        self.set_state(window, 0, "_NET_WM_STATE_FULLSCREEN")
        self.set_state(window, 0, "_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ")
        time.sleep(0.15)
        flags = (1 << 12) | (1 << 8) | (1 << 9) | (1 << 10) | (1 << 11)
        self.send_client_message(
            window,
            "_NET_MOVERESIZE_WINDOW",
            [flags, monitor.x, monitor.y, monitor.width, monitor.height],
        )
        time.sleep(0.15)
        self.set_state(window, 1, "_NET_WM_STATE_FULLSCREEN")
        self.send_client_message(window, "_NET_ACTIVE_WINDOW", [1, 0, 0, 0, 0])

    def close_window(self, window: int) -> None:
        self.send_client_message(window, "_NET_CLOSE_WINDOW", [0, 1, 0, 0, 0])


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def request(self, method: str, payload: dict[str, Any], timeout: int = 20) -> Any:
        request = urllib.request.Request(
            self.base_url + method,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                description = json.loads(error.read()).get("description", str(error))
            except (json.JSONDecodeError, UnicodeDecodeError):
                description = str(error)
            raise RuntimeError(f"Telegram API error: {description}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError(f"Cannot reach the Telegram API: {error.reason if hasattr(error, 'reason') else error}") from error

        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result.get('description', 'unknown error')}")
        return result.get("result")

    def get_me(self) -> dict[str, Any]:
        return self.request("getMe", {})

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "edited_message", "channel_post"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload, timeout=timeout + 10)

    def send_message(self, chat_id: int, text: str) -> None:
        self.request("sendMessage", {"chat_id": chat_id, "text": text})


def normalize_youtube_url(candidate: str) -> str | None:
    candidate = candidate.strip().rstrip(TRAILING_URL_PUNCTUATION)
    if "://" not in candidate:
        candidate = "https://" + candidate

    try:
        parsed = urllib.parse.urlsplit(candidate)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password:
        return None
    if port not in {None, 80, 443}:
        return None
    if not any(hostname == root or hostname.endswith("." + root) for root in YOUTUBE_HOSTS):
        return None

    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key.lower() != "autoplay"]
    query.append(("autoplay", "1"))
    netloc = hostname
    return urllib.parse.urlunsplit(("https", netloc, parsed.path or "/", urllib.parse.urlencode(query), parsed.fragment))


def youtube_playback_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query_values = dict(query)
    path_parts = [part for part in parsed.path.split("/") if part]
    video_id: str | None = None

    if hostname == "youtu.be" or hostname.endswith(".youtu.be"):
        video_id = path_parts[0] if path_parts else None
    elif path_parts[:1] == ["watch"]:
        video_id = query_values.get("v")
    elif len(path_parts) >= 2 and path_parts[0] in {"shorts", "live", "embed"}:
        video_id = path_parts[1]

    if video_id and re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        embed_query = [(key, value) for key, value in query if key not in {"v", "autoplay"}]
        embed_query.append(("autoplay", "1"))
        return urllib.parse.urlunsplit(
            ("https", "www.youtube.com", f"/embed/{video_id}", urllib.parse.urlencode(embed_query), "")
        )

    if path_parts[:1] == ["playlist"] and query_values.get("list"):
        playlist_query = [(key, value) for key, value in query if key in {"list", "index"}]
        playlist_query.append(("autoplay", "1"))
        return urllib.parse.urlunsplit(
            ("https", "www.youtube.com", "/embed/videoseries", urllib.parse.urlencode(playlist_query), "")
        )
    return url


def _youtube_start_seconds(value: str) -> int:
    if value.isdigit():
        return min(int(value), 24 * 60 * 60)
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if not match or not any(match.groups()):
        return 0
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return min(hours * 3600 + minutes * 60 + seconds, 24 * 60 * 60)


@dataclass(frozen=True)
class QueueItem:
    token: str
    kind: str
    media_id: str
    source_url: str
    title: str
    start_seconds: int = 0
    index: int = 0

    @classmethod
    def load(cls, data: dict[str, Any]) -> "QueueItem":
        item = cls(
            token=str(data["token"]),
            kind=str(data["kind"]),
            media_id=str(data["media_id"]),
            source_url=str(data["source_url"]),
            title=str(data["title"]),
            start_seconds=int(data.get("start_seconds", 0)),
            index=int(data.get("index", 0)),
        )
        if item.kind not in {"video", "playlist"}:
            raise ValueError("Invalid queue item type")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", item.media_id):
            raise ValueError("Invalid YouTube media ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", item.token):
            raise ValueError("Invalid queue token")
        return item


def youtube_queue_item(url: str, title: str = "") -> QueueItem:
    normalized = normalize_youtube_url(url)
    if not normalized:
        raise ValueError("A valid YouTube link is required")
    embed = urllib.parse.urlsplit(youtube_playback_url(normalized))
    parts = [part for part in embed.path.split("/") if part]
    query = dict(urllib.parse.parse_qsl(embed.query, keep_blank_values=True))

    if parts == ["embed", "videoseries"] and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", query.get("list", "")):
        kind = "playlist"
        media_id = query["list"]
        source_url = f"https://www.youtube.com/playlist?list={media_id}"
        try:
            index = max(int(query.get("index", "1")) - 1, 0)
        except ValueError:
            index = 0
        start_seconds = 0
    elif len(parts) == 2 and parts[0] == "embed" and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", parts[1]):
        kind = "video"
        media_id = parts[1]
        source_url = f"https://www.youtube.com/watch?v={media_id}"
        index = 0
        start_seconds = _youtube_start_seconds(query.get("start") or query.get("t") or "")
    else:
        raise ValueError("The YouTube link does not identify a video or playlist")

    clean_title = re.sub(r"\s+", " ", title).strip()[:200]
    return QueueItem(
        token=secrets.token_urlsafe(12),
        kind=kind,
        media_id=media_id,
        source_url=source_url,
        title=clean_title or source_url,
        start_seconds=start_seconds,
        index=index,
    )


class PlaybackQueue:
    def __init__(self, path: Path | None = QUEUE_FILE) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.current: QueueItem | None = None
        self.pending: list[QueueItem] = []
        self.revision = 0
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            current = QueueItem.load(data["current"]) if data.get("current") else None
            pending = [QueueItem.load(item) for item in data.get("pending", [])]
        except FileNotFoundError:
            return
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            LOG.warning("Ignoring invalid playback queue: %s", error)
            return
        if current is None and pending:
            current = pending.pop(0)
        self.current = current
        self.pending = pending

    def _save(self) -> None:
        if not self.path:
            return
        if self.current is None and not self.pending:
            self.path.unlink(missing_ok=True)
            return
        write_json_secure(
            self.path,
            {
                "current": asdict(self.current) if self.current else None,
                "pending": [asdict(item) for item in self.pending],
            },
        )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "revision": self.revision,
                "current": asdict(self.current) if self.current else None,
                "pending": [asdict(item) for item in self.pending],
            }

    def enqueue(self, items: list[QueueItem]) -> list[int]:
        if not items:
            return []
        with self.lock:
            positions: list[int] = []
            for item in items:
                if self.current is None:
                    self.current = item
                    positions.append(0)
                else:
                    self.pending.append(item)
                    positions.append(len(self.pending))
            self.revision += 1
            self._save()
            return positions

    def advance(self, expected_token: str | None = None) -> tuple[QueueItem | None, QueueItem | None]:
        with self.lock:
            if self.current is None or (expected_token and expected_token != self.current.token):
                return None, self.current
            previous = self.current
            self.current = self.pending.pop(0) if self.pending else None
            self.revision += 1
            self._save()
            return previous, self.current

    def skip_to_last(self) -> tuple[QueueItem | None, QueueItem | None, int]:
        with self.lock:
            if self.current is None:
                return None, None, 0
            if not self.pending:
                return self.current, self.current, 0
            previous = self.current
            skipped = len(self.pending)
            self.current = self.pending[-1]
            self.pending.clear()
            self.revision += 1
            self._save()
            return previous, self.current, skipped

    def clear_pending(self) -> int:
        with self.lock:
            count = len(self.pending)
            if count:
                self.pending.clear()
                self.revision += 1
                self._save()
            return count

    def clear(self) -> int:
        with self.lock:
            count = len(self.pending) + int(self.current is not None)
            if count:
                self.current = None
                self.pending.clear()
                self.revision += 1
                self._save()
            return count


def player_script(control_token: str, origin: str) -> str:
    script = """const controlToken = __CONTROL_TOKEN__;
const playerOrigin = __PLAYER_ORIGIN__;
let player = null;
let ready = false;
let currentToken = null;
let wanted = null;
let hasPlayed = false;
let advancing = false;

async function requestState(path, options = {}) {
  const headers = Object.assign({}, options.headers || {}, {"X-Player-Token": controlToken});
  const response = await fetch(path, Object.assign({}, options, {headers, cache: "no-store"}));
  if (!response.ok) throw new Error(`Player request failed: ${response.status}`);
  return response.json();
}

function loadItem(item) {
  currentToken = item.token;
  hasPlayed = false;
  if (item.kind === "playlist") {
    player.loadPlaylist({list: item.media_id, listType: "playlist", index: item.index || 0});
  } else {
    player.loadVideoById({videoId: item.media_id, startSeconds: item.start_seconds || 0});
  }
}

function applyState(state) {
  wanted = state.current;
  if (!ready) return;
  if (!wanted) {
    if (currentToken !== null || hasPlayed) player.stopVideo();
    currentToken = null;
    hasPlayed = false;
    return;
  }
  if (wanted.token !== currentToken) loadItem(wanted);
}

async function syncState() {
  try {
    applyState(await requestState("/api/queue"));
  } catch (_) {
  }
}

async function advance() {
  if (!currentToken || advancing) return;
  const completedToken = currentToken;
  advancing = true;
  try {
    const state = await requestState("/api/advance", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token: completedToken})
    });
    if (currentToken === completedToken) currentToken = null;
    applyState(state);
  } catch (_) {
    setTimeout(advance, 1000);
  } finally {
    advancing = false;
  }
}

function onStateChange(event) {
  if (event.data === YT.PlayerState.PLAYING) hasPlayed = true;
  if (event.data !== YT.PlayerState.ENDED || !hasPlayed || !wanted) return;
  if (wanted.kind === "playlist") {
    const playlist = player.getPlaylist() || [];
    const index = player.getPlaylistIndex();
    if (playlist.length && index >= 0 && index < playlist.length - 1) return;
  }
  advance();
}

window.onYouTubeIframeAPIReady = function () {
  player = new YT.Player("player", {
    width: "100%",
    height: "100%",
    playerVars: {autoplay: 1, origin: playerOrigin},
    events: {
      onReady: function () { ready = true; applyState({current: wanted}); },
      onStateChange: onStateChange,
      onError: advance
    }
  });
};

syncState();
setInterval(syncState, 1000);
"""
    return script.replace("__CONTROL_TOKEN__", json.dumps(control_token)).replace(
        "__PLAYER_ORIGIN__", json.dumps(origin)
    )


def player_document(control_token: str, origin: str) -> bytes:
    script = player_script(control_token, origin)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="referrer" content="strict-origin-when-cross-origin">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>YouTube Player</title>
  <style>
    html, body, #player {{ width: 100%; height: 100%; margin: 0; border: 0; overflow: hidden; background: #000; }}
  </style>
</head>
<body>
  <div id="player"></div>
  <script src="https://www.youtube.com/iframe_api"></script>
  <script>{script}</script>
</body>
</html>
""".encode("utf-8")


class LocalPlaybackServer:
    def __init__(self, queue: PlaybackQueue) -> None:
        owner = self

        class RequestHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.handle(self)

            def do_POST(self) -> None:
                owner.handle(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
        self.server.daemon_threads = True
        host, port = self.server.server_address
        self.origin = f"http://{host}:{port}"
        self.queue = queue
        self.control_token = secrets.token_urlsafe(24)
        self.thread = threading.Thread(target=self.server.serve_forever, name="local-youtube-player", daemon=True)
        self.thread.start()

    def page_url(self) -> str:
        return f"{self.origin}/player?token={urllib.parse.quote(self.control_token, safe='')}"

    @staticmethod
    def _response(request: http.server.BaseHTTPRequestHandler, status: int, content_type: str, body: bytes) -> None:
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.end_headers()
        request.wfile.write(body)

    def _authorized(self, request: http.server.BaseHTTPRequestHandler) -> bool:
        return secrets.compare_digest(request.headers.get("X-Player-Token", ""), self.control_token)

    def handle(self, request: http.server.BaseHTTPRequestHandler) -> None:
        parsed = urllib.parse.urlsplit(request.path)
        if request.command == "GET" and parsed.path == "/player":
            token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
            if not secrets.compare_digest(token, self.control_token):
                self._response(request, 403, "text/plain; charset=utf-8", b"Forbidden")
                return
            document = player_document(self.control_token, self.origin)
            request.send_response(200)
            request.send_header("Content-Type", "text/html; charset=utf-8")
            request.send_header("Content-Length", str(len(document)))
            request.send_header("Cache-Control", "no-store")
            request.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            request.send_header("X-Content-Type-Options", "nosniff")
            request.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline' https://www.youtube.com https://s.ytimg.com; "
                "frame-src https://www.youtube.com https://www.youtube-nocookie.com; connect-src 'self'; "
                "style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            request.end_headers()
            request.wfile.write(document)
            return

        if parsed.path not in {"/api/queue", "/api/advance"} or not self._authorized(request):
            self._response(request, 403, "text/plain; charset=utf-8", b"Forbidden")
            return
        if request.command == "GET" and parsed.path == "/api/queue":
            body = json.dumps(self.queue.snapshot()).encode("utf-8")
            self._response(request, 200, "application/json; charset=utf-8", body)
            return
        if request.command == "POST" and parsed.path == "/api/advance":
            try:
                length = int(request.headers.get("Content-Length", "0"))
                if length < 2 or length > 4096:
                    raise ValueError
                payload = json.loads(request.rfile.read(length))
                completed_token = str(payload["token"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._response(request, 400, "text/plain; charset=utf-8", b"Invalid request")
                return
            self.queue.advance(completed_token)
            body = json.dumps(self.queue.snapshot()).encode("utf-8")
            self._response(request, 200, "application/json; charset=utf-8", body)
            return
        self._response(request, 405, "text/plain; charset=utf-8", b"Method not allowed")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def telegram_entity_text(text: str, offset: int, length: int) -> str:
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")


def message_text(message: dict[str, Any]) -> str:
    return str(message.get("text") or message.get("caption") or "").strip()


def message_links(message: dict[str, Any]) -> list[str]:
    text = message_text(message)
    entities = message.get("entities") or message.get("caption_entities") or []
    candidates: list[str] = []

    for entity in entities:
        if entity.get("type") == "text_link" and entity.get("url"):
            candidates.append(str(entity["url"]))
        elif entity.get("type") == "url":
            try:
                candidates.append(telegram_entity_text(text, int(entity["offset"]), int(entity["length"])))
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                pass

    candidates.extend(match.group(0) for match in URL_PATTERN.finditer(text))
    links: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip().rstrip(TRAILING_URL_PUNCTUATION)
        if candidate and candidate not in links:
            links.append(candidate)
    return links


def youtube_links(message: dict[str, Any]) -> list[str]:
    links: list[str] = []
    for candidate in message_links(message):
        normalized = normalize_youtube_url(candidate)
        if normalized and normalized not in links:
            links.append(normalized)
    return links


def normalize_public_url(candidate: str) -> str | None:
    candidate = candidate.strip()
    if not candidate or any(character.isspace() or ord(character) < 32 for character in candidate):
        return None
    try:
        parsed = urllib.parse.urlsplit(candidate)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password:
        return None
    if not hostname or port not in {None, 80, 443}:
        return None

    try:
        address = ipaddress.ip_address(socket.inet_aton(hostname))
    except OSError:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
    if address is not None:
        if not address.is_global:
            return None
    else:
        try:
            hostname.encode("idna")
        except UnicodeError:
            return None
        blocked_suffixes = (".local", ".internal", ".home", ".lan", ".test", ".invalid")
        if (
            hostname == "localhost"
            or hostname.endswith(blocked_suffixes)
            or "." not in hostname
            or all(part.isdigit() for part in hostname.split("."))
        ):
            return None
    return candidate


def validate_ollama_model_name(model: str) -> str:
    model = model.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)?", model):
        raise RuntimeError("Invalid Ollama model name")
    return model


def ollama_executable(system: str | None = None) -> Path | None:
    system = native.host_system(system)
    command = shutil.which("ollama")
    if command:
        return Path(command)
    if system == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        candidates = (local / "Programs/Ollama/ollama.exe", Path("C:/Program Files/Ollama/ollama.exe"))
    elif system == "Darwin":
        candidates = (
            Path("/opt/homebrew/bin/ollama"),
            Path("/usr/local/bin/ollama"),
            Path("/Applications/Ollama.app/Contents/Resources/ollama"),
        )
    else:
        candidates = (Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama"))
    return next((path for path in candidates if path.is_file()), None)


def ensure_ollama_model(model: str, pull_if_missing: bool = False) -> None:
    model = validate_ollama_model_name(model)
    ollama = ollama_executable()
    if not ollama:
        raise RuntimeError("Ollama is not installed or is not on PATH")
    try:
        result = subprocess.run(
            [str(ollama), "list"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("Could not connect to the local Ollama service") from error
    installed = {
        line.split()[0]
        for line in (result.stdout or "").splitlines()[1:]
        if line.split()
    }
    if model in installed:
        return
    if not pull_if_missing:
        raise RuntimeError(f"Ollama model {model} is not installed")
    print(f"Downloading Ollama model {model}...")
    try:
        subprocess.run([str(ollama), "pull", model], check=True, timeout=1800)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"Could not download Ollama model {model}") from error


def link_finder_python(script: Path) -> Path:
    candidates = [
        script.parent / ".venv/bin/python",
        script.parent / ".venv/Scripts/python.exe",
    ]
    if not getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve())
    candidates.extend(Path(path) for name in ("python3", "python") if (path := shutil.which(name)))
    for candidate in candidates:
        if candidate.is_file() and (platform.system() == "Windows" or os.access(candidate, os.X_OK)):
            return candidate
    raise RuntimeError("No Python interpreter is available for the link finder")


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str


class LinkFinder:
    def __init__(self, config: Config) -> None:
        self.api_key = config.ollama_api_key.strip()
        self.model = validate_ollama_model_name(config.ollama_model)
        raw_script = config.find_link_script.strip()
        self.bundled = raw_script in {"", "bundled"}
        self.script = BUNDLED_FIND_LINK_SCRIPT if self.bundled else Path(raw_script).expanduser().resolve()
        self.python = None if self.bundled else link_finder_python(self.script)

    def check(self, pull_if_missing: bool = False) -> None:
        if not self.api_key:
            raise RuntimeError("Ollama web-search API key is not configured")
        ensure_ollama_model(self.model, pull_if_missing=pull_if_missing)
        if self.bundled:
            return
        if not self.script.is_file():
            raise RuntimeError(f"Link finder script not found: {self.script}")
        environment = os.environ.copy()
        environment["OLLAMA_MODEL"] = self.model
        try:
            result = subprocess.run(
                [
                    str(self.python),
                    "-c",
                    "import runpy,sys; runpy.run_path(sys.argv[1], run_name='dependency_check')",
                    str(self.script),
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Timed out while checking the link finder") from error
        if result.returncode != 0:
            raise RuntimeError(f"The link finder cannot run with {self.python}")

    def find(self, query: str) -> SearchResult:
        query = " ".join(query.split())[:500]
        if not query:
            raise RuntimeError("A search query is required")
        if self.bundled:
            payload = link_search.search(query, self.api_key, self.model)
        else:
            environment = os.environ.copy()
            environment["OLLAMA_API_KEY"] = self.api_key
            environment["OLLAMA_MODEL"] = self.model
            try:
                result = subprocess.run(
                    [str(self.python), str(self.script), query],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=240,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("Link search timed out") from error
            if result.returncode != 0:
                raise RuntimeError("Link search failed")
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise RuntimeError("Link search returned an invalid response") from error
        try:
            url = normalize_public_url(str(payload["url"]))
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Link search returned an invalid response") from error
        if not url:
            raise RuntimeError("Link search did not return a safe public URL")
        title = re.sub(r"\s+", " ", str(payload.get("title") or "Search result")).strip()[:200]
        return SearchResult(title=title or "Search result", url=url)


def configured_monitors(monitors_file: Path = MONITORS_FILE) -> list[Monitor]:
    return native.configured_monitors(monitors_file=monitors_file)


def find_monitor(product: str, monitors_file: Path = MONITORS_FILE, connector: str = "") -> Monitor:
    return native.find_monitor(product, connector, monitors_file=monitors_file)


def browser_profile_dir(
    browser_type: str,
    executable: Path | None = None,
    system: str | None = None,
) -> Path:
    browser_spec(browser_type)
    if native.host_system(system) == "Linux" and executable and executable.as_posix().startswith("/snap/"):
        snap_name = executable.name
        return Path.home() / "snap" / snap_name / "common" / f"{APP_NAME}-profile"
    if browser_type == "firefox":
        return PROFILE_DIR
    return CONFIG_DIR / f"{browser_type}-profile"


def prepare_firefox_profile(profile_dir: Path = PROFILE_DIR) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    user_js = """user_pref("media.autoplay.default", 0);
user_pref("media.autoplay.blocking_policy", 0);
user_pref("extensions.autoDisableScopes", 0);
user_pref("extensions.enabledScopes", 15);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
"""
    (profile_dir / "user.js").write_text(user_js, encoding="ascii")


def prepare_brave_profile(profile_dir: Path) -> None:
    local_state_file = profile_dir / "Local State"
    try:
        local_state = json.loads(local_state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        local_state = {}
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"Invalid Brave local state in {local_state_file}: {error}") from error

    brave = local_state.setdefault("brave", {})
    if not isinstance(brave, dict):
        raise RuntimeError(f"Invalid Brave local state in {local_state_file}")
    p3a = brave.setdefault("p3a", {})
    if not isinstance(p3a, dict):
        raise RuntimeError(f"Invalid Brave local state in {local_state_file}")
    p3a["enabled"] = False
    p3a["notice_acknowledged"] = True
    write_json_secure(local_state_file, local_state)


def ublock_lite_dir(profile_dir: Path) -> Path:
    return profile_dir / "extensions/uBlock-Origin-Lite"


def valid_ublock_lite_extension(extension_dir: Path) -> bool:
    try:
        manifest = json.loads((extension_dir / "manifest.json").read_text(encoding="utf-8"))
        messages = json.loads((extension_dir / "_locales/en/messages.json").read_text(encoding="utf-8"))
        return (
            manifest.get("manifest_version") == 3
            and manifest.get("author") == "Raymond Hill"
            and messages.get("extName", {}).get("message") == "uBlock Origin Lite"
            and (extension_dir / "rulesets/main/easylist.json").is_file()
            and (extension_dir / "rulesets/main/easyprivacy.json").is_file()
            and (extension_dir / "rulesets/main/ublock-filters.json").is_file()
        )
    except (FileNotFoundError, TypeError, json.JSONDecodeError):
        return False


def install_ublock_lite_archive(archive_file: Path, extension_dir: Path) -> None:
    staging = extension_dir.with_name(extension_dir.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_file) as archive:
            if sum(member.file_size for member in archive.infolist()) > UBLOCK_LITE_MAX_UNPACKED:
                raise RuntimeError("uBlock Origin Lite archive is too large")
            for member in archive.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise RuntimeError(f"Unsafe path in uBlock Origin Lite archive: {member.filename}")
            archive.extractall(staging)
        if not valid_ublock_lite_extension(staging):
            raise RuntimeError("The downloaded extension is not uBlock Origin Lite")
        shutil.rmtree(extension_dir, ignore_errors=True)
        os.replace(staging, extension_dir)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(f"Could not unpack uBlock Origin Lite: {error}") from error


def prepare_ublock_lite(profile_dir: Path) -> None:
    extension_dir = ublock_lite_dir(profile_dir)
    if valid_ublock_lite_extension(extension_dir):
        return

    extension_dir.parent.mkdir(parents=True, exist_ok=True)
    archive_file = extension_dir.parent / "uBlock-Origin-Lite.zip.tmp"
    try:
        release_request = urllib.request.Request(
            UBLOCK_LITE_RELEASE_API,
            headers={"Accept": "application/vnd.github+json", "User-Agent": APP_NAME},
        )
        with urllib.request.urlopen(release_request, timeout=30) as response:
            release = json.load(response)
        assets = release.get("assets", [])
        asset = next(
            (
                item
                for item in assets
                if isinstance(item, dict)
                and re.fullmatch(r"uBOLite_[0-9.]+\.chromium\.zip", str(item.get("name", "")))
            ),
            None,
        )
        asset_url = str(asset.get("browser_download_url", "")) if isinstance(asset, dict) else ""
        if not asset_url.startswith(UBLOCK_LITE_RELEASE_PREFIX):
            raise RuntimeError("The latest official Chromium release asset was not found")
        asset_request = urllib.request.Request(asset_url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(asset_request, timeout=60) as response:
            archive_data = response.read(UBLOCK_LITE_MAX_DOWNLOAD + 1)
        if len(archive_data) > UBLOCK_LITE_MAX_DOWNLOAD:
            raise RuntimeError("The uBlock Origin Lite download is too large")
        archive_file.write_bytes(archive_data)
        install_ublock_lite_archive(archive_file, extension_dir)
    except (
        OSError,
        TypeError,
        urllib.error.URLError,
        json.JSONDecodeError,
        RuntimeError,
    ) as error:
        raise RuntimeError(f"Could not install uBlock Origin Lite from its official release: {error}") from error
    finally:
        archive_file.unlink(missing_ok=True)


def prepare_player_integration(spec: BrowserSpec, executable: Path, profile_dir: Path) -> None:
    if not executable.is_file() or (platform.system() != "Windows" and not os.access(executable, os.X_OK)):
        raise RuntimeError(f"{spec.label} executable not found: {executable}")
    profile_dir.mkdir(parents=True, exist_ok=True)
    if spec.key == "brave":
        prepare_brave_profile(profile_dir)
    elif spec.key == "chromium":
        prepare_ublock_lite(profile_dir)
    if spec.family != "firefox":
        return

    prepare_firefox_profile(profile_dir)

    extension = profile_dir / "extensions/uBlock0@raymondhill.net.xpi"
    if not extension.is_file():
        extension.parent.mkdir(parents=True, exist_ok=True)
        temporary = extension.with_suffix(".xpi.tmp")
        try:
            with urllib.request.urlopen(UBLOCK_ORIGIN_URL, timeout=30) as response:
                temporary.write_bytes(response.read())
            with zipfile.ZipFile(temporary) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            extension_id = (
                manifest.get("browser_specific_settings", {}).get("gecko", {}).get("id")
                or manifest.get("applications", {}).get("gecko", {}).get("id")
            )
            if extension_id != "uBlock0@raymondhill.net":
                raise RuntimeError("The downloaded extension is not uBlock Origin")
            os.replace(temporary, extension)
        except (OSError, urllib.error.URLError, zipfile.BadZipFile, KeyError, json.JSONDecodeError, RuntimeError) as error:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Could not install uBlock Origin from Mozilla Add-ons: {error}") from error


def build_browser_command(
    spec: BrowserSpec,
    executable: Path,
    profile_dir: Path,
    url: str,
    monitor: Monitor | None = None,
    system: str | None = None,
) -> list[str]:
    command = native.build_browser_command(spec, executable, profile_dir, url, monitor, system)
    if spec.key == "chromium":
        command.insert(-2, f"--load-extension={ublock_lite_dir(profile_dir)}")
    return command


class BrowserPlayer:
    def __init__(self, config: Config) -> None:
        self.spec = browser_spec(config.browser_type)
        self.executable = Path(config.browser_path).expanduser()
        self.profile_dir = browser_profile_dir(config.browser_type, self.executable)
        self.monitor_product = config.target_monitor_product
        self.monitor_connector = config.target_monitor_connector
        self.process: subprocess.Popen[bytes] | None = None
        self.window_id: int | None = None

    def _require_runtime(self) -> None:
        if not self.executable.is_file() or (platform.system() != "Windows" and not os.access(self.executable, os.X_OK)):
            raise RuntimeError(f"{self.spec.label} executable not found: {self.executable}")
        if platform.system() == "Linux":
            if not ctypes.util.find_library("X11"):
                raise RuntimeError("libX11 is required for monitor placement")
            if not os.environ.get("DISPLAY"):
                raise RuntimeError("DISPLAY is not set; start the service from the graphical user session")

    @staticmethod
    def _valid_saved_process(pid: int, profile_dir: Path, executable: Path | None = None) -> bool:
        return native.valid_saved_process(pid, profile_dir, executable)

    def _saved_player(self) -> dict[str, Any] | None:
        for state_file in (PLAYER_PID_FILE, LEGACY_PLAYER_PID_FILE):
            try:
                saved = json.loads(state_file.read_text(encoding="utf-8"))
                candidate_pid = int(saved["pid"])
                default_profile = PROFILE_DIR if state_file == LEGACY_PLAYER_PID_FILE else self.profile_dir
                saved_profile = Path(str(saved.get("profile_dir") or default_profile))
                saved_executable = Path(str(saved["executable"])) if saved.get("executable") else None
            except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if self._valid_saved_process(candidate_pid, saved_profile, saved_executable):
                saved["pid"] = candidate_pid
                saved["window_id"] = int(saved.get("window_id") or 0)
                return saved
        return None

    def is_running(self, url: str | None = None) -> bool:
        saved = self._saved_player()
        return bool(saved and (url is None or saved.get("url") == url))

    def stop(self) -> bool:
        stopped = False
        saved = self._saved_player()
        pid = int(saved["pid"]) if saved else 0
        window_id = int(saved["window_id"]) if saved else 0

        if platform.system() == "Linux" and window_id and pid and os.environ.get("DISPLAY"):
            try:
                with X11WindowManager() as window_manager:
                    window_manager.close_window(window_id)
                stopped = True
                time.sleep(0.5)
            except RuntimeError as error:
                LOG.warning("Could not close the old player window cleanly: %s", error)

        if pid:
            native.terminate_process(pid)
            stopped = True

        PLAYER_PID_FILE.unlink(missing_ok=True)
        LEGACY_PLAYER_PID_FILE.unlink(missing_ok=True)
        self.process = None
        self.window_id = None
        return stopped

    def _find_window(self, pid: int, window_manager: X11WindowManager, timeout: float = 20) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            window = window_manager.find_window_for_process_group(pid)
            if window:
                return window
            if self.process and self.process.poll() is not None:
                raise RuntimeError(
                    f"{self.spec.label} exited before creating a window (exit code {self.process.returncode})"
                )
            time.sleep(0.25)
        raise RuntimeError(f"Timed out waiting for the {self.spec.label} player window")

    def play(self, url: str) -> Monitor:
        self._require_runtime()
        monitor = find_monitor(self.monitor_product, connector=self.monitor_connector)
        self.stop()
        prepare_player_integration(self.spec, self.executable, self.profile_dir)

        environment = os.environ.copy()
        if self.spec.family == "firefox" and platform.system() == "Linux":
            environment["MOZ_ENABLE_WAYLAND"] = "0"  # XWayland permits deterministic window placement.
        command = build_browser_command(self.spec, self.executable, self.profile_dir, url, monitor)
        self.process = native.launch_process(command, environment)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        write_json_secure(
            PLAYER_PID_FILE,
            {
                "pid": self.process.pid,
                "window_id": 0,
                "profile_dir": str(self.profile_dir),
                "executable": str(self.executable),
                "url": url,
            },
        )

        try:
            if platform.system() == "Linux":
                with X11WindowManager() as window_manager:
                    self.window_id = self._find_window(self.process.pid, window_manager)
                    window_manager.place_fullscreen(self.window_id, monitor)
            else:
                self.window_id = native.place_browser_window(self.process.pid, monitor, self.spec)
        except Exception:
            self.stop()
            raise

        write_json_secure(
            PLAYER_PID_FILE,
            {
                "pid": self.process.pid,
                "window_id": self.window_id,
                "profile_dir": str(self.profile_dir),
                "executable": str(self.executable),
                "url": url,
            },
        )
        return monitor


def write_json_secure(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
        output.write("\n")
    os.replace(temporary, path)
    path.chmod(0o600)


def load_offset() -> int | None:
    try:
        return int(json.loads(STATE_FILE.read_text(encoding="utf-8"))["offset"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_offset(offset: int) -> None:
    write_json_secure(STATE_FILE, {"offset": offset})


def message_from_update(update: dict[str, Any]) -> dict[str, Any] | None:
    return update.get("message") or update.get("edited_message") or update.get("channel_post")


def authorized(config: Config, message: dict[str, Any]) -> bool:
    try:
        return int(message["chat"]["id"]) == config.allowed_chat_id and int(message["from"]["id"]) == config.allowed_user_id
    except (KeyError, TypeError, ValueError):
        return False


def queue_item_text(item: dict[str, Any]) -> str:
    title = str(item.get("title") or item.get("source_url") or "YouTube item")
    source_url = str(item.get("source_url") or "")
    return title if title == source_url or not source_url else f"{title} ({source_url})"


def queue_status_text(queue: PlaybackQueue) -> str:
    state = queue.snapshot()
    current = state["current"]
    pending = state["pending"]
    if not current:
        return "Queue is empty."
    lines = [f"Now: {queue_item_text(current)}"]
    lines.extend(f"{index}. {queue_item_text(item)}" for index, item in enumerate(pending[:10], 1))
    if len(pending) > 10:
        lines.append(f"...and {len(pending) - 10} more.")
    return "\n".join(lines)


def ensure_queue_player(player: BrowserPlayer, playback_server: LocalPlaybackServer) -> Monitor:
    page_url = playback_server.page_url()
    if player.is_running(page_url):
        return find_monitor(player.monitor_product, connector=player.monitor_connector)
    return player.play(page_url)


def run_bot() -> None:
    config = Config.load()
    api = TelegramAPI(config.bot_token)
    player = BrowserPlayer(config)
    link_finder = LinkFinder(config)
    link_finder.check()
    queue = PlaybackQueue()
    playback_server = LocalPlaybackServer(queue)
    offset = load_offset()
    retry_delay = 1
    bot = api.get_me()
    LOG.info(
        "Listening as @%s for chat %s; local player at %s",
        bot.get("username", "unknown"),
        config.allowed_chat_id,
        playback_server.origin,
    )
    if queue.snapshot()["current"]:
        ensure_queue_player(player, playback_server)

    while True:
        try:
            updates = api.get_updates(offset)
            retry_delay = 1
        except RuntimeError as error:
            LOG.error("%s; retrying in %s seconds", error, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue

        for update in updates:
            try:
                offset = int(update["update_id"]) + 1
                message = message_from_update(update)
                if not message or not authorized(config, message):
                    continue

                chat_id = int(message["chat"]["id"])
                text = message_text(message)
                command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""

                if command in {"/start", "/help"}:
                    api.send_message(
                        chat_id,
                        f"Send a YouTube link or describe a page to open on {config.target_monitor_product}. "
                        "Commands: /queue, /skip, /skipall, /clear, /status, /stop",
                    )
                    continue
                if command == "/stop":
                    removed = queue.clear()
                    stopped = player.stop()
                    api.send_message(chat_id, "Playback stopped and queue cleared." if removed or stopped else "Nothing is playing.")
                    continue
                if command == "/queue":
                    api.send_message(chat_id, queue_status_text(queue))
                    continue
                if command == "/skip":
                    previous, next_item = queue.advance()
                    if not previous:
                        api.send_message(chat_id, "Queue is empty.")
                    elif next_item:
                        ensure_queue_player(player, playback_server)
                        api.send_message(chat_id, f"Skipping to {queue_item_text(asdict(next_item))}.")
                    else:
                        api.send_message(chat_id, "Skipped. Queue is empty.")
                    continue
                if command == "/skipall":
                    previous, last_item, skipped = queue.skip_to_last()
                    if not previous:
                        api.send_message(chat_id, "Queue is empty.")
                    else:
                        ensure_queue_player(player, playback_server)
                        if skipped:
                            api.send_message(chat_id, f"Skipped {skipped} items to {queue_item_text(asdict(last_item))}.")
                        else:
                            api.send_message(chat_id, "Already playing the last queue item.")
                    continue
                if command == "/clear":
                    removed = queue.clear_pending()
                    suffix = "s" if removed != 1 else ""
                    api.send_message(chat_id, f"Removed {removed} queued item{suffix}.")
                    continue
                if command == "/status":
                    monitor = find_monitor(
                        config.target_monitor_product,
                        connector=config.target_monitor_connector,
                    )
                    spec = browser_spec(config.browser_type)
                    state = queue.snapshot()
                    queue_size = len(state["pending"]) + int(state["current"] is not None)
                    api.send_message(
                        chat_id,
                        f"Ready. Browser: {spec.label}. Target: {monitor.product} on "
                        f"{monitor.connector} ({monitor.width}x{monitor.height}). Queue: {queue_size}.",
                    )
                    continue
                if command:
                    api.send_message(chat_id, "Unknown command. Use /help for available commands.")
                    continue

                links = youtube_links(message)
                if not links:
                    if message_links(message):
                        api.send_message(
                            chat_id,
                            "Only YouTube links can be sent directly. Send a description to search for another page.",
                        )
                        continue
                    if not text:
                        continue
                    api.send_message(chat_id, "Searching for the best matching page...")
                    result = link_finder.find(text)
                    youtube_result = normalize_youtube_url(result.url)
                    if youtube_result:
                        item = youtube_queue_item(youtube_result, result.title)
                        position = queue.enqueue([item])[0]
                        monitor = ensure_queue_player(player, playback_server)
                        action = "Playing" if position == 0 else f"Queued at position {position}"
                        api.send_message(chat_id, f"{action}: {result.title} on {monitor.product}.")
                    else:
                        queue.clear()
                        monitor = player.play(result.url)
                        api.send_message(chat_id, f"Opening {result.title} on {monitor.product}.")
                    continue

                items = [youtube_queue_item(link) for link in links]
                positions = queue.enqueue(items)
                monitor = ensure_queue_player(player, playback_server)
                if positions[0] == 0:
                    queued = len(items) - 1
                    suffix = f" {queued} more queued." if queued else ""
                    api.send_message(chat_id, f"Playing on {monitor.product}.{suffix}")
                else:
                    suffix = "s" if len(items) != 1 else ""
                    api.send_message(
                        chat_id,
                        f"Added {len(items)} item{suffix} to the queue from position {positions[0]}.",
                    )
            except Exception as error:
                LOG.exception("Could not handle Telegram update")
                if message_from_update(update) and authorized(config, message_from_update(update) or {}):
                    try:
                        api.send_message(config.allowed_chat_id, f"Could not start playback: {error}")
                    except RuntimeError:
                        LOG.exception("Could not send the failure response")
            finally:
                if offset is not None:
                    save_offset(offset)


def prompt_yes_no(prompt: str, default: bool) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def configure_telegram(current: Config | None) -> tuple[str, int, int]:
    if current and prompt_yes_no("Keep the existing Telegram bot and authorized chat?", True):
        bot = TelegramAPI(current.bot_token).get_me()
        print(f"Using @{bot.get('username', 'unknown')} with the existing authorization.")
        return current.bot_token, current.allowed_chat_id, current.allowed_user_id

    print("Create a Telegram bot with @BotFather using /newbot, then enter its token.")
    token = getpass.getpass("Bot token (hidden): ").strip()
    if not token:
        raise RuntimeError("A bot token is required")

    api = TelegramAPI(token)
    bot = api.get_me()
    print(f"Connected to @{bot.get('username', 'unknown')}.")
    input("Open that bot in Telegram, send it /start, then press Enter here... ")
    print("Waiting up to 60 seconds for the authorization message...")

    deadline = time.monotonic() + 60
    selected: dict[str, Any] | None = None
    while time.monotonic() < deadline and selected is None:
        poll_timeout = min(15, max(1, round(deadline - time.monotonic())))
        for update in api.get_updates(None, timeout=poll_timeout):
            message = message_from_update(update)
            if message and message.get("from") and message.get("chat"):
                selected = message
        if selected is None:
            print("Still waiting...")
    if selected is None:
        raise RuntimeError("No message arrived. Send /start to the bot and run configure again.")

    sender = selected["from"]
    chat = selected["chat"]
    sender_name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
    sender_name = sender_name or sender.get("username", "unknown")
    chat_name = chat.get("title") or chat.get("username") or sender_name
    if not prompt_yes_no(f"Authorize {sender_name!r} in chat {chat_name!r}?", False):
        raise RuntimeError("Configuration cancelled")
    return token, int(chat["id"]), int(sender["id"])


def configure_monitor(current: Config | None) -> Monitor:
    monitors = configured_monitors()
    default_index = next((index for index, monitor in enumerate(monitors) if monitor.primary), 0)
    if current:
        default_index = next(
            (
                index
                for index, monitor in enumerate(monitors)
                if monitor.product == current.target_monitor_product
                and (not current.target_monitor_connector or monitor.connector == current.target_monitor_connector)
            ),
            default_index,
        )

    print("Available monitors:")
    for index, monitor in enumerate(monitors, start=1):
        primary = ", primary" if monitor.primary else ""
        print(
            f"  {index}. {monitor.product} on {monitor.connector} "
            f"({monitor.width}x{monitor.height} at {monitor.x},{monitor.y}{primary})"
        )
    while True:
        answer = input(f"Target monitor [{default_index + 1}]: ").strip()
        if not answer:
            return monitors[default_index]
        try:
            return monitors[int(answer) - 1]
        except (ValueError, IndexError):
            print(f"Enter a number from 1 to {len(monitors)}.")


def browser_candidates(spec: BrowserSpec) -> list[Path]:
    return native.browser_candidates(spec)


def configure_browser(current: Config | None) -> tuple[BrowserSpec, Path]:
    default_index = 0
    if current:
        default_index = next(
            (index for index, spec in enumerate(BROWSER_SPECS) if spec.key == current.browser_type),
            0,
        )
    else:
        detected_default = next((browser for browser in native.detected_browsers() if browser.default), None)
        if detected_default:
            default_index = next(
                index for index, spec in enumerate(BROWSER_SPECS) if spec.key == detected_default.spec.key
            )

    print("Supported browsers:")
    for index, spec in enumerate(BROWSER_SPECS, start=1):
        detected = browser_candidates(spec)
        suffix = f" ({detected[0]})" if detected else " (not detected)"
        print(f"  {index}. {spec.label}{suffix}; blocking: {spec.blocking}")
    while True:
        answer = input(f"Playback browser [{default_index + 1}]: ").strip()
        if not answer:
            spec = BROWSER_SPECS[default_index]
            break
        try:
            spec = BROWSER_SPECS[int(answer) - 1]
            break
        except (ValueError, IndexError):
            print(f"Enter a number from 1 to {len(BROWSER_SPECS)}.")

    candidates = browser_candidates(spec)
    current_path = Path(current.browser_path).expanduser() if current and current.browser_type == spec.key else None
    default = current_path if current_path and current_path.is_file() else (candidates[0] if candidates else None)

    if candidates:
        print(f"Detected {spec.label} executables:")
        for path in candidates:
            print(f"  {path}")
    prompt = f"{spec.label} executable [{default}]: " if default else f"{spec.label} executable: "
    answer = input(prompt).strip()
    path = Path(answer).expanduser() if answer else default
    if path is None or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"{spec.label} executable not found: {path}")
    return spec, path


def configure_link_search(current: Config | None) -> tuple[str, str, str]:
    print("Plain-text requests use Ollama web search to find a page to open.")
    if current and current.ollama_api_key and prompt_yes_no("Keep the existing Ollama API key?", True):
        api_key = current.ollama_api_key
    else:
        api_key = getpass.getpass("Ollama web-search API key (hidden): ").strip()
    if not api_key:
        raise RuntimeError("An Ollama web-search API key is required")

    default_model = current.ollama_model if current and current.ollama_model else DEFAULT_OLLAMA_MODEL
    model = validate_ollama_model_name(input(f"Local Ollama model [{default_model}]: ").strip() or default_model)

    current_value = current.find_link_script if current and current.find_link_script else ""
    current_script = Path(current_value).expanduser() if current_value not in {"", "bundled"} else None
    default_script = str(current_script) if current_script and current_script.is_file() else DEFAULT_FIND_LINK_SCRIPT
    answer = input(f"Link finder script [{default_script}]: ").strip()
    value = answer or default_script
    if value != "bundled":
        script = Path(value).expanduser().resolve()
        if not script.is_file():
            raise RuntimeError(f"Link finder script not found: {script}")
        value = str(script)
    return api_key, model, value


def configure() -> None:
    try:
        current = Config.load()
    except RuntimeError:
        current = None

    token, chat_id, user_id = configure_telegram(current)
    monitor = configure_monitor(current)
    spec, browser = configure_browser(current)
    ollama_api_key, ollama_model, find_link_script = configure_link_search(current)
    config = Config(
        bot_token=token,
        allowed_chat_id=chat_id,
        allowed_user_id=user_id,
        target_monitor_product=monitor.product,
        target_monitor_connector=monitor.connector,
        browser_type=spec.key,
        browser_path=str(browser),
        ollama_api_key=ollama_api_key,
        ollama_model=ollama_model,
        find_link_script=find_link_script,
    )
    LinkFinder(config).check(pull_if_missing=True)
    write_json_secure(CONFIG_FILE, asdict(config))
    prepare_player_integration(spec, browser, browser_profile_dir(spec.key, browser))
    print(f"Configuration saved securely to {CONFIG_FILE}")
    print(f"Next run: {PROJECT_DIR / 'player.py'} install")


def service_contents() -> str:
    command = native.installed_runtime_command(Path(__file__))
    return native.linux_service_contents(command, os.environ.get("DISPLAY", ":0"))


def install_service() -> None:
    config = Config.load()
    spec = browser_spec(config.browser_type)
    browser = Path(config.browser_path).expanduser()
    LinkFinder(config).check(pull_if_missing=True)
    prepare_player_integration(spec, browser, browser_profile_dir(spec.key, browser))
    missing = []
    if not browser.is_file() or (platform.system() != "Windows" and not os.access(browser, os.X_OK)):
        missing.append(f"{spec.label} ({browser})")
    if platform.system() == "Linux" and not ctypes.util.find_library("X11"):
        missing.append("libX11")
    if missing:
        raise RuntimeError("Missing runtime dependency: " + ", ".join(missing))

    command = native.installed_runtime_command(Path(__file__))
    startup_file = native.install_autostart(command, SERVICE_FILE)
    print(f"Installed and started {APP_NAME}: {startup_file}")


def check_configuration() -> None:
    config = Config.load()
    monitor = find_monitor(
        config.target_monitor_product,
        connector=config.target_monitor_connector,
    )
    spec = browser_spec(config.browser_type)
    browser = Path(config.browser_path).expanduser()
    if not browser.is_file() or (platform.system() != "Windows" and not os.access(browser, os.X_OK)):
        raise RuntimeError(f"{spec.label} executable not found: {browser}")
    if platform.system() == "Linux" and not ctypes.util.find_library("X11"):
        raise RuntimeError("libX11 is not installed")
    link_finder = LinkFinder(config)
    link_finder.check()
    bot = TelegramAPI(config.bot_token).get_me()
    print(f"Bot: @{bot.get('username', 'unknown')}")
    print(f"Authorized chat/user: {config.allowed_chat_id}/{config.allowed_user_id}")
    print(f"Monitor: {monitor.product} on {monitor.connector} at {monitor.x},{monitor.y} ({monitor.width}x{monitor.height})")
    print(f"Browser: {spec.label} ({browser})")
    print(f"Content blocking: {spec.blocking}")
    print(f"Link search: {link_finder.model} via {link_finder.script}")
    print("Configuration check passed.")


def queue_smoke_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        queue_path = Path(directory) / "queue.json"
        queue = PlaybackQueue(queue_path)
        first = youtube_queue_item("https://youtu.be/first123?t=12")
        second = youtube_queue_item("https://www.youtube.com/watch?v=second456")
        if queue.enqueue([first, second]) != [0, 1]:
            raise RuntimeError("Queue order test failed")
        server = LocalPlaybackServer(queue)
        try:
            with urllib.request.urlopen(server.page_url(), timeout=5) as response:
                page = response.read().decode("utf-8")
            if "youtube.com/iframe_api" not in page:
                raise RuntimeError("Player API test failed")

            headers = {"X-Player-Token": server.control_token}
            request = urllib.request.Request(f"{server.origin}/api/queue", headers=headers)
            with urllib.request.urlopen(request, timeout=5) as response:
                state = json.load(response)
            if state["current"]["media_id"] != "first123" or len(state["pending"]) != 1:
                raise RuntimeError("Queue state test failed")

            body = json.dumps({"token": first.token}).encode("utf-8")
            request = urllib.request.Request(
                f"{server.origin}/api/advance",
                data=body,
                method="POST",
                headers={**headers, "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                state = json.load(response)
            if state["current"]["media_id"] != "second456":
                raise RuntimeError("Queue advance test failed")
        finally:
            server.close()

        restored = PlaybackQueue(queue_path).snapshot()
        if restored["current"]["media_id"] != "second456":
            raise RuntimeError("Queue persistence test failed")
        queue = PlaybackQueue(queue_path)
        queue.enqueue(
            [
                youtube_queue_item("https://youtu.be/third789"),
                youtube_queue_item("https://youtu.be/final012"),
            ]
        )
        _, last_item, skipped = queue.skip_to_last()
        if skipped != 2 or last_item is None or last_item.media_id != "final012" or queue.snapshot()["pending"]:
            raise RuntimeError("Skip-all test failed")
        return {"queue": "ok", "player_api": "ok", "persistence": "ok", "skip_all": "ok"}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open Telegram links on a selected monitor.")
    parser.add_argument(
        "command",
        nargs="?",
        default="setup",
        choices=["run", "setup", "configure", "install", "check", "smoke-test", "queue-smoke-test"],
    )
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "run":
            run_bot()
        elif args.command == "setup":
            from setup_ui import launch

            launch()
        elif args.command == "configure":
            configure()
        elif args.command == "install":
            install_service()
        elif args.command == "check":
            check_configuration()
        elif args.command == "smoke-test":
            print(json.dumps(native.smoke_test(), indent=2))
        elif args.command == "queue-smoke-test":
            print(json.dumps(queue_smoke_test(), indent=2))
    except (RuntimeError, subprocess.CalledProcessError) as error:
        LOG.error("%s", error)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
