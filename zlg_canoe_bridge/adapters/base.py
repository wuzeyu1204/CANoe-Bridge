from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
from zlg_canoe_bridge.frame import CanFdFrame


class CanAdapter(ABC):
    # True when receive() exposes bus RX frames only and filters/does not emit
    # this application's transmit confirmations. BridgeCore uses this to avoid
    # content-based echo suppression when the driver provides reliable direction.
    rx_only: bool = False

    @abstractmethod
    def open(self) -> None:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    @abstractmethod
    def send(self, frame: CanFdFrame) -> None:
        pass

    @abstractmethod
    def receive(self, timeout_ms: int = 10) -> Optional[CanFdFrame]:
        pass
