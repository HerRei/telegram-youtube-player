import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

import player
import link_search
import platform_support as native


class YouTubeURLTests(unittest.TestCase):
    def test_accepts_expected_youtube_hosts(self):
        cases = [
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=2",
            "http://music.youtube.com/watch?v=abc",
            "www.youtube.com/shorts/abc",
            "youtube.com/watch?v=abc",
            "m.youtube.com/watch?v=abc",
            "https://www.youtube-nocookie.com/embed/abc",
        ]
        for url in cases:
            with self.subTest(url=url):
                normalized = player.normalize_youtube_url(url)
                self.assertIsNotNone(normalized)
                self.assertIn("autoplay=1", normalized)
                self.assertTrue(normalized.startswith("https://"))

    def test_rejects_lookalikes_and_unsafe_urls(self):
        cases = [
            "https://youtube.com.example.org/watch?v=abc",
            "https://notyoutube.com/watch?v=abc",
            "javascript:alert(1)",
            "https://user:password@youtube.com/watch?v=abc",
            "https://youtube.com:444/watch?v=abc",
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertIsNone(player.normalize_youtube_url(url))

    def test_extracts_plain_and_hidden_links(self):
        message = {
            "text": "Try https://example.com then https://youtu.be/abc.",
            "entities": [{"type": "text_link", "url": "https://youtube.com/watch?v=hidden"}],
        }
        links = player.youtube_links(message)
        self.assertEqual(2, len(links))
        self.assertIn("youtube.com/watch", links[0])
        self.assertIn("youtu.be/abc", links[1])

    def test_detects_non_youtube_link_without_treating_it_as_youtube(self):
        message = {"text": "Open https://example.com/docs"}
        self.assertEqual(["https://example.com/docs"], player.message_links(message))
        self.assertEqual([], player.youtube_links(message))

    def test_telegram_utf16_entity_offset(self):
        text = "\U0001f600 https://youtu.be/abc"
        message = {
            "text": text,
            "entities": [{"type": "url", "offset": 3, "length": 20}],
        }
        self.assertEqual(["https://youtu.be/abc?autoplay=1"], player.youtube_links(message))

    def test_converts_video_links_to_full_viewport_player(self):
        cases = {
            "https://www.youtube.com/watch?v=abc_123&list=PL1&autoplay=1":
                "https://www.youtube.com/embed/abc_123?list=PL1&autoplay=1",
            "https://youtu.be/abc-123?t=20&autoplay=1":
                "https://www.youtube.com/embed/abc-123?t=20&autoplay=1",
            "https://www.youtube.com/shorts/abc123?autoplay=1":
                "https://www.youtube.com/embed/abc123?autoplay=1",
            "https://www.youtube.com/live/abc123?autoplay=1":
                "https://www.youtube.com/embed/abc123?autoplay=1",
            "https://www.youtube.com/playlist?list=PL123&autoplay=1":
                "https://www.youtube.com/embed/videoseries?list=PL123&autoplay=1",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected, player.youtube_playback_url(source))

    def test_creates_queue_items_for_videos_and_playlists(self):
        video = player.youtube_queue_item("https://youtu.be/abc123?t=1m20s", "Example video")
        playlist = player.youtube_queue_item("https://youtube.com/playlist?list=PL123&index=3")
        self.assertEqual(("video", "abc123", 80), (video.kind, video.media_id, video.start_seconds))
        self.assertEqual("Example video", video.title)
        self.assertEqual(("playlist", "PL123", 2), (playlist.kind, playlist.media_id, playlist.index))

    def test_rejects_non_playable_youtube_queue_links(self):
        with self.assertRaisesRegex(ValueError, "video or playlist"):
            player.youtube_queue_item("https://youtube.com/@example")

    def test_local_player_uses_iframe_api_and_origin(self):
        origin = "http://127.0.0.1:8765"
        document = player.player_document("control-token", origin).decode()
        self.assertIn('name="referrer" content="strict-origin-when-cross-origin"', document)
        self.assertIn("width: 100%; height: 100%", document)
        self.assertIn("https://www.youtube.com/iframe_api", document)
        self.assertIn(json.dumps(origin), document)
        self.assertIn(json.dumps("control-token"), document)

    def test_accepts_only_public_web_search_results(self):
        accepted = (
            "https://example.com/path?q=one",
            "http://93.184.216.34/",
        )
        rejected = (
            "file:///etc/passwd",
            "https://user:secret@example.com/",
            "http://127.0.0.1/private",
            "http://127.1/private",
            "http://2130706433/private",
            "http://192.168.1.2/",
            "https://localhost/admin",
            "https://service.local/",
            "https://example.com:8443/",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(url, player.normalize_public_url(url))
        for url in rejected:
            with self.subTest(url=url):
                self.assertIsNone(player.normalize_public_url(url))


class PlaybackQueueTests(unittest.TestCase):
    def test_preserves_order_and_restores_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            queue = player.PlaybackQueue(path)
            items = [
                player.youtube_queue_item("https://youtu.be/first"),
                player.youtube_queue_item("https://youtu.be/second"),
                player.youtube_queue_item("https://youtu.be/third"),
            ]
            self.assertEqual([0, 1, 2], queue.enqueue(items))
            self.assertEqual("first", queue.snapshot()["current"]["media_id"])

            previous, current = queue.advance("wrong-token")
            self.assertIsNone(previous)
            self.assertEqual("first", current.media_id)
            previous, current = queue.advance(items[0].token)
            self.assertEqual("first", previous.media_id)
            self.assertEqual("second", current.media_id)

            restored = player.PlaybackQueue(path)
            self.assertEqual("second", restored.snapshot()["current"]["media_id"])
            self.assertEqual(["third"], [item["media_id"] for item in restored.snapshot()["pending"]])
            self.assertEqual(1, restored.clear_pending())
            restored.advance()
            self.assertFalse(path.exists())

    def test_local_api_requires_token_and_advances_once(self):
        queue = player.PlaybackQueue(None)
        first = player.youtube_queue_item("https://youtu.be/first")
        second = player.youtube_queue_item("https://youtu.be/second")
        queue.enqueue([first, second])
        server = player.LocalPlaybackServer(queue)
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(f"{server.origin}/api/queue", timeout=5)
            self.assertEqual(403, denied.exception.code)

            headers = {"X-Player-Token": server.control_token}
            request = urllib.request.Request(f"{server.origin}/api/queue", headers=headers)
            with urllib.request.urlopen(request, timeout=5) as response:
                state = json.load(response)
            self.assertEqual("first", state["current"]["media_id"])

            body = json.dumps({"token": first.token}).encode()
            for _ in range(2):
                request = urllib.request.Request(
                    f"{server.origin}/api/advance",
                    data=body,
                    method="POST",
                    headers={**headers, "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    state = json.load(response)
            self.assertEqual("second", state["current"]["media_id"])
            self.assertEqual([], state["pending"])
        finally:
            server.close()

    def test_queue_status_lists_current_and_pending(self):
        queue = player.PlaybackQueue(None)
        queue.enqueue(
            [
                player.youtube_queue_item("https://youtu.be/first", "First"),
                player.youtube_queue_item("https://youtu.be/second", "Second"),
            ]
        )
        status = player.queue_status_text(queue)
        self.assertIn("Now: First", status)
        self.assertIn("1. Second", status)

    def test_skip_to_last_keeps_final_item(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            queue = player.PlaybackQueue(path)
            queue.enqueue(
                [
                    player.youtube_queue_item("https://youtu.be/first"),
                    player.youtube_queue_item("https://youtu.be/second"),
                    player.youtube_queue_item("https://youtu.be/last"),
                ]
            )
            previous, current, skipped = queue.skip_to_last()
            self.assertEqual("first", previous.media_id)
            self.assertEqual("last", current.media_id)
            self.assertEqual(2, skipped)
            self.assertEqual([], queue.snapshot()["pending"])
            self.assertEqual("last", player.PlaybackQueue(path).snapshot()["current"]["media_id"])

            previous, current, skipped = queue.skip_to_last()
            self.assertEqual("last", previous.media_id)
            self.assertEqual("last", current.media_id)
            self.assertEqual(0, skipped)


class MonitorTests(unittest.TestCase):
    def test_finds_and_scales_monitor(self):
        xml = """<monitors version="2"><configuration><logicalmonitor>
          <x>1152</x><y>550</y><scale>1.333333333333</scale><primary>yes</primary>
          <monitor><monitorspec><connector>DP-2</connector><vendor>ABC</vendor>
          <product>DeskDisplay-27</product><serial>123</serial></monitorspec>
          <mode><width>2560</width><height>1440</height><rate>60</rate></mode></monitor>
        </logicalmonitor></configuration></monitors>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitors.xml"
            path.write_text(xml)
            monitor = native.find_monitor("DeskDisplay-27", "DP-2", system="Linux", monitors_file=path)
        self.assertEqual((1152, 550, 1920, 1080), (monitor.x, monitor.y, monitor.width, monitor.height))
        self.assertEqual("DP-2", monitor.connector)
        self.assertTrue(monitor.primary)

    def test_missing_monitor_raises(self):
        xml = "<monitors version=\"2\"><configuration /></monitors>"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitors.xml"
            path.write_text(xml)
            with self.assertRaises(RuntimeError):
                native.find_monitor("missing", system="Linux", monitors_file=path)

    def test_connector_disambiguates_identical_monitor_models(self):
        xml = """<monitors version="2"><configuration>
          <logicalmonitor><x>0</x><y>0</y><scale>1</scale>
            <monitor><monitorspec><connector>DP-1</connector><vendor>ABC</vendor>
            <product>MatchingDisplay</product><serial>one</serial></monitorspec>
            <mode><width>1920</width><height>1080</height></mode></monitor>
          </logicalmonitor>
          <logicalmonitor><x>1920</x><y>0</y><scale>1</scale>
            <monitor><monitorspec><connector>DP-2</connector><vendor>ABC</vendor>
            <product>MatchingDisplay</product><serial>two</serial></monitorspec>
            <mode><width>1920</width><height>1080</height></mode></monitor>
          </logicalmonitor>
        </configuration></monitors>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitors.xml"
            path.write_text(xml)
            monitor = native.find_monitor("MatchingDisplay", "DP-2", system="Linux", monitors_file=path)
        self.assertEqual(("DP-2", 1920), (monitor.connector, monitor.x))


class ConfigTests(unittest.TestCase):
    def test_loads_personalized_configuration(self):
        data = {
            "bot_token": "test-token",
            "allowed_chat_id": -100123,
            "allowed_user_id": 456,
            "target_monitor_product": "OfficeDisplay",
            "target_monitor_connector": "DP-3",
            "browser_type": "brave",
            "browser_path": "/opt/brave/brave-browser",
            "ollama_api_key": "private-test-key",
            "ollama_model": "qwen3:0.6b-q4_K_M",
            "find_link_script": "/opt/link-search/find-link.py",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data))
            original = player.CONFIG_FILE
            player.CONFIG_FILE = path
            try:
                config = player.Config.load()
            finally:
                player.CONFIG_FILE = original
        self.assertEqual("OfficeDisplay", config.target_monitor_product)
        self.assertEqual("DP-3", config.target_monitor_connector)
        self.assertEqual("brave", config.browser_type)
        self.assertEqual("/opt/brave/brave-browser", config.browser_path)
        self.assertEqual("private-test-key", config.ollama_api_key)
        self.assertEqual("qwen3:0.6b-q4_K_M", config.ollama_model)
        self.assertEqual("/opt/link-search/find-link.py", config.find_link_script)

    def test_loads_legacy_firefox_configuration(self):
        data = {
            "bot_token": "test-token",
            "allowed_chat_id": 123,
            "allowed_user_id": 456,
            "target_monitor_product": "OfficeDisplay",
            "target_monitor_connector": "DP-3",
            "firefox_path": "/opt/firefox/firefox",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data))
            original = player.CONFIG_FILE
            player.CONFIG_FILE = path
            try:
                config = player.Config.load()
            finally:
                player.CONFIG_FILE = original
        self.assertEqual("firefox", config.browser_type)
        self.assertEqual("/opt/firefox/firefox", config.browser_path)
        self.assertEqual("", config.ollama_api_key)
        self.assertEqual(player.DEFAULT_OLLAMA_MODEL, config.ollama_model)


class LinkFinderTests(unittest.TestCase):
    def test_bundled_search_runs_without_an_external_script(self):
        config = player.Config(
            "telegram-token",
            1,
            2,
            ollama_api_key="private-api-key",
            find_link_script="bundled",
        )
        finder = player.LinkFinder(config)
        with mock.patch("player.ensure_ollama_model") as ensure, mock.patch(
            "player.link_search.search",
            return_value={"title": "Example", "url": "https://example.com/page"},
        ) as search:
            finder.check()
            result = finder.find("open example")
        ensure.assert_called_once_with("qwen3:0.6b-q4_K_M", pull_if_missing=False)
        search.assert_called_once_with("open example", "private-api-key", "qwen3:0.6b-q4_K_M")
        self.assertEqual("https://example.com/page", result.url)

    def test_local_model_disables_thinking(self):
        response = {
            "message": {
                "content": json.dumps(
                    {"title": "Ollama Documentation", "url": "https://docs.ollama.com/"}
                )
            }
        }
        results = [
            {
                "title": "Ollama Documentation",
                "url": "https://docs.ollama.com/",
                "content": "Official documentation for Ollama.",
            }
        ]
        with mock.patch.object(link_search, "json_request", return_value=response) as request:
            selection = link_search.choose_result("official Ollama documentation", results)

        payload = request.call_args.args[1]
        self.assertIs(payload["think"], False)
        self.assertEqual("https://docs.ollama.com/", selection["url"])

    def test_prefers_virtual_environment_next_to_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "find-link.py"
            python = root / ".venv/bin/python"
            script.write_text("pass\n")
            python.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n")
            python.chmod(0o755)
            self.assertEqual(python, player.link_finder_python(script))

    def test_secret_is_passed_only_in_helper_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "find-link.py"
            script.write_text("pass\n")
            config = player.Config(
                "telegram-token",
                1,
                2,
                ollama_api_key="private-api-key",
                ollama_model="qwen3:0.6b-q4_K_M",
                find_link_script=str(script),
            )
            finder = player.LinkFinder(config)
            completed = subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps({"title": "Example", "url": "https://example.com/page"}),
                stderr="",
            )
            with mock.patch("player.subprocess.run", return_value=completed) as run:
                result = finder.find("open the example page")

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("private-api-key", " ".join(command))
        self.assertEqual("private-api-key", environment["OLLAMA_API_KEY"])
        self.assertEqual("qwen3:0.6b-q4_K_M", environment["OLLAMA_MODEL"])
        self.assertEqual(240, run.call_args.kwargs["timeout"])
        self.assertEqual("https://example.com/page", result.url)

    def test_rejects_unsafe_helper_result(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "find-link.py"
            script.write_text("pass\n")
            config = player.Config(
                "telegram-token",
                1,
                2,
                ollama_api_key="private-api-key",
                find_link_script=str(script),
            )
            finder = player.LinkFinder(config)
            completed = subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"title": "Router", "url": "http://192.168.1.1"}), stderr=""
            )
            with mock.patch("player.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(RuntimeError, "safe public URL"):
                    finder.find("router")

    def test_pulls_configured_model_when_missing(self):
        listed = subprocess.CompletedProcess([], 0, stdout="NAME ID SIZE MODIFIED\nother:latest 1 1 GB now\n", stderr="")
        pulled = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch("player.shutil.which", return_value="/usr/bin/ollama"), mock.patch(
            "player.subprocess.run", side_effect=[listed, pulled]
        ) as run, mock.patch("builtins.print"):
            player.ensure_ollama_model("qwen3:0.6b-q4_K_M", pull_if_missing=True)
        self.assertEqual(
            ["/usr/bin/ollama", "pull", "qwen3:0.6b-q4_K_M"],
            run.call_args_list[1].args[0],
        )

    def test_ollama_executable_uses_platform_install_location(self):
        with mock.patch("player.shutil.which", return_value=None), mock.patch.object(Path, "is_file", return_value=True):
            executable = player.ollama_executable("Windows")
        self.assertEqual("ollama.exe", executable.name)


class BrowserTests(unittest.TestCase):
    def test_browser_specs_are_available(self):
        self.assertEqual(
            {"firefox", "chromium", "chrome", "brave"},
            {spec.key for spec in player.BROWSER_SPECS},
        )
        with self.assertRaises(RuntimeError):
            player.browser_spec("unknown")

    def test_firefox_command_uses_dedicated_profile(self):
        spec = player.browser_spec("firefox")
        command = player.build_browser_command(
            spec,
            Path("/opt/firefox/firefox"),
            Path("/tmp/firefox-profile"),
            "about:blank",
            system="Linux",
        )
        self.assertEqual(str(Path("/opt/firefox/firefox")), command[0])
        self.assertIn("--no-remote", command)
        self.assertIn("--profile", command)
        self.assertIn(str(Path("/tmp/firefox-profile")), command)
        self.assertIn("--kiosk", command)

    def test_chromium_family_commands_use_isolated_profiles_and_x11(self):
        for browser_type in ("chromium", "chrome", "brave"):
            with self.subTest(browser_type=browser_type):
                spec = player.browser_spec(browser_type)
                profile = Path(f"/tmp/{browser_type}-profile")
                command = player.build_browser_command(
                    spec,
                    Path(f"/usr/bin/{browser_type}"),
                    profile,
                    "about:blank",
                    system="Linux",
                )
                self.assertIn(f"--user-data-dir={profile}", command)
                self.assertIn("--autoplay-policy=no-user-gesture-required", command)
                self.assertIn("--ozone-platform=x11", command)
                self.assertIn(f"--class={spec.window_class}", command)
                self.assertIn("--kiosk", command)
                extension_flags = [item for item in command if item.startswith("--load-extension=")]
                if browser_type == "chromium":
                    self.assertEqual(
                        [f"--load-extension={player.ublock_lite_dir(profile)}"],
                        extension_flags,
                    )
                else:
                    self.assertEqual([], extension_flags)

    def test_each_browser_has_a_separate_profile(self):
        profiles = {
            browser_type: player.browser_profile_dir(browser_type)
            for browser_type in ("firefox", "chromium", "chrome", "brave")
        }
        self.assertEqual(4, len(set(profiles.values())))
        self.assertEqual(player.PROFILE_DIR, profiles["firefox"])

    def test_snap_browsers_use_snap_accessible_profiles(self):
        cases = (("firefox", "firefox"), ("chromium", "chromium"), ("brave", "brave"))
        for browser_type, command in cases:
            with self.subTest(browser_type=browser_type):
                profile = player.browser_profile_dir(browser_type, Path(f"/snap/bin/{command}"), system="Linux")
                self.assertEqual(
                    Path.home() / f"snap/{command}/common/telegram-youtube-player-profile",
                    profile,
                )

    def test_brave_profile_disables_p3a_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            player.prepare_brave_profile(profile)
            local_state = json.loads((profile / "Local State").read_text())
        self.assertFalse(local_state["brave"]["p3a"]["enabled"])
        self.assertTrue(local_state["brave"]["p3a"]["notice_acknowledged"])

    def test_installs_valid_ublock_lite_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_file = root / "ublock-lite.zip"
            extension_dir = root / "extension"
            with zipfile.ZipFile(archive_file, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps({"manifest_version": 3, "author": "Raymond Hill"}),
                )
                archive.writestr(
                    "_locales/en/messages.json",
                    json.dumps({"extName": {"message": "uBlock Origin Lite"}}),
                )
                for ruleset in ("easylist", "easyprivacy", "ublock-filters"):
                    archive.writestr(f"rulesets/main/{ruleset}.json", "[]")
            player.install_ublock_lite_archive(archive_file, extension_dir)
            self.assertTrue(player.valid_ublock_lite_extension(extension_dir))

    def test_rejects_unsafe_ublock_lite_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_file = root / "unsafe.zip"
            with zipfile.ZipFile(archive_file, "w") as archive:
                archive.writestr("../outside", "unsafe")
            with self.assertRaises(RuntimeError):
                player.install_ublock_lite_archive(archive_file, root / "extension")
            self.assertFalse((root / "outside").exists())

    def test_stop_uses_profile_recorded_by_previous_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            old_profile = state_dir / "old-browser-profile"
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)", str(old_profile)],
                start_new_session=True,
            )
            original_pid_file = player.PLAYER_PID_FILE
            original_legacy_file = player.LEGACY_PLAYER_PID_FILE
            player.PLAYER_PID_FILE = state_dir / "browser-player.json"
            player.LEGACY_PLAYER_PID_FILE = state_dir / "firefox-player.json"
            player.PLAYER_PID_FILE.write_text(
                json.dumps(
                    {
                        "pid": process.pid,
                        "window_id": 0,
                        "profile_dir": str(old_profile),
                        "url": "http://127.0.0.1/player",
                    }
                )
            )
            config = player.Config("test", 0, 0, browser_type="chrome", browser_path="/bin/true")
            try:
                browser = player.BrowserPlayer(config)
                self.assertTrue(browser.is_running("http://127.0.0.1/player"))
                self.assertFalse(browser.is_running("https://example.com"))
                self.assertTrue(browser.stop())
                process.wait(timeout=2)
            finally:
                player.PLAYER_PID_FILE = original_pid_file
                player.LEGACY_PLAYER_PID_FILE = original_legacy_file
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
