"""Deterministic tests for image observation provenance and freshness."""

from pathlib import Path
import shutil
import tempfile
import unittest

from kimiya import image


FIXTURE = Path(__file__).with_name("fixtures") / "screen.png"


class ImageObservationTests(unittest.TestCase):
    def test_png_observe_and_prepare(self):
        with tempfile.TemporaryDirectory() as directory:
            record = image.observe(FIXTURE, Path(directory) / "artifacts")
            self.assertTrue(record["exists"], record)
            self.assertEqual((record["width"], record["height"]), (400, 300))
            paths, metadata = image.prepare([record])
            self.assertEqual(paths, [record["preview_path"]])
            self.assertEqual(metadata[0]["sha"], record["sha"])

    def test_missing_image_is_failed_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.raf"
            record = image.observe(missing, Path(directory) / "artifacts")
            self.assertFalse(record["exists"])
            with self.assertRaisesRegex(image.ImageError, "does not exist"):
                image.prepare([record])

    def test_source_freshness_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            shutil.copyfile(FIXTURE, source)
            record = image.observe(source, Path(directory) / "artifacts")
            with source.open("ab") as handle:
                handle.write(b"changed")
            with self.assertRaisesRegex(image.ImageError, "source changed"):
                image.prepare([record])

    def test_preview_freshness_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            shutil.copyfile(FIXTURE, source)
            record = image.observe(source, Path(directory) / "artifacts")
            with Path(record["preview_path"]).open("ab") as handle:
                handle.write(b"changed")
            with self.assertRaisesRegex(image.ImageError, "preview changed"):
                image.prepare([record])

    @unittest.skipUnless(shutil.which("sips"), "macOS sips is unavailable")
    def test_external_decoder_route_used_for_raf_suffix(self):
        # The bytes are PNG so this is not a camera-codec conformance test.
        # It verifies the RAF dispatch, external process, JPEG preview, and
        # provenance path without checking a private photograph into Git.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.raf"
            shutil.copyfile(FIXTURE, source)
            record = image.observe(source, Path(directory) / "artifacts")
            self.assertTrue(record["exists"], record)
            self.assertEqual(record["decoder"], "sips")
            self.assertEqual(Path(record["preview_path"]).suffix, ".jpg")
            image.prepare([record])


if __name__ == "__main__":
    unittest.main()
