"""Small, dependency-free regression tests for the monitoring serializer."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


SERIALIZER_PATH = Path(__file__).resolve().parents[1] / "bot" / "monitoring" / "serializer.py"
SPEC = importlib.util.spec_from_file_location("wzml_monitoring_serializer", SERIALIZER_PATH)
serializer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serializer)


class DummyStatus:
    engine = "TestEngine"

    def __init__(self):
        self.listener = SimpleNamespace(
            name="Example task",
            size=1024**3,
            download_start_time=1_700_000_000,
            mode=("Torrent", "Leech"),
            message=SimpleNamespace(
                id=987,
                chat=SimpleNamespace(id=-1001234567890),
                date=None,
            ),
        )

    async def status(self):
        return "Download"

    def gid(self):
        return "abcd1234"

    def name(self):
        return self.listener.name

    def progress(self):
        return "45.5%"

    def processed_bytes(self):
        return "465.92MB"

    def size(self):
        return "1GB"

    def speed(self):
        return "5MB/s"

    def eta(self):
        return "2m30s"


class DummyQbitStatus(DummyStatus):
    def __init__(self):
        super().__init__()
        self.engine = "qBit v5"
        self._info = SimpleNamespace(
            progress=0.645,
            downloaded=1_385_126_953,
            size=2_147_483_648,
            dlspeed=5_123_456,
            upspeed=123_456,
            eta=__import__("datetime").timedelta(seconds=180),
            num_seeds=9,
            num_leechs=2,
            uploaded=10_000_000,
            ratio=0.01,
            hash="abc123",
        )

    def progress(self):
        return "64.5%"

    def processed_bytes(self):
        return "1.29GB"  # Formatted method must not override raw integer.

    def size(self):
        return "2GB"  # Formatted method must not override raw integer.

    def speed(self):
        return "4.89MB/s"  # Formatted method must not override raw integer.

    def seed_speed(self):
        return "120KB/s"

    def eta(self):
        return "3m"


class MonitoringSerializerTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_bytes(self):
        self.assertEqual(serializer.parse_bytes("1.5GB"), int(1.5 * 1024**3))
        self.assertEqual(serializer.parse_bytes("512 KB"), 512 * 1024)
        self.assertEqual(serializer.parse_bytes(1234), 1234)
        self.assertIsNone(serializer.parse_bytes("Unknown"))

    def test_parse_seconds(self):
        self.assertEqual(serializer.parse_seconds("1h2m3s"), 3723)
        self.assertEqual(serializer.parse_seconds("02:30"), 150)
        self.assertEqual(serializer.parse_seconds("-"), None)

    def test_state_mapping(self):
        expected = {
            "Download": "downloading",
            "Upload": "uploading",
            "Clone": "cloning",
            "QueueDl": "queued_download",
            "QueueUp": "queued_upload",
            "Pause": "paused",
            "Archive": "archiving",
            "Extract": "extracting",
            "Split": "splitting",
            "CheckUp": "checking",
            "Seed": "seeding",
            "SamVid": "sample_processing",
            "Convert": "converting",
            "FFmpeg": "ffmpeg_processing",
            "YouTube": "youtube_processing",
            "Metadata": "metadata_processing",
            "Verifying": "checking",
            "UnknownEngineState": "other",
        }
        for raw, normalized in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(serializer.normalize_state(raw), normalized)

    async def test_raw_qbittorrent_values_take_precedence_over_formatted_methods(self):
        result = await serializer.serialize_task("qbit-1", DummyQbitStatus())
        self.assertEqual(result["progress_percent"], 64.5)
        self.assertEqual(result["processed_bytes"], 1_385_126_953)
        self.assertEqual(result["total_bytes"], 2_147_483_648)
        self.assertEqual(result["download_speed_bytes_per_second"], 5_123_456)
        self.assertEqual(result["upload_speed_bytes_per_second"], 123_456)
        self.assertEqual(result["eta_seconds"], 180)

    async def test_task_labels_redact_urls_and_absolute_paths(self):
        self.assertEqual(serializer._safe_label("https://example.test/file.mkv"), "[redacted-url]")
        self.assertEqual(serializer._safe_label("magnet:?xt=urn:btih:secret"), "[redacted-url]")
        self.assertEqual(serializer._safe_label("/root/private/Example.mkv"), "Example.mkv")
        self.assertEqual(serializer._safe_label("C:\\private\\Example.mkv"), "Example.mkv")

    async def test_serialize_task_fields_and_origin(self):
        result = await serializer.serialize_task("987", DummyStatus())
        self.assertEqual(result["task_key"], "987")
        self.assertEqual(result["gid"], "abcd1234")
        self.assertEqual(result["state"], "downloading")
        self.assertEqual(result["progress_percent"], 45.5)
        self.assertEqual(result["total_bytes"], 1024**3)
        self.assertEqual(result["download_speed_bytes_per_second"], 5 * 1024**2)
        self.assertEqual(result["eta_seconds"], 150)
        self.assertEqual(result["origin"]["chat_id"], -1001234567890)
        self.assertEqual(result["origin"]["message_id"], 987)
        self.assertEqual(result["mode_out"], "Leech")


if __name__ == "__main__":
    unittest.main()
