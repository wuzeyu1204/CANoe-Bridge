from __future__ import annotations
import threading
import time
from collections import deque
from typing import Deque
import logging

from zlg_canoe_bridge.adapters.base import CanAdapter
from zlg_canoe_bridge.frame import CanFdFrame


class EchoSuppressor:
    """Suppress short-term TX echo frames.

    Many CAN adapters can echo back frames that were just transmitted. For bridge
    software this can create an infinite loop. This suppressor stores the latest
    sent frames for a short time and drops matching frames from the opposite RX path.
    """
    def __init__(self, window_ms: int = 50, max_items: int = 2048):
        self.window_us = window_ms * 1000
        self.max_items = max_items
        self.items: Deque[tuple[int, tuple]] = deque()
        self.lock = threading.Lock()

    def mark_tx(self, frame: CanFdFrame) -> None:
        now = time.monotonic_ns() // 1000
        with self.lock:
            self.items.append((now, frame.normalized_key()))
            self._gc(now)

    def is_echo(self, frame: CanFdFrame) -> bool:
        now = time.monotonic_ns() // 1000
        key = frame.normalized_key()
        with self.lock:
            self._gc(now)
            return any(k == key for _, k in self.items)

    def _gc(self, now_us: int) -> None:
        while self.items and (now_us - self.items[0][0] > self.window_us):
            self.items.popleft()
        while len(self.items) > self.max_items:
            self.items.popleft()


class BridgeCore:
    def __init__(
        self,
        vector: CanAdapter,
        zlg: CanAdapter,
        logger: logging.Logger,
        echo_suppression: bool = True,
        echo_window_ms: int = 50,
        name: str = "CH0",
    ) -> None:
        self.vector = vector
        self.zlg = zlg
        self.log = logger
        self.name = name
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.echo_suppression = echo_suppression
        self.v2z_echo = EchoSuppressor(echo_window_ms)
        self.z2v_echo = EchoSuppressor(echo_window_ms)
        self.count_v2z = 0
        self.count_z2v = 0
        self.count_drop = 0

    def start(self) -> None:
        self.log.info("[%s] Opening Vector side...", self.name)
        self.vector.open()
        try:
            self.log.info("[%s] Opening ZLG side...", self.name)
            self.zlg.open()
            self.stop_event.clear()
            self.threads = [
                threading.Thread(target=self._vector_to_zlg_loop, name=f"{self.name}-VectorToZLG", daemon=True),
                threading.Thread(target=self._zlg_to_vector_loop, name=f"{self.name}-ZLGToVector", daemon=True),
            ]
            for t in self.threads:
                t.start()
            self.log.info("[%s] Bridge started. Press Ctrl+C to stop.", self.name)
        except Exception:
            self.log.exception("[%s] Bridge start failed, closing adapters...", self.name)
            try:
                self.zlg.close()
            finally:
                self.vector.close()
            raise

    def stop(self) -> None:
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=1.0)
        self.log.info("[%s] Closing adapters...", self.name)
        try:
            self.vector.close()
        finally:
            self.zlg.close()
        self.log.info("[%s] Stopped. v2z=%d z2v=%d drop=%d", self.name, self.count_v2z, self.count_z2v, self.count_drop)

    def _vector_to_zlg_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame = self.vector.receive(timeout_ms=10)
                if frame is None:
                    continue
                frame.direction = f"{self.name}:CANoe->ZLG"
                if self.echo_suppression and self.z2v_echo.is_echo(frame):
                    self.count_drop += 1
                    self.log.debug("[%s] Drop Vector RX echo: %s", self.name, frame.short())
                    continue
                self.zlg.send(frame)
                self.v2z_echo.mark_tx(frame)
                self.count_v2z += 1
                self.log.debug("[%s CANoe -> ZLG] %s", self.name, frame.short())
            except Exception as e:
                self.log.exception("[%s] Vector->ZLG loop error: %s", self.name, e)
                time.sleep(0.1)

    def _zlg_to_vector_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                frame = self.zlg.receive(timeout_ms=10)
                if frame is None:
                    continue
                frame.direction = f"{self.name}:ZLG->CANoe"
                if self.echo_suppression and self.v2z_echo.is_echo(frame):
                    self.count_drop += 1
                    self.log.debug("[%s] Drop ZLG RX echo: %s", self.name, frame.short())
                    continue
                self.vector.send(frame)
                self.z2v_echo.mark_tx(frame)
                self.count_z2v += 1
                self.log.debug("[%s ZLG -> CANoe] %s", self.name, frame.short())
            except Exception as e:
                self.log.exception("[%s] ZLG->Vector loop error: %s", self.name, e)
                time.sleep(0.1)
