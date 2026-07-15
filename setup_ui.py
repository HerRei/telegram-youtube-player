from __future__ import annotations

import threading
import tkinter as tk
import webbrowser
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk
from typing import TypeVar, cast

import platform_support as native
import player


T = TypeVar("T")
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"
OLLAMA_WEB_SEARCH_GUIDE_URL = "https://docs.ollama.com/capabilities/web-search"
OLLAMA_KEYS_URL = "https://ollama.com/settings/keys"


def open_external_url(url: str) -> None:
    if not webbrowser.open(url, new=2):
        raise RuntimeError(f"Could not open {url}")


def apply_launch_options(
    command: list[str],
    start_at_login: bool,
    desktop_icon: bool,
    service_file: Path,
) -> tuple[Path | None, Path | None]:
    if desktop_icon:
        shortcut = native.install_desktop_shortcut(command[:-1])
    else:
        native.remove_desktop_shortcut()
        shortcut = None
    if start_at_login:
        startup_file = native.install_autostart(command, service_file)
    else:
        native.remove_autostart(service_file)
        native.start_application(command)
        startup_file = None
    return startup_file, shortcut


class SetupWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Telegram YouTube Player Setup")
        self.root.geometry("720x730")
        self.root.minsize(660, 680)
        self.current = self._current_config()
        self.monitors: list[native.Monitor] = []
        self.browsers: list[native.DetectedBrowser] = []
        self.monitor_by_label: dict[str, native.Monitor] = {}
        self.browser_by_label: dict[str, native.DetectedBrowser] = {}

        self.token = tk.StringVar(value=self.current.bot_token if self.current else "")
        self.chat_id = tk.StringVar(value=str(self.current.allowed_chat_id) if self.current else "")
        self.user_id = tk.StringVar(value=str(self.current.allowed_user_id) if self.current else "")
        self.monitor = tk.StringVar()
        self.browser = tk.StringVar()
        self.browser_path = tk.StringVar(value=self.current.browser_path if self.current else "")
        self.api_key = tk.StringVar(value=self.current.ollama_api_key if self.current else "")
        self.model = tk.StringVar(value=self.current.ollama_model if self.current else player.DEFAULT_OLLAMA_MODEL)
        self.start_at_login = tk.BooleanVar(value=native.autostart_installed() if self.current else True)
        self.desktop_icon = tk.BooleanVar(value=native.desktop_shortcut_installed() if self.current else True)
        self.status = tk.StringVar(value="Detecting monitors and browsers...")

        self._build()
        self.root.after(50, self.refresh_devices)

    @staticmethod
    def _current_config() -> player.Config | None:
        try:
            return player.Config.load()
        except RuntimeError:
            return None

    def _build(self) -> None:
        body = ttk.Frame(self.root, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="Telegram YouTube Player", font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 16)
        )

        ttk.Label(body, text="Bot token").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.token, show="*").grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        ttk.Label(body, text="Chat ID").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.chat_id).grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(body, text="User ID").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.user_id).grid(row=3, column=1, sticky="ew", pady=5)
        ttk.Button(body, text="Detect from /start", command=self.detect_telegram).grid(
            row=2, column=2, rowspan=2, sticky="nsew", padx=(8, 0), pady=5
        )

        ttk.Separator(body).grid(row=4, column=0, columnspan=3, sticky="ew", pady=14)

        ttk.Label(body, text="Monitor").grid(row=5, column=0, sticky="w", pady=5)
        self.monitor_box = ttk.Combobox(body, textvariable=self.monitor, state="readonly")
        self.monitor_box.grid(row=5, column=1, sticky="ew", pady=5)
        ttk.Button(body, text="Refresh", command=self.refresh_devices).grid(row=5, column=2, padx=(8, 0), pady=5)

        ttk.Label(body, text="Browser").grid(row=6, column=0, sticky="w", pady=5)
        self.browser_box = ttk.Combobox(body, textvariable=self.browser, state="readonly")
        self.browser_box.grid(row=6, column=1, columnspan=2, sticky="ew", pady=5)
        self.browser_box.bind("<<ComboboxSelected>>", self._browser_selected)
        ttk.Label(body, text="Browser path").grid(row=7, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.browser_path).grid(row=7, column=1, sticky="ew", pady=5)
        ttk.Button(body, text="Browse", command=self.choose_browser).grid(row=7, column=2, padx=(8, 0), pady=5)

        ttk.Separator(body).grid(row=8, column=0, columnspan=3, sticky="ew", pady=14)

        ttk.Label(body, text="Ollama API key").grid(row=9, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.api_key, show="*").grid(row=9, column=1, sticky="ew", pady=5)
        ttk.Button(body, text="Create API key", command=lambda: self._open_url(OLLAMA_KEYS_URL)).grid(
            row=9, column=2, padx=(8, 0), pady=5
        )
        ttk.Label(
            body,
            text="The key is used for Ollama Web Search. A free Ollama account is required.",
            wraplength=520,
        ).grid(row=10, column=1, sticky="w", pady=(0, 5))
        ttk.Button(body, text="API key guide", command=lambda: self._open_url(OLLAMA_WEB_SEARCH_GUIDE_URL)).grid(
            row=10, column=2, padx=(8, 0), pady=(0, 5)
        )
        ttk.Label(body, text="Local model").grid(row=11, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.model).grid(row=11, column=1, sticky="ew", pady=5)
        self.ollama_button = ttk.Button(body, text="Download Ollama", command=lambda: self._open_url(OLLAMA_DOWNLOAD_URL))
        self.ollama_button.grid(row=11, column=2, padx=(8, 0), pady=5)

        ttk.Separator(body).grid(row=12, column=0, columnspan=3, sticky="ew", pady=14)
        ttk.Checkbutton(
            body,
            text="Start automatically when I sign in",
            variable=self.start_at_login,
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Checkbutton(
            body,
            text="Create a desktop shortcut",
            variable=self.desktop_icon,
        ).grid(row=14, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Label(body, textvariable=self.status, wraplength=660).grid(row=15, column=0, columnspan=3, sticky="w", pady=(10, 12))
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.grid(row=16, column=0, columnspan=3, sticky="ew", pady=(0, 12))
        self.progress.grid_remove()
        self.install_button = ttk.Button(body, text="Install and start", command=self.install)
        self.install_button.grid(row=17, column=2, sticky="e")

    def _open_url(self, url: str) -> None:
        try:
            open_external_url(url)
        except RuntimeError as error:
            messagebox.showerror("Could not open link", str(error), parent=self.root)

    def _run(self, work: Callable[[], T], done: Callable[[T], None]) -> None:
        self.install_button.configure(state="disabled")
        self.progress.grid()
        self.progress.start(12)
        results: Queue[tuple[bool, object]] = Queue()

        def task() -> None:
            try:
                results.put((True, work()))
            except Exception as error:
                results.put((False, error))

        def poll() -> None:
            try:
                success, result = results.get_nowait()
            except Empty:
                self.root.after(50, poll)
                return
            if success:
                done(cast(T, result))
            else:
                self._finish_error(cast(Exception, result))

        threading.Thread(target=task, daemon=True).start()
        self.root.after(50, poll)

    def _finish(self) -> None:
        self.progress.stop()
        self.progress.grid_remove()
        self.install_button.configure(state="normal")

    def _finish_error(self, error: Exception) -> None:
        self._finish()
        self.status.set(str(error))
        messagebox.showerror("Setup failed", str(error), parent=self.root)

    def refresh_devices(self) -> None:
        try:
            self.monitors = native.configured_monitors()
            self.browsers = native.detected_browsers()
        except Exception as error:
            self.status.set(str(error))
            return

        if player.ollama_executable():
            self.ollama_button.configure(text="Ollama installed", state="disabled")
        else:
            self.ollama_button.configure(text="Download Ollama", state="normal")

        if self.current:
            current_path = Path(self.current.browser_path)
            if current_path.is_file() and not any(item.path == current_path for item in self.browsers):
                self.browsers.insert(
                    0,
                    native.DetectedBrowser(player.browser_spec(self.current.browser_type), current_path),
                )

        self.monitor_by_label = {item.label: item for item in self.monitors}
        self.monitor_box.configure(values=list(self.monitor_by_label))
        selected_monitor = None
        if self.current:
            selected_monitor = next(
                (
                    item
                    for item in self.monitors
                    if item.product == self.current.target_monitor_product
                    and (not self.current.target_monitor_connector or item.connector == self.current.target_monitor_connector)
                ),
                None,
            )
        selected_monitor = selected_monitor or next((item for item in self.monitors if item.primary), self.monitors[0])
        self.monitor.set(selected_monitor.label)

        self.browser_by_label = {item.label: item for item in self.browsers}
        self.browser_box.configure(values=list(self.browser_by_label))
        selected_browser = None
        if self.current:
            selected_browser = next(
                (item for item in self.browsers if item.spec.key == self.current.browser_type and item.path == Path(self.current.browser_path)),
                None,
            )
        selected_browser = selected_browser or next((item for item in self.browsers if item.default), None)
        selected_browser = selected_browser or (self.browsers[0] if self.browsers else None)
        if selected_browser:
            self.browser.set(selected_browser.label)
            self.browser_path.set(str(selected_browser.path))
            self.status.set("Ready to install.")
        else:
            self.status.set("No supported browser was detected. Select its executable manually.")

    def _browser_selected(self, _event: object = None) -> None:
        selected = self.browser_by_label.get(self.browser.get())
        if selected:
            self.browser_path.set(str(selected.path))

    def choose_browser(self) -> None:
        path = filedialog.askopenfilename(title="Select browser executable", parent=self.root)
        if path:
            self.browser_path.set(path)

    def detect_telegram(self) -> None:
        token = self.token.get().strip()
        if not token:
            self._finish_error(RuntimeError("Enter the Telegram bot token first."))
            return
        self.status.set("Waiting for a recent /start message...")

        def work() -> tuple[str, int, int]:
            api = player.TelegramAPI(token)
            bot = api.get_me()
            updates = api.get_updates(None, timeout=10)
            messages = [player.message_from_update(update) for update in updates]
            message = next((item for item in reversed(messages) if item and item.get("from") and item.get("chat")), None)
            if not message:
                raise RuntimeError("No message found. Send /start to the bot and try again.")
            return str(bot.get("username", "unknown")), int(message["chat"]["id"]), int(message["from"]["id"])

        def done(result: tuple[str, int, int]) -> None:
            username, chat_id, user_id = result
            self.chat_id.set(str(chat_id))
            self.user_id.set(str(user_id))
            self.status.set(f"Connected to @{username}.")
            self._finish()

        self._run(work, done)

    def _configuration(self) -> player.Config:
        monitor = self.monitor_by_label.get(self.monitor.get())
        path = Path(self.browser_path.get().strip()).expanduser()
        selected = self.browser_by_label.get(self.browser.get())
        spec = selected.spec if selected else next(
            (
                candidate
                for candidate in native.browser_specs()
                if path.name.casefold() in {Path(item).name.casefold() for item in candidate.common_paths}
            ),
            None,
        )
        if not monitor:
            raise RuntimeError("Select a monitor.")
        if not spec:
            raise RuntimeError("Select a supported browser.")
        if not path.is_file():
            raise RuntimeError(f"Browser executable not found: {path}")
        try:
            chat_id = int(self.chat_id.get().strip())
            user_id = int(self.user_id.get().strip())
        except ValueError as error:
            raise RuntimeError("Chat ID and user ID must be numbers.") from error
        token = self.token.get().strip()
        api_key = self.api_key.get().strip()
        if not token or not api_key:
            raise RuntimeError("Bot token and Ollama API key are required.")
        return player.Config(
            bot_token=token,
            allowed_chat_id=chat_id,
            allowed_user_id=user_id,
            target_monitor_product=monitor.product,
            target_monitor_connector=monitor.connector,
            browser_type=spec.key,
            browser_path=str(path),
            ollama_api_key=api_key,
            ollama_model=player.validate_ollama_model_name(self.model.get()),
            find_link_script="bundled",
        )

    def install(self) -> None:
        try:
            config = self._configuration()
        except Exception as error:
            self._finish_error(error)
            return
        start_at_login = self.start_at_login.get()
        desktop_icon = self.desktop_icon.get()
        self.status.set("Checking Telegram, Ollama, browser integration, and installation...")

        def work() -> tuple[Path | None, Path | None]:
            player.TelegramAPI(config.bot_token).get_me()
            player.LinkFinder(config).check(pull_if_missing=True)
            player.write_json_secure(player.CONFIG_FILE, asdict(config))
            spec = player.browser_spec(config.browser_type)
            browser = Path(config.browser_path)
            player.prepare_player_integration(spec, browser, player.browser_profile_dir(spec.key, browser))
            command = native.installed_runtime_command(Path(player.__file__))
            return apply_launch_options(command, start_at_login, desktop_icon, player.SERVICE_FILE)

        def done(result: tuple[Path | None, Path | None]) -> None:
            startup_file, shortcut = result
            self._finish()
            options = []
            if startup_file:
                options.append("startup enabled")
            if shortcut:
                options.append("desktop shortcut created")
            suffix = "; ".join(options) if options else "manual startup"
            self.status.set(f"Installed and started; {suffix}.")
            messagebox.showinfo("Setup complete", "Telegram YouTube Player is installed and running.", parent=self.root)

        self._run(work, done)

    def run(self) -> None:
        self.root.mainloop()


def launch() -> None:
    SetupWindow().run()
