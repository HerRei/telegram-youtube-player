import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import platform_support as native


class PathTests(unittest.TestCase):
    def test_platform_data_paths(self):
        home = Path("/users/test")
        linux = native.application_paths("Linux", home, {})
        macos = native.application_paths("Darwin", home, {})
        windows = native.application_paths(
            "Windows",
            home,
            {"APPDATA": "C:/Users/Test/AppData/Roaming", "LOCALAPPDATA": "C:/Users/Test/AppData/Local"},
        )
        self.assertEqual(home / ".config/telegram-youtube-player", linux.config_dir)
        self.assertEqual(home / "Library/Application Support/telegram-youtube-player", macos.config_dir)
        self.assertEqual(Path("C:/Users/Test/AppData/Roaming/telegram-youtube-player"), windows.config_dir)
        self.assertEqual("telegram-youtube-player.cmd", windows.service_file.name)
        self.assertEqual("com.herrei.telegram-youtube-player.plist", macos.service_file.name)


class MonitorParserTests(unittest.TestCase):
    def test_xrandr_parser(self):
        output = """HDMI-1 connected primary 2560x1440+1440+758 (normal left inverted right x axis y axis)
eDP-1 connected 1440x900+0+0 (normal left inverted right x axis y axis)
"""
        monitors = native._xrandr_monitors(output)
        self.assertEqual(2, len(monitors))
        self.assertEqual(("HDMI-1", 1440, 758, 2560, 1440, True), (
            monitors[0].connector,
            monitors[0].x,
            monitors[0].y,
            monitors[0].width,
            monitors[0].height,
            monitors[0].primary,
        ))

    def test_macos_parser(self):
        monitors = native._macos_monitors(
            json.dumps(
                [
                    {"product": "Built-in Display", "connector": "1", "x": 0, "y": 900, "width": 1440, "height": 900},
                    {"product": "Studio Display", "connector": "2", "x": 1440, "y": 0, "width": 2560, "height": 1440, "primary": True},
                ]
            )
        )
        self.assertEqual("Studio Display", monitors[1].product)
        self.assertEqual((1440, 0, 2560, 1440), (monitors[1].x, monitors[1].y, monitors[1].width, monitors[1].height))
        self.assertTrue(monitors[1].primary)


class BrowserTests(unittest.TestCase):
    def test_default_browser_identifiers(self):
        cases = {
            "FirefoxURL-308046B0AF4A39CB": "firefox",
            "BraveHTML": "brave",
            "ChromiumHTM": "chromium",
            "ChromeHTML": "chrome",
            "org.mozilla.firefox": "firefox",
        }
        for identifier, expected in cases.items():
            with self.subTest(identifier=identifier):
                self.assertEqual(expected, native.browser_key_from_identifier(identifier))

    def test_commands_are_platform_specific(self):
        monitor = native.Monitor("Display", "1", 100, 200, 1920, 1080)
        for system in ("Linux", "Windows", "Darwin"):
            specs = native.browser_specs(system, {})
            firefox = native.browser_spec("firefox", specs)
            chromium = native.browser_spec("chromium", specs)
            firefox_command = native.build_browser_command(
                firefox, Path(firefox.common_paths[0]), Path("profile"), "about:blank", monitor, system
            )
            chromium_command = native.build_browser_command(
                chromium, Path(chromium.common_paths[0]), Path("profile"), "about:blank", monitor, system
            )
            self.assertIn("--no-remote", firefox_command)
            self.assertIn("--kiosk", chromium_command)
            self.assertIn("--window-position=100,200", chromium_command)
            if system == "Linux":
                self.assertIn("--kiosk", firefox_command)
                self.assertIn("--ozone-platform=x11", chromium_command)
            else:
                self.assertNotIn("--kiosk", firefox_command)
                self.assertNotIn("--ozone-platform=x11", chromium_command)


class ProcessTests(unittest.TestCase):
    def test_windows_missing_process_output_is_not_running(self):
        completed = subprocess.CompletedProcess([], 0, stdout=None, stderr=None)
        with mock.patch("platform_support.subprocess.run", return_value=completed):
            self.assertFalse(native.valid_saved_process(1234, Path("profile"), system="Windows"))

    def test_macos_missing_process_output_is_not_running(self):
        completed = subprocess.CompletedProcess([], 0, stdout=None, stderr=None)
        with mock.patch("platform_support.subprocess.run", return_value=completed):
            self.assertFalse(native.valid_saved_process(1234, Path("profile"), system="Darwin"))


class StartupTests(unittest.TestCase):
    def test_linux_service(self):
        service = native.linux_service_contents(["/usr/bin/python3", "/tmp/player.py", "run"], ":1")
        self.assertIn('ExecStart="/usr/bin/python3" "/tmp/player.py" "run"', service)
        self.assertIn("Environment=DISPLAY=:1", service)

    def test_windows_startup_script(self):
        script = native.windows_startup_contents(["C:\\Program Files\\Player\\player.exe", "run"])
        self.assertIn("start \"\" /min", script)
        self.assertIn('"C:\\Program Files\\Player\\player.exe" run', script)

    def test_macos_launch_agent(self):
        payload = native.macos_launch_agent(["/Applications/Player.app/Contents/MacOS/Player", "run"])
        self.assertEqual("com.herrei.telegram-youtube-player", payload["Label"])
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual("run", payload["ProgramArguments"][-1])

    def test_removes_windows_startup_item(self):
        with tempfile.TemporaryDirectory() as directory:
            startup = Path(directory) / "telegram-youtube-player.cmd"
            startup.write_text("start player")
            self.assertTrue(native.remove_autostart(startup, "Windows"))
            self.assertFalse(startup.exists())


class DesktopShortcutTests(unittest.TestCase):
    def test_platform_shortcut_locations(self):
        home = Path("/users/test")
        self.assertEqual(
            home / "Desktop/Telegram YouTube Player.desktop",
            native.desktop_shortcut_paths("Linux", home, {})[0],
        )
        self.assertEqual(
            home / "Desktop/Telegram YouTube Player.lnk",
            native.desktop_shortcut_paths("Windows", home, {"USERPROFILE": str(home)})[0],
        )
        self.assertEqual(
            home / "Desktop/Telegram YouTube Player.app",
            native.desktop_shortcut_paths("Darwin", home, {})[0],
        )

    def test_linux_desktop_shortcut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shortcut = root / "Desktop/Telegram YouTube Player.desktop"
            with mock.patch("platform_support.desktop_shortcut_paths", return_value=(shortcut,)):
                result = native.install_desktop_shortcut(["/opt/player", "setup"], "Linux")
            contents = shortcut.read_text()
            self.assertEqual(shortcut, result)
            self.assertIn('Exec="/opt/player" "setup"', contents)
            self.assertIn("Terminal=false", contents)
            self.assertTrue(shortcut.stat().st_mode & 0o100)

    def test_windows_shortcut_script(self):
        script = native.windows_shortcut_script(
            Path("C:/Users/Test/Desktop/Telegram YouTube Player.lnk"),
            ["C:/Program Files/Player/player.exe", "setup"],
        )
        self.assertIn("WScript.Shell", script)
        self.assertIn("player.exe", script)
        self.assertIn("$shortcut.Save()", script)

    def test_macos_desktop_shortcut_links_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Player.app/Contents/MacOS/Player"
            executable.parent.mkdir(parents=True)
            executable.touch()
            paths = (root / "Desktop/Telegram YouTube Player.app", root / "Desktop/Telegram YouTube Player.command")
            with mock.patch("platform_support.desktop_shortcut_paths", return_value=paths):
                shortcut = native.install_desktop_shortcut([str(executable)], "Darwin")
            self.assertTrue(shortcut.is_symlink())
            self.assertEqual(root / "Player.app", shortcut.resolve())


class HostSmokeTests(unittest.TestCase):
    def test_all_platform_smoke_descriptions(self):
        for system in ("Linux", "Windows", "Darwin"):
            with self.subTest(system=system):
                result = native.smoke_test(system)
                self.assertEqual(system, result["system"])
                self.assertEqual(["firefox", "chromium", "chrome", "brave"], result["browsers"])
                self.assertEqual({"firefox", "chromium", "chrome", "brave"}, set(result["commands"]))


if __name__ == "__main__":
    unittest.main()
