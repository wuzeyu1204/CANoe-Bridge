from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zlg_canoe_bridge.license import generate_license, machine_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a local license key for ZLG-CANoe Bridge")
    parser.add_argument("--owner", default="WDJR", help="license owner/copyright holder")
    parser.add_argument("--expires", default="2099-12-31", help="expiry date in YYYY-MM-DD")
    parser.add_argument("--machine", default=machine_id(), help="target machine id")
    args = parser.parse_args()
    print("Machine ID:", args.machine)
    print("License Key:")
    print(generate_license(args.owner, args.expires, args.machine))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
