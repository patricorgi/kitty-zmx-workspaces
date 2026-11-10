import json
import unittest
from unittest.mock import Mock, patch

from kzw import watcher


class WatcherMarkerTests(unittest.TestCase):
    def test_initializes_once_and_respects_later_toggle(self):
        window = Mock(spec=["set_marker"])
        marker = ["function", "helpers/privacy_marker.py"]
        with patch.dict("os.environ", {"KZW_INHERIT_MARKER": json.dumps(marker)}, clear=True):
            watcher.on_resize(None, window, {})
            watcher.on_focus_change(None, window, {})
        window.set_marker.assert_called_once_with(marker)

    def test_no_marker_does_not_change_pane(self):
        window = Mock(spec=["set_marker"])
        with patch.dict("os.environ", {}, clear=True):
            watcher.on_resize(None, window, {})
        window.set_marker.assert_not_called()
