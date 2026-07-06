from __future__ import annotations
import queue
import time
from typing import Optional
from zlg_canoe_bridge.adapters.base import CanAdapter
from zlg_canoe_bridge.frame import CanFdFrame


class MockAdapter(CanAdapter):
    """Offline adapter for checking the bridge without CANoe/ZLG hardware."""
    def __init__(self, name: str):
        self.name = name
        self.rx: "queue.Queue[CanFdFrame]" = queue.Queue()
        self.peer: Optional[MockAdapter] = None
        self.opened = False

    def connect_peer(self, peer: "MockAdapter") -> None:
        self.peer = peer
        peer.peer = self

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def send(self, frame: CanFdFrame) -> None:
        if self.peer is not None:
            self.peer.rx.put(frame)

    def receive(self, timeout_ms: int = 10) -> Optional[CanFdFrame]:
        try:
            return self.rx.get(timeout=max(timeout_ms, 1) / 1000)
        except queue.Empty:
            return None

    def inject(self, frame: CanFdFrame) -> None:
        self.rx.put(frame)
