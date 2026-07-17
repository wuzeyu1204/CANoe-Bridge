from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


PRODUCT_ID = "ZLG_CANOE_BRIDGE"
LICENSE_PREFIX = "ZCB2"
DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue a ZLG-CANoe Bridge customer license")
    parser.add_argument("--owner", required=True, help="customer or license owner")
    parser.add_argument("--expires", default="2099-12-31", help="expiry date in YYYY-MM-DD")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--machine", help="customer machine ID shown by the application")
    target.add_argument("--portable", action="store_true", help="license usable on any machine; can ship with EXE")
    parser.add_argument("--private-key", default="license_private/license_private_key.json")
    parser.add_argument("--output", default="license.lic")
    parser.add_argument("--print-key", action="store_true")
    args = parser.parse_args()

    private = json.loads(Path(args.private_key).read_text(encoding="utf-8"))
    n, d = int(private["n"]), int(private["d"])
    license_type = "portable" if args.portable else "node_locked"
    payload = {
        "version": 2,
        "product": PRODUCT_ID,
        "owner": args.owner.strip(),
        "machine": "*" if args.portable else args.machine.strip().upper(),
        "license_type": license_type,
        "expires": args.expires,
        "issued": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    payload_b64 = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _rsa_sign(payload_b64.encode("ascii"), n, d)
    key = f"{LICENSE_PREFIX}.{payload_b64}.{_b64(signature)}"
    output = Path(args.output)
    output.write_text(key + "\n", encoding="utf-8")
    print(f"License written: {output.resolve()}")
    print(f"Type: {license_type}; Owner: {payload['owner']}; Expires: {payload['expires']}")
    if args.print_key:
        print(key)
    return 0


def _rsa_sign(message: bytes, n: int, d: int) -> bytes:
    key_bytes = (n.bit_length() + 7) // 8
    digest_info = DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_length = key_bytes - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("RSA key is too small")
    encoded = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    return pow(int.from_bytes(encoded, "big"), d, n).to_bytes(key_bytes, "big")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


if __name__ == "__main__":
    raise SystemExit(main())
