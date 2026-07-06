from __future__ import annotations

import argparse
import copy
import ctypes as ct
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any

from zlg_canoe_bridge.__main__ import build_adapters, channel_configs, load_config
from zlg_canoe_bridge.bridge import BridgeCore
from zlg_canoe_bridge.canoe_control import close_canoe, find_canoe_exe, start_canoe
from zlg_canoe_bridge.license import current_license, generate_license, machine_id, register_license


APP_TITLE = "CANoe-ZLG CAN/CANFD 桥接工具"
APP_VERSION = "V1.0.0"
DEFAULT_CONFIG = "config/bridge_config.json"

BRIDGE_TEXT = {
    "stopped": "未启动",
    "starting": "启动中",
    "running": "运行中",
    "paused": "已暂停",
    "stopping": "停止中",
    "error": "异常",
}

MODE_TEXT = {"native": "原生桥接", "mock": "调试模式"}
MODE_VALUE = {value: key for key, value in MODE_TEXT.items()}


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: "queue.Queue[tuple[str, str]]") -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            created = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            message = _friendly_error(record.getMessage())
            self.log_queue.put((record.levelname, f"[{created}] [{record.levelname}] {message}"))
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
            self.log.info("桥接运行中：Vector Virtual CAN/CANFD 与 ZLG 硬件通道已打开。")
            while not self.stop_event.is_set():
                time.sleep(0.1)
        except Exception as exc:
            friendly = _friendly_error(str(exc))
            self.log.exception("桥接运行异常：%s", friendly)
            self._set_status("error", friendly)
        finally:
            for bridge in reversed(self.started_bridges):
                try:
                    bridge.stop()
                except Exception as exc:
                    self.log.exception("停止 %s 时发生异常：%s", bridge.name, _friendly_error(str(exc)))
            if self.snapshot()["status"] != "error":
                self._set_status("stopped")
            self.log.info("桥接已停止，Vector/ZLG 通道已释放。")


class BridgeGui(tk.Tk):
    def __init__(self, config_path: str) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self._set_window_icon()
        self.geometry("1180x780")
        self.minsize(1040, 680)
        self._configure_style()

        self.log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.logger = self._create_logger("logs", "INFO")
        self.runtime: BridgeRuntime | None = None
        self.canoe_process: subprocess.Popen | None = None
        self.settings_window: tk.Toplevel | None = None
        self.pending_canoe_autostart = False
        self.user_paused = False
        self.cfg: dict[str, Any] = {}
        self.config_path = tk.StringVar(value=str(Path(config_path).resolve()))

        self.auth_state = tk.StringVar(value="未授权")
        self.bridge_state = tk.StringVar(value="未启动")
        self.canoe_state = tk.StringVar(value="未连接")
        self.zlg_state = tk.StringVar(value="未连接")
        self.bus_state = tk.StringVar(value="未知")

        self.mode = tk.StringVar(value="原生桥接")
        self.log_level = tk.StringVar(value="INFO")
        self.vector_dll = tk.StringVar(value="vxlapi64.dll")
        self.vector_app = tk.StringVar(value="ZLG_CANOE_BRIDGE")
        self.vector_channel = tk.StringVar(value="0")
        self.vector_app_channel = tk.StringVar(value="0")
        self.zlg_dll = tk.StringVar(value="zlgcan.dll")
        self.zlg_model = tk.StringVar(value="USBCANFD-100U-mini，deviceType=43")
        self.zlg_device_type = tk.StringVar(value="43")
        self.zlg_device_index = tk.StringVar(value="0")
        self.zlg_channel = tk.StringVar(value="0")
        self.rx_queue_len = tk.StringVar(value="8192")
        self.arb_bitrate = tk.StringVar(value="500K")
        self.data_bitrate = tk.StringVar(value="2M")
        self.sample_point = tk.StringVar(value="80%")
        self.tx_timeout = tk.StringVar(value="1000")
        self.echo_window = tk.StringVar(value="50")
        self.reconnect_interval = tk.StringVar(value="1000")
        self.channel_name = tk.StringVar(value="CH0")
        self.can_mode = tk.StringVar(value="Classical CAN")
        self.iso_canfd = tk.BooleanVar(value=True)
        self.can_fd_enabled = tk.BooleanVar(value=False)
        self.brs = tk.BooleanVar(value=True)
        self.force_fd = tk.BooleanVar(value=False)
        self.termination = tk.BooleanVar(value=False)
        self.echo_suppression = tk.BooleanVar(value=True)
        self.auto_detect_zlg = tk.BooleanVar(value=True)
        self.auto_reconnect = tk.BooleanVar(value=False)
        self.channel_enabled = tk.BooleanVar(value=True)
        self.canoe_exe = tk.StringVar(value=find_canoe_exe())
        self.canoe_config = tk.StringVar(value="")
        self.canoe_auto_start = tk.BooleanVar(value=True)

        self.status_cards: dict[str, ttk.Label] = {}
        self.channel_rows: dict[str, str] = {}
        self.current_log_file: Path | None = None

        self._build_widgets()
        self._load_from_path(show_error=False)
        self._update_license_state()
        self._refresh_channel_table()
        self.after(100, self._poll_runtime)
        self.after(100, self._poll_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Subtle.TLabel", foreground="#5f6b7a")
        style.configure("CardTitle.TLabel", foreground="#5f6b7a", font=("Microsoft YaHei UI", 9))
        style.configure("CardValue.TLabel", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=28)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))

    def _set_window_icon(self) -> None:
        icon_path = _resource_path("assets/app_icon.ico")
        if icon_path.is_file():
            try:
                self.iconbitmap(str(icon_path))
            except tk.TclError:
                pass

    def _create_logger(self, log_dir: str, level: str, timestamped: bool = False) -> logging.Logger:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("ZLG_CANoe_Bridge_GUI")
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.propagate = False
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()

        queue_handler = QueueLogHandler(self.log_queue)
        logger.addHandler(queue_handler)

        if timestamped:
            filename = f"bridge_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        else:
            filename = "bridge_gui.log"
        self.current_log_file = log_path / filename
        file_handler = logging.FileHandler(self.current_log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)
        return logger

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root)
        header.pack(fill=tk.X)
        title_box = ttk.Frame(header)
        title_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(title_box, text=APP_TITLE, style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(title_box, text=f"版本号：{APP_VERSION}    授权状态：", style="Subtle.TLabel").pack(side=tk.LEFT)
        ttk.Label(title_box, textvariable=self.auth_state, font=("Microsoft YaHei UI", 10, "bold")).pack(side=tk.LEFT)

        ttk.Button(header, text="关于软件", command=self._show_about_dialog).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(header, text="授权管理", command=self._show_license_dialog).pack(side=tk.RIGHT, padx=(8, 0))

        cards = ttk.Frame(root)
        cards.pack(fill=tk.X, pady=(16, 12))
        self._status_card(cards, "桥接状态", self.bridge_state, 0)
        self._status_card(cards, "CANoe 状态", self.canoe_state, 1)
        self._status_card(cards, "ZLG 状态", self.zlg_state, 2)
        self._status_card(cards, "总线状态", self.bus_state, 3)
        for i in range(4):
            cards.columnconfigure(i, weight=1, uniform="cards")

        actions = ttk.LabelFrame(root, text="操作")
        actions.pack(fill=tk.X, pady=(0, 12))
        self._build_actions(actions)

        table_frame = ttk.LabelFrame(root, text="通道状态")
        table_frame.pack(fill=tk.X, pady=(0, 12))
        self._build_channel_table(table_frame)

        logs = ttk.LabelFrame(root, text="运行日志")
        logs.pack(fill=tk.BOTH, expand=True)
        self._build_logs(logs)

    def _status_card(self, parent: ttk.Frame, title: str, variable: tk.StringVar, column: int) -> None:
        frame = ttk.Frame(parent, padding=12, relief=tk.RIDGE)
        frame.grid(row=0, column=column, sticky=tk.EW, padx=(0 if column == 0 else 8, 0))
        ttk.Label(frame, text=title, style="CardTitle.TLabel").pack(anchor=tk.W)
        value = ttk.Label(frame, textvariable=variable, style="CardValue.TLabel")
        value.pack(anchor=tk.W, pady=(6, 0))
        self.status_cards[title] = value

    def _build_actions(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent, padding=8)
        row.pack(fill=tk.X)
        self.start_button = ttk.Button(row, text="启动桥接", style="Primary.TButton", command=self._start_bridge)
        self.pause_button = ttk.Button(row, text="暂停桥接", command=self._pause_bridge, state=tk.DISABLED)
        self.stop_button = ttk.Button(row, text="停止桥接", command=self._stop_bridge, state=tk.DISABLED)
        self.start_canoe_button = ttk.Button(row, text="启动 CANoe", command=self._start_canoe)
        self.close_canoe_button = ttk.Button(row, text="关闭 CANoe", command=self._close_canoe)
        self.detect_button = ttk.Button(row, text="设备检测", command=lambda: self._detect_device(show_popup=True))
        self.settings_button = ttk.Button(row, text="参数设置", command=self._show_settings_dialog)
        self.save_log_button = ttk.Button(row, text="保存日志", command=self._save_log)
        self.clear_log_button = ttk.Button(row, text="清空日志", command=self._clear_logs)

        buttons = [
            self.start_button,
            self.pause_button,
            self.stop_button,
            self.start_canoe_button,
            self.close_canoe_button,
            self.detect_button,
            self.settings_button,
            self.save_log_button,
            self.clear_log_button,
        ]
        for index, button in enumerate(buttons):
            button.grid(row=0, column=index, sticky=tk.EW, padx=4, pady=2)
            row.columnconfigure(index, weight=1)

    def _build_channel_table(self, parent: ttk.Frame) -> None:
        columns = ("channel", "mode", "arb", "data", "v2z", "z2v", "drop", "status")
        self.channel_table = ttk.Treeview(parent, columns=columns, show="headings", height=4)
        headings = {
            "channel": "通道",
            "mode": "模式",
            "arb": "仲裁波特率",
            "data": "数据波特率",
            "v2z": "CANoe→ZLG",
            "z2v": "ZLG→CANoe",
            "drop": "丢帧",
            "status": "状态",
        }
        widths = {
            "channel": 90,
            "mode": 150,
            "arb": 120,
            "data": 120,
            "v2z": 120,
            "z2v": 120,
            "drop": 80,
            "status": 120,
        }
        for col in columns:
            self.channel_table.heading(col, text=headings[col])
            self.channel_table.column(col, width=widths[col], anchor=tk.CENTER)
        self.channel_table.pack(fill=tk.X, padx=8, pady=8)

    def _build_logs(self, parent: ttk.Frame) -> None:
        self.log_text = ScrolledText(parent, wrap=tk.WORD, height=18, state=tk.DISABLED, font=("Consolas", 10))
        self.log_text.tag_configure("ERROR", foreground="#b00020")
        self.log_text.tag_configure("WARNING", foreground="#a36b00")
        self.log_text.tag_configure("INFO", foreground="#1f4e79")
        self.log_text.tag_configure("DEBUG", foreground="#555555")
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _build_settings(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True)

        basic = ttk.Frame(notebook, padding=12)
        canoe = ttk.Frame(notebook, padding=12)
        zlg = ttk.Frame(notebook, padding=12)
        can = ttk.Frame(notebook, padding=12)
        advanced = ttk.Frame(notebook, padding=12)

        notebook.add(basic, text="基本设置")
        notebook.add(canoe, text="CANoe 设置")
        notebook.add(zlg, text="ZLG 硬件设置")
        notebook.add(can, text="CAN / CAN FD 参数")
        notebook.add(advanced, text="高级设置")

        self._row(basic, 0, "桥接模式", ttk.Combobox(basic, textvariable=self.mode, values=("原生桥接", "调试模式"), state="readonly"))
        self._row(basic, 1, "日志等级", ttk.Combobox(basic, textvariable=self.log_level, values=("DEBUG", "INFO", "WARNING", "ERROR"), state="readonly"))
        ttk.Checkbutton(basic, text="启动桥接后自动打开 CANoe", variable=self.canoe_auto_start).grid(row=2, column=1, sticky=tk.W, padx=8, pady=5)
        ttk.Checkbutton(basic, text="启动时自动检测 ZLG 设备", variable=self.auto_detect_zlg).grid(row=3, column=1, sticky=tk.W, padx=8, pady=5)
        ttk.Checkbutton(basic, text="启用回环抑制", variable=self.echo_suppression).grid(row=4, column=1, sticky=tk.W, padx=8, pady=5)
        self._row(basic, 5, "回环抑制时间窗口 ms", ttk.Entry(basic, textvariable=self.echo_window))

        self._row(canoe, 0, "CANoe 程序路径", ttk.Entry(canoe, textvariable=self.canoe_exe))
        self._row(canoe, 1, "CANoe 工程路径", ttk.Entry(canoe, textvariable=self.canoe_config))
        self._row(canoe, 2, "Vector 应用名称", ttk.Entry(canoe, textvariable=self.vector_app))
        self._row(canoe, 3, "Vector 应用通道", ttk.Entry(canoe, textvariable=self.vector_channel))
        ttk.Button(canoe, text="浏览 CANoe", command=self._browse_canoe_exe).grid(row=4, column=0, sticky=tk.EW, padx=8, pady=8)
        ttk.Button(canoe, text="浏览工程", command=self._browse_canoe_config).grid(row=4, column=1, sticky=tk.W, padx=8, pady=8)
        ttk.Button(canoe, text="检测 CANoe 配置", command=self._check_canoe_config).grid(row=4, column=1, sticky=tk.E, padx=8, pady=8)

        self._row(zlg, 0, "ZLG DLL 路径", ttk.Entry(zlg, textvariable=self.zlg_dll))
        model = ttk.Combobox(zlg, textvariable=self.zlg_model, values=("USBCANFD-100U-mini，deviceType=43", "自定义"), state="readonly")
        model.bind("<<ComboboxSelected>>", lambda _event: self._on_zlg_model_changed())
        self._row(zlg, 1, "ZLG 设备型号", model)
        self._row(zlg, 2, "设备类型 deviceType", ttk.Entry(zlg, textvariable=self.zlg_device_type))
        self._row(zlg, 3, "设备索引 deviceIndex", ttk.Entry(zlg, textvariable=self.zlg_device_index))
        self._row(zlg, 4, "通道索引 channelIndex", ttk.Entry(zlg, textvariable=self.zlg_channel))
        ttk.Button(zlg, text="浏览 DLL", command=self._browse_zlg_dll).grid(row=5, column=0, sticky=tk.EW, padx=8, pady=8)
        ttk.Button(zlg, text="检测设备", command=lambda: self._detect_device(show_popup=True)).grid(row=5, column=1, sticky=tk.W, padx=8, pady=8)

        ttk.Checkbutton(can, text="通道启用", variable=self.channel_enabled).grid(row=0, column=1, sticky=tk.W, padx=8, pady=5)
        self._row(can, 1, "通道号", ttk.Combobox(can, textvariable=self.channel_name, values=("CH0", "CH1"), state="readonly"))
        can_mode_box = ttk.Combobox(can, textvariable=self.can_mode, values=("Classical CAN", "CAN FD"), state="readonly")
        can_mode_box.bind("<<ComboboxSelected>>", lambda _event: self._on_can_mode_changed())
        self._row(can, 2, "CAN 模式", can_mode_box)
        self._row(can, 3, "仲裁波特率", ttk.Combobox(can, textvariable=self.arb_bitrate, values=("125K", "250K", "500K", "1M", "自定义")))
        self._row(can, 4, "数据波特率", ttk.Combobox(can, textvariable=self.data_bitrate, values=("500K", "1M", "2M", "4M", "5M", "自定义")))
        ttk.Checkbutton(can, text="BRS：启用", variable=self.brs).grid(row=5, column=1, sticky=tk.W, padx=8, pady=5)
        ttk.Checkbutton(can, text="ISO CAN FD：启用", variable=self.iso_canfd).grid(row=6, column=1, sticky=tk.W, padx=8, pady=5)
        self._row(can, 7, "采样点", ttk.Entry(can, textvariable=self.sample_point))
        ttk.Checkbutton(can, text="终端电阻：仅提示，不强制配置", variable=self.termination).grid(row=8, column=1, sticky=tk.W, padx=8, pady=5)

        self._row(advanced, 0, "Vector DLL 名称", ttk.Entry(advanced, textvariable=self.vector_dll))
        self._row(advanced, 1, "Vector App Channel", ttk.Entry(advanced, textvariable=self.vector_app_channel))
        self._row(advanced, 2, "ZLG 接收队列长度", ttk.Entry(advanced, textvariable=self.rx_queue_len))
        self._row(advanced, 3, "发送超时时间", ttk.Entry(advanced, textvariable=self.tx_timeout))
        ttk.Checkbutton(advanced, text="自动重连开关", variable=self.auto_reconnect).grid(row=4, column=1, sticky=tk.W, padx=8, pady=5)
        self._row(advanced, 5, "自动重连间隔", ttk.Entry(advanced, textvariable=self.reconnect_interval))

        for frame in (basic, canoe, zlg, can, advanced):
            frame.columnconfigure(1, weight=1)

    def _row(self, parent: ttk.Frame, row: int, label: str, widget: tk.Widget) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, padx=8, pady=5)
        widget.grid(row=row, column=1, sticky=tk.EW, padx=8, pady=5)

    def _show_settings_dialog(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        dialog = tk.Toplevel(self)
        self.settings_window = dialog
        dialog.title("参数设置")
        dialog.geometry("780x720")
        dialog.minsize(720, 620)
        dialog.transient(self)

        shell = ttk.Frame(dialog, padding=12)
        shell.pack(fill=tk.BOTH, expand=True)

        config_bar = ttk.LabelFrame(shell, text="配置文件")
        config_bar.pack(fill=tk.X, pady=(0, 10))
        ttk.Entry(config_bar, textvariable=self.config_path).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8, pady=8)
        ttk.Button(config_bar, text="浏览", command=self._browse_config).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(config_bar, text="加载", command=self._load_from_path).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(config_bar, text="保存", command=self._save_to_path).pack(side=tk.LEFT, padx=(0, 8))

        self._build_settings(shell)

        def on_close() -> None:
            self.settings_window = None
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", on_close)

    def _browse_config(self) -> None:
        initial = Path(self.config_path.get()).resolve().parent if self.config_path.get() else Path.cwd()
        path = filedialog.askopenfilename(title="选择配置文件", filetypes=(("JSON 配置", "*.json"), ("全部文件", "*.*")), initialdir=str(initial))
        if path:
            self.config_path.set(path)
            self._load_from_path()

    def _browse_canoe_exe(self) -> None:
        initial = str(Path(self.canoe_exe.get()).resolve().parent) if self.canoe_exe.get() else "C:\\"
        path = filedialog.askopenfilename(
            title="选择 CANoe 程序",
            filetypes=(("CANoe 程序", "CANoe*.exe"), ("可执行文件", "*.exe"), ("全部文件", "*.*")),
            initialdir=initial,
        )
        if path:
            self.canoe_exe.set(path)

    def _browse_canoe_config(self) -> None:
        path = filedialog.askopenfilename(title="选择 CANoe 工程", filetypes=(("CANoe 工程", "*.cfg *.canoe"), ("全部文件", "*.*")))
        if path:
            self.canoe_config.set(path)

    def _browse_zlg_dll(self) -> None:
        initial = str(Path(self.zlg_dll.get()).resolve().parent) if self.zlg_dll.get() else "C:\\"
        path = filedialog.askopenfilename(title="选择 ZLG zlgcan.dll", filetypes=(("ZLG DLL", "zlgcan.dll"), ("DLL", "*.dll"), ("全部文件", "*.*")), initialdir=initial)
        if path:
            self.zlg_dll.set(path)

    def _load_from_path(self, show_error: bool = True) -> None:
        try:
            self.cfg = load_config(self.config_path.get())
            self._apply_cfg_to_form()
            self._log("INFO", f"已加载配置文件：{Path(self.config_path.get()).resolve()}")
            self._refresh_channel_table()
        except Exception as exc:
            if show_error:
                messagebox.showerror(APP_TITLE, f"加载配置失败：\n{_friendly_error(str(exc))}")
            self._log("ERROR", f"加载配置失败：{_friendly_error(str(exc))}")

    def _save_to_path(self) -> None:
        try:
            self.cfg = self._cfg_from_form()
            path = Path(self.config_path.get())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            self._log("INFO", f"配置保存成功：{path.resolve()}")
            self._refresh_channel_table()
            messagebox.showinfo(APP_TITLE, "配置保存成功")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"保存配置失败：\n{_friendly_error(str(exc))}")

    def _apply_cfg_to_form(self) -> None:
        cfg = self.cfg
        channel = self._first_configured_channel(cfg)
        canfd = self._merged(cfg.get("canfd", {}), channel.get("canfd", {}))
        vector = self._merged(cfg.get("vector", {}), channel.get("vector", {}))
        zlg = self._merged(cfg.get("zlg", {}), channel.get("zlg", {}))
        canoe = cfg.get("canoe", {})

        self.mode.set(MODE_TEXT.get(str(cfg.get("mode", "native")).lower(), "原生桥接"))
        self.log_level.set(str(cfg.get("logLevel", "INFO")).upper())
        self.echo_suppression.set(bool(channel.get("echoSuppression", cfg.get("echoSuppression", True))))
        self.echo_window.set(str(channel.get("echoWindowMs", cfg.get("echoWindowMs", 50))))
        self.auto_detect_zlg.set(bool(cfg.get("autoDetectZlgOnStart", True)))
        self.auto_reconnect.set(bool(cfg.get("autoReconnect", False)))
        self.reconnect_interval.set(str(cfg.get("autoReconnectIntervalMs", 1000)))
        self.rx_queue_len.set(str(cfg.get("rxQueueLength", 8192)))

        self.vector_dll.set(str(vector.get("dllPath", "vxlapi64.dll")))
        self.vector_app.set(str(vector.get("applicationName", "ZLG_CANOE_BRIDGE")))
        self.vector_channel.set(str(vector.get("applicationChannel", 0)))
        self.vector_app_channel.set(str(vector.get("applicationChannel", 0)))

        device_type = int(zlg.get("deviceType", 43))
        self.zlg_dll.set(str(zlg.get("dllPath", "zlgcan.dll")))
        self.zlg_device_type.set(str(device_type))
        self.zlg_model.set("USBCANFD-100U-mini，deviceType=43" if device_type == 43 else "自定义")
        self.zlg_device_index.set(str(zlg.get("deviceIndex", 0)))
        self.zlg_channel.set(str(zlg.get("channelIndex", 0)))
        self.tx_timeout.set(str(zlg.get("txTimeoutMs", 1000)))
        self.termination.set(bool(zlg.get("enableTermination", False)))

        can_fd = bool(canfd.get("canFdEnabled", False))
        self.arb_bitrate.set(_display_bitrate(canfd.get("arbitrationBitrate", 500000)))
        self.data_bitrate.set(_display_bitrate(canfd.get("dataBitrate", 2000000)))
        self.can_fd_enabled.set(can_fd)
        self.can_mode.set("CAN FD" if can_fd else "Classical CAN")
        self.iso_canfd.set(bool(canfd.get("isoCanFd", True)))
        self.brs.set(bool(canfd.get("brs", True)))
        self.force_fd.set(bool(canfd.get("forceFd", False)))
        self.sample_point.set(str(canfd.get("samplePoint", "80%")))

        self.canoe_exe.set(str(canoe.get("exePath") or find_canoe_exe()))
        self.canoe_config.set(str(canoe.get("configPath") or ""))
        self.canoe_auto_start.set(bool(canoe.get("autoStartAfterBridge", True)))
        self.channel_name.set(str(channel.get("name", "CH0")))
        self.channel_enabled.set(bool(channel.get("enabled", True)))

    def _cfg_from_form(self) -> dict[str, Any]:
        cfg = copy.deepcopy(self.cfg) if self.cfg else {}
        can_fd = self.can_mode.get() == "CAN FD"
        self.can_fd_enabled.set(can_fd)

        cfg["mode"] = MODE_VALUE.get(self.mode.get(), "native")
        cfg["logLevel"] = self.log_level.get().strip() or "INFO"
        cfg["logDir"] = cfg.get("logDir", "logs")
        cfg["echoSuppression"] = bool(self.echo_suppression.get())
        cfg["echoWindowMs"] = self._int(self.echo_window.get(), "回环抑制时间窗口 ms")
        cfg["autoDetectZlgOnStart"] = bool(self.auto_detect_zlg.get())
        cfg["autoReconnect"] = bool(self.auto_reconnect.get())
        cfg["autoReconnectIntervalMs"] = self._int(self.reconnect_interval.get(), "自动重连间隔")
        cfg["rxQueueLength"] = self._int(self.rx_queue_len.get(), "ZLG 接收队列长度")

        cfg.setdefault("canfd", {})
        cfg["canfd"].update(
            {
                "arbitrationBitrate": _normalize_bitrate(self.arb_bitrate.get(), 500000),
                "dataBitrate": _normalize_bitrate(self.data_bitrate.get(), 2000000),
                "canFdEnabled": can_fd,
                "brs": bool(self.brs.get()) if can_fd else False,
                "isoCanFd": bool(self.iso_canfd.get()),
                "forceFd": bool(self.force_fd.get()) if can_fd else False,
                "samplePoint": self.sample_point.get().strip() or "80%",
            }
        )

        cfg.setdefault("vector", {})
        cfg["vector"].update(
            {
                "dllPath": self.vector_dll.get().strip() or "vxlapi64.dll",
                "applicationName": self.vector_app.get().strip(),
                "receiveTxOk": False,
            }
        )

        cfg.setdefault("zlg", {})
        cfg["zlg"].update(
            {
                "dllPath": self.zlg_dll.get().strip() or "zlgcan.dll",
                "deviceType": self._int(self.zlg_device_type.get(), "设备类型 deviceType"),
                "deviceIndex": self._int(self.zlg_device_index.get(), "设备索引 deviceIndex"),
                "enableTermination": bool(self.termination.get()),
                "useSetValue": True,
                "txTimeoutMs": self._int(self.tx_timeout.get(), "发送超时时间"),
            }
        )

        channels = cfg.setdefault("channels", [{"name": "CH0", "enabled": True}])
        if not channels:
            channels.append({"name": "CH0", "enabled": True})
        channel = channels[0]
        channel["name"] = self.channel_name.get() or "CH0"
        channel["enabled"] = bool(self.channel_enabled.get())
        channel.setdefault("vector", {})
        channel.setdefault("zlg", {})
        app_channel = self._int(self.vector_channel.get(), "Vector 应用通道")
        channel["vector"]["applicationChannel"] = app_channel
        channel["zlg"]["channelIndex"] = self._int(self.zlg_channel.get(), "通道索引 channelIndex")
        self.vector_app_channel.set(str(app_channel))

        cfg["canoe"] = {
            "exePath": self.canoe_exe.get().strip() or find_canoe_exe(),
            "configPath": self.canoe_config.get().strip(),
            "autoStartAfterBridge": bool(self.canoe_auto_start.get()),
        }
        return cfg

    def _start_bridge(self) -> None:
        try:
            self.cfg = self._cfg_from_form()
            errors, warnings = self._preflight(self.cfg)
            if warnings:
                self._log("WARNING", "\n".join(warnings))
            if errors:
                message = "启动前自检未通过：\n\n" + "\n\n".join(errors)
                self._log("ERROR", message)
                messagebox.showerror(APP_TITLE, message)
                return

            self.logger = self._create_logger(self.cfg.get("logDir", "logs"), self.cfg.get("logLevel", "INFO"), timestamped=True)
            self.runtime = BridgeRuntime(copy.deepcopy(self.cfg), self.logger)
            self.user_paused = False
            self.runtime.start()
            self.bridge_state.set("启动中")
            self.bus_state.set("未知")
            self._update_button_states("starting")
            self._log("INFO", "正在启动桥接，请稍候。")
            self.pending_canoe_autostart = bool(self.canoe_auto_start.get())
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"启动桥接失败：\n{_friendly_error(str(exc))}")

    def _pause_bridge(self) -> None:
        if self.runtime:
            self.user_paused = True
            self.runtime.stop()
            self.bridge_state.set("停止中")
            self._update_button_states("stopping")
            self._log("INFO", "已请求暂停桥接，正在释放 Vector/ZLG 通道。")

    def _stop_bridge(self) -> None:
        if self.runtime:
            self.user_paused = False
            self.runtime.stop()
            if self.runtime.thread:
                self.runtime.thread.join(timeout=3.0)
            self.runtime = None
        self.pending_canoe_autostart = False
        self.bridge_state.set("未启动")
        self.bus_state.set("未知")
        self._update_button_states("stopped")
        self._refresh_channel_table(status_override="未启动")
        self._log("INFO", "桥接已停止，后台不会继续占用硬件通道。")

    def _start_canoe(self) -> None:
        try:
            self.cfg = self._cfg_from_form()
            exe = Path(self.cfg.get("canoe", {}).get("exePath", ""))
            if not exe.is_file():
                raise FileNotFoundError(f"CANoe 程序路径不存在：{exe}")
            self.canoe_process = start_canoe(self.cfg)
            self.canoe_state.set("已启动")
            self._log("INFO", f"已请求启动 CANoe：pid={self.canoe_process.pid}")
        except Exception as exc:
            self.canoe_state.set("未连接")
            messagebox.showerror(APP_TITLE, f"启动 CANoe 失败：\n{_friendly_error(str(exc))}")

    def _close_canoe(self) -> None:
        try:
            count = close_canoe()
            self.canoe_state.set("未连接")
            self._log("INFO", f"已请求关闭 CANoe，发送关闭请求窗口数：{count}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"关闭 CANoe 失败：\n{_friendly_error(str(exc))}")

    def _detect_device(self, show_popup: bool = False) -> bool:
        try:
            device_type = self._int(self.zlg_device_type.get(), "设备类型 deviceType")
            device_index = self._int(self.zlg_device_index.get(), "设备索引 deviceIndex")
            ok, message = detect_zlg_device(self.zlg_dll.get(), device_type, device_index)
            self.zlg_state.set("已连接" if ok else "打开失败")
            self._log("INFO" if ok else "ERROR", message)
            if show_popup:
                if ok:
                    messagebox.showinfo(APP_TITLE, message)
                else:
                    messagebox.showerror(APP_TITLE, message)
            return ok
        except Exception as exc:
            message = _friendly_error(str(exc))
            self.zlg_state.set("打开失败")
            self._log("ERROR", message)
            if show_popup:
                messagebox.showerror(APP_TITLE, message)
            return False

    def _check_canoe_config(self) -> None:
        errors: list[str] = []
        exe = Path(self.canoe_exe.get())
        if not exe.is_file():
            errors.append(f"CANoe 程序路径不存在：{exe}")
        if self.canoe_config.get().strip() and not Path(self.canoe_config.get()).is_file():
            errors.append(f"CANoe 工程路径不存在：{self.canoe_config.get()}")
        if not self.vector_app.get().strip():
            errors.append("Vector 应用名称为空，请与 Vector Hardware Config 中的 Application Name 保持一致。")
        if errors:
            message = "\n".join(errors)
            self._log("ERROR", message)
            messagebox.showerror(APP_TITLE, message)
            return
        self.canoe_state.set("已连接 Vector Virtual CAN")
        message = "CANoe 配置检查通过：程序路径、工程路径和 Vector 应用名称已填写。"
        self._log("INFO", message)
        messagebox.showinfo(APP_TITLE, message)

    def _preflight(self, cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []

        lic = current_license()
        self._update_license_state()
        if not lic.valid:
            errors.append(f"License 无效或未注册。\n当前机器码：{machine_id()}\n请先点击【授权管理】完成注册。")

        config = Path(self.config_path.get())
        if not config.is_file():
            errors.append(f"配置文件不存在：{config}\n请在【参数设置】中选择或保存 config/bridge_config.json。")

        canoe_exe = Path(cfg.get("canoe", {}).get("exePath", ""))
        if not canoe_exe.is_file():
            errors.append(f"CANoe 程序路径不存在：{canoe_exe}\n请在【参数设置】-【CANoe 设置】中重新选择 CANoe64.exe。")

        zlg_dll = Path(cfg.get("zlg", {}).get("dllPath", ""))
        if not zlg_dll.is_file() and zlg_dll.name.lower() != "zlgcan.dll":
            errors.append(f"ZLG DLL 路径不存在：{zlg_dll}\n请确认 zlgcan.dll 路径正确，并与当前 Python/EXE 位数匹配。")

        if not str(cfg.get("vector", {}).get("applicationName", "")).strip():
            errors.append("Vector 应用名称为空，请设置为 Vector Hardware Config 中已配置的应用名称。")

        canfd = cfg.get("canfd", {})
        if not canfd.get("arbitrationBitrate"):
            errors.append("仲裁波特率未配置。")
        if canfd.get("canFdEnabled") and not canfd.get("dataBitrate"):
            errors.append("当前为 CAN FD 模式，但数据波特率未配置。")
        if not canfd.get("canFdEnabled") and (self.brs.get() or self.iso_canfd.get()):
            warnings.append("当前为 Classical CAN 模式，BRS/ISO CAN FD 参数不会参与实际发送。")

        if cfg.get("autoDetectZlgOnStart", True) and not errors:
            ok = self._detect_device(show_popup=False)
            if not ok:
                errors.append(
                    "ZLG 设备检测失败。\n"
                    "可能原因：\n"
                    "1. ZXDoc 或 ZCANPRO 正在占用设备；\n"
                    "2. 设备类型选择错误；\n"
                    "3. ZLG DLL 路径错误；\n"
                    "4. 驱动未安装或版本不匹配。\n"
                    "建议先关闭 ZXDoc/ZCANPRO，然后点击【设备检测】重新检测。"
                )
        return errors, warnings

    def _show_license_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("授权管理")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("700x460")

        lic = current_license()
        owner = tk.StringVar(value=lic.owner or "WDJR")
        expires = tk.StringVar(value=lic.expires or "2099-12-31")

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=f"机器码：{machine_id()}").pack(anchor=tk.W)
        ttk.Label(frame, text=f"授权状态：{_license_text(lic)}").pack(anchor=tk.W, pady=(0, 8))

        form = ttk.Frame(frame)
        form.pack(fill=tk.X)
        ttk.Label(form, text="版权所有者").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(form, textvariable=owner).grid(row=0, column=1, sticky=tk.EW, padx=8, pady=4)
        ttk.Label(form, text="到期日期").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(form, textvariable=expires).grid(row=1, column=1, sticky=tk.EW, padx=8, pady=4)
        form.columnconfigure(1, weight=1)

        text = ScrolledText(frame, height=9, wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, pady=8)

        def generate() -> None:
            generated = generate_license(owner.get(), expires.get(), machine_id())
            text.delete("1.0", tk.END)
            text.insert(tk.END, generated)

        def register() -> None:
            candidate = text.get("1.0", tk.END).strip()
            info = register_license(candidate)
            self._update_license_state()
            if info.valid:
                messagebox.showinfo(APP_TITLE, f"授权注册成功：{info.owner}，有效期至 {info.expires}")
                dialog.destroy()
            else:
                messagebox.showerror(APP_TITLE, f"授权注册失败：{_license_text(info)}")

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="生成本机授权码", command=generate).pack(side=tk.LEFT)
        ttk.Button(buttons, text="注册授权", command=register).pack(side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)

    def _show_about_dialog(self) -> None:
        lic = current_license()
        messagebox.showinfo(
            "关于软件",
            "\n".join(
                [
                    f"软件名称：{APP_TITLE}",
                    f"版本号：{APP_VERSION}",
                    f"授权状态：{_license_text(lic)}",
                    "支持硬件：USBCANFD-100U-mini",
                    "支持模式：Classical CAN / CAN FD",
                    "",
                    "说明：本工具只做 CAN/CANFD 报文透明桥接，",
                    "不实现 UDS、ISO-TP、CDD、ODX、DBC 解析。",
                ]
            ),
        )

    def _update_license_state(self) -> None:
        lic = current_license()
        self.auth_state.set(_license_text(lic))

    def _poll_runtime(self) -> None:
        if self.runtime:
            snapshot = self.runtime.snapshot()
            status = snapshot["status"]
            if status == "running":
                self.bridge_state.set("运行中")
                self.bus_state.set("正常")
                self.canoe_state.set("已连接 Vector Virtual CAN")
                self._update_button_states("running")
                if self.pending_canoe_autostart:
                    self.pending_canoe_autostart = False
                    self._start_canoe()
            elif status == "starting":
                self.bridge_state.set("启动中")
                self._update_button_states("starting")
            elif status == "stopping":
                self.bridge_state.set("停止中")
                self._update_button_states("stopping")
            elif status == "error":
                self.pending_canoe_autostart = False
                self.bridge_state.set("异常")
                self.bus_state.set("未知")
                self._update_button_states("error")
            else:
                if self.user_paused:
                    self.bridge_state.set("已暂停")
                    self._update_button_states("paused")
                else:
                    self.bridge_state.set("未启动")
                    self._update_button_states("stopped")

            self._refresh_channel_table(snapshot=snapshot)
        self.after(200, self._poll_runtime)

    def _poll_logs(self) -> None:
        while True:
            try:
                level, line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line, level)
        self.after(100, self._poll_logs)

    def _append_log(self, line: str, level: str = "INFO") -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, line + "\n", level if level in {"ERROR", "WARNING", "INFO", "DEBUG"} else "INFO")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _log(self, level: str, message: str) -> None:
        level = level.upper()
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._append_log(f"[{stamp}] [{level}] {message}", level)

    def _clear_logs(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _save_log(self) -> None:
        log_dir = Path(self.cfg.get("logDir", "logs") if self.cfg else "logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"bridge_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        content = self.log_text.get("1.0", tk.END).strip()
        path.write_text(content + ("\n" if content else ""), encoding="utf-8")
        self._log("INFO", f"日志已保存：{path.resolve()}")
        messagebox.showinfo(APP_TITLE, f"日志已保存：\n{path.resolve()}")

    def _refresh_channel_table(self, snapshot: dict[str, Any] | None = None, status_override: str | None = None) -> None:
        cfg = self._cfg_from_form_safe()
        rows = []
        bridges = {item["name"]: item for item in (snapshot or {}).get("bridges", [])}
        channels = cfg.get("channels") or [{"name": "CH0", "enabled": True}]
        canfd = cfg.get("canfd", {})
        for index, channel in enumerate(channels):
            name = str(channel.get("name", f"CH{index}"))
            if not channel.get("enabled", True) and name != "CH1":
                continue
            item = bridges.get(name, {})
            mode = "CAN FD" if canfd.get("canFdEnabled") else "Classical CAN"
            data = _display_bitrate(canfd.get("dataBitrate", 2000000)) if canfd.get("canFdEnabled") else "-"
            if status_override:
                status = status_override
            elif not channel.get("enabled", True):
                status = "未启用"
            else:
                status = self.bridge_state.get()
            rows.append(
                (
                    name,
                    mode,
                    _display_bitrate(canfd.get("arbitrationBitrate", 500000)),
                    data,
                    item.get("v2z", 0),
                    item.get("z2v", 0),
                    item.get("drop", 0),
                    status,
                )
            )

        for row_id in self.channel_table.get_children():
            self.channel_table.delete(row_id)
        for row in rows:
            self.channel_table.insert("", tk.END, values=row)

    def _update_button_states(self, status: str) -> None:
        if status in ("running", "starting", "stopping"):
            self.start_button.configure(state=tk.DISABLED)
            self.pause_button.configure(state=tk.NORMAL if status == "running" else tk.DISABLED)
            self.stop_button.configure(state=tk.NORMAL)
        elif status == "paused":
            self.start_button.configure(state=tk.NORMAL)
            self.pause_button.configure(state=tk.DISABLED)
            self.stop_button.configure(state=tk.NORMAL)
        else:
            self.start_button.configure(state=tk.NORMAL)
            self.pause_button.configure(state=tk.DISABLED)
            self.stop_button.configure(state=tk.DISABLED)

    def _on_close(self) -> None:
        if self.runtime and self.runtime.snapshot()["status"] in ("running", "starting", "stopping"):
            self.runtime.stop()
            if self.runtime.thread:
                self.runtime.thread.join(timeout=3.0)
        self.destroy()

    def _on_zlg_model_changed(self) -> None:
        if self.zlg_model.get().startswith("USBCANFD-100U-mini"):
            self.zlg_device_type.set("43")

    def _on_can_mode_changed(self) -> None:
        self.can_fd_enabled.set(self.can_mode.get() == "CAN FD")

    def _cfg_from_form_safe(self) -> dict[str, Any]:
        try:
            return self._cfg_from_form()
        except Exception:
            return copy.deepcopy(self.cfg) if self.cfg else {}

    def _first_configured_channel(self, cfg: dict[str, Any]) -> dict[str, Any]:
        channels = cfg.get("channels") or []
        return channels[0] if channels else {}

    def _merged(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        result = dict(base)
        result.update(override)
        return result

    def _int(self, value: str, label: str) -> int:
        try:
            return int(str(value).strip(), 0)
        except ValueError as exc:
            raise ValueError(f"{label} 必须是整数") from exc


def detect_zlg_device(dll_path: str, device_type: int, device_index: int) -> tuple[bool, str]:
    dll_file = Path(dll_path)
    handles: list[Any] = []
    dll = None
    dev = None
    try:
        if os.name == "nt" and dll_file.parent != Path("."):
            dll_dir = dll_file.resolve().parent
            if dll_dir.is_dir():
                handles.append(os.add_dll_directory(str(dll_dir)))
            kernel_dir = dll_dir / "kerneldlls"
            if kernel_dir.is_dir():
                handles.append(os.add_dll_directory(str(kernel_dir)))
        dll = ct.WinDLL(str(dll_file))
        dll.ZCAN_OpenDevice.argtypes = [ct.c_uint, ct.c_uint, ct.c_uint]
        dll.ZCAN_OpenDevice.restype = ct.c_void_p
        dll.ZCAN_CloseDevice.argtypes = [ct.c_void_p]
        dll.ZCAN_CloseDevice.restype = ct.c_uint
        dev = dll.ZCAN_OpenDevice(device_type, device_index, 0)
        if not dev:
            return False, _friendly_error("OPEN_DEVICE_FAILED: ZCAN_OpenDevice returned 0")
        dll.ZCAN_CloseDevice(dev)
        dev = None
        return True, f"ZLG 设备检测成功：deviceType={device_type}, deviceIndex={device_index}"
    except OSError as exc:
        return False, _friendly_error(f"DLL_LOAD_FAILED: cannot load {dll_path}: {exc}")
    except Exception as exc:
        return False, _friendly_error(str(exc))
    finally:
        try:
            if dll is not None and dev:
                dll.ZCAN_CloseDevice(dev)
        finally:
            while handles:
                handles.pop().close()


def _friendly_error(message: str) -> str:
    text = str(message)
    if "OPEN_DEVICE_FAILED" in text or "ZCAN_OpenDevice returned 0" in text:
        return (
            "ZLG 设备打开失败。\n"
            "可能原因：\n"
            "1. ZXDoc 或 ZCANPRO 正在占用设备；\n"
            "2. 设备类型选择错误；\n"
            "3. ZLG DLL 路径错误；\n"
            "4. 驱动未安装或版本不匹配。\n"
            "建议先关闭 ZXDoc/ZCANPRO，然后点击【设备检测】重新检测。"
        )
    if "DLL_LOAD_FAILED" in text or "cannot load" in text or "WinDLL" in text:
        return (
            "DLL 加载失败。\n"
            "可能原因：DLL 路径错误、缺少 kerneldlls 依赖、32/64 位不匹配或驱动未安装。\n"
            f"详细信息：{text}"
        )
    if "CONFIG_VALUE_FAILED" in text or "ZCAN_SetValue" in text:
        return "ZLG 通道参数配置失败，请检查 CAN/CANFD 波特率、通道索引和 SDK 版本。"
    if "FAIL_OPEN_CHANNEL" in text or "ZCAN_InitCAN" in text:
        return "ZLG 通道初始化失败，请检查 channelIndex、CAN/CANFD 模式和设备是否支持当前配置。"
    if "CHANNEL_START_FAILED" in text or "ZCAN_StartCAN" in text:
        return "ZLG 通道启动失败，请检查硬件连接、驱动状态和设备占用情况。"
    if "xlGetApplConfig" in text:
        return "Vector 应用配置未找到，请在 Vector Hardware Config 中配置对应 Application Name 和 Application Channel。"
    if "xlCanFdSetConfiguration" in text:
        return "Vector CAN FD 参数配置失败，请确认 CANoe 虚拟通道支持 CAN FD，或改用 Classical CAN 模式。"
    if "Classic CAN channel cannot transmit payloads longer than 8 bytes" in text:
        return "Classical CAN 通道无法发送超过 8 字节的数据，请检查 CANoe 工程和 ECU 报文模式是否一致。"
    return text


def _normalize_bitrate(value: str, default: int) -> int:
    text = str(value).strip().upper()
    mapping = {
        "125K": 125000,
        "250K": 250000,
        "500K": 500000,
        "1M": 1000000,
        "2M": 2000000,
        "4M": 4000000,
        "5M": 5000000,
        "自定义": default,
    }
    if text in mapping:
        return mapping[text]
    try:
        return int(text, 0)
    except ValueError as exc:
        raise ValueError(f"波特率格式无效：{value}") from exc


def _display_bitrate(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    mapping = {
        125000: "125K",
        250000: "250K",
        500000: "500K",
        1000000: "1M",
        2000000: "2M",
        4000000: "4M",
        5000000: "5M",
    }
    return mapping.get(number, str(number))


def _license_text(lic: Any) -> str:
    if lic.valid:
        return f"授权正常（{lic.owner} / {lic.expires}）"
    if "expired" in str(lic.message).lower():
        return "已过期"
    return "未授权"


def _resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / relative


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("config", nargs="?", default=DEFAULT_CONFIG, help="bridge_config.json 路径")
    args = parser.parse_args()
    app = BridgeGui(args.config)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
