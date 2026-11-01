"""Image source inventories reject links before inspecting their targets."""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "image_inventory.py"
SPEC = importlib.util.spec_from_file_location("image_inventory", SCRIPT)
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


class ImageInventoryTests(unittest.TestCase):
    def test_rejects_directory_symlinks_including_source_roots_and_ignored_names(self):
        for relative in (".", "src", "src/extension", "src/__pycache__", "tools"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = base / "checkout"
                outside = base / "outside"
                outside.mkdir()
                (outside / "untracked.py").write_text("VALUE = 1\n")
                for name in inventory.BUILD_FILES:
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("synthetic build input\n")
                (root / "src").mkdir()
                link = root / relative
                if link.is_dir():
                    shutil.rmtree(link)
                link.symlink_to(outside, target_is_directory=True)

                with self.assertRaisesRegex(ValueError, "symbolic links"):
                    inventory.build_inputs(root)


if __name__ == "__main__":
    unittest.main()
