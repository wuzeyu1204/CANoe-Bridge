from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the vendor RSA license signing key")
    parser.add_argument("--private-key", default="license_private/license_private_key.json")
    parser.add_argument("--public-module", default="zlg_canoe_bridge/license_public_key.py")
    parser.add_argument("--force", action="store_true", help="replace keys and invalidate all issued licenses")
    args = parser.parse_args()
    private_path = Path(args.private_key)
    public_path = Path(args.public_module)
    if not args.force and (private_path.exists() or public_path.exists()):
        parser.error("key already exists; use --force only when intentionally invalidating issued licenses")

    try:
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError as exc:
        parser.error(f"key generation requires the cryptography package: {exc}")
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    numbers = key.private_numbers()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_text(json.dumps({
        "n": str(numbers.public_numbers.n), "e": numbers.public_numbers.e, "d": str(numbers.d)
    }, indent=2), encoding="utf-8")
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_text(
        "# Generated public verification key. Safe to distribute with the application.\n"
        f"RSA_PUBLIC_N = {numbers.public_numbers.n}\n"
        f"RSA_PUBLIC_E = {numbers.public_numbers.e}\n",
        encoding="utf-8",
    )
    print(f"Private signing key: {private_path.resolve()} (KEEP SECRET; BACK UP SECURELY)")
    print(f"Public application key: {public_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
