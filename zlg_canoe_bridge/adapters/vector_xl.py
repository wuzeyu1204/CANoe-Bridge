from __future__ import annotations
import ctypes as ct
import logging
import time
from typing import Optional

from zlg_canoe_bridge.adapters.base import CanAdapter
from zlg_canoe_bridge.frame import CanFdFrame, dlc_to_len

# NOTE:
# Vector XL API definitions vary between driver versions. This module follows the
# common 64-bit vxlapi layout for CAN FD. If your vxlapi.h differs, adjust the
# structures below according to the official Vector sample project.

XL_SUCCESS = 0
XL_ERR_QUEUE_IS_EMPTY = 10
XL_BUS_TYPE_CAN = 0x00000001
XL_INTERFACE_VERSION = 3
XL_INTERFACE_VERSION_V4 = 4
XL_ACTIVATE_NONE = 0

XL_TRANSMIT_MSG = 10
XL_RECEIVE_MSG = 1

XL_CAN_EXT_MSG_ID = 0x80000000
XL_CAN_MSG_FLAG_REMOTE_FRAME = 0x0010
XL_CAN_MSG_FLAG_TX_COMPLETED = 0x0040

XL_CANFD_TXMSG_FLAG_EDL = 0x0001
XL_CANFD_TXMSG_FLAG_BRS = 0x0002
XL_CANFD_TXMSG_FLAG_ESI = 0x0004
XL_CANFD_TXMSG_FLAG_RTR = 0x0010
XL_CANFD_TXMSG_FLAG_IDE = 0x0020

XL_CAN_RXMSG_FLAG_EDL = 0x0001
XL_CAN_RXMSG_FLAG_BRS = 0x0002
XL_CAN_RXMSG_FLAG_ESI = 0x0004
XL_CAN_RXMSG_FLAG_RTR = 0x0010
XL_CAN_RXMSG_FLAG_IDE = 0x0020

XL_CAN_EV_TAG_RX_OK = 0x0400
XL_CAN_EV_TAG_TX_OK = 0x0440


class XLcanFdConf(ct.Structure):
    _fields_ = [
        ("arbitrationBitRate", ct.c_uint),
        ("sjwAbr", ct.c_uint),
        ("tseg1Abr", ct.c_uint),
        ("tseg2Abr", ct.c_uint),
        ("dataBitRate", ct.c_uint),
        ("sjwDbr", ct.c_uint),
        ("tseg1Dbr", ct.c_uint),
        ("tseg2Dbr", ct.c_uint),
        ("reserved", ct.c_uint * 2),
    ]


class XLclassicCanMsg(ct.Structure):
    _fields_ = [
        ("id", ct.c_ulong),
        ("flags", ct.c_ushort),
        ("dlc", ct.c_ushort),
        ("res1", ct.c_longlong),
        ("data", ct.c_ubyte * 8),
        ("res2", ct.c_longlong),
    ]


class XLclassicTagData(ct.Union):
    _fields_ = [
        ("msg", XLclassicCanMsg),
        ("raw", ct.c_ubyte * 32),
    ]


class XLclassicEvent(ct.Structure):
    _fields_ = [
        ("tag", ct.c_ubyte),
        ("chanIndex", ct.c_ubyte),
        ("transId", ct.c_ushort),
        ("portHandle", ct.c_ushort),
        ("flags", ct.c_ubyte),
        ("reserved", ct.c_ubyte),
        ("timeStamp", ct.c_longlong),
        ("tagData", XLclassicTagData),
    ]


class XLcanTxMsg(ct.Structure):
    _fields_ = [
        ("canId", ct.c_uint),
        ("msgFlags", ct.c_uint),
        ("dlc", ct.c_ubyte),
        ("reserved", ct.c_ubyte * 7),
        ("data", ct.c_ubyte * 64),
    ]


class XLcanTxEvent(ct.Structure):
    _fields_ = [
        ("tag", ct.c_uint),
        ("transId", ct.c_ubyte),
        ("channelIndex", ct.c_ubyte),
        ("reserved", ct.c_ubyte * 2),
        ("tagData", XLcanTxMsg),
    ]


class XLcanRxMsg(ct.Structure):
    _fields_ = [
        ("canId", ct.c_uint),
        ("msgFlags", ct.c_uint),
        ("crc", ct.c_uint),
        ("reserved1", ct.c_ubyte * 12),
        ("totalBitCnt", ct.c_ushort),
        ("dlc", ct.c_ubyte),
        ("reserved", ct.c_ubyte),
        ("data", ct.c_ubyte * 64),
    ]


class XLcanRxEvent(ct.Structure):
    _fields_ = [
        ("size", ct.c_uint),
        ("tag", ct.c_uint),
        ("channelIndex", ct.c_ubyte),
        ("userHandle", ct.c_ubyte),
        ("flagsChip", ct.c_ushort),
        ("reserved", ct.c_uint),
        ("timeStamp", ct.c_ulonglong),
        ("tagData", XLcanRxMsg),
    ]


class VectorXLAdapter(CanAdapter):
    rx_only = True

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dll_path = cfg.get("dllPath") or "vxlapi64.dll"
        self.app_name = cfg.get("app_name", cfg.get("applicationName", "ZLG_CANOE_BRIDGE")).encode("ascii")
        self.app_channel = int(cfg.get("channel", cfg.get("applicationChannel", 0)))
        self.channel_owner = str(cfg.get("channel_owner", cfg.get("channelOwner", "canoe"))).lower()
        if self.channel_owner not in ("canoe", "bridge"):
            raise ValueError("vector.channel_owner must be 'canoe' or 'bridge'")
        self.shared_virtual_channel = bool(cfg.get("shared_virtual_channel", cfg.get("sharedVirtualChannel", True)))
        if self.channel_owner == "canoe" and not self.shared_virtual_channel:
            raise ValueError("vector.shared_virtual_channel must be true when channel_owner is 'canoe'")
        self.arb_bitrate = int(cfg.get("arbitrationBitrate", 500000))
        self.data_bitrate = int(cfg.get("dataBitrate", 2000000))
        self.can_fd_enabled = bool(cfg.get("canFdEnabled", True))
        self.force_fd = bool(cfg.get("forceFd", False))
        # A bridge must never turn its own transmit confirmations into bus RX.
        # Keep the legacy option readable for config compatibility, but do not
        # allow it to weaken the direction boundary.
        self.receive_tx_ok = False
        self.port_handle = ct.c_long(-1)
        self.access_mask = ct.c_ulonglong(0)
        self.permission_mask = ct.c_ulonglong(0)
        self.dll = None
        self.log = logging.getLogger("zlg_canoe_bridge")
        self.requested_permission_mask = 0
        self.granted_permission_mask = 0

    def _check(self, status: int, api: str) -> None:
        if status != XL_SUCCESS:
            raise RuntimeError(f"{api} failed, XLstatus={status}")

    def open(self) -> None:
        self.dll = ct.WinDLL(self.dll_path)
        driver_open = False
        port_open = False
        try:
            self._declare_api()
            self._check(self.dll.xlOpenDriver(), "xlOpenDriver")
            driver_open = True

            hw_type = ct.c_uint(0)
            hw_index = ct.c_uint(0)
            hw_channel = ct.c_uint(0)
            status = self.dll.xlGetApplConfig(
                ct.c_char_p(self.app_name), ct.c_uint(self.app_channel),
                ct.byref(hw_type), ct.byref(hw_index), ct.byref(hw_channel),
                ct.c_uint(XL_BUS_TYPE_CAN),
            )
            self._check(status, f"xlGetApplConfig. 请在 Vector Hardware Config 中配置应用 {self.app_name.decode()}")

            ch_index = self.dll.xlGetChannelIndex(hw_type.value, hw_index.value, hw_channel.value)
            if ch_index < 0:
                raise RuntimeError("xlGetChannelIndex failed. 检查 Virtual CAN 通道分配")

            self.access_mask = ct.c_ulonglong(1 << ch_index)
            # A non-zero input requests init access. Shared/CANoe-owner mode must
            # always pass a real pointer whose input value is exactly zero.
            requested = 0 if self.channel_owner == "canoe" else self.access_mask.value
            self.requested_permission_mask = requested
            self.permission_mask = ct.c_ulonglong(requested)

            status = self.dll.xlOpenPort(
                ct.byref(self.port_handle), ct.c_char_p(self.app_name), self.access_mask,
                ct.byref(self.permission_mask), ct.c_uint(8192),
                ct.c_uint(XL_INTERFACE_VERSION_V4 if self.can_fd_enabled else XL_INTERFACE_VERSION),
                ct.c_uint(XL_BUS_TYPE_CAN),
            )
            self._check(status, "xlOpenPort")
            port_open = True
            self.granted_permission_mask = int(self.permission_mask.value)

            self.log.info(
                "[Vector] Application: %s | Channel owner: %s | Mode: %s | "
                "Init access requested: %s | Requested permission mask: 0x%X | "
                "Granted permission mask: 0x%X | CAN FD supported: %s",
                self.app_name.decode(), "CANoe" if self.channel_owner == "canoe" else "Bridge",
                "Shared RX/TX" if self.channel_owner == "canoe" else "Bridge initialized",
                "No" if requested == 0 else "Yes", requested, self.granted_permission_mask,
                "Yes" if self.can_fd_enabled else "No",
            )

            if self.channel_owner == "bridge":
                if (self.granted_permission_mask & self.access_mask.value) != self.access_mask.value:
                    raise RuntimeError(
                        "Vector init access was requested but not granted; refusing to configure the channel. "
                        f"requested=0x{requested:X}, granted=0x{self.granted_permission_mask:X}"
                    )
                self._configure_owned_channel()

            status = self.dll.xlActivateChannel(
                self.port_handle, self.access_mask, ct.c_uint(XL_BUS_TYPE_CAN), ct.c_uint(XL_ACTIVATE_NONE)
            )
            self._check(status, "xlActivateChannel")
        except Exception:
            if port_open:
                self.dll.xlClosePort(self.port_handle)
            if driver_open:
                self.dll.xlCloseDriver()
            self.dll = None
            self.port_handle = ct.c_long(-1)
            raise

    def _configure_owned_channel(self) -> None:
        if not self.can_fd_enabled:
            self._check(
                self.dll.xlCanSetChannelBitrate(self.port_handle, self.access_mask, ct.c_ulong(self.arb_bitrate)),
                "xlCanSetChannelBitrate",
            )
            return
        fd_conf = XLcanFdConf()
        fd_conf.arbitrationBitRate = self.arb_bitrate
        fd_conf.dataBitRate = self.data_bitrate
        fd_conf.sjwAbr = int(self.cfg.get("sjwAbr", 2))
        fd_conf.tseg1Abr = int(self.cfg.get("tseg1Abr", 63))
        fd_conf.tseg2Abr = int(self.cfg.get("tseg2Abr", 16))
        fd_conf.sjwDbr = int(self.cfg.get("sjwDbr", 2))
        fd_conf.tseg1Dbr = int(self.cfg.get("tseg1Dbr", 15))
        fd_conf.tseg2Dbr = int(self.cfg.get("tseg2Dbr", 4))
        self._check(
            self.dll.xlCanFdSetConfiguration(self.port_handle, self.access_mask, ct.byref(fd_conf)),
            "xlCanFdSetConfiguration",
        )

    def close(self) -> None:
        if self.dll is None:
            return
        try:
            if self.port_handle.value != -1:
                self.dll.xlDeactivateChannel(self.port_handle, self.access_mask)
                self.dll.xlClosePort(self.port_handle)
        finally:
            self.dll.xlCloseDriver()
            self.dll = None
            self.port_handle = ct.c_long(-1)

    def send(self, frame: CanFdFrame) -> None:
        assert self.dll is not None
        if not self.can_fd_enabled:
            if frame.is_fd:
                raise ValueError("Classic CAN Vector channel cannot transmit a CAN FD frame")
            self._send_classic(frame)
            return
        if not self.can_fd_enabled and len(frame.data) > 8:
            raise RuntimeError("Classic CAN channel cannot transmit payloads longer than 8 bytes")
        ev = XLcanTxEvent()
        ev.tag = 0  # unused by xlCanTransmitEx in many samples
        ev.tagData.canId = frame.can_id & (0x1FFFFFFF if frame.is_extended else 0x7FF)
        flags = 0
        if self.can_fd_enabled and (frame.is_fd or self.force_fd):
            flags |= XL_CANFD_TXMSG_FLAG_EDL
        if self.can_fd_enabled and frame.brs:
            flags |= XL_CANFD_TXMSG_FLAG_BRS
        if self.can_fd_enabled and frame.esi:
            flags |= XL_CANFD_TXMSG_FLAG_ESI
        if frame.is_extended:
            flags |= XL_CANFD_TXMSG_FLAG_IDE
        if frame.is_remote:
            flags |= XL_CANFD_TXMSG_FLAG_RTR
        ev.tagData.msgFlags = flags
        ev.tagData.dlc = frame.dlc
        for i, b in enumerate(frame.data):
            ev.tagData.data[i] = b

        msg_count = ct.c_uint(1)
        sent = ct.c_uint(0)
        status = self.dll.xlCanTransmitEx(self.port_handle, self.access_mask, msg_count, ct.byref(sent), ct.byref(ev))
        self._check(status, "xlCanTransmitEx")
        if sent.value != 1:
            raise RuntimeError("xlCanTransmitEx did not send the frame")

    def receive(self, timeout_ms: int = 10) -> Optional[CanFdFrame]:
        assert self.dll is not None
        if not self.can_fd_enabled:
            return self._receive_classic(timeout_ms)
        deadline = time.time() + timeout_ms / 1000
        while True:
            ev = XLcanRxEvent()
            status = self.dll.xlCanReceive(self.port_handle, ct.byref(ev))
            if status == XL_SUCCESS:
                if ev.tag not in (XL_CAN_EV_TAG_RX_OK, XL_CAN_EV_TAG_TX_OK):
                    continue
                if ev.tag == XL_CAN_EV_TAG_TX_OK:
                    continue
                msg = ev.tagData
                flags = int(msg.msgFlags)
                dlc = int(msg.dlc)
                length = dlc_to_len(dlc)
                is_remote = bool(flags & XL_CAN_RXMSG_FLAG_RTR)
                data = b"" if is_remote else bytes(msg.data[:length])
                return CanFdFrame(
                    can_id=int(msg.canId & 0x1FFFFFFF),
                    data=data,
                    is_fd=bool(flags & XL_CAN_RXMSG_FLAG_EDL),
                    is_extended=bool(flags & XL_CAN_RXMSG_FLAG_IDE),
                    brs=bool(flags & XL_CAN_RXMSG_FLAG_BRS),
                    esi=bool(flags & XL_CAN_RXMSG_FLAG_ESI),
                    is_remote=is_remote,
                    timestamp_us=int(ev.timeStamp // 1000),
                    channel=int(ev.channelIndex),
                    dlc_value=dlc,
                )
            if status != XL_ERR_QUEUE_IS_EMPTY:
                self._check(status, "xlCanReceive")
            if time.time() >= deadline:
                return None
            time.sleep(0.001)

    def _send_classic(self, frame: CanFdFrame) -> None:
        if len(frame.data) > 8:
            raise RuntimeError("Classic CAN channel cannot transmit payloads longer than 8 bytes")
        ev = XLclassicEvent()
        ev.tag = XL_TRANSMIT_MSG
        can_id = frame.can_id & (0x1FFFFFFF if frame.is_extended else 0x7FF)
        if frame.is_extended:
            can_id |= XL_CAN_EXT_MSG_ID
        ev.tagData.msg.id = can_id
        ev.tagData.msg.dlc = frame.dlc
        flags = 0
        if frame.is_remote:
            flags |= XL_CAN_MSG_FLAG_REMOTE_FRAME
        ev.tagData.msg.flags = flags
        for i, b in enumerate(frame.data):
            ev.tagData.msg.data[i] = b

        msg_count = ct.c_uint(1)
        status = self.dll.xlCanTransmit(self.port_handle, self.access_mask, ct.byref(msg_count), ct.byref(ev))
        self._check(status, "xlCanTransmit")
        if msg_count.value != 1:
            raise RuntimeError("xlCanTransmit did not send the frame")

    def _receive_classic(self, timeout_ms: int) -> Optional[CanFdFrame]:
        deadline = time.time() + timeout_ms / 1000
        while True:
            ev = XLclassicEvent()
            msg_count = ct.c_uint(1)
            status = self.dll.xlReceive(self.port_handle, ct.byref(msg_count), ct.byref(ev))
            if status == XL_SUCCESS:
                if ev.tag != XL_RECEIVE_MSG:
                    continue
                msg = ev.tagData.msg
                flags = int(msg.flags)
                if flags & XL_CAN_MSG_FLAG_TX_COMPLETED:
                    continue
                dlc = int(msg.dlc)
                length = min(dlc, 8)
                raw_id = int(msg.id)
                is_remote = bool(flags & XL_CAN_MSG_FLAG_REMOTE_FRAME)
                return CanFdFrame(
                    can_id=raw_id & 0x1FFFFFFF,
                    data=b"" if is_remote else bytes(msg.data[:length]),
                    is_fd=False,
                    is_extended=bool(raw_id & XL_CAN_EXT_MSG_ID),
                    brs=False,
                    esi=False,
                    is_remote=is_remote,
                    timestamp_us=int(ev.timeStamp // 1000),
                    channel=int(ev.chanIndex),
                    dlc_value=dlc,
                )
            if status != XL_ERR_QUEUE_IS_EMPTY:
                self._check(status, "xlReceive")
            if time.time() >= deadline:
                return None
            time.sleep(0.001)

    def _declare_api(self) -> None:
        d = self.dll
        d.xlOpenDriver.restype = ct.c_short
        d.xlCloseDriver.restype = ct.c_short
        d.xlGetApplConfig.argtypes = [ct.c_char_p, ct.c_uint, ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.POINTER(ct.c_uint), ct.c_uint]
        d.xlGetApplConfig.restype = ct.c_short
        d.xlGetChannelIndex.argtypes = [ct.c_int, ct.c_int, ct.c_int]
        d.xlGetChannelIndex.restype = ct.c_int
        d.xlOpenPort.argtypes = [ct.POINTER(ct.c_long), ct.c_char_p, ct.c_ulonglong, ct.POINTER(ct.c_ulonglong), ct.c_uint, ct.c_uint, ct.c_uint]
        d.xlOpenPort.restype = ct.c_short
        d.xlClosePort.argtypes = [ct.c_long]
        d.xlClosePort.restype = ct.c_short
        d.xlActivateChannel.argtypes = [ct.c_long, ct.c_ulonglong, ct.c_uint, ct.c_uint]
        d.xlActivateChannel.restype = ct.c_short
        d.xlDeactivateChannel.argtypes = [ct.c_long, ct.c_ulonglong]
        d.xlDeactivateChannel.restype = ct.c_short
        d.xlCanFdSetConfiguration.argtypes = [ct.c_long, ct.c_ulonglong, ct.POINTER(XLcanFdConf)]
        d.xlCanFdSetConfiguration.restype = ct.c_short
        d.xlCanSetChannelBitrate.argtypes = [ct.c_long, ct.c_ulonglong, ct.c_ulong]
        d.xlCanSetChannelBitrate.restype = ct.c_short
        d.xlCanTransmitEx.argtypes = [ct.c_long, ct.c_ulonglong, ct.c_uint, ct.POINTER(ct.c_uint), ct.POINTER(XLcanTxEvent)]
        d.xlCanTransmitEx.restype = ct.c_short
        d.xlCanReceive.argtypes = [ct.c_long, ct.POINTER(XLcanRxEvent)]
        d.xlCanReceive.restype = ct.c_short
        d.xlCanTransmit.argtypes = [ct.c_long, ct.c_ulonglong, ct.POINTER(ct.c_uint), ct.POINTER(XLclassicEvent)]
        d.xlCanTransmit.restype = ct.c_short
        d.xlReceive.argtypes = [ct.c_long, ct.POINTER(ct.c_uint), ct.POINTER(XLclassicEvent)]
        d.xlReceive.restype = ct.c_short
