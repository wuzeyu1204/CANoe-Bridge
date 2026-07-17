from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Deque, Optional

from zlg_canoe_bridge.adapters.base import CanAdapter
from zlg_canoe_bridge.frame import CanFdFrame


class EchoSuppressor:
    """Optional short-window fallback for drivers without RX/TX direction."""

    def __init__(self, direction: str, window_ms: int = 5, max_items: int = 2048):
        self.direction = direction
        self.window_us = max(0, window_ms) * 1000
        self.max_items = max_items
        self.items: Deque[tuple[int, tuple]] = deque()
        self.lock = threading.Lock()

    def _key(self, frame: CanFdFrame) -> tuple:
        return (self.direction, *frame.normalized_key())

    def mark_tx(self, frame: CanFdFrame) -> None:
        now = time.monotonic_ns() // 1000
        with self.lock:
            self.items.append((now, self._key(frame)))
            self._gc(now)

    def is_echo(self, frame: CanFdFrame) -> bool:
        now = time.monotonic_ns() // 1000
        key = self._key(frame)
        with self.lock:
            self._gc(now)
            for index, (_, saved) in enumerate(self.items):
                if saved == key:
                    del self.items[index]  # consume once; never blacklist periodic frames
                    return True
            return False

    def _gc(self, now_us: int) -> None:
        while self.items and now_us - self.items[0][0] > self.window_us:
            self.items.popleft()
        while len(self.items) > self.max_items:
            self.items.popleft()


@dataclass
class BridgeStats:
    vector_rx: int = 0
    vector_tx: int = 0
    zlg_rx: int = 0
    zlg_tx: int = 0
    dropped: int = 0
    queue_overflow: int = 0
    loop_filtered: int = 0
    conversion_failed: int = 0
    reconnects: int = 0
    last_error: str = ""
    running: bool = False
    vector_state: str = "closed"
    zlg_state: str = "closed"
    mode: str = "unknown"
    vector_init_owner: str = "CANoe"


class BridgeCore:
    def __init__(
        self,
        vector: CanAdapter,
        zlg: CanAdapter,
        logger: logging.Logger,
        echo_suppression: bool = True,
        echo_window_ms: int = 5,
        name: str = "CH0",
        queue_size: int = 1024,
        reconnect_initial_s: float = 0.25,
        reconnect_max_s: float = 5.0,
    ) -> None:
        self.vector = vector
        self.zlg = zlg
        self.log = logger
        self.name = name
        self.stop_event = threading.Event()
        self.worker_stop = threading.Event()
        self.reconnect_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.supervisor: Optional[threading.Thread] = None
        # Direction-aware driver events are authoritative. Content matching is
        # only enabled on the receiving side when that adapter cannot guarantee it.
        self.filter_vector_echo = echo_suppression and not bool(getattr(vector, "rx_only", False))
        self.filter_zlg_echo = echo_suppression and not bool(getattr(zlg, "rx_only", False))
        self.v2z_echo = EchoSuppressor("zlg_rx_after_vector_tx", echo_window_ms)
        self.z2v_echo = EchoSuppressor("vector_rx_after_zlg_tx", echo_window_ms)
        self.v2z_queue: queue.Queue[CanFdFrame] = queue.Queue(maxsize=max(1, queue_size))
        self.z2v_queue: queue.Queue[CanFdFrame] = queue.Queue(maxsize=max(1, queue_size))
        self.stats = BridgeStats()
        vector_fd = bool(getattr(vector, "can_fd_enabled", False))
        zlg_fd = bool(getattr(zlg, "can_fd_enabled", False))
        if vector_fd == zlg_fd:
            self.stats.mode = "CAN FD" if vector_fd else "Classic CAN"
        else:
            vector_mode = "CAN FD" if vector_fd else "Classic"
            zlg_mode = "CAN FD" if zlg_fd else "Classic"
            self.stats.mode = f"Vector {vector_mode} / ZLG {zlg_mode}"
        self.stats.vector_init_owner = "CANoe" if getattr(vector, "channel_owner", "canoe") == "canoe" else "Bridge"
        self.stats_lock = threading.Lock()
        self.lifecycle_lock = threading.Lock()
        self.reconnect_initial_s = max(0.01, reconnect_initial_s)
        self.reconnect_max_s = max(self.reconnect_initial_s, reconnect_max_s)

    @property
    def count_v2z(self) -> int:
        return self.stats.zlg_tx

    @property
    def count_z2v(self) -> int:
        return self.stats.vector_tx

    @property
    def count_drop(self) -> int:
        return self.stats.dropped

    def status(self) -> dict:
        with self.stats_lock:
            return asdict(self.stats)

    def start(self) -> None:
        with self.lifecycle_lock:
            if self.stats.running:
                return
            self.stop_event.clear()
            self._open_adapters()
            self._start_workers()
            with self.stats_lock:
                self.stats.running = True
            self.supervisor = threading.Thread(
                target=self._supervisor_loop, name=f"{self.name}-Supervisor", daemon=True
            )
            self.supervisor.start()
        self.log.info("[%s] Bridge started with bounded queues (size=%d)", self.name, self.v2z_queue.maxsize)

    def stop(self) -> None:
        self.stop_event.set()
        self.worker_stop.set()  # stop reception and forwarding first
        self.reconnect_event.set()
        self._join_workers()
        self._clear_queues()
        if self.supervisor and self.supervisor is not threading.current_thread():
            self.supervisor.join(timeout=2.0)
        with self.lifecycle_lock:
            self._close_adapters()
            with self.stats_lock:
                self.stats.running = False
        alive = [t.name for t in self.threads if t.is_alive()]
        if alive:
            self._record_error(f"worker threads did not stop: {alive}")
        state = self.status()
        self.log.info(
            "[%s] Stopped. vector_rx=%d vector_tx=%d zlg_rx=%d zlg_tx=%d "
            "drop=%d overflow=%d loop_filtered=%d",
            self.name, state["vector_rx"], state["vector_tx"], state["zlg_rx"], state["zlg_tx"],
            state["dropped"], state["queue_overflow"], state["loop_filtered"],
        )

    def _open_adapters(self) -> None:
        self.log.info("[%s] Opening Vector side...", self.name)
        self.vector.open()
        with self.stats_lock:
            self.stats.vector_state = "active"
        try:
            self.log.info("[%s] Opening ZLG side...", self.name)
            self.zlg.open()
            with self.stats_lock:
                self.stats.zlg_state = "active"
        except Exception:
            try:
                self.zlg.close()
            finally:
                self.vector.close()
            with self.stats_lock:
                self.stats.vector_state = "closed"
            raise

    def _close_adapters(self) -> None:
        # Each adapter deactivates its channel, closes its port/device, then its driver.
        zlg_error: Optional[Exception] = None
        try:
            self.zlg.close()
        except Exception as exc:
            zlg_error = exc
        finally:
            with self.stats_lock:
                self.stats.zlg_state = "closed"
        try:
            self.vector.close()
        finally:
            with self.stats_lock:
                self.stats.vector_state = "closed"
        if zlg_error is not None:
            raise zlg_error

    def _start_workers(self) -> None:
        self.worker_stop.clear()
        self.threads = [
            threading.Thread(target=self._rx_loop, args=(self.vector, self.v2z_queue, "vector"), name=f"{self.name}-VectorRX", daemon=True),
            threading.Thread(target=self._tx_loop, args=(self.zlg, self.v2z_queue, "zlg"), name=f"{self.name}-ZlgTX", daemon=True),
            threading.Thread(target=self._rx_loop, args=(self.zlg, self.z2v_queue, "zlg"), name=f"{self.name}-ZlgRX", daemon=True),
            threading.Thread(target=self._tx_loop, args=(self.vector, self.z2v_queue, "vector"), name=f"{self.name}-VectorTX", daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def _join_workers(self) -> None:
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)

    def _rx_loop(self, adapter: CanAdapter, output: queue.Queue[CanFdFrame], side: str) -> None:
        while not self.stop_event.is_set() and not self.worker_stop.is_set():
            try:
                frame = adapter.receive(timeout_ms=20)
                if frame is None:
                    continue
                frame.direction = f"{side}_rx"
                suppressor = self.z2v_echo if side == "vector" else self.v2z_echo
                use_fallback = self.filter_vector_echo if side == "vector" else self.filter_zlg_echo
                if use_fallback and suppressor.is_echo(frame):
                    self._inc("loop_filtered")
                    self._inc("dropped")
                    continue
                self._inc(f"{side}_rx")
                try:
                    output.put_nowait(frame)
                except queue.Full:
                    # Explicit drop-newest policy keeps older bus ordering intact.
                    self._inc("queue_overflow")
                    self._inc("dropped")
            except Exception as exc:
                if isinstance(exc, ValueError):
                    self._inc("conversion_failed")
                    self._inc("dropped")
                    self._record_error(f"{side} receive conversion: {exc}")
                    continue
                self._worker_failed(side, "receive", exc)
                return

    def _tx_loop(self, adapter: CanAdapter, source: queue.Queue[CanFdFrame], side: str) -> None:
        while not self.stop_event.is_set() and not self.worker_stop.is_set():
            try:
                frame = source.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                adapter.send(frame)
                frame.direction = f"{side}_tx"
                self._inc(f"{side}_tx")
                if side == "zlg" and self.filter_zlg_echo:
                    self.v2z_echo.mark_tx(frame)
                elif side == "vector" and self.filter_vector_echo:
                    self.z2v_echo.mark_tx(frame)
            except Exception as exc:
                if isinstance(exc, ValueError):
                    self._inc("conversion_failed")
                    self._inc("dropped")
                    self._record_error(f"{side} send conversion: {exc}")
                    continue
                self._worker_failed(side, "send", exc)
                return
            finally:
                source.task_done()

    def _worker_failed(self, side: str, operation: str, exc: Exception) -> None:
        self._record_error(f"{side} {operation}: {exc}")
        with self.stats_lock:
            setattr(self.stats, f"{side}_state", "error")
        self.worker_stop.set()
        self.reconnect_event.set()

    def _supervisor_loop(self) -> None:
        delay = self.reconnect_initial_s
        while not self.stop_event.is_set():
            if not self.reconnect_event.wait(timeout=0.2):
                continue
            self.reconnect_event.clear()
            if self.stop_event.is_set():
                return
            self._join_workers()
            self._clear_queues()
            with self.lifecycle_lock:
                try:
                    self._close_adapters()
                except Exception as exc:
                    self._record_error(f"close before reconnect: {exc}")
            while not self.stop_event.wait(delay):
                try:
                    with self.lifecycle_lock:
                        self._open_adapters()
                        self._start_workers()
                    self._inc("reconnects")
                    self.log.info("[%s] Reconnected after adapter failure", self.name)
                    delay = self.reconnect_initial_s
                    break
                except Exception as exc:
                    self._record_error(f"reconnect failed: {exc}")
                    delay = min(delay * 2, self.reconnect_max_s)

    def _clear_queues(self) -> None:
        for pending in (self.v2z_queue, self.z2v_queue):
            while True:
                try:
                    pending.get_nowait()
                    pending.task_done()
                    self._inc("dropped")
                except queue.Empty:
                    break

    def _inc(self, field: str, amount: int = 1) -> None:
        with self.stats_lock:
            setattr(self.stats, field, getattr(self.stats, field) + amount)

    def _record_error(self, message: str) -> None:
        with self.stats_lock:
            self.stats.last_error = message
        self.log.error("[%s] %s", self.name, message)
