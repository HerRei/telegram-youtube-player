import unittest
from pathlib import Path
from unittest import mock

import setup_ui


class LinkTests(unittest.TestCase):
    def test_ollama_links_are_official(self):
        self.assertEqual("https://ollama.com/download", setup_ui.OLLAMA_DOWNLOAD_URL)
        self.assertEqual("https://docs.ollama.com/capabilities/web-search", setup_ui.OLLAMA_WEB_SEARCH_GUIDE_URL)
        self.assertEqual("https://ollama.com/settings/keys", setup_ui.OLLAMA_KEYS_URL)

    def test_external_link_opens_in_browser(self):
        with mock.patch("setup_ui.webbrowser.open", return_value=True) as open_browser:
            setup_ui.open_external_url(setup_ui.OLLAMA_DOWNLOAD_URL)
        open_browser.assert_called_once_with(setup_ui.OLLAMA_DOWNLOAD_URL, new=2)

    def test_external_link_failure_is_reported(self):
        with mock.patch("setup_ui.webbrowser.open", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "Could not open"):
                setup_ui.open_external_url(setup_ui.OLLAMA_DOWNLOAD_URL)


class LaunchOptionTests(unittest.TestCase):
    def test_installs_selected_launch_options(self):
        service = Path("startup-item")
        shortcut = Path("desktop-shortcut")
        with mock.patch("setup_ui.native.install_desktop_shortcut", return_value=shortcut) as install_shortcut, mock.patch(
            "setup_ui.native.install_autostart", return_value=service
        ) as install_startup, mock.patch("setup_ui.native.start_application") as start:
            result = setup_ui.apply_launch_options(["player", "run"], True, True, service)
        self.assertEqual((service, shortcut), result)
        install_shortcut.assert_called_once_with(["player"])
        install_startup.assert_called_once_with(["player", "run"], service)
        start.assert_not_called()

    def test_removes_unselected_options_and_starts_once(self):
        service = Path("startup-item")
        with mock.patch("setup_ui.native.remove_desktop_shortcut") as remove_shortcut, mock.patch(
            "setup_ui.native.remove_autostart"
        ) as remove_startup, mock.patch("setup_ui.native.start_application") as start:
            result = setup_ui.apply_launch_options(["player", "run"], False, False, service)
        self.assertEqual((None, None), result)
        remove_shortcut.assert_called_once_with()
        remove_startup.assert_called_once_with(service)
        start.assert_called_once_with(["player", "run"])


if __name__ == "__main__":
    unittest.main()
