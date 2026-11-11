"""Compile actual IPC/chat C with synthetic kernel mocks; no game data is used."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLANG = Path(os.environ.get("HWAOI_TOOLS", "F:") + "/llvm-mingw/bin/clang.exe")


@unittest.skipUnless(os.name == "nt" and CLANG.is_file(), "Windows Clang required")
class IpcDomainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="ipc-domain-")
        cls.executable = Path(cls.temporary.name) / "ipc-domain.exe"
        command = [str(CLANG), "-std=gnu11", "-O1", "-flto", "-march=x86-64-v3", "-ffunction-sections",
                   "-fdata-sections", "-Wl,--gc-sections", "-Wl,--exclude-all-symbols", "-I", str(ROOT / "runtime"),
                   str(ROOT / "tests/hwaoi/ipc_domain_fixture.c"), "-o", str(cls.executable)]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if result.returncode:
            cls.temporary.cleanup()
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_case(self, name):
        result = subprocess.run([str(self.executable), name], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check passed", result.stdout)

    def test_chat_lifecycle_and_domain_framing(self):
        self.run_case("lifecycle")

    def test_copy_root_is_ordinary_and_outlives_domain(self):
        self.run_case("copy-root")

    def test_native_moved_handles_are_not_domain_ids(self):
        self.run_case("native-move")

    def test_copy_conversion_does_not_change_other_sessions(self):
        self.run_case("copy-convert")

    def test_ordinary_clone_conversion_does_not_change_original(self):
        self.run_case("clone-convert")

    def test_domain_ids_follow_output_data_without_extra_alignment(self):
        self.run_case("output-offset")

    def test_clone_ex_shares_namespace(self):
        self.run_case("clone-ex")

    def test_closed_object_is_rejected(self):
        self.run_case("invalid-id")

    def test_unknown_chat_command_is_rejected(self):
        self.run_case("unknown-chat")

    def test_domain_object_table_is_bounded(self):
        self.run_case("table-limit")

    def test_malformed_domain_length_is_rejected(self):
        self.run_case("malformed-domain")

    def test_gsv_bootstrap_chat_subscriptions_and_cleanup(self):
        self.run_case("gsv-lifecycle")

    def test_gsv_invalid_order_is_rejected(self):
        self.run_case("gsv-order")

    def test_gsv_duplicate_initialization_is_rejected(self):
        self.run_case("gsv-duplicate")

    def test_gsv_boot_metadata_and_size_are_checked(self):
        self.run_case("gsv-boot")

    def test_gsv_unknown_and_buffer_commands_fail(self):
        self.run_case("gsv-data")

    def test_gsv_unknown_subscription_is_rejected(self):
        self.run_case("gsv-subscription")

    def test_gsv_display_endpoint_metadata_and_event_lifetime(self):
        self.run_case("gsv-display")

    def test_gsv_display_rejects_equal_out_of_range_and_used_endpoints(self):
        self.run_case("gsv-display-bad")

    def test_gsv_frame_policy_is_rejected_by_default(self):
        self.run_case("gsv-frame-default")

    def test_gsv_frame_policy_explicit_opt_in_stores_only_known_tokens(self):
        self.run_case("gsv-frame-local")

    def test_gsv_frame_policy_unknown_token_and_malformed_requests_are_rejected(self):
        self.run_case("gsv-frame-bad")


if __name__ == "__main__":
    unittest.main()
