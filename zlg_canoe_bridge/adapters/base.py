from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
from zlg_canoe_bridge.frame import CanFdFrame


class CanAdapter(ABC):
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
