from __future__ import annotations

import ctypes
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


APP_NAME = "telegram-youtube-player"
APP_TITLE = "Telegram YouTube Player"


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    state_dir: Path
    service_file: Path


@dataclass(frozen=True)
class Monitor:
    product: str
    connector: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    @property
    def label(self) -> str:
        primary = ", primary" if self.primary else ""
        return (
            f"{self.product} on {self.connector} "
            f"({self.width}x{self.height} at {self.x},{self.y}{primary})"
        )


@dataclass(frozen=True)
class BrowserSpec:
    key: str
    label: str
    family: str
    commands: tuple[str, ...]
    common_paths: tuple[str, ...]
    window_class: str
    blocking: str


@dataclass(frozen=True)
class DetectedBrowser:
    spec: BrowserSpec
    path: Path
    default: bool = False

    @property
    def label(self) -> str:
        suffix = " (system default)" if self.default else ""
        return f"{self.spec.label}{suffix} - {self.path}"


def host_system(system: str | None = None) -> str:
    value = system or platform.system()
    if value not in {"Linux", "Windows", "Darwin"}:
        raise RuntimeError(f"Unsupported operating system: {value}")
    return value


def application_paths(
    system: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> AppPaths:
    system = host_system(system)
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    if system == "Windows":
        roaming = Path(environ.get("APPDATA", home / "AppData/Roaming"))
        local = Path(environ.get("LOCALAPPDATA", home / "AppData/Local"))
        config_dir = roaming / APP_NAME
        state_dir = local / APP_NAME
        service_file = roaming / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{APP_NAME}.cmd"
    elif system == "Darwin":
        config_dir = home / "Library/Application Support" / APP_NAME
        state_dir = home / "Library/Caches" / APP_NAME
        service_file = home / "Library/LaunchAgents" / f"com.herrei.{APP_NAME}.plist"
    else:
        config_root = Path(environ.get("XDG_CONFIG_HOME", home / ".config"))
        state_root = Path(environ.get("XDG_STATE_HOME", home / ".local/state"))
        config_dir = config_root / APP_NAME
        state_dir = state_root / APP_NAME
        service_file = config_root / "systemd/user" / f"{APP_NAME}.service"
    return AppPaths(config_dir, state_dir, service_file)


def browser_specs(
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[BrowserSpec, ...]:
    system = host_system(system)
    environ = os.environ if environ is None else environ
    if system == "Windows":
        program_files = Path(environ.get("PROGRAMFILES", "C:/Program Files"))
        program_files_x86 = Path(environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        local = Path(environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        paths = {
            "firefox": (program_files / "Mozilla Firefox/firefox.exe", program_files_x86 / "Mozilla Firefox/firefox.exe"),
            "chromium": (local / "Chromium/Application/chrome.exe", program_files / "Chromium/Application/chrome.exe"),
            "chrome": (program_files / "Google/Chrome/Application/chrome.exe", local / "Google/Chrome/Application/chrome.exe"),
            "brave": (program_files / "BraveSoftware/Brave-Browser/Application/brave.exe", local / "BraveSoftware/Brave-Browser/Application/brave.exe"),
        }
    elif system == "Darwin":
        paths = {
            "firefox": (Path("/Applications/Firefox.app/Contents/MacOS/firefox"), Path.home() / "Applications/Firefox.app/Contents/MacOS/firefox"),
            "chromium": (Path("/Applications/Chromium.app/Contents/MacOS/Chromium"), Path.home() / "Applications/Chromium.app/Contents/MacOS/Chromium"),
            "chrome": (Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "brave": (Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"), Path.home() / "Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        }
    else:
        default_firefox = Path(environ.get("FIREFOX", shutil.which("firefox") or "/usr/bin/firefox"))
        paths = {
            "firefox": (default_firefox, Path("/usr/bin/firefox"), Path("/usr/bin/firefox-esr"), Path("/snap/bin/firefox")),
            "chromium": (Path("/usr/bin/chromium"), Path("/usr/bin/chromium-browser"), Path("/snap/bin/chromium")),
            "chrome": (Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable")),
            "brave": (Path("/usr/bin/brave-browser"), Path("/snap/bin/brave")),
        }
    return (
        BrowserSpec("firefox", "Firefox", "firefox", ("firefox", "firefox-esr"), tuple(map(str, paths["firefox"])), "firefox", "uBlock Origin"),
        BrowserSpec("chromium", "Chromium", "chromium", ("chromium", "chromium-browser"), tuple(map(str, paths["chromium"])), "chromium", "uBlock Origin Lite"),
        BrowserSpec("chrome", "Google Chrome", "chromium", ("google-chrome", "google-chrome-stable", "chrome"), tuple(map(str, paths["chrome"])), "google-chrome", "Browser content controls"),
        BrowserSpec("brave", "Brave", "chromium", ("brave-browser", "brave"), tuple(map(str, paths["brave"])), "brave-browser", "Brave Shields"),
    )


def browser_spec(key: str, specs: Sequence[BrowserSpec] | None = None) -> BrowserSpec:
    specs = browser_specs() if specs is None else specs
    for spec in specs:
        if spec.key == key:
            return spec
    supported = ", ".join(spec.key for spec in specs)
    raise RuntimeError(f"Unsupported browser type {key!r}. Choose one of: {supported}")


def _executable(path: Path, system: str) -> bool:
    return path.is_file() and (system == "Windows" or os.access(path, os.X_OK))


def browser_candidates(spec: BrowserSpec, system: str | None = None) -> list[Path]:
    system = host_system(system)
    candidates: list[Path] = []
    raw_paths: list[str | Path | None] = []
    if spec.key == "firefox":
        raw_paths.append(os.environ.get("FIREFOX"))
    raw_paths.extend(shutil.which(command) for command in spec.commands)
    raw_paths.extend(spec.common_paths)
    for raw_path in raw_paths:
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if _executable(path, system) and path not in candidates:
            candidates.append(path)
    return candidates


def browser_key_from_identifier(identifier: str) -> str | None:
    value = identifier.casefold()
    if "firefox" in value:
        return "firefox"
    if "brave" in value:
        return "brave"
    if "chromium" in value:
        return "chromium"
    if "chrome" in value or "google" in value:
        return "chrome"
    return None


def _linux_desktop_executable(desktop_name: str) -> Path | None:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    data_dirs = [Path(item) for item in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")]
    for root in [data_home, *data_dirs]:
        desktop_file = root / "applications" / desktop_name.strip()
        try:
            lines = desktop_file.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            continue
        exec_line = next((line.removeprefix("Exec=") for line in lines if line.startswith("Exec=")), "")
        try:
            executable = shlex.split(exec_line)[0]
        except (ValueError, IndexError):
            continue
        path = Path(executable).expanduser()
        if not path.is_absolute() and (resolved := shutil.which(executable)):
            path = Path(resolved)
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _macos_app_executable(app: Path, key: str | None) -> Path | None:
    names = {
        "firefox": "firefox",
        "chromium": "Chromium",
        "chrome": "Google Chrome",
        "brave": "Brave Browser",
    }
    if app.is_file():
        return app
    name = names.get(key or "")
    executable = app / "Contents/MacOS" / name if name else None
    return executable if executable and executable.is_file() else None


def default_browser_info(system: str | None = None) -> tuple[str | None, Path | None]:
    system = host_system(system)
    try:
        if system == "Linux":
            result = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return browser_key_from_identifier(result.stdout), _linux_desktop_executable(result.stdout)
        if system == "Windows":
            import winreg

            key_path = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value = str(winreg.QueryValueEx(key, "ProgId")[0])
            return browser_key_from_identifier(value), None
        script = """
ObjC.import('AppKit')
const url = $.NSURL.URLWithString('https://example.com')
const app = $.NSWorkspace.sharedWorkspace.URLForApplicationToOpenURL(url)
ObjC.unwrap(app.path)
"""
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        app = Path((result.stdout or result.stderr).strip())
        key = browser_key_from_identifier(str(app))
        return key, _macos_app_executable(app, key)
    except (FileNotFoundError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None, None


def default_browser_key(system: str | None = None) -> str | None:
    return default_browser_info(system)[0]


def detected_browsers(system: str | None = None) -> list[DetectedBrowser]:
    system = host_system(system)
    default_key, default_path = default_browser_info(system)
    found: list[DetectedBrowser] = []
    for spec in browser_specs(system):
        candidates = browser_candidates(spec, system)
        if spec.key == default_key and default_path and default_path not in candidates:
            candidates.insert(0, default_path)
        for path in candidates:
            found.append(DetectedBrowser(spec, path, spec.key == default_key))
    found.sort(key=lambda browser: (not browser.default, [spec.key for spec in browser_specs(system)].index(browser.spec.key)))
    return found


def _gnome_monitors(monitors_file: Path) -> list[Monitor]:
    try:
        root = ET.parse(monitors_file).getroot()
    except (FileNotFoundError, ET.ParseError) as error:
        raise RuntimeError(f"Cannot read GNOME monitor configuration {monitors_file}: {error}") from error
    for configuration in root.findall("configuration"):
        monitors: list[Monitor] = []
        for logical in configuration.findall("logicalmonitor"):
            for monitor_node in logical.findall("monitor"):
                spec = monitor_node.find("monitorspec")
                mode = monitor_node.find("mode")
                if spec is None or mode is None:
                    continue
                scale = float(logical.findtext("scale", "1"))
                width = round(int(mode.findtext("width", "0")) / scale)
                height = round(int(mode.findtext("height", "0")) / scale)
                if logical.findtext("transform/rotation", "normal") in {"left", "right"}:
                    width, height = height, width
                monitors.append(
                    Monitor(
                        product=spec.findtext("product", "unknown"),
                        connector=spec.findtext("connector", "unknown"),
                        x=int(logical.findtext("x", "0")),
                        y=int(logical.findtext("y", "0")),
                        width=width,
                        height=height,
                        primary=logical.findtext("primary", "no") == "yes",
                    )
                )
        if monitors:
            return monitors
    raise RuntimeError(f"No active monitors were found in {monitors_file}")


def _xrandr_monitors(output: str) -> list[Monitor]:
    monitors = []
    pattern = re.compile(r"^(\S+) connected (primary )?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", re.MULTILINE)
    for match in pattern.finditer(output):
        connector, primary, width, height, x, y = match.groups()
        monitors.append(Monitor(connector, connector, int(x), int(y), int(width), int(height), bool(primary)))
    return monitors


def _windows_monitors() -> list[Monitor]:
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), callback_type, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    monitors: list[Monitor] = []

    def collect(handle: int, _dc: int, _rect: object, _data: int) -> bool:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.rcMonitor
            device = str(info.szDevice)
            monitors.append(
                Monitor(
                    product=device.replace("\\\\.\\", "") or f"Display {len(monitors) + 1}",
                    connector=device or str(len(monitors) + 1),
                    x=rect.left,
                    y=rect.top,
                    width=rect.right - rect.left,
                    height=rect.bottom - rect.top,
                    primary=bool(info.dwFlags & 1),
                )
            )
        return True

    callback = callback_type(collect)
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise RuntimeError("Windows could not enumerate displays")
    return monitors


def _macos_monitors(output: str) -> list[Monitor]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("macOS returned invalid display information") from error
    return [
        Monitor(
            product=str(item.get("product") or f"Display {index + 1}"),
            connector=str(item.get("connector") or index + 1),
            x=round(float(item["x"])),
            y=round(float(item["y"])),
            width=round(float(item["width"])),
            height=round(float(item["height"])),
            primary=bool(item.get("primary")),
        )
        for index, item in enumerate(payload)
        if float(item.get("width", 0)) > 0 and float(item.get("height", 0)) > 0
    ]


def configured_monitors(system: str | None = None, monitors_file: Path | None = None) -> list[Monitor]:
    system = host_system(system)
    if system == "Windows":
        monitors = _windows_monitors()
    elif system == "Darwin":
        script = """
ObjC.import('AppKit')
const screens = $.NSScreen.screens
const count = Number(screens.count)
const values = []
let top = 0
for (let index = 0; index < count; index++) {
  const frame = screens.objectAtIndex(index).frame
  top = Math.max(top, frame.origin.y + frame.size.height)
}
for (let index = 0; index < count; index++) {
  const screen = screens.objectAtIndex(index)
  const frame = screen.frame
  values.push({
    product: ObjC.unwrap(screen.localizedName), connector: String(index + 1),
    x: frame.origin.x, y: top - frame.origin.y - frame.size.height,
    width: frame.size.width, height: frame.size.height, primary: index === 0
  })
}
const result = values
JSON.stringify(result)
"""
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        monitors = _macos_monitors((result.stdout or result.stderr).strip())
    else:
        file = monitors_file or Path.home() / ".config/monitors.xml"
        try:
            monitors = _gnome_monitors(file)
        except RuntimeError as original_error:
            try:
                result = subprocess.run(
                    ["xrandr", "--query"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                monitors = _xrandr_monitors(result.stdout)
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                raise original_error
    if not monitors:
        raise RuntimeError("No active monitors were detected")
    return monitors


def find_monitor(product: str, connector: str = "", **kwargs: object) -> Monitor:
    for monitor in configured_monitors(**kwargs):
        if monitor.product == product and (not connector or monitor.connector == connector):
            return monitor
    description = f"{product!r}" + (f" on {connector!r}" if connector else "")
    raise RuntimeError(f"Monitor {description} was not found")


def build_browser_command(
    spec: BrowserSpec,
    executable: Path,
    profile_dir: Path,
    url: str,
    monitor: Monitor | None = None,
    system: str | None = None,
) -> list[str]:
    system = host_system(system)
    if spec.family == "firefox":
        command = [str(executable), "--no-remote", "--new-instance", "--profile", str(profile_dir)]
        if system == "Linux":
            command.extend(["--class", spec.window_class, "--kiosk"])
        command.append(url)
        return command
    command = [
        str(executable),
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--autoplay-policy=no-user-gesture-required",
    ]
    if system == "Linux":
        command.extend(["--ozone-platform=x11", f"--class={spec.window_class}"])
    if monitor:
        command.extend(
            [
                f"--window-position={monitor.x},{monitor.y}",
                f"--window-size={monitor.width},{monitor.height}",
            ]
        )
    command.extend(["--kiosk", url])
    return command


def _windows_process_ids(root_pid: int) -> set[int]:
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return {root_pid}
    parents: dict[int, int] = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    result = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in result and pid not in result:
                result.add(pid)
                changed = True
    return result


def _windows_find_window(root_pid: int, timeout: float = 20) -> int:
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids = _windows_process_ids(root_pid)
        windows: list[int] = []

        def collect(hwnd: int, _data: int) -> bool:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) in pids and user32.IsWindowVisible(hwnd):
                windows.append(int(hwnd))
            return True

        callback = callback_type(collect)
        user32.EnumWindows(callback, 0)
        if windows:
            return windows[0]
        time.sleep(0.25)
    raise RuntimeError("Timed out waiting for the browser window")


def place_browser_window(root_pid: int, monitor: Monitor, spec: BrowserSpec, system: str | None = None) -> int:
    system = host_system(system)
    if system == "Windows":
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
        window = _windows_find_window(root_pid)
        user32.ShowWindow(window, 9)
        user32.MoveWindow(window, monitor.x, monitor.y, monitor.width, monitor.height, True)
        user32.SetForegroundWindow(window)
        if spec.family == "firefox":
            time.sleep(0.3)
            user32.keybd_event(0x7A, 0, 0, 0)
            user32.keybd_event(0x7A, 0, 2, 0)
        return window
    if system == "Darwin":
        if spec.family != "firefox":
            return 0
        script = """
on run argv
  set targetPid to item 1 of argv as integer
  set px to item 2 of argv as integer
  set py to item 3 of argv as integer
  set pw to item 4 of argv as integer
  set ph to item 5 of argv as integer
  tell application "System Events"
    repeat 80 times
      set matches to every application process whose unix id is targetPid
      if (count matches) > 0 then
        tell item 1 of matches
          if (count windows) > 0 then
            set frontmost to true
            set position of front window to {px, py}
            set size of front window to {pw, ph}
            delay 0.3
            keystroke "f" using {control down, command down}
            return
          end if
        end tell
      end if
      delay 0.25
    end repeat
    error "Timed out waiting for the Firefox window"
  end tell
end run
"""
        subprocess.run(
            ["osascript", "-e", script, str(root_pid), str(monitor.x), str(monitor.y), str(monitor.width), str(monitor.height)],
            check=True,
            capture_output=True,
            text=True,
            timeout=25,
        )
        return 0
    raise RuntimeError("Linux window placement is handled by X11")


def valid_saved_process(pid: int, profile_dir: Path, executable: Path | None = None, system: str | None = None) -> bool:
    system = host_system(system)
    if system == "Windows":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        output = result.stdout or ""
        return str(pid) in output and (executable is None or executable.name.casefold() in output.casefold())
    if system == "Linux":
        try:
            command = (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (FileNotFoundError, ProcessLookupError, PermissionError, UnicodeDecodeError):
            return False
    else:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            command = result.stdout or ""
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
    return str(profile_dir) in command


def terminate_process(pid: int, system: str | None = None) -> None:
    system = host_system(system)
    try:
        if system == "Windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(pid), 15)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        return


def launch_process(command: Sequence[str], environment: Mapping[str, str]) -> subprocess.Popen[bytes]:
    options: dict[str, object] = {
        "env": dict(environment),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if host_system() == "Windows":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(list(command), **options)


def _quoted_systemd(command: Sequence[str]) -> str:
    return " ".join('"' + item.replace("\\", "\\\\").replace('"', '\\"') + '"' for item in command)


def linux_service_contents(command: Sequence[str], display: str = ":0") -> str:
    if not re.fullmatch(r":[0-9]+(?:\.[0-9]+)?", display):
        display = ":0"
    return f"""[Unit]
Description=Telegram YouTube player
After=network-online.target graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={_quoted_systemd(command)}
Restart=on-failure
RestartSec=5
Environment=DISPLAY={display}
Environment=MOZ_ENABLE_WAYLAND=0
UMask=0077

[Install]
WantedBy=graphical-session.target
"""


def macos_launch_agent(command: Sequence[str]) -> dict[str, object]:
    return {
        "Label": f"com.herrei.{APP_NAME}",
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "StandardOutPath": str(application_paths("Darwin").state_dir / "player.log"),
        "StandardErrorPath": str(application_paths("Darwin").state_dir / "player.log"),
    }


def windows_startup_contents(command: Sequence[str]) -> str:
    return "@echo off\r\nstart \"\" /min " + subprocess.list2cmdline(list(command)) + "\r\n"


def autostart_installed(service_file: Path | None = None, system: str | None = None) -> bool:
    paths = application_paths(system)
    return (service_file or paths.service_file).is_file()


def install_autostart(command: Sequence[str], service_file: Path | None = None, system: str | None = None) -> Path:
    system = host_system(system)
    paths = application_paths(system)
    service_file = paths.service_file if service_file is None else service_file
    service_file.parent.mkdir(parents=True, exist_ok=True)
    if system == "Linux":
        service_file.write_text(linux_service_contents(command, os.environ.get("DISPLAY", ":0")), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", APP_NAME], check=True)
        subprocess.run(["systemctl", "--user", "restart", APP_NAME], check=True)
    elif system == "Windows":
        service_file.write_text(windows_startup_contents(command), encoding="utf-8", newline="")
        subprocess.Popen(
            list(command),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
        )
    else:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        with service_file.open("wb") as output:
            plistlib.dump(macos_launch_agent(command), output)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(service_file)], check=False, capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(service_file)], check=True)
    return service_file


def remove_autostart(service_file: Path | None = None, system: str | None = None) -> bool:
    system = host_system(system)
    paths = application_paths(system)
    service_file = paths.service_file if service_file is None else service_file
    existed = service_file.is_file()
    if system == "Linux" and existed:
        subprocess.run(["systemctl", "--user", "disable", "--now", service_file.name], check=False, capture_output=True)
        service_file.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, capture_output=True)
    elif system == "Darwin" and existed:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(service_file)], check=False, capture_output=True)
        service_file.unlink(missing_ok=True)
    else:
        service_file.unlink(missing_ok=True)
    return existed


def start_application(command: Sequence[str], system: str | None = None) -> subprocess.Popen[bytes]:
    system = host_system(system)
    options: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if system == "Windows":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return subprocess.Popen(list(command), **options)


def desktop_directory(
    system: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    system = host_system(system)
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    if system == "Windows":
        return Path(environ.get("USERPROFILE", home)) / "Desktop"
    if system == "Darwin":
        return home / "Desktop"
    configured = environ.get("XDG_DESKTOP_DIR", "").strip()
    if not configured:
        user_dirs = Path(environ.get("XDG_CONFIG_HOME", home / ".config")) / "user-dirs.dirs"
        try:
            match = re.search(r'^XDG_DESKTOP_DIR="([^"]+)"', user_dirs.read_text(encoding="utf-8"), re.MULTILINE)
            configured = match.group(1) if match else ""
        except OSError:
            configured = ""
    if configured:
        return Path(configured.replace("$HOME", str(home))).expanduser()
    return home / "Desktop"


def desktop_shortcut_paths(
    system: str | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    system = host_system(system)
    desktop = desktop_directory(system, home, environ)
    if system == "Windows":
        return (desktop / f"{APP_TITLE}.lnk",)
    if system == "Darwin":
        return (desktop / f"{APP_TITLE}.app", desktop / f"{APP_TITLE}.command")
    return (desktop / f"{APP_TITLE}.desktop",)


def desktop_shortcut_installed(system: str | None = None) -> bool:
    return any(path.is_file() or path.is_symlink() for path in desktop_shortcut_paths(system))


def remove_desktop_shortcut(system: str | None = None) -> bool:
    removed = False
    for path in desktop_shortcut_paths(system):
        if path.is_file() or path.is_symlink():
            path.unlink()
            removed = True
    return removed


def linux_desktop_shortcut_contents(command: Sequence[str]) -> str:
    executable = " ".join('"' + item.replace("\\", "\\\\").replace('"', '\\"') + '"' for item in command)
    return f"""[Desktop Entry]
Name={APP_TITLE}
Comment=Configure Telegram-controlled browser playback
Exec={executable}
Icon=video-display
Terminal=false
Type=Application
Categories=AudioVideo;Utility;
"""


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def windows_shortcut_script(shortcut: Path, command: Sequence[str]) -> str:
    target = str(command[0])
    arguments = subprocess.list2cmdline(list(command[1:]))
    return (
        "$shell = New-Object -ComObject WScript.Shell; "
        f"$shortcut = $shell.CreateShortcut({_powershell_literal(str(shortcut))}); "
        f"$shortcut.TargetPath = {_powershell_literal(target)}; "
        f"$shortcut.Arguments = {_powershell_literal(arguments)}; "
        f"$shortcut.WorkingDirectory = {_powershell_literal(str(Path(target).parent))}; "
        f"$shortcut.IconLocation = {_powershell_literal(target)}; "
        f"$shortcut.Description = {_powershell_literal(APP_TITLE)}; "
        "$shortcut.Save()"
    )


def install_desktop_shortcut(command: Sequence[str], system: str | None = None) -> Path:
    if not command:
        raise RuntimeError("Desktop shortcut command is empty")
    system = host_system(system)
    paths = desktop_shortcut_paths(system)
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    remove_desktop_shortcut(system)
    if system == "Linux":
        shortcut = paths[0]
        shortcut.write_text(linux_desktop_shortcut_contents(command), encoding="utf-8")
        shortcut.chmod(0o755)
        return shortcut
    if system == "Windows":
        shortcut = paths[0]
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", windows_shortcut_script(shortcut, command)],
            check=True,
            timeout=30,
        )
        return shortcut
    executable = Path(command[0])
    bundle = next((parent for parent in executable.parents if parent.suffix == ".app"), None)
    if bundle:
        shortcut = paths[0]
        shortcut.symlink_to(bundle)
    else:
        shortcut = paths[1]
        shortcut.write_text("#!/bin/sh\nexec " + shlex.join(list(command)) + "\n", encoding="utf-8")
        shortcut.chmod(0o755)
    return shortcut


def installed_runtime_command(source_script: Path, system: str | None = None) -> list[str]:
    system = host_system(system)
    if not getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve()), str(source_script.resolve()), "run"]
    executable = Path(sys.executable).resolve()
    if system == "Windows":
        target = application_paths(system).state_dir.parent / "Programs" / APP_TITLE / executable.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if executable != target:
            shutil.copy2(executable, target)
    elif system == "Linux":
        target = Path.home() / ".local/bin" / APP_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        if executable != target:
            shutil.copy2(executable, target)
            target.chmod(0o755)
    else:
        source_bundle = next((parent for parent in executable.parents if parent.suffix == ".app"), None)
        if source_bundle:
            target_bundle = Path.home() / "Applications" / source_bundle.name
            if source_bundle != target_bundle:
                shutil.copytree(source_bundle, target_bundle, dirs_exist_ok=True)
            target = target_bundle / executable.relative_to(source_bundle)
        else:
            target = Path.home() / "Applications" / APP_NAME
            target.parent.mkdir(parents=True, exist_ok=True)
            if executable != target:
                shutil.copy2(executable, target)
                target.chmod(0o755)
    return [str(target), "run"]


def smoke_test(system: str | None = None) -> dict[str, object]:
    system = host_system(system)
    specs = browser_specs(system)
    sample = Monitor("Display", "1", 100, 200, 1920, 1080, True)
    commands = {
        spec.key: build_browser_command(spec, Path(spec.common_paths[0]), Path("profile"), "about:blank", sample, system)
        for spec in specs
    }
    return {
        "system": system,
        "config_dir": str(application_paths(system).config_dir),
        "browsers": [spec.key for spec in specs],
        "commands": commands,
        "autostart": str(application_paths(system).service_file),
    }
