from __future__ import annotations

import argparse
import copy
import json
import logging
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from zlg_canoe_bridge.__main__ import build_adapters, channel_configs, load_config
from zlg_canoe_bridge.bridge import BridgeCore
from zlg_canoe_bridge.canoe_control import close_canoe, find_canoe_exe, start_canoe
from zlg_canoe_bridge.license import current_license, generate_license, machine_id, register_license


APP_TITLE = "ZLG-CANoe CANFD Bridge"


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put(self.format(record))
        except Exception:
            self.handleError(record)


class BridgeRuntime:
    def __init__(self, cfg: dict[str, Any], logger: logging.Logger) -> None:
        self.cfg = cfg
        self.log = logger
        self.bridges: list[BridgeCore] = []
        self.started_bridges: list[BridgeCore] = []
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.status = "stopped"
        self.error = ""

    def start(self) -> None:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.status = "starting"
            self.error = ""
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, name="BridgeRuntime", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        with self.lock:
            if self.status in ("running", "starting"):
                self.status = "stopping"
            self.stop_event.set()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            status = self.status
            error = self.error
        return {
            "status": status,
            "error": error,
            "bridges": [
                {
                    "name": bridge.name,
                    "v2z": bridge.count_v2z,
                    "z2v": bridge.count_z2v,
                    "drop": bridge.count_drop,
                }
                for bridge in self.bridges
            ],
        }

    def _set_status(self, status: str, error: str = "") -> None:
        with self.lock:
            self.status = status
            self.error = error

    def _run(self) -> None:
        self.bridges = []
        self.started_bridges = []
        try:
            for channel in channel_configs(self.cfg):
                vector, zlg = build_adapters(self.cfg, channel)
                bridge = BridgeCore(
                    vector,
                    zlg,
                    self.log,
                    echo_suppression=bool(channel.get("echoSuppression", self.cfg.get("echoSuppression", True))),
                    echo_window_ms=int(channel.get("echoWindowMs", self.cfg.get("echoWindowMs", 50))),
                    name=str(channel.get("name", "CH0")),
                )
                self.bridges.append(bridge)

            for bridge in self.bridges:
                bridge.start()
                self.started_bridges.append(bridge)

            self._set_status("running")
            self.log.info("Bridge runtime is running.")
            while not self.stop_event.is_set():
                time.sleep(0.1)
        except Exception as exc:
            self.log.exception("Bridge runtime failed: %s", exc)
            self._set_status("error", str(exc))
        finally:
            for bridge in reversed(self.started_bridges):
                try:
                    bridge.stop()
                except Exception as exc:
                    self.log.exception("Error while stopping %s: %s", bridge.name, exc)
            if self.snapshot()["status"] != "error":
                self._set_status("stopped")
            self.log.info("Bridge runtime stopped.")


class BridgeGui(tk.Tk):
    def __init__(self, config_path: str) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self._set_window_icon()
        self.geometry("1120x760")
        self.minsize(980, 640)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.logger = self._create_logger("logs", "INFO")
        self.runtime: BridgeRuntime | None = None
        self.canoe_process: subprocess.Popen | None = None
        self.settings_window: tk.Toplevel | None = None
        self.pending_canoe_autostart = False
        self.cfg: dict[str, Any] = {}
        self.config_path = tk.StringVar(value=str(Path(config_path).resolve()))

        self.status_text = tk.StringVar(value="Stopped")
        self.counter_text = tk.StringVar(value="CH0 v2z=0 z2v=0 drop=0")
        self.license_text = tk.StringVar(value="License: Unregistered")

        self.mode = tk.StringVar(value="native")
        self.log_level = tk.StringVar(value="INFO")
        self.vector_dll = tk.StringVar(value="vxlapi64.dll")
        self.vector_app = tk.StringVar(value="ZLG_CANOE_BRIDGE")
        self.vector_channel = tk.StringVar(value="0")
        self.zlg_dll = tk.StringVar(value="zlgcan.dll")
        self.zlg_device_type = tk.StringVar(value="41")
        self.zlg_device_index = tk.StringVar(value="0")
        self.zlg_channel = tk.StringVar(value="0")
        self.arb_bitrate = tk.StringVar(value="500000")
        self.data_bitrate = tk.StringVar(value="2000000")
        self.tx_timeout = tk.StringVar(value="1000")
        self.echo_window = tk.StringVar(value="50")
        self.iso_canfd = tk.BooleanVar(value=True)
        self.can_fd_enabled = tk.BooleanVar(value=False)
        self.brs = tk.BooleanVar(value=True)
        self.force_fd = tk.BooleanVar(value=False)
        self.termination = tk.BooleanVar(value=False)
        self.echo_suppression = tk.BooleanVar(value=True)
        self.canoe_exe = tk.StringVar(value=find_canoe_exe())
        self.canoe_config = tk.StringVar(value="")
        self.canoe_auto_start = tk.BooleanVar(value=True)

        self._build_widgets()
        self._load_from_path()
        self.after(100, self._poll_runtime)
        self.after(100, self._poll_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self) -> None:
        icon_path = _resource_path("assets/app_icon.ico")
        if icon_path.is_file():
            try:
                self.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

    def _create_logger(self, log_dir: str, level: str) -> logging.Logger:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("ZLG_CANoe_Bridge_GUI")
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()
        formatter = logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", "%H:%M:%S")

        queue_handler = QueueLogHandler(self.log_queue)
        queue_handler.setFormatter(formatter)
        logger.addHandler(queue_handler)

        file_handler = logging.FileHandler(Path(log_dir) / "bridge_gui.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        return logger

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=tk.X)
        ttk.Label(header, text=APP_TITLE, font=("", 16, "bold")).pack(side=tk.LEFT)
        ttk.Button(header, text="Floating Settings", command=self._show_settings_dialog).pack(side=tk.RIGHT)
        ttk.Button(header, text="License", command=self._show_license_dialog).pack(side=tk.RIGHT, padx=(0, 8))

        status = ttk.Frame(root)
        status.pack(fill=tk.X, pady=(16, 8))
        self.start_button = ttk.Button(status, text="Start Bridge", command=self._start_bridge)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(status, text="Pause Bridge", command=self._pause_bridge, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 16))
        ttk.Button(status, text="Start CANoe", command=self._start_canoe).pack(side=tk.LEFT)
        ttk.Button(status, text="Close CANoe", command=self._close_canoe).pack(side=tk.LEFT, padx=(8, 16))
        ttk.Label(status, textvariable=self.status_text, font=("", 11, "bold")).pack(side=tk.LEFT)
        ttk.Label(status, textvariable=self.license_text).pack(side=tk.RIGHT)

        counters = ttk.LabelFrame(root, text="Runtime Counters")
        counters.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(counters, textvariable=self.counter_text, font=("", 11)).pack(anchor=tk.W, padx=10, pady=10)

        logs = ttk.Frame(root)
        logs.pack(fill=tk.BOTH, expand=True)
        self._build_logs(logs)

    def _build_settings(self, parent: ttk.Frame) -> None:
        general = ttk.LabelFrame(parent, text="Bridge")
        general.pack(fill=tk.X, pady=(0, 10))
        self._row(general, 0, "Mode", ttk.Combobox(general, textvariable=self.mode, values=("native", "mock"), state="readonly"))
        self._row(general, 1, "Log level", ttk.Combobox(general, textvariable=self.log_level, values=("DEBUG", "INFO", "WARNING", "ERROR"), state="readonly"))
        self._row(general, 2, "Echo window ms", ttk.Entry(general, textvariable=self.echo_window))
        ttk.Checkbutton(general, text="Echo suppression", variable=self.echo_suppression).grid(row=3, column=1, sticky=tk.W, padx=8, pady=4)

        canoe = ttk.LabelFrame(parent, text="CANoe")
        canoe.pack(fill=tk.X, pady=(0, 10))
        self._row(canoe, 0, "CANoe exe", ttk.Entry(canoe, textvariable=self.canoe_exe))
        self._row(canoe, 1, "CANoe config", ttk.Entry(canoe, textvariable=self.canoe_config))
        ttk.Button(canoe, text="Browse exe", command=self._browse_canoe_exe).grid(row=2, column=0, sticky=tk.EW, padx=8, pady=4)
        ttk.Button(canoe, text="Browse config", command=self._browse_canoe_config).grid(row=2, column=1, sticky=tk.W, padx=8, pady=4)
        ttk.Checkbutton(canoe, text="Open CANoe after bridge starts", variable=self.canoe_auto_start).grid(row=3, column=1, sticky=tk.W, padx=8, pady=4)

        vector = ttk.LabelFrame(parent, text="Vector Virtual CAN/CANFD")
        vector.pack(fill=tk.X, pady=(0, 10))
        self._row(vector, 0, "DLL", ttk.Entry(vector, textvariable=self.vector_dll))
        self._row(vector, 1, "App name", ttk.Entry(vector, textvariable=self.vector_app))
        self._row(vector, 2, "App channel", ttk.Entry(vector, textvariable=self.vector_channel))

        zlg = ttk.LabelFrame(parent, text="ZLG ZCAN Hardware")
        zlg.pack(fill=tk.X, pady=(0, 10))
        self._row(zlg, 0, "DLL", ttk.Entry(zlg, textvariable=self.zlg_dll))
        self._row(zlg, 1, "Device type", ttk.Entry(zlg, textvariable=self.zlg_device_type))
        self._row(zlg, 2, "Device index", ttk.Entry(zlg, textvariable=self.zlg_device_index))
        self._row(zlg, 3, "Channel index", ttk.Entry(zlg, textvariable=self.zlg_channel))
        self._row(zlg, 4, "Tx timeout ms", ttk.Entry(zlg, textvariable=self.tx_timeout))
        ttk.Checkbutton(zlg, text="120 ohm termination", variable=self.termination).grid(row=5, column=1, sticky=tk.W, padx=8, pady=4)

        timing = ttk.LabelFrame(parent, text="CAN FD Timing")
        timing.pack(fill=tk.X)
        self._row(timing, 0, "Arb bitrate", ttk.Entry(timing, textvariable=self.arb_bitrate))
        self._row(timing, 1, "Data bitrate", ttk.Entry(timing, textvariable=self.data_bitrate))
        ttk.Checkbutton(timing, text="Enable CAN FD", variable=self.can_fd_enabled).grid(row=2, column=1, sticky=tk.W, padx=8, pady=4)
        ttk.Checkbutton(timing, text="ISO CAN FD", variable=self.iso_canfd).grid(row=3, column=1, sticky=tk.W, padx=8, pady=4)
        ttk.Checkbutton(timing, text="BRS", variable=self.brs).grid(row=4, column=1, sticky=tk.W, padx=8, pady=4)
        ttk.Checkbutton(timing, text="Force FD when Tx to CANoe", variable=self.force_fd).grid(row=5, column=1, sticky=tk.W, padx=8, pady=4)

        for frame in (general, canoe, vector, zlg, timing):
            frame.columnconfigure(1, weight=1)

    def _build_logs(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=tk.X)
        ttk.Label(toolbar, text="Runtime Log").pack(side=tk.LEFT)
        ttk.Button(toolbar, text="Clear", command=self._clear_logs).pack(side=tk.RIGHT)

        self.log_text = ScrolledText(parent, wrap=tk.WORD, height=30, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

    def _row(self, parent: ttk.Frame, row: int, label: str, widget: ttk.Widget) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, padx=8, pady=4)
        widget.grid(row=row, column=1, sticky=tk.EW, padx=8, pady=4)

    def _show_settings_dialog(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        dialog = tk.Toplevel(self)
        self.settings_window = dialog
        dialog.title("Floating Settings")
        dialog.geometry("620x760")
        dialog.transient(self)

        shell = ttk.Frame(dialog, padding=12)
        shell.pack(fill=tk.BOTH, expand=True)

        config_bar = ttk.LabelFrame(shell, text="Config File")
        config_bar.pack(fill=tk.X, pady=(0, 10))
        ttk.Entry(config_bar, textvariable=self.config_path).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, pady=8)
        ttk.Button(config_bar, text="Browse", command=self._browse_config).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(config_bar, text="Load", command=self._load_from_path).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(config_bar, text="Save", command=self._save_to_path).pack(side=tk.LEFT, padx=(0, 8))

        canvas = tk.Canvas(shell, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient=tk.VERTICAL, command=canvas.yview)
        content = ttk.Frame(canvas)
        content.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._build_settings(content)

        def on_close() -> None:
            self.settings_window = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", on_close)

    def _browse_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Select bridge config",
            filetypes=(("JSON config", "*.json"), ("All files", "*.*")),
            initialdir=str(Path(self.config_path.get()).resolve().parent),
        )
        if path:
            self.config_path.set(path)
            self._load_from_path()

    def _browse_canoe_exe(self) -> None:
        path = filedialog.askopenfilename(
            title="Select CANoe executable",
            filetypes=(("CANoe executable", "CANoe*.exe"), ("Executable", "*.exe"), ("All files", "*.*")),
            initialdir=str(Path(self.canoe_exe.get()).resolve().parent) if self.canoe_exe.get() else "C:\\",
        )
        if path:
            self.canoe_exe.set(path)

    def _browse_canoe_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Select CANoe configuration",
            filetypes=(("CANoe config", "*.cfg *.canoe"), ("All files", "*.*")),
        )
        if path:
            self.canoe_config.set(path)

    def _load_from_path(self) -> None:
        try:
            self.cfg = load_config(self.config_path.get())
            self._apply_cfg_to_form()
            self._append_log(f"Loaded config: {Path(self.config_path.get()).resolve()}")
            self._update_license_text()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Load config failed:\n{exc}")

    def _save_to_path(self) -> None:
        try:
            self.cfg = self._cfg_from_form()
            path = Path(self.config_path.get())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self._append_log(f"Saved config: {path.resolve()}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Save config failed:\n{exc}")

    def _apply_cfg_to_form(self) -> None:
        cfg = self.cfg
        channel = self._first_enabled_channel(cfg)
        canfd = self._merged(cfg.get("canfd", {}), channel.get("canfd", {}))
        vector = self._merged(cfg.get("vector", {}), channel.get("vector", {}))
        zlg = self._merged(cfg.get("zlg", {}), channel.get("zlg", {}))

        self.mode.set(str(cfg.get("mode", "native")))
        self.log_level.set(str(cfg.get("logLevel", "INFO")).upper())
        self.echo_suppression.set(bool(channel.get("echoSuppression", cfg.get("echoSuppression", True))))
        self.echo_window.set(str(channel.get("echoWindowMs", cfg.get("echoWindowMs", 50))))

        self.vector_dll.set(str(vector.get("dllPath", "vxlapi64.dll")))
        self.vector_app.set(str(vector.get("applicationName", "ZLG_CANOE_BRIDGE")))
        self.vector_channel.set(str(vector.get("applicationChannel", 0)))

        self.zlg_dll.set(str(zlg.get("dllPath", "zlgcan.dll")))
        self.zlg_device_type.set(str(zlg.get("deviceType", 41)))
        self.zlg_device_index.set(str(zlg.get("deviceIndex", 0)))
        self.zlg_channel.set(str(zlg.get("channelIndex", 0)))
        self.tx_timeout.set(str(zlg.get("txTimeoutMs", 1000)))
        self.termination.set(bool(zlg.get("enableTermination", False)))

        self.arb_bitrate.set(str(canfd.get("arbitrationBitrate", 500000)))
        self.data_bitrate.set(str(canfd.get("dataBitrate", 2000000)))
        self.can_fd_enabled.set(bool(canfd.get("canFdEnabled", False)))
        self.iso_canfd.set(bool(canfd.get("isoCanFd", True)))
        self.brs.set(bool(canfd.get("brs", True)))
        self.force_fd.set(bool(canfd.get("forceFd", False)))
        canoe = cfg.get("canoe", {})
        self.canoe_exe.set(str(canoe.get("exePath") or find_canoe_exe()))
        self.canoe_config.set(str(canoe.get("configPath") or ""))
        self.canoe_auto_start.set(bool(canoe.get("autoStartAfterBridge", True)))

    def _cfg_from_form(self) -> dict[str, Any]:
        cfg = copy.deepcopy(self.cfg) if self.cfg else {}
        cfg["mode"] = self.mode.get().strip() or "native"
        cfg["logLevel"] = self.log_level.get().strip() or "INFO"
        cfg["logDir"] = cfg.get("logDir", "logs")
        cfg["echoSuppression"] = bool(self.echo_suppression.get())
        cfg["echoWindowMs"] = self._int(self.echo_window.get(), "Echo window ms")

        cfg.setdefault("canfd", {})
        cfg["canfd"].update({
            "arbitrationBitrate": self._int(self.arb_bitrate.get(), "Arb bitrate"),
            "dataBitrate": self._int(self.data_bitrate.get(), "Data bitrate"),
            "canFdEnabled": bool(self.can_fd_enabled.get()),
            "brs": bool(self.brs.get()),
            "isoCanFd": bool(self.iso_canfd.get()),
            "forceFd": bool(self.force_fd.get()),
        })

        cfg.setdefault("vector", {})
        cfg["vector"].update({
            "dllPath": self.vector_dll.get().strip() or "vxlapi64.dll",
            "applicationName": self.vector_app.get().strip() or "ZLG_CANOE_BRIDGE",
            "receiveTxOk": False,
        })

        cfg.setdefault("zlg", {})
        cfg["zlg"].update({
            "dllPath": self.zlg_dll.get().strip() or "zlgcan.dll",
            "deviceType": self._int(self.zlg_device_type.get(), "Device type"),
            "deviceIndex": self._int(self.zlg_device_index.get(), "Device index"),
            "enableTermination": bool(self.termination.get()),
            "useSetValue": True,
            "txTimeoutMs": self._int(self.tx_timeout.get(), "Tx timeout ms"),
        })

        channels = cfg.setdefault("channels", [{"name": "CH0", "enabled": True}])
        if not channels:
            channels.append({"name": "CH0", "enabled": True})
        channel = channels[0]
        channel["name"] = channel.get("name", "CH0")
        channel["enabled"] = True
        channel.setdefault("vector", {})
        channel.setdefault("zlg", {})
        channel["vector"]["applicationChannel"] = self._int(self.vector_channel.get(), "App channel")
        channel["zlg"]["channelIndex"] = self._int(self.zlg_channel.get(), "ZLG channel index")
        cfg["canoe"] = {
            "exePath": self.canoe_exe.get().strip() or find_canoe_exe(),
            "configPath": self.canoe_config.get().strip(),
            "autoStartAfterBridge": bool(self.canoe_auto_start.get()),
        }
        return cfg

    def _start_bridge(self) -> None:
        try:
            lic = current_license()
            if not lic.valid:
                self._update_license_text()
                messagebox.showerror(APP_TITLE, f"License required before starting bridge.\nMachine ID: {machine_id()}")
                return
            self.cfg = self._cfg_from_form()
            self.logger = self._create_logger(self.cfg.get("logDir", "logs"), self.cfg.get("logLevel", "INFO"))
            self.runtime = BridgeRuntime(copy.deepcopy(self.cfg), self.logger)
            self.runtime.start()
            self.start_button.configure(state=tk.DISABLED)
            self.stop_button.configure(state=tk.NORMAL)
            self.status_text.set("Starting...")
            self._append_log("Start requested.")
            self.pending_canoe_autostart = bool(self.canoe_auto_start.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Start failed:\n{exc}")

    def _pause_bridge(self) -> None:
        if self.runtime:
            self.runtime.stop()
            self.status_text.set("Stopping...")
            self._append_log("Pause requested.")

    def _start_canoe(self) -> None:
        try:
            self.cfg = self._cfg_from_form()
            self.canoe_process = start_canoe(self.cfg)
            self._append_log(f"CANoe start requested: pid={self.canoe_process.pid}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Start CANoe failed:\n{exc}")

    def _close_canoe(self) -> None:
        try:
            count = close_canoe()
            self._append_log(f"CANoe close requested: windows={count}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Close CANoe failed:\n{exc}")

    def _show_license_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("License Registration")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("680x420")

        lic = current_license()
        owner = tk.StringVar(value=lic.owner or "WDJR")
        expires = tk.StringVar(value=lic.expires or "2099-12-31")
        key = tk.StringVar(value="")

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=f"Machine ID: {machine_id()}").pack(anchor=tk.W)
        ttk.Label(frame, text=f"Status: {lic.message}").pack(anchor=tk.W, pady=(0, 8))

        form = ttk.Frame(frame)
        form.pack(fill=tk.X)
        ttk.Label(form, text="Owner").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(form, textvariable=owner).grid(row=0, column=1, sticky=tk.EW, padx=8, pady=4)
        ttk.Label(form, text="Expires").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(form, textvariable=expires).grid(row=1, column=1, sticky=tk.EW, padx=8, pady=4)
        form.columnconfigure(1, weight=1)

        text = ScrolledText(frame, height=8, wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, pady=8)

        def generate() -> None:
            generated = generate_license(owner.get(), expires.get(), machine_id())
            text.delete("1.0", tk.END)
            text.insert(tk.END, generated)

        def register() -> None:
            candidate = text.get("1.0", tk.END).strip() or key.get().strip()
            info = register_license(candidate)
            self._update_license_text()
            if info.valid:
                messagebox.showinfo(APP_TITLE, f"Registered to {info.owner}, expires {info.expires}")
                dialog.destroy()
            else:
                messagebox.showerror(APP_TITLE, info.message)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Generate Local License", command=generate).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Register", command=register).pack(side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side=tk.RIGHT)

    def _update_license_text(self) -> None:
        lic = current_license()
        if lic.valid:
            self.license_text.set(f"License: {lic.owner} / {lic.expires}")
        else:
            self.license_text.set("License: Unregistered")

    def _poll_runtime(self) -> None:
        if self.runtime:
            snapshot = self.runtime.snapshot()
            status = snapshot["status"]
            if status == "running":
                self.status_text.set("Running")
                self.start_button.configure(state=tk.DISABLED)
                self.stop_button.configure(state=tk.NORMAL)
                if self.pending_canoe_autostart:
                    self.pending_canoe_autostart = False
                    self._start_canoe()
            elif status == "starting":
                self.status_text.set("Starting...")
            elif status == "stopping":
                self.status_text.set("Stopping...")
            elif status == "error":
                self.pending_canoe_autostart = False
                self.status_text.set(f"Error: {snapshot['error']}")
                self.start_button.configure(state=tk.NORMAL)
                self.stop_button.configure(state=tk.DISABLED)
            else:
                self.pending_canoe_autostart = False
                self.status_text.set("Stopped")
                self.start_button.configure(state=tk.NORMAL)
                self.stop_button.configure(state=tk.DISABLED)

            if snapshot["bridges"]:
                parts = [
                    f"{item['name']} v2z={item['v2z']} z2v={item['z2v']} drop={item['drop']}"
                    for item in snapshot["bridges"]
                ]
                self.counter_text.set(" | ".join(parts))
        self.after(200, self._poll_runtime)

    def _poll_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
        self.after(100, self._poll_logs)

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_logs(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self.runtime and self.runtime.snapshot()["status"] in ("running", "starting", "stopping"):
            self.runtime.stop()
            if self.runtime.thread:
                self.runtime.thread.join(timeout=3.0)
        self.destroy()

    def _first_enabled_channel(self, cfg: dict[str, Any]) -> dict[str, Any]:
        channels = cfg.get("channels") or []
        for channel in channels:
            if channel.get("enabled", True):
                return channel
        return channels[0] if channels else {}

    def _merged(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        result = dict(base)
        result.update(override)
        return result

    def _int(self, value: str, label: str) -> int:
        try:
            return int(str(value).strip(), 0)
        except ValueError as exc:
            raise ValueError(f"{label} must be an integer") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="ZLG-CANoe CANFD bridge GUI")
    parser.add_argument("config", nargs="?", default="config/bridge_config.json", help="bridge_config.json path")
    args = parser.parse_args()
    app = BridgeGui(args.config)
    app.mainloop()
    return 0


def _resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / relative


if __name__ == "__main__":
    raise SystemExit(main())
