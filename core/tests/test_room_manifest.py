# File: core/tests/test_room_manifest.py

import tempfile
import unittest
from pathlib import Path

from ui.room.manifest import build_manifest


class RoomManifestTests(unittest.TestCase):
    def test_lists_every_glb_under_the_root_with_posix_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "Motion" / "Idle").mkdir(parents=True)
            (root / "Clothes" / "Outfits").mkdir(parents=True)
            (root / ".hidden").mkdir()
            (root / "Iris.glb").write_bytes(b"")
            (root / "Motion" / "Idle" / "Idle3.glb").write_bytes(b"")
            (root / "Motion" / "Idle" / "Idle 3 To Home.GLB").write_bytes(b"")
            (root / "Clothes" / "Outfits" / "Biker.glb").write_bytes(b"")
            (root / "Clothes" / "Outfits" / "notes.txt").write_bytes(b"")
            (root / ".hidden" / "Old.glb").write_bytes(b"")

            manifest = build_manifest(root)

        self.assertEqual(
            manifest["files"],
            ["Clothes/Outfits/Biker.glb", "Iris.glb", "Motion/Idle/Idle 3 To Home.GLB", "Motion/Idle/Idle3.glb"],
        )


if __name__ == "__main__":
    unittest.main()
