from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import ctypes as ct
from ctypes import wintypes


DEFAULT_CANOE_PATHS = [
    r"C:\Program Files (x86)\CANwin\Exec64\CANoe64.exe",
    r"C:\Program Files\Vector CANoe 15\Exec64\CANoe64.exe",
    r"C:\Program Files\Vector CANoe 15\Exec32\CANoe32.exe",
]


def find_canoe_exe() -> str:
    for path in DEFAULT_CANOE_PATHS:
        if Path(path).is_file():
            return path
    return DEFAULT_CANOE_PATHS[0]


def start_canoe(cfg: dict[str, Any]) -> subprocess.Popen:
    canoe_cfg = cfg.get("canoe", {})
    exe_path = str(canoe_cfg.get("exePath") or find_canoe_exe())
    config_path = str(canoe_cfg.get("configPath") or "").strip()
    args = [exe_path]
    if config_path:
        args.append(config_path)
    workdir = str(Path(exe_path).resolve().parent)
    return subprocess.Popen(args, cwd=workdir)


def is_canoe_running() -> bool:
    return bool(_canoe_processes())


def close_canoe(timeout_s: float = 15.0) -> int:
    closed = 0
    for process in _canoe_processes():
        if _close_main_window(process["pid"]):
            closed += 1

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _canoe_processes():
            return closed
        time.sleep(0.2)
    return closed


def _canoe_processes() -> list[dict[str, Any]]:
    if hasattr(ct, "windll"):
        return _canoe_processes_win32()
    return _canoe_processes_powershell()


def _canoe_processes_win32() -> list[dict[str, Any]]:
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ct.c_void_p(-1).value

    class PROCESSENTRY32W(ct.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ct.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ct.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ct.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ct.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ct.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return _canoe_processes_powershell()
    entry = PROCESSENTRY32W()
    entry.dwSize = ct.sizeof(PROCESSENTRY32W)
    result = []
    try:
        ok = kernel32.Process32FirstW(snapshot, ct.byref(entry))
        while ok:
            name = entry.szExeFile
            if name.lower() in ("canoe.exe", "canoe32.exe", "canoe64.exe"):
                result.append({"pid": int(entry.th32ProcessID), "name": name, "cmd": ""})
            ok = kernel32.Process32NextW(snapshot, ct.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return result


def _canoe_processes_powershell() -> list[dict[str, Any]]:
    output = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match '^CANoe(32|64)?\\.exe$' } | "
            "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    text = output.stdout.strip()
    if not text:
        return []
    import json

    data = json.loads(text)
    if isinstance(data, dict):
        data = [data]
    return [{"pid": int(item["ProcessId"]), "name": item.get("Name", ""), "cmd": item.get("CommandLine", "")} for item in data]


def _close_main_window(pid: int) -> bool:
    user32 = ct.windll.user32
    WM_CLOSE = 0x0010
    closed = False

    enum_proc_type = ct.WINFUNCTYPE(ct.c_bool, ct.c_void_p, ct.c_void_p)

    def callback(hwnd, _lparam):
        nonlocal closed
        window_pid = ct.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ct.byref(window_pid))
        if window_pid.value == pid and user32.IsWindowVisible(hwnd):
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            closed = True
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    return closed
