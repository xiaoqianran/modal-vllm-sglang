"""共享模型 Volume 只读检查入口的离线路径校验。"""
import unittest

from scripts.probe_shared_model import validate_relative_path


class SharedVolumeProbeTests(unittest.TestCase):
    def test_valid_hf_snapshot_path(self):
        path = "models--sshleifer--tiny-gpt2/snapshots/abc/pytorch_model.bin"
        self.assertEqual(validate_relative_path(path), path)

    def test_path_traversal_and_noncanonical_paths(self):
        for path in ("", "/etc/passwd", "../a", "a/../b", "a//b",
                     "./a", r"a\b", "a/"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_relative_path(path)


if __name__ == "__main__":
    unittest.main()
