from __future__ import annotations

import ctypes as ct
import logging
import os
import time
from pathlib import Path
from typing import Optional

from zlg_canoe_bridge.adapters.base import CanAdapter
from zlg_canoe_bridge.frame import CanFdFrame, dlc_to_len

STATUS_OK = 1
TYPE_CANFD = 1
TYPE_CAN = 0

CANFD_BRS = 0x01
CANFD_ESI = 0x02
ZCAN_ID_EFF_FLAG = 0x80000000
ZCAN_ID_RTR_FLAG = 0x40000000
ZCAN_ID_ERR_FLAG = 0x20000000

# These ZCAN device families use the CAN FD controller initialization layout
# even when the application intentionally accepts/transmits classic CAN only.
ZCAN_CANFD_CONTROLLER_DEVICE_TYPES = {41, 42, 43, 59, 76, 77, 80, 81, 85, 86, 87, 99}


class ZCAN_CHANNEL_CANFD_INIT_CONFIG(ct.Structure):
    _fields_ = [
        ("acc_code", ct.c_uint),
        ("acc_mask", ct.c_uint),
        ("abit_timing", ct.c_uint),
        ("dbit_timing", ct.c_uint),
        ("brp", ct.c_uint),
        ("filter", ct.c_ubyte),
        ("mode", ct.c_ubyte),
        ("pad", ct.c_ushort),
        ("reserved", ct.c_uint),
    ]


class ZCAN_CHANNEL_CAN_INIT_CONFIG(ct.Structure):
    _fields_ = [
        ("acc_code", ct.c_uint),
        ("acc_mask", ct.c_uint),
        ("reserved", ct.c_uint),
        ("filter", ct.c_ubyte),
        ("timing0", ct.c_ubyte),
        ("timing1", ct.c_ubyte),
        ("mode", ct.c_ubyte),
    ]


class ZCAN_CHANNEL_INIT_UNION(ct.Union):
    _fields_ = [
        ("can", ZCAN_CHANNEL_CAN_INIT_CONFIG),
        ("canfd", ZCAN_CHANNEL_CANFD_INIT_CONFIG),
    ]


class ZCAN_CHANNEL_INIT_CONFIG(ct.Structure):
    _fields_ = [
        ("can_type", ct.c_uint),
        ("config", ZCAN_CHANNEL_INIT_UNION),
    ]

    @property
    def canfd(self):
        return self.config.canfd

    @property
    def can(self):
        return self.config.can


class ZCAN_CANFD_FRAME(ct.Structure):
    _fields_ = [
        ("can_id", ct.c_uint),
        ("len", ct.c_ubyte),
        ("flags", ct.c_ubyte),
        ("__res0", ct.c_ubyte),
        ("__res1", ct.c_ubyte),
        ("data", ct.c_ubyte * 64),
    ]


class ZCAN_CAN_FRAME(ct.Structure):
    _fields_ = [
        ("can_id", ct.c_uint),
        ("can_dlc", ct.c_ubyte),
        ("__pad", ct.c_ubyte),
        ("__res0", ct.c_ubyte),
        ("__res1", ct.c_ubyte),
        ("data", ct.c_ubyte * 8),
    ]


class ZCAN_Transmit_Data(ct.Structure):
    _fields_ = [
        ("frame", ZCAN_CAN_FRAME),
        ("transmit_type", ct.c_uint),
    ]


class ZCAN_Receive_Data(ct.Structure):
    _fields_ = [
        ("frame", ZCAN_CAN_FRAME),
        ("timestamp", ct.c_ulonglong),
    ]


class ZCAN_TransmitFD_Data(ct.Structure):
    _fields_ = [
        ("frame", ZCAN_CANFD_FRAME),
        ("transmit_type", ct.c_uint),
    ]


class ZCAN_ReceiveFD_Data(ct.Structure):
    _fields_ = [
        ("frame", ZCAN_CANFD_FRAME),
        ("timestamp", ct.c_ulonglong),
    ]


class ZlgZcanAdapter(CanAdapter):
    # transmit_type=0 and the normal ZCAN receive APIs provide bus RX frames,
    # not a separate transmit-confirmation event stream.
    rx_only = True

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dll_path = cfg.get("dllPath", "zlgcan.dll")
        self.device_type = int(cfg.get("deviceType", 41))
        self.device_index = int(cfg.get("deviceIndex", 0))
        self.channel_index = int(cfg.get("channelIndex", 0))
        self.termination = bool(cfg.get("enableTermination", False))
        self.arb_bitrate = int(cfg.get("arbitrationBitrate", 500000))
        self.data_bitrate = int(cfg.get("dataBitrate", 2000000))
        self.can_fd_enabled = bool(cfg.get("canFdEnabled", True))
        default_controller_type = TYPE_CANFD if self.device_type in ZCAN_CANFD_CONTROLLER_DEVICE_TYPES else TYPE_CAN
        self.controller_can_type = int(cfg.get("controllerCanType", default_controller_type))
        self.iso_canfd = bool(cfg.get("isoCanFd", True))
        self.brs_enabled = bool(cfg.get("brs", True))
        self.use_set_value = bool(cfg.get("useSetValue", True))
        self.tx_timeout_ms = int(cfg.get("txTimeoutMs", 1000))
        self.dll = None
        self.dll_dir_handles = []
        self.dev = None
        self.chn = None
        self.log = logging.getLogger("zlg_canoe_bridge")

    def open(self) -> None:
        attempts = int(self.cfg.get("openRetries", 2))
        delay_s = float(self.cfg.get("openRetryDelayS", 0.8))
        for attempt in range(1, attempts + 1):
            try:
                self._open_once()
                return
            except RuntimeError as exc:
                self.close()
                if "CHANNEL_START_FAILED" not in str(exc) or attempt >= attempts:
                    raise
                time.sleep(delay_s)

    def _open_once(self) -> None:
        dll_file = Path(self.dll_path)
        if os.name == "nt" and dll_file.parent != Path("."):
            dll_dir = dll_file.resolve().parent
            self.dll_dir_handles.append(os.add_dll_directory(str(dll_dir)))
            kernel_dir = dll_dir / "kerneldlls"
            if kernel_dir.is_dir():
                self.dll_dir_handles.append(os.add_dll_directory(str(kernel_dir)))

        try:
            self.dll = ct.WinDLL(self.dll_path)
        except OSError as exc:
            raise RuntimeError(f"DLL_LOAD_FAILED: cannot load {self.dll_path}: {exc}") from exc

        self._declare_api()
        self.dev = self.dll.ZCAN_OpenDevice(self.device_type, self.device_index, 0)
        if not self.dev:
            raise RuntimeError(
                "OPEN_DEVICE_FAILED: ZCAN_OpenDevice returned 0. Check device type/index, "
                "driver installation, DLL package, and whether ZCANPRO/ZXDoc is occupying the device."
            )

        if self.use_set_value:
            self._set_value(f"{self.channel_index}/canfd_abit_baud_rate", str(self.arb_bitrate))
            if self.can_fd_enabled:
                self._set_value(f"{self.channel_index}/canfd_standard", "0" if self.iso_canfd else "1")
                self._set_value(f"{self.channel_index}/canfd_dbit_baud_rate", str(self.data_bitrate))
            self._set_value(f"{self.channel_index}/tx_timeout", str(self.tx_timeout_ms))

        init = ZCAN_CHANNEL_INIT_CONFIG()
        init.can_type = self.controller_can_type
        if self.controller_can_type == TYPE_CANFD:
            init.canfd.acc_code = 0
            init.canfd.acc_mask = 0xFFFFFFFF
            init.canfd.brp = 0
            init.canfd.filter = 0
            init.canfd.mode = 0
            init.canfd.abit_timing = int(self.cfg.get("abitTiming", 0))
            init.canfd.dbit_timing = int(self.cfg.get("dbitTiming", 0))
        else:
            init.can.acc_code = 0
            init.can.acc_mask = 0xFFFFFFFF
            init.can.filter = 0
            init.can.mode = 0

        self.chn = self.dll.ZCAN_InitCAN(self.dev, self.channel_index, ct.byref(init))
        if not self.chn:
            raise RuntimeError("FAIL_OPEN_CHANNEL: ZCAN_InitCAN failed. Check CANFD init structure, channel index, and SDK version.")

        if self.use_set_value:
            self._set_value(f"{self.channel_index}/initenal_resistance", "1" if self.termination else "0")

        ret = self.dll.ZCAN_StartCAN(self.chn)
        if ret != STATUS_OK:
            raise RuntimeError(f"CHANNEL_START_FAILED: ZCAN_StartCAN failed, ret={ret}")
        self.log.info(
            "[ZLG] Channel owner: Bridge | Mode: %s | Controller: %s | Channel: %d | Nominal bitrate: %d | "
            "Data bitrate: %s | BRS: %s | Termination: %s",
            "CAN FD" if self.can_fd_enabled else "Classic CAN",
            "TYPE_CANFD" if self.controller_can_type == TYPE_CANFD else "TYPE_CAN",
            self.channel_index, self.arb_bitrate,
            str(self.data_bitrate) if self.can_fd_enabled else "N/A",
            "Yes" if self.can_fd_enabled and self.brs_enabled else "No", "On" if self.termination else "Off",
        )

    def close(self) -> None:
        try:
            if self.dll is not None and self.chn and hasattr(self.dll, "ZCAN_ResetCAN"):
                self.dll.ZCAN_ResetCAN(self.chn)
            if self.dll is not None and self.dev:
                self.dll.ZCAN_CloseDevice(self.dev)
        finally:
            self.dev = None
            self.chn = None
            self.dll = None
            while self.dll_dir_handles:
                self.dll_dir_handles.pop().close()

    def send(self, frame: CanFdFrame) -> None:
        assert self.dll is not None and self.chn is not None
        if not frame.is_fd:
            self._send_classic(frame)
            return
        if not self.can_fd_enabled:
            raise ValueError("Classic CAN ZLG channel cannot transmit a CAN FD frame")
        tx = ZCAN_TransmitFD_Data()
        can_id = frame.can_id & (0x1FFFFFFF if frame.is_extended else 0x7FF)
        if frame.is_extended:
            can_id |= ZCAN_ID_EFF_FLAG
        if frame.is_remote:
            can_id |= ZCAN_ID_RTR_FLAG
        tx.frame.can_id = can_id
        tx.frame.len = dlc_to_len(frame.dlc)
        flags = 0
        if frame.brs:
            flags |= CANFD_BRS
        if frame.esi:
            flags |= CANFD_ESI
        tx.frame.flags = flags
        for i, b in enumerate(frame.data):
            tx.frame.data[i] = b
        tx.transmit_type = 0
        ret = self.dll.ZCAN_TransmitFD(self.chn, ct.byref(tx), 1)
        if ret != 1:
            raise RuntimeError(f"TX_FAILED: ZCAN_TransmitFD failed, ret={ret}")

    def receive(self, timeout_ms: int = 10) -> Optional[CanFdFrame]:
        assert self.dll is not None and self.chn is not None
        if not self.can_fd_enabled:
            return self._receive_classic(timeout_ms)
        # A CAN FD-capable ZCAN channel has separate classic-CAN and CAN-FD
        # receive APIs/queues. Poll both so a mixed bus does not lose classic frames.
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
        while True:
            classic = self._receive_classic(0)
            if classic is not None:
                return classic
            fd = self._receive_fd(0)
            if fd is not None:
                return fd
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.001)

    def _receive_fd(self, timeout_ms: int) -> Optional[CanFdFrame]:
        rx = ZCAN_ReceiveFD_Data()
        ret = self.dll.ZCAN_ReceiveFD(self.chn, ct.byref(rx), 1, timeout_ms)
        if ret <= 0:
            return None
        raw_id = int(rx.frame.can_id)
        is_ext = bool(raw_id & ZCAN_ID_EFF_FLAG)
        is_rtr = bool(raw_id & ZCAN_ID_RTR_FLAG)
        can_id = raw_id & (0x1FFFFFFF if is_ext else 0x7FF)
        length = int(rx.frame.len)
        if length > 64:
            raise ValueError(f"invalid ZCAN CAN FD data length: {length}")
        data = bytes(rx.frame.data[:length])
        flags = int(rx.frame.flags)
        return CanFdFrame(
            can_id=can_id,
            data=data,
            is_fd=True,
            is_extended=is_ext,
            brs=bool(flags & CANFD_BRS),
            esi=bool(flags & CANFD_ESI),
            is_remote=is_rtr,
            timestamp_us=int(rx.timestamp),
            channel=self.channel_index,
            dlc_value=frame_dlc_from_length(length),
        )

    def _send_classic(self, frame: CanFdFrame) -> None:
        if len(frame.data) > 8:
            raise RuntimeError("Classic CAN channel cannot transmit payloads longer than 8 bytes")
        tx = ZCAN_Transmit_Data()
        can_id = frame.can_id & (0x1FFFFFFF if frame.is_extended else 0x7FF)
        if frame.is_extended:
            can_id |= ZCAN_ID_EFF_FLAG
        if frame.is_remote:
            can_id |= ZCAN_ID_RTR_FLAG
        tx.frame.can_id = can_id
        tx.frame.can_dlc = frame.dlc
        setattr(tx.frame, "__pad", 0)
        setattr(tx.frame, "__res0", 0)
        setattr(tx.frame, "__res1", 0)
        for i, b in enumerate(frame.data):
            tx.frame.data[i] = b
        tx.transmit_type = 0
        ret = self.dll.ZCAN_Transmit(self.chn, ct.byref(tx), 1)
        if ret != 1:
            raise RuntimeError(f"TX_FAILED: ZCAN_Transmit failed, ret={ret}")

    def _receive_classic(self, timeout_ms: int) -> Optional[CanFdFrame]:
        rx = ZCAN_Receive_Data()
        ret = self.dll.ZCAN_Receive(self.chn, ct.byref(rx), 1, timeout_ms)
        if ret <= 0:
            return None
        raw_id = int(rx.frame.can_id)
        is_ext = bool(raw_id & ZCAN_ID_EFF_FLAG)
        is_rtr = bool(raw_id & ZCAN_ID_RTR_FLAG)
        can_id = raw_id & (0x1FFFFFFF if is_ext else 0x7FF)
        length = min(int(rx.frame.can_dlc), 8)
        return CanFdFrame(
            can_id=can_id,
            data=b"" if is_rtr else bytes(rx.frame.data[:length]),
            is_fd=False,
            is_extended=is_ext,
            brs=False,
            esi=False,
            is_remote=is_rtr,
            timestamp_us=int(rx.timestamp),
            channel=self.channel_index,
            dlc_value=int(rx.frame.can_dlc),
        )

    def _set_value(self, path: str, value: str) -> None:
        ret = self.dll.ZCAN_SetValue(self.dev, path.encode("ascii"), value.encode("ascii"))
        if ret == 0:
            raise RuntimeError(f"CONFIG_VALUE_FAILED: ZCAN_SetValue({path}={value}) failed")

    def _declare_api(self) -> None:
        d = self.dll
        required = [
            "ZCAN_OpenDevice",
            "ZCAN_CloseDevice",
            "ZCAN_InitCAN",
            "ZCAN_StartCAN",
            "ZCAN_TransmitFD",
            "ZCAN_ReceiveFD",
            "ZCAN_SetValue",
        ]
        # Classic CAN and CAN FD frames use distinct ZCAN APIs even on an FD channel.
        required.extend(["ZCAN_Transmit", "ZCAN_Receive"])
        missing = [name for name in required if not hasattr(d, name)]
        if missing:
            raise RuntimeError(f"API_SYMBOLS_MISSING: {', '.join(missing)}")

        d.ZCAN_OpenDevice.argtypes = [ct.c_uint, ct.c_uint, ct.c_uint]
        d.ZCAN_OpenDevice.restype = ct.c_void_p
        d.ZCAN_CloseDevice.argtypes = [ct.c_void_p]
        d.ZCAN_CloseDevice.restype = ct.c_uint
        d.ZCAN_InitCAN.argtypes = [ct.c_void_p, ct.c_uint, ct.POINTER(ZCAN_CHANNEL_INIT_CONFIG)]
        d.ZCAN_InitCAN.restype = ct.c_void_p
        d.ZCAN_StartCAN.argtypes = [ct.c_void_p]
        d.ZCAN_StartCAN.restype = ct.c_uint
        if hasattr(d, "ZCAN_ResetCAN"):
            d.ZCAN_ResetCAN.argtypes = [ct.c_void_p]
            d.ZCAN_ResetCAN.restype = ct.c_uint
        d.ZCAN_TransmitFD.argtypes = [ct.c_void_p, ct.POINTER(ZCAN_TransmitFD_Data), ct.c_uint]
        d.ZCAN_TransmitFD.restype = ct.c_uint
        d.ZCAN_ReceiveFD.argtypes = [ct.c_void_p, ct.POINTER(ZCAN_ReceiveFD_Data), ct.c_uint, ct.c_int]
        d.ZCAN_ReceiveFD.restype = ct.c_uint
        if hasattr(d, "ZCAN_Transmit"):
            d.ZCAN_Transmit.argtypes = [ct.c_void_p, ct.POINTER(ZCAN_Transmit_Data), ct.c_uint]
            d.ZCAN_Transmit.restype = ct.c_uint
        if hasattr(d, "ZCAN_Receive"):
            d.ZCAN_Receive.argtypes = [ct.c_void_p, ct.POINTER(ZCAN_Receive_Data), ct.c_uint, ct.c_int]
            d.ZCAN_Receive.restype = ct.c_uint
        d.ZCAN_SetValue.argtypes = [ct.c_void_p, ct.c_char_p, ct.c_char_p]
        d.ZCAN_SetValue.restype = ct.c_uint


def frame_dlc_from_length(length: int) -> int:
    """Convert the ZCAN CAN FD byte-length field to its canonical DLC."""
    from zlg_canoe_bridge.frame import len_to_dlc
    return len_to_dlc(length)
