from __future__ import annotations
from dataclasses import dataclass, field
from typing import ByteString
import time

DLC_TO_LEN = {
    0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8,
    9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64,
}
LEN_TO_DLC = {
    0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8,
    12: 9, 16: 10, 20: 11, 24: 12, 32: 13, 48: 14, 64: 15,
}


def dlc_to_len(dlc: int) -> int:
    if dlc not in DLC_TO_LEN:
        raise ValueError(f"invalid CAN/CANFD DLC: {dlc}")
    return DLC_TO_LEN[dlc]


def len_to_dlc(length: int) -> int:
    """Return canonical CANFD DLC for a payload length.

    For non-CANFD, length must be <= 8. For CANFD, non-canonical lengths are
    rounded up to the next valid CANFD DLC because hardware APIs usually need DLC.
    """
    if length < 0 or length > 64:
        raise ValueError(f"invalid CANFD length: {length}")
    if length in LEN_TO_DLC:
        return LEN_TO_DLC[length]
    for dlc, size in DLC_TO_LEN.items():
        if size >= length:
            return dlc
    return 15


@dataclass(slots=True)
class CanFdFrame:
    can_id: int
    data: bytes = field(default_factory=bytes)
    is_fd: bool = True
    is_extended: bool = False
    brs: bool = True
    esi: bool = False
    is_remote: bool = False
    timestamp_us: int = 0
    direction: str = ""  # e.g. vector_rx, zlg_rx, vector_tx, zlg_tx

    def __post_init__(self) -> None:
        if isinstance(self.data, bytearray):
            self.data = bytes(self.data)
        if len(self.data) > 64:
            raise ValueError("CANFD payload cannot exceed 64 bytes")
        if not self.is_fd and len(self.data) > 8:
            raise ValueError("classic CAN payload cannot exceed 8 bytes")
        if self.timestamp_us == 0:
            self.timestamp_us = time.monotonic_ns() // 1000

    @property
    def dlc(self) -> int:
        return len_to_dlc(len(self.data))

    @property
    def ide_str(self) -> str:
        return "EXT" if self.is_extended else "STD"

    def normalized_key(self) -> tuple:
        """A compact key for echo suppression."""
        return (
            self.can_id,
            self.is_extended,
            self.is_fd,
            self.brs,
            self.esi,
            self.is_remote,
            bytes(self.data),
        )

    def short(self) -> str:
        flags = []
        flags.append("CANFD" if self.is_fd else "CAN")
        flags.append(self.ide_str)
        if self.brs:
            flags.append("BRS")
        if self.esi:
            flags.append("ESI")
        if self.is_remote:
            flags.append("RTR")
        data_hex = " ".join(f"{b:02X}" for b in self.data)
        return f"{'/'.join(flags)} ID=0x{self.can_id:X} DLC={self.dlc} LEN={len(self.data)} DATA=[{data_hex}]"
