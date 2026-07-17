from __future__ import annotations
import argparse
import json
import signal
import time
from pathlib import Path
from typing import Any

from zlg_canoe_bridge.logger import setup_logger
from zlg_canoe_bridge.bridge import BridgeCore
from zlg_canoe_bridge.adapters.mock import MockAdapter
from zlg_canoe_bridge.frame import CanFdFrame


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def channel_configs(cfg: dict) -> list[dict]:
    channels = cfg.get("channels")
    if not channels:
        return [{
            "name": "CH0",
            "enabled": True,
            "canfd": cfg.get("canfd", {}),
            "vector": cfg.get("vector", {}),
            "zlg": cfg.get("zlg", {}),
        }]

    result = []
    global_defaults = {
        "canfd": cfg.get("canfd", {}),
        "vector": cfg.get("vector", {}),
        "zlg": cfg.get("zlg", {}),
    }
    for index, channel in enumerate(channels):
        if not channel.get("enabled", True):
            continue
        merged = _deep_merge(global_defaults, channel)
        # A per-channel legacy alias must override a global new-style value.
        # Without this normalization, global vector.channel=0 would silently
        # win over channels[n].vector.applicationChannel=1.
        vector_override = channel.get("vector", {})
        merged_vector = merged.setdefault("vector", {})
        alias_pairs = (
            ("applicationChannel", "channel"),
            ("applicationName", "app_name"),
            ("channelOwner", "channel_owner"),
            ("sharedVirtualChannel", "shared_virtual_channel"),
        )
        for legacy, current in alias_pairs:
            if current not in vector_override and legacy in vector_override:
                merged_vector[current] = vector_override[legacy]
        merged.setdefault("name", f"CH{index}")
        result.append(merged)
    if not result:
        raise ValueError("No enabled bridge channels configured")
    return result


def build_adapters(cfg: dict, channel: dict):
    mode = cfg.get("mode", "native").lower()
    if mode == "mock":
        v = MockAdapter("vector")
        z = MockAdapter("zlg")
        # Do NOT connect peers here; the BridgeCore is the connection.
        return v, z

    from zlg_canoe_bridge.adapters.vector_xl import VectorXLAdapter
    from zlg_canoe_bridge.adapters.zlg_zcan import ZlgZcanAdapter

    common_fd = channel.get("canfd", {})
    vcfg = dict(common_fd)
    vcfg.update(channel.get("vector", {}))
    zcfg = dict(common_fd)
    zcfg.update(channel.get("zlg", {}))
    return VectorXLAdapter(vcfg), ZlgZcanAdapter(zcfg)


def main() -> int:
    parser = argparse.ArgumentParser(description="ZLG-CANoe CANFD bridge implemented in Python")
    parser.add_argument("config", nargs="?", default="config/bridge_config.json", help="bridge_config.json path")
    parser.add_argument("--mock-inject", action="store_true", help="mock mode: inject one CANoe request and one ECU response")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log = setup_logger(cfg.get("logDir", "logs"), cfg.get("logLevel", "INFO"))

    log.info("ZLG-CANoe Python CANFD Bridge")
    log.info("config=%s", Path(args.config).resolve())

    bridges = []
    mock_pairs = []
    for channel in channel_configs(cfg):
        vector, zlg = build_adapters(cfg, channel)
        bridge = BridgeCore(
            vector,
            zlg,
            log,
            echo_suppression=bool(channel.get("echoSuppression", cfg.get("echoSuppression", True))),
            echo_window_ms=int(channel.get("echoWindowMs", cfg.get("echoWindowMs", 5))),
            name=str(channel.get("name", "CH0")),
            queue_size=int(channel.get("queueSize", cfg.get("queueSize", 1024))),
            reconnect_initial_s=float(cfg.get("reconnectInitialMs", 250)) / 1000.0,
            reconnect_max_s=float(cfg.get("reconnectMaxMs", 5000)) / 1000.0,
        )
        bridges.append(bridge)
        if cfg.get("mode", "native").lower() == "mock":
            mock_pairs.append((vector, zlg))

    stop = False

    def _sigint(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    try:
        for bridge in bridges:
            bridge.start()
        if args.mock_inject and cfg.get("mode", "native").lower() == "mock":
            # Inject frames into adapters to demonstrate log flow.
            vector, zlg = mock_pairs[0]
            assert isinstance(vector, MockAdapter) and isinstance(zlg, MockAdapter)
            vector.inject(CanFdFrame(0x7F1, bytes.fromhex("02 10 03 00 00 00 00 00"), is_fd=True, brs=True))
            time.sleep(0.05)
            zlg.inject(CanFdFrame(0x7F9, bytes.fromhex("02 50 03 00 00 00 00 00"), is_fd=True, brs=True))
        while not stop:
            time.sleep(0.2)
    finally:
        for bridge in reversed(bridges):
            bridge.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
