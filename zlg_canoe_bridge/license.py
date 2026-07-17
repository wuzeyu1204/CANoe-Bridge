from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from zlg_canoe_bridge.license_public_key import RSA_PUBLIC_E, RSA_PUBLIC_N


PRODUCT_ID = "ZLG_CANOE_BRIDGE"
LICENSE_VERSION = 2
LICENSE_PREFIX = "ZCB2"
PORTABLE_MACHINE = "*"
SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


@dataclass(frozen=True)
class LicenseInfo:
    valid: bool
    owner: str = ""
    machine_id: str = ""
    expires: str = ""
    license_type: str = ""
    message: str = "Unregistered"


def license_dir() -> Path:
    root = os.environ.get("PROGRAMDATA") or str(Path.home())
    return Path(root) / "WDJR" / "ZLG_CANoe_Bridge"


def license_path() -> Path:
    return license_dir() / "license.json"


def machine_id() -> str:
    """Stable Windows installation ID; independent of login user and hostname."""
    components = [PRODUCT_ID]
    machine_guid = _windows_machine_guid()
    if machine_guid:
        components.append(machine_guid)
    else:
        components.extend([str(uuid.getnode()), platform.machine(), platform.processor()])
    return hashlib.sha256("|".join(components).encode("utf-8")).hexdigest()[:32].upper()


def register_license(key: str) -> LicenseInfo:
    info = validate_license_key(key)
    if not info.valid:
        return info
    license_dir().mkdir(parents=True, exist_ok=True)
    license_path().write_text(json.dumps({"key": key.strip()}, indent=2), encoding="utf-8")
    return info


def current_license() -> LicenseInfo:
    path = license_path()
    if not path.is_file():
        return LicenseInfo(False, machine_id=machine_id(), message="Unregistered")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return LicenseInfo(False, machine_id=machine_id(), message=f"License file read failed: {exc}")
    return validate_license_key(str(data.get("key", "")))


def validate_license_key(key: str) -> LicenseInfo:
    key = key.strip()
    parts = key.split(".")
    if len(parts) != 3 or parts[0] != LICENSE_PREFIX:
        return LicenseInfo(False, machine_id=machine_id(), message="Invalid or obsolete license format")
    payload_b64, signature_b64 = parts[1], parts[2]
    try:
        signature = _unb64(signature_b64)
    except Exception:
        return LicenseInfo(False, machine_id=machine_id(), message="Invalid license signature encoding")
    if not _verify_rsa_signature(payload_b64.encode("ascii"), signature):
        return LicenseInfo(False, machine_id=machine_id(), message="Invalid license signature")
    try:
        payload = json.loads(_unb64(payload_b64).decode("utf-8"))
    except Exception as exc:
        return LicenseInfo(False, machine_id=machine_id(), message=f"Invalid license payload: {exc}")

    owner = str(payload.get("owner", ""))
    machine = str(payload.get("machine", ""))
    expires = str(payload.get("expires", ""))
    license_type = str(payload.get("license_type", ""))
    info_args = (owner, machine, expires, license_type)
    if payload.get("version") != LICENSE_VERSION or payload.get("product") != PRODUCT_ID:
        return LicenseInfo(False, *info_args, message="License product or version mismatch")
    if not owner:
        return LicenseInfo(False, *info_args, message="License owner is empty")
    if license_type not in ("node_locked", "portable"):
        return LicenseInfo(False, *info_args, message="License type is invalid")
    if license_type == "portable":
        if machine != PORTABLE_MACHINE:
            return LicenseInfo(False, *info_args, message="Portable license payload is invalid")
    elif machine.upper() != machine_id():
        return LicenseInfo(False, *info_args, message="License is not for this machine")
    try:
        expire_date = date.fromisoformat(expires)
    except ValueError:
        return LicenseInfo(False, *info_args, message="License expiry date is invalid")
    if expire_date < date.today():
        return LicenseInfo(False, *info_args, message="License expired")
    return LicenseInfo(True, *info_args, message="Registered")


def _verify_rsa_signature(message: bytes, signature: bytes) -> bool:
    key_bytes = (RSA_PUBLIC_N.bit_length() + 7) // 8
    if len(signature) != key_bytes:
        return False
    recovered = pow(int.from_bytes(signature, "big"), RSA_PUBLIC_E, RSA_PUBLIC_N).to_bytes(key_bytes, "big")
    digest_info = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_length = key_bytes - len(digest_info) - 3
    if padding_length < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    return recovered == expected


def _windows_machine_guid() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg
        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip().upper()
    except OSError:
        return ""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
