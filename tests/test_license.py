from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import zlg_canoe_bridge.license as license_module
from tools.generate_license import _b64, _rsa_sign


TEST_N = 143369288878196913666826456088212619997488646959412543063240974694417968294879823255194903960070098876102341793788509429886619958464989533375736336903668934991325330876889532845590462548987669252341285228462699418942893980246011322360756457129562163873216217405844615981079472118831383043246365155799180042093
TEST_E = 65537
TEST_D = 134349779623198883927162976245502385591738525784905655121318968214641883285193851799663393683014862632969913467563781041656726400799073305197636013162059943209794490887223289706339092337085960024874793178057401466618197672025247228799127642917869589405881780815190675771261502228864004521037443577089926945729
MACHINE = "0123456789ABCDEF0123456789ABCDEF"


def make_key(license_type="node_locked", machine=MACHINE, expires="2099-12-31"):
    payload = {
        "version": 2, "product": "ZLG_CANOE_BRIDGE", "owner": "Test Customer",
        "machine": machine, "license_type": license_type, "expires": expires,
        "issued": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    payload_b64 = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signature = _rsa_sign(payload_b64.encode("ascii"), TEST_N, TEST_D)
    return f"ZCB2.{payload_b64}.{_b64(signature)}"


class LicenseTests(unittest.TestCase):
    def validate(self, key):
        with patch.object(license_module, "RSA_PUBLIC_N", TEST_N), \
             patch.object(license_module, "RSA_PUBLIC_E", TEST_E), \
             patch.object(license_module, "machine_id", return_value=MACHINE):
            return license_module.validate_license_key(key)

    def test_node_locked_license_matches_only_target_machine(self):
        self.assertTrue(self.validate(make_key()).valid)
        self.assertFalse(self.validate(make_key(machine="F" * 32)).valid)

    def test_portable_license_can_ship_with_application(self):
        info = self.validate(make_key("portable", "*"))
        self.assertTrue(info.valid)
        self.assertEqual(info.license_type, "portable")

    def test_tampered_and_old_hmac_licenses_are_rejected(self):
        key = make_key()
        self.assertFalse(self.validate(key[:-1] + ("A" if key[-1] != "A" else "B")).valid)
        self.assertFalse(self.validate("oldpayload.oldhmacsignature").valid)

    def test_register_writes_only_valid_signed_license(self):
        key = make_key("portable", "*")
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict(os.environ, {"PROGRAMDATA": temp}), \
             patch.object(license_module, "RSA_PUBLIC_N", TEST_N), \
             patch.object(license_module, "RSA_PUBLIC_E", TEST_E), \
             patch.object(license_module, "machine_id", return_value=MACHINE):
            self.assertTrue(license_module.register_license(key).valid)
            self.assertTrue(license_module.license_path().is_file())
            self.assertTrue(license_module.current_license().valid)


if __name__ == "__main__":
    unittest.main()
