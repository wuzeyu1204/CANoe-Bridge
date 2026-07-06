from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import platform
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PRODUCT_ID = "ZLG_CANOE_BRIDGE"
LICENSE_SECRET = b"WDJR-ZLG-CANOE-BRIDGE-LOCAL-LICENSE-v1"


@dataclass(frozen=True)
class LicenseInfo:
    valid: bool
    owner: str = ""
    machine_id: str = ""
    expires: str = ""
    message: str = "Unregistered"


def license_dir() -> Path:
    root = os.environ.get("PROGRAMDATA") or str(Path.home())
    return Path(root) / "WDJR" / "ZLG_CANoe_Bridge"


def license_path() -> Path:
    return license_dir() / "license.json"


def machine_id() -> str:
    raw = "|".join(
        [
            PRODUCT_ID,
            platform.node(),
            getpass.getuser(),
            str(uuid.getnode()),
            os.environ.get("PROCESSOR_IDENTIFIER", ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32].upper()


def generate_license(owner: str, expires: str = "2099-12-31", machine: str | None = None) -> str:
    payload = {
        "product": PRODUCT_ID,
        "owner": owner.strip(),
        "machine": machine or machine_id(),
        "expires": expires.strip(),
        "issued": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    payload_b64 = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _sign(payload_b64)
    return f"{payload_b64}.{signature}"


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
    if "." not in key:
        return LicenseInfo(False, machine_id=machine_id(), message="Invalid license format")
    payload_b64, signature = key.split(".", 1)
    expected = _sign(payload_b64)
    if not hmac.compare_digest(signature, expected):
        return LicenseInfo(False, machine_id=machine_id(), message="Invalid license signature")
    try:
        payload = json.loads(_unb64(payload_b64).decode("utf-8"))
    except Exception as exc:
        return LicenseInfo(False, machine_id=machine_id(), message=f"Invalid license payload: {exc}")

    owner = str(payload.get("owner", ""))
    machine = str(payload.get("machine", ""))
    expires = str(payload.get("expires", ""))
    if payload.get("product") != PRODUCT_ID:
        return LicenseInfo(False, owner, machine, expires, "License product mismatch")
    if machine != machine_id():
        return LicenseInfo(False, owner, machine, expires, "License is not for this machine")
    try:
        expire_date = date.fromisoformat(expires)
    except ValueError:
        return LicenseInfo(False, owner, machine, expires, "License expiry date is invalid")
    if expire_date < date.today():
        return LicenseInfo(False, owner, machine, expires, "License expired")
    return LicenseInfo(True, owner, machine, expires, "Registered")


def _sign(payload_b64: str) -> str:
    digest = hmac.new(LICENSE_SECRET, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64(digest)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)
