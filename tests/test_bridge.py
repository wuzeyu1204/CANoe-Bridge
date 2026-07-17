from __future__ import annotations

import ctypes as ct
import logging
import queue
import threading
import time
import unittest
from unittest.mock import patch

from zlg_canoe_bridge.adapters.mock import MockAdapter
from zlg_canoe_bridge.adapters.vector_xl import (
    XL_ACTIVATE_NONE,
    XL_CAN_EV_TAG_RX_OK,
    XL_CAN_EV_TAG_TX_OK,
    XL_CAN_RXMSG_FLAG_BRS,
    XL_CAN_RXMSG_FLAG_EDL,
    XL_CAN_RXMSG_FLAG_ESI,
    XL_CAN_RXMSG_FLAG_IDE,
    VectorXLAdapter,
)
from zlg_canoe_bridge.bridge import BridgeCore, EchoSuppressor
from zlg_canoe_bridge.frame import CanFdFrame, dlc_to_len, len_to_dlc
from zlg_canoe_bridge.adapters.zlg_zcan import (
    CANFD_BRS,
    CANFD_ESI,
    TYPE_CAN,
    TYPE_CANFD,
    ZCAN_CHANNEL_INIT_CONFIG,
    ZCAN_ID_EFF_FLAG,
    ZCAN_ReceiveFD_Data,
    ZlgZcanAdapter,
)
from zlg_canoe_bridge.__main__ import channel_configs


class FakeFunction:
    def __init__(self, name, result=0, hook=None):
        self.name = name
        self.result = result
        self.hook = hook
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.hook(*args) if self.hook else self.result


class FakeVectorDll:
    def __init__(self, grant_permission=True):
        self.permission_inputs = []
        self.rx_events = queue.Queue()
        self.tx_fd = []

        def appl(_app, _channel, hw_type, hw_index, hw_channel, _bus):
            hw_type._obj.value = 1
            hw_index._obj.value = 0
            hw_channel._obj.value = 0
            return 0

        def open_port(port, _app, access, permission, *_rest):
            requested = permission._obj.value
            self.permission_inputs.append(requested)
            port._obj.value = 7
            permission._obj.value = access.value if requested and grant_permission else 0
            return 0

        def receive(_port, event):
            try:
                source = self.rx_events.get_nowait()
            except queue.Empty:
                return 10
            ct.memmove(event, ct.byref(source), ct.sizeof(source))
            return 0

        self.xlOpenDriver = FakeFunction("xlOpenDriver")
        self.xlCloseDriver = FakeFunction("xlCloseDriver")
        self.xlGetApplConfig = FakeFunction("xlGetApplConfig", hook=appl)
        self.xlGetChannelIndex = FakeFunction("xlGetChannelIndex", result=2)
        self.xlOpenPort = FakeFunction("xlOpenPort", hook=open_port)
        self.xlClosePort = FakeFunction("xlClosePort")
        self.xlActivateChannel = FakeFunction("xlActivateChannel")
        self.xlDeactivateChannel = FakeFunction("xlDeactivateChannel")
        self.xlCanFdSetConfiguration = FakeFunction("xlCanFdSetConfiguration")
        self.xlCanSetChannelBitrate = FakeFunction("xlCanSetChannelBitrate")
        self.xlCanTransmitEx = FakeFunction("xlCanTransmitEx", hook=self._transmit_ex)
        self.xlCanReceive = FakeFunction("xlCanReceive", hook=receive)
        self.xlCanTransmit = FakeFunction("xlCanTransmit", hook=self._transmit)
        self.xlReceive = FakeFunction("xlReceive", result=10)

    def _transmit_ex(self, _port, _mask, count, sent, event):
        msg = event._obj.tagData
        self.tx_fd.append((int(msg.canId), int(msg.msgFlags), int(msg.dlc), bytes(msg.data)))
        sent._obj.value = count.value
        return 0

    @staticmethod
    def _transmit(_port, _mask, count, _event):
        count._obj.value = 1
        return 0


class VectorOwnershipTests(unittest.TestCase):
    def open_adapter(self, owner="canoe", can_fd=True, grant=True, **extra):
        dll = FakeVectorDll(grant)
        cfg = {"channel_owner": owner, "canFdEnabled": can_fd, "app_name": "ZLGBridge"}
        cfg.update(extra)
        adapter = VectorXLAdapter(cfg)
        with patch("zlg_canoe_bridge.adapters.vector_xl.ct.WinDLL", return_value=dll):
            adapter.open()
        return adapter, dll

    def test_canoe_owner_requests_zero_and_never_configures(self):
        adapter, dll = self.open_adapter("canoe", True)
        self.assertEqual(dll.permission_inputs, [0])
        self.assertEqual(adapter.granted_permission_mask, 0)
        self.assertFalse(dll.xlCanFdSetConfiguration.calls)
        self.assertFalse(dll.xlCanSetChannelBitrate.calls)
        self.assertEqual(dll.xlActivateChannel.calls[0][3].value, XL_ACTIVATE_NONE)
        self.assertEqual(dll.xlOpenPort.calls[0][5].value, 4)
        adapter.close()

    def test_missing_owner_defaults_to_canoe_and_requests_no_init_access(self):
        dll = FakeVectorDll()
        adapter = VectorXLAdapter({"canFdEnabled": True})
        with patch("zlg_canoe_bridge.adapters.vector_xl.ct.WinDLL", return_value=dll):
            adapter.open()
        self.assertEqual(adapter.channel_owner, "canoe")
        self.assertEqual(dll.permission_inputs, [0])
        adapter.close()

    def test_legacy_per_channel_alias_overrides_global_new_channel(self):
        cfg = {
            "vector": {"channel": 0, "app_name": "ZLGBridge"},
            "channels": [{"name": "CH1", "vector": {"applicationChannel": 1}}],
        }
        channel = channel_configs(cfg)[0]
        self.assertEqual(channel["vector"]["channel"], 1)

    def test_canoe_owner_classic_never_configures(self):
        adapter, dll = self.open_adapter("canoe", False)
        self.assertEqual(dll.permission_inputs, [0])
        self.assertFalse(dll.xlCanSetChannelBitrate.calls)
        adapter.close()

    def test_bridge_owner_configures_fd_only_after_grant(self):
        adapter, dll = self.open_adapter("bridge", True, True)
        self.assertEqual(dll.permission_inputs, [4])
        self.assertEqual(len(dll.xlCanFdSetConfiguration.calls), 1)
        adapter.close()

    def test_bridge_owner_refuses_to_configure_without_grant(self):
        dll = FakeVectorDll(False)
        adapter = VectorXLAdapter({"channel_owner": "bridge", "canFdEnabled": True})
        with patch("zlg_canoe_bridge.adapters.vector_xl.ct.WinDLL", return_value=dll):
            with self.assertRaisesRegex(RuntimeError, "not granted"):
                adapter.open()
        self.assertFalse(dll.xlCanFdSetConfiguration.calls)
        self.assertEqual(len(dll.xlClosePort.calls), 1)
        self.assertEqual(len(dll.xlCloseDriver.calls), 1)

    def test_tx_ok_is_not_forwarded_but_rx_flags_are_preserved(self):
        from zlg_canoe_bridge.adapters.vector_xl import XLcanRxEvent
        adapter, dll = self.open_adapter("canoe", True)
        tx_ok = XLcanRxEvent()
        tx_ok.tag = XL_CAN_EV_TAG_TX_OK
        dll.rx_events.put(tx_ok)
        rx = XLcanRxEvent()
        rx.tag = XL_CAN_EV_TAG_RX_OK
        rx.channelIndex = 2
        rx.timeStamp = 123000
        rx.tagData.canId = 0x1ABCDE
        rx.tagData.msgFlags = XL_CAN_RXMSG_FLAG_EDL | XL_CAN_RXMSG_FLAG_BRS | XL_CAN_RXMSG_FLAG_ESI | XL_CAN_RXMSG_FLAG_IDE
        rx.tagData.dlc = 9
        for i in range(12):
            rx.tagData.data[i] = i
        dll.rx_events.put(rx)
        frame = adapter.receive(20)
        self.assertIsNotNone(frame)
        self.assertEqual((frame.can_id, frame.dlc, len(frame.data)), (0x1ABCDE, 9, 12))
        self.assertTrue(frame.is_extended and frame.is_fd and frame.brs and frame.esi)
        self.assertEqual(frame.channel, 2)
        adapter.close()

    def test_legacy_receive_tx_ok_true_cannot_enable_forwarding(self):
        from zlg_canoe_bridge.adapters.vector_xl import XLcanRxEvent
        adapter, dll = self.open_adapter("canoe", True, receiveTxOk=True)
        tx_ok = XLcanRxEvent()
        tx_ok.tag = XL_CAN_EV_TAG_TX_OK
        dll.rx_events.put(tx_ok)
        self.assertIsNone(adapter.receive(2))
        adapter.close()

    def test_classic_vector_channel_rejects_fd_frame(self):
        adapter, _dll = self.open_adapter("canoe", False)
        with self.assertRaisesRegex(ValueError, "cannot transmit a CAN FD"):
            adapter.send(CanFdFrame(0x123, b"x", is_fd=True))
        adapter.close()

    def test_vector_fd_transmit_preserves_flags_dlc_and_64_bytes(self):
        from zlg_canoe_bridge.adapters.vector_xl import (
            XL_CANFD_TXMSG_FLAG_BRS, XL_CANFD_TXMSG_FLAG_EDL,
            XL_CANFD_TXMSG_FLAG_ESI, XL_CANFD_TXMSG_FLAG_IDE,
        )
        adapter, dll = self.open_adapter("canoe", True)
        payload = bytes(range(64))
        adapter.send(CanFdFrame(0x1ABCDE, payload, is_fd=True, is_extended=True, brs=True, esi=True))
        can_id, flags, dlc, data = dll.tx_fd[0]
        expected = XL_CANFD_TXMSG_FLAG_EDL | XL_CANFD_TXMSG_FLAG_BRS | XL_CANFD_TXMSG_FLAG_ESI | XL_CANFD_TXMSG_FLAG_IDE
        self.assertEqual((can_id, dlc, data[:64]), (0x1ABCDE, 15, payload))
        self.assertEqual(flags & expected, expected)
        adapter.close()


class FrameTests(unittest.TestCase):
    def test_all_can_fd_dlc_mappings(self):
        expected = {9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64}
        for dlc, length in expected.items():
            self.assertEqual(dlc_to_len(dlc), length)
            self.assertEqual(len_to_dlc(length), dlc)

    def test_standard_extended_classic_and_remote_metadata(self):
        standard = CanFdFrame(0x7FF, b"12345678", is_fd=False, brs=False)
        extended = CanFdFrame(0x1FFFFFFF, b"x", is_fd=False, is_extended=True, brs=False)
        remote = CanFdFrame(0x123, b"", is_fd=False, brs=False, is_remote=True, dlc_value=8)
        self.assertEqual((standard.dlc, extended.dlc, remote.dlc), (8, 1, 8))
        with self.assertRaises(ValueError):
            CanFdFrame(0x800, b"", is_fd=False)

    def test_brs_esi_and_noncanonical_length(self):
        frame = CanFdFrame(0x123, bytes(range(11)), is_fd=True, brs=True, esi=True)
        self.assertEqual(frame.dlc, 9)
        self.assertEqual(dlc_to_len(frame.dlc), 12)


class FakeZlgDll:
    def __init__(self):
        self.init_types = []
        self.set_values = []
        self.transmitted_lengths = []
        self.transmitted_fd = []
        self.classic_tx_count = 0
        self.rx_items = queue.Queue()
        self.classic_rx_items = queue.Queue()

        def init_can(_dev, _channel, config):
            cfg = ct.cast(config, ct.POINTER(ZCAN_CHANNEL_INIT_CONFIG)).contents
            self.init_types.append(cfg.can_type)
            return 2

        def set_value(_dev, path, value):
            self.set_values.append((path.decode(), value.decode()))
            return 1

        def transmit_fd(_channel, tx, _count):
            self.transmitted_lengths.append(tx._obj.frame.len)
            self.transmitted_fd.append((tx._obj.frame.can_id, tx._obj.frame.flags, bytes(tx._obj.frame.data)))
            return 1

        def receive_fd(_channel, target, _count, _timeout):
            try:
                source = self.rx_items.get_nowait()
            except queue.Empty:
                return 0
            ct.memmove(target, ct.byref(source), ct.sizeof(source))
            return 1
        def transmit_classic(_channel, _tx, _count):
            self.classic_tx_count += 1
            return 1
        def receive_classic(_channel, target, _count, _timeout):
            try:
                source = self.classic_rx_items.get_nowait()
            except queue.Empty:
                return 0
            ct.memmove(target, ct.byref(source), ct.sizeof(source))
            return 1

        names = {
            "ZCAN_OpenDevice": FakeFunction("ZCAN_OpenDevice", result=1),
            "ZCAN_CloseDevice": FakeFunction("ZCAN_CloseDevice", result=1),
            "ZCAN_InitCAN": FakeFunction("ZCAN_InitCAN", hook=init_can),
            "ZCAN_StartCAN": FakeFunction("ZCAN_StartCAN", result=1),
            "ZCAN_ResetCAN": FakeFunction("ZCAN_ResetCAN", result=1),
            "ZCAN_TransmitFD": FakeFunction("ZCAN_TransmitFD", hook=transmit_fd),
            "ZCAN_ReceiveFD": FakeFunction("ZCAN_ReceiveFD", hook=receive_fd),
            "ZCAN_Transmit": FakeFunction("ZCAN_Transmit", hook=transmit_classic),
            "ZCAN_Receive": FakeFunction("ZCAN_Receive", hook=receive_classic),
            "ZCAN_SetValue": FakeFunction("ZCAN_SetValue", hook=set_value),
        }
        self.__dict__.update(names)


class ZlgAdapterTests(unittest.TestCase):
    def test_zlg_fully_initializes_fd_and_resets_on_close(self):
        dll = FakeZlgDll()
        adapter = ZlgZcanAdapter({
            "canFdEnabled": True, "channelIndex": 1, "arbitrationBitrate": 500000,
            "dataBitrate": 2000000, "enableTermination": True,
        })
        with patch("zlg_canoe_bridge.adapters.zlg_zcan.ct.WinDLL", return_value=dll):
            adapter.open()
        self.assertEqual(dll.init_types, [TYPE_CANFD])
        self.assertIn(("1/canfd_abit_baud_rate", "500000"), dll.set_values)
        self.assertIn(("1/canfd_dbit_baud_rate", "2000000"), dll.set_values)
        self.assertIn(("1/initenal_resistance", "1"), dll.set_values)
        adapter.close()
        self.assertEqual(len(dll.ZCAN_ResetCAN.calls), 1)

    def test_usbcandfd_classic_mode_still_uses_fd_controller_init_type(self):
        dll = FakeZlgDll()
        adapter = ZlgZcanAdapter({"canFdEnabled": False, "deviceType": 43})
        with patch("zlg_canoe_bridge.adapters.zlg_zcan.ct.WinDLL", return_value=dll):
            adapter.open()
        self.assertEqual(dll.init_types, [TYPE_CANFD])
        adapter.close()

    def test_non_fd_controller_can_still_request_classic_init_layout(self):
        dll = FakeZlgDll()
        adapter = ZlgZcanAdapter({"canFdEnabled": False, "deviceType": 3, "controllerCanType": TYPE_CAN})
        with patch("zlg_canoe_bridge.adapters.zlg_zcan.ct.WinDLL", return_value=dll):
            adapter.open()
        self.assertEqual(dll.init_types, [TYPE_CAN])
        adapter.close()

    def test_zlg_fd_lengths_and_flags_round_trip(self):
        dll = FakeZlgDll()
        adapter = ZlgZcanAdapter({"canFdEnabled": True, "useSetValue": False})
        adapter.dll, adapter.chn = dll, 2
        for length in (12, 16, 20, 24, 32, 48, 64):
            adapter.send(CanFdFrame(0x1ABCDE, bytes(range(length)), is_extended=True, brs=True, esi=True))
        self.assertEqual(dll.transmitted_lengths, [12, 16, 20, 24, 32, 48, 64])
        raw_id, flags, data = dll.transmitted_fd[-1]
        self.assertTrue(raw_id & ZCAN_ID_EFF_FLAG)
        self.assertEqual(flags & (CANFD_BRS | CANFD_ESI), CANFD_BRS | CANFD_ESI)
        self.assertEqual(data, bytes(range(64)))
        rx = ZCAN_ReceiveFD_Data()
        rx.frame.can_id = ZCAN_ID_EFF_FLAG | 0x1ABCDE
        rx.frame.len = 12
        rx.frame.flags = CANFD_BRS | CANFD_ESI
        for i in range(12):
            rx.frame.data[i] = i
        dll.rx_items.put(rx)
        frame = adapter.receive(1)
        self.assertEqual((frame.dlc, len(frame.data)), (9, 12))
        self.assertTrue(frame.is_extended and frame.brs and frame.esi)

    def test_fd_channel_preserves_classic_frames_via_classic_zcan_api(self):
        from zlg_canoe_bridge.adapters.zlg_zcan import ZCAN_Receive_Data
        dll = FakeZlgDll()
        adapter = ZlgZcanAdapter({"canFdEnabled": True, "useSetValue": False})
        adapter.dll, adapter.chn = dll, 2
        classic = CanFdFrame(0x321, b"abc", is_fd=False, brs=False)
        adapter.send(classic)
        self.assertEqual(dll.classic_tx_count, 1)
        rx = ZCAN_Receive_Data()
        rx.frame.can_id = 0x321
        rx.frame.can_dlc = 3
        rx.frame.data[:3] = b"abc"
        dll.classic_rx_items.put(rx)
        received = adapter.receive(2)
        self.assertFalse(received.is_fd)
        self.assertEqual((received.dlc, received.data), (3, b"abc"))

    def test_classic_zlg_channel_rejects_fd_frame(self):
        dll = FakeZlgDll()
        adapter = ZlgZcanAdapter({"canFdEnabled": False, "useSetValue": False})
        adapter.dll, adapter.chn = dll, 2
        with self.assertRaisesRegex(ValueError, "cannot transmit a CAN FD"):
            adapter.send(CanFdFrame(0x123, b"x", is_fd=True))


class RecordingAdapter(MockAdapter):
    def __init__(self, name, send_delay=0.0):
        super().__init__(name)
        self.sent = []
        self.send_delay = send_delay

    def send(self, frame):
        if self.send_delay:
            time.sleep(self.send_delay)
        self.sent.append(frame)


class FailOnceAdapter(RecordingAdapter):
    def __init__(self, name):
        super().__init__(name)
        self.open_count = 0
        self.failed = False

    def open(self):
        super().open()
        self.open_count += 1

    def receive(self, timeout_ms=10):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated disconnect")
        return super().receive(timeout_ms)


class BridgeRuntimeTests(unittest.TestCase):
    def make_bridge(self, queue_size=8, zlg_delay=0.0):
        vector = RecordingAdapter("vector")
        zlg = RecordingAdapter("zlg", zlg_delay)
        bridge = BridgeCore(vector, zlg, logging.getLogger("test"), queue_size=queue_size)
        return bridge, vector, zlg

    def test_bidirectional_repeated_frames_are_not_permanently_dropped(self):
        bridge, vector, zlg = self.make_bridge()
        bridge.start()
        request = CanFdFrame(0x7E0, b"\x02\x10\x03", is_fd=False, brs=False)
        response = CanFdFrame(0x7E8, b"\x02\x50\x03", is_fd=False, brs=False)
        vector.inject(request)
        vector.inject(request)
        zlg.inject(response)
        zlg.inject(response)
        time.sleep(0.12)
        bridge.stop()
        self.assertEqual(len(zlg.sent), 2)
        self.assertEqual(len(vector.sent), 2)
        self.assertEqual(bridge.status()["loop_filtered"], 0)

    def test_mixed_vector_classic_and_zlg_fd_mode_is_reported_explicitly(self):
        vector = RecordingAdapter("vector")
        zlg = RecordingAdapter("zlg")
        vector.can_fd_enabled = False
        zlg.can_fd_enabled = True
        bridge = BridgeCore(vector, zlg, logging.getLogger("test"))
        self.assertEqual(bridge.status()["mode"], "Vector Classic / ZLG CAN FD")

    def test_queue_overflow_has_drop_newest_policy_and_counter(self):
        bridge, vector, _zlg = self.make_bridge(queue_size=1, zlg_delay=0.05)
        bridge.start()
        for value in range(20):
            vector.inject(CanFdFrame(0x100 + value, bytes([value]), is_fd=False, brs=False))
        time.sleep(0.08)
        bridge.stop()
        self.assertGreater(bridge.status()["queue_overflow"], 0)

    def test_stop_leaves_no_worker_threads(self):
        bridge, _vector, _zlg = self.make_bridge()
        bridge.start()
        bridge.stop()
        self.assertFalse(any(thread.is_alive() for thread in bridge.threads))
        self.assertFalse(bridge.supervisor.is_alive())
        self.assertFalse(bridge.status()["running"])

    def test_fallback_echo_is_directional_short_lived_and_consumed_once(self):
        suppressor = EchoSuppressor("vector_rx", window_ms=5)
        frame = CanFdFrame(0x123, b"abc", is_fd=False, brs=False)
        suppressor.mark_tx(frame)
        self.assertTrue(suppressor.is_echo(frame))
        self.assertFalse(suppressor.is_echo(frame))
        suppressor.mark_tx(frame)
        time.sleep(0.03)
        self.assertFalse(suppressor.is_echo(frame))

    def test_adapter_failure_reconnects_with_clean_restart(self):
        vector = FailOnceAdapter("vector")
        zlg = RecordingAdapter("zlg")
        bridge = BridgeCore(
            vector, zlg, logging.getLogger("test"), reconnect_initial_s=0.01, reconnect_max_s=0.02
        )
        bridge.start()
        deadline = time.time() + 1.0
        while bridge.status()["reconnects"] == 0 and time.time() < deadline:
            time.sleep(0.01)
        bridge.stop()
        self.assertEqual(bridge.status()["reconnects"], 1)
        self.assertGreaterEqual(vector.open_count, 2)


if __name__ == "__main__":
    unittest.main()
