from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from zlg_canoe_bridge.gui import BridgeGui


class Value:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class CanoeWaitDummy:
    def __init__(self):
        self.waiting_for_canoe = True
        self.canoe_wait_deadline = time.monotonic() + 10
        self.canoe_state = Value()
        self.bridge_state = Value()
        self.started = False
        self.scheduled = []
        self.logs = []

    def _log(self, level, message):
        self.logs.append((level, message))

    def _start_bridge_now(self):
        self.started = True

    def _update_button_states(self, state):
        pass

    def _refresh_channel_table(self, status_override=None):
        pass

    def after(self, delay, callback):
        self.scheduled.append((delay, callback))

    def _wait_for_canoe_then_start(self):
        return BridgeGui._wait_for_canoe_then_start(self)


class GuiStateTests(unittest.TestCase):
    def test_canoe_detection_starts_bridge_immediately(self):
        dummy = CanoeWaitDummy()
        with patch("zlg_canoe_bridge.gui.is_canoe_running", return_value=True):
            BridgeGui._wait_for_canoe_then_start(dummy)
        self.assertTrue(dummy.started)
        self.assertFalse(dummy.waiting_for_canoe)
        self.assertEqual(dummy.canoe_state.value, "进程已启动")
        self.assertFalse(dummy.scheduled)

    def test_waiting_canoe_is_polled_without_fixed_eight_second_delay(self):
        dummy = CanoeWaitDummy()
        with patch("zlg_canoe_bridge.gui.is_canoe_running", return_value=False):
            BridgeGui._wait_for_canoe_then_start(dummy)
        self.assertFalse(dummy.started)
        self.assertEqual(dummy.scheduled[0][0], 250)


if __name__ == "__main__":
    unittest.main()
