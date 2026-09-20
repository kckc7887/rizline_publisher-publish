import base64
import copy
import hashlib
import io
import json
import os
import struct
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from rizline_publisher.core import apply_overrides, atomic_write, build, json_bytes, max_combo, publish, read_json, relative_path, rollback, sha256, supplement_template, validate_catalog, validate_release, verify_remote_object
from rizline_publisher.__main__ import main
from rizline_publisher.upstream import Addressables, attach_achievements, chart_stats, parse_stats, verified_stats
from rizline_publisher.audio import acb_duration, utf_rows


def fixture():
    song = {"id": "Song.artist.0", "title": "Song", "artist": "Artist", "illustrator": None, "packId": "Disc 1", "packName": "Disc 1", "bpm": "150", "durationSeconds": None, "updatedAt": None, "coverPath": "covers/test.png", "audioPath": "audio/test.acb", "charts": [{"id": "chart.Song.artist.0.IN", "songId": "Song.artist.0", "difficulty": "IN", "level": "12+", "constant": 12.6, "designer": "Designer", "hit": 20, "combo": 56, "maxScore": 1001000, "riztimeHit": 10, "chartPath": "charts/test.json"}], "achievements": []}
    return {"schemaVersion": 1, "resourceVersion": "v141_example", "gameVersion": "2.7.1", "songs": [song]}


def overrides():
    return {"schemaVersion": 1, "songs": {}, "charts": {}, "statAliases": {}, "achievementSongs": {}}


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH9sAAAAASUVORK5CYII=")
CHART_JSON = json_bytes({"bPM": 150, "lines": []})


def cri_utf_table(fields):
    strings, binary, schema, row = b"Header\0", b"", b"", b""
    for name, value in fields.items():
        name_offset = len(strings)
        strings += name.encode() + b"\0"
        kind = 11 if isinstance(value, bytes) else 4
        schema += bytes([0x50 | kind]) + struct.pack(">I", name_offset)
        if kind == 11:
            row += struct.pack(">II", len(binary), len(value))
            binary += value
        else:
            row += struct.pack(">I", value)
    rows_at = 32 + len(schema)
    strings_at = rows_at + len(row)
    binary_at = strings_at + len(strings)
    size = binary_at + len(binary)
    header = b"@UTF" + struct.pack(">IHHIIIHHI", size - 8, 1, rows_at - 8, strings_at - 8, binary_at - 8, 0, len(fields), len(row), 1)
    return header + schema + row + strings + binary


def sample_acb(samples=1720):
    hca = b"HCA\0" + struct.pack(">HH", 0x300, 32) + b"fmt\0" + b"\2" + (44100).to_bytes(3, "big") + struct.pack(">IHH", 2, 128, 200) + b"comp" + struct.pack(">H", 10) + b"\0\0" + bytes(20)
    bank = b"AFS2" + bytes([2, 4]) + struct.pack("<HIHH", 4, 1, 32, 0) + struct.pack("<III", 0, 28, 32 + len(hca)) + bytes(4) + hca
    return cri_utf_table({"WaveformTable": cri_utf_table({"NumSamples": samples, "SamplingRate": 44100}), "AwbFile": bank})


def write_fixture_assets(root):
    atomic_write(root / "covers/test.png", PNG)
    atomic_write(root / "audio/test.acb", sample_acb())
    atomic_write(root / "charts/test.json", CHART_JSON)


class MemoryS3:
    def __init__(self):
        self.values, self.order, self.requests, self.events = {}, [], [], []
        self.fail, self.corrupt_upload, self.race = False, False, None
        self.list_requests, self.delete_requests = [], []
        self.page_size, self.failed_deletions, self.ignored_deletions = 1000, set(), set()
        self.lock = threading.RLock()
        self.meta = SimpleNamespace(events=Mock())

    @staticmethod
    def error(status):
        from botocore.exceptions import ClientError
        return ClientError({"ResponseMetadata": {"HTTPStatusCode": status}, "Error": {"Code": str(status)}}, "Object")

    def get_object(self, Bucket, Key):
        with self.lock:
            self.events.append(("get", Key))
            if Key not in self.values:
                raise self.error(404)
            value = self.values[Key]
            return {"ContentLength": value["ContentLength"], "Metadata": value["Metadata"], "Body": io.BytesIO(value["Body"]), "ETag": chr(34) + hashlib.md5(value["Body"], usedforsecurity=False).hexdigest() + chr(34)}

    def put_object(self, **kwargs):
        with self.lock:
            return self._put_object(**kwargs)

    def _put_object(self, **kwargs):
        if self.fail:
            raise OSError("upload failed")
        data, key = kwargs["Body"], kwargs["Key"]
        expected_md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")
        if kwargs.get("ContentMD5") != expected_md5:
            raise self.error(400)
        self.requests.append(kwargs)
        if self.race and key != "rizline/current.json":
            raced = data if self.race == "identical" else bytes([data[0] ^ 1]) + data[1:]
            self.values[key] = {"ContentLength": len(data), "Metadata": kwargs["Metadata"], "Body": raced}
            self.race = None
            raise self.error(412)
        if "IfMatch" in kwargs and (key not in self.values or kwargs["IfMatch"] != chr(34) + hashlib.md5(self.values[key]["Body"], usedforsecurity=False).hexdigest() + chr(34)):
            raise self.error(412)
        if kwargs.get("IfNoneMatch") == "*" and key in self.values:
            raise self.error(412)
        self.order.append(key)
        self.events.append(("put", key))
        self.values[key] = {"ContentLength": len(data), "Metadata": kwargs["Metadata"], "Body": bytes([data[0] ^ 1]) + data[1:] if self.corrupt_upload else data}

    def seed(self, key, data=b"old resource"):
        self.values[key] = {"ContentLength": len(data), "Metadata": {"sha256": sha256(data)}, "Body": data}

    def list_objects_v2(self, **kwargs):
        self.list_requests.append(kwargs)
        keys = sorted(key for key in self.values if key.startswith(kwargs["Prefix"]) and key > kwargs.get("ContinuationToken", ""))
        page = keys[:min(kwargs["MaxKeys"], self.page_size)]
        response = {"IsTruncated": len(page) < len(keys), "Contents": [{"Key": key} for key in page]}
        if response["IsTruncated"]:
            response["NextContinuationToken"] = page[-1]
        return response

    def copy_object(self, Bucket, Key, CopySource, **kwargs):
        source = CopySource["Key"] if isinstance(CopySource, dict) else str(CopySource).split("/", 1)[-1]
        with self.lock:
            self.events.append(("copy", source, Key))
            if source not in self.values:
                raise self.error(404)
            data = self.values[source]["Body"]
            self.values[Key] = {"ContentLength": len(data), "Metadata": dict(self.values[source].get("Metadata") or {}), "Body": data}

    def head_object(self, Bucket, Key):
        with self.lock:
            self.events.append(("head", Key))
            if Key not in self.values:
                raise self.error(404)
            return {"ContentLength": self.values[Key]["ContentLength"]}

    def delete_object(self, Bucket, Key):
        with self.lock:
            self.events.append(("delete_single", Key))
            self.values.pop(Key, None)

    def delete_objects(self, **kwargs):
        self.delete_requests.append(kwargs)
        response = {"Deleted": [], "Errors": []}
        for item in kwargs["Delete"]["Objects"]:
            key = item["Key"]
            self.events.append(("delete", key))
            if key in self.failed_deletions:
                response["Errors"].append({"Key": key, "Code": "AccessDenied"})
            else:
                if key not in self.ignored_deletions:
                    self.values.pop(key, None)
                response["Deleted"].append({"Key": key})
        return response


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.override, self.output = self.root / "work/catalog.json", self.root / "overrides.json", self.root / "dist"
        atomic_write(self.source, json_bytes(fixture()))
        # Local PNG/ACB/JSON fixtures; no network is needed for tests.
        write_fixture_assets(self.source.parent)
        atomic_write(self.override, json_bytes(overrides()))

    def tearDown(self):
        self.temp.cleanup()

    def execute_publish(self, client):
        with patch("boto3.Session") as session:
            session.return_value.client.return_value = client
            return publish(self.output, execute=True, workers=2)

    def test_build_is_deterministic_preserves_input_and_override(self):
        patch_value = overrides()
        patch_value["songs"]["Song.artist.0"] = {"updatedAt": "2026-09-11", "durationSeconds": 123.456}
        atomic_write(self.override, json_bytes(patch_value))
        before = self.override.read_bytes(), self.source.read_bytes()
        first = build(self.source, self.override, self.output)
        pointer = (self.output / "rizline/current.json").read_bytes()
        self.assertEqual(first, build(self.source, self.override, self.output))
        self.assertEqual(pointer, (self.output / "rizline/current.json").read_bytes())
        self.assertEqual(before, (self.override.read_bytes(), self.source.read_bytes()))
        self.assertEqual(first["missingUpdateDates"], 0)
        current = read_json(self.output / "rizline/current.json")
        manifest = read_json(self.output / current["manifestPath"])
        catalog = read_json(self.output / manifest["catalogPath"])
        paths = {asset["path"] for asset in manifest["files"]}
        song = catalog["songs"][0]
        self.assertTrue(song["audioPath"].endswith(".acb") and song["audioPath"] in paths)
        self.assertTrue(song["charts"][0]["chartPath"].endswith(".json") and song["charts"][0]["chartPath"] in paths)
        self.assertEqual((self.output / song["audioPath"]).read_bytes(), sample_acb())
        self.assertEqual((self.output / song["charts"][0]["chartPath"]).read_bytes(), CHART_JSON)
        template = read_json(self.source.parent / "supplement-template.json")
        self.assertNotIn("updatedAt", template["songs"]["Song.artist.0"])

    def test_supplement_template_lists_full_ids_and_only_missing_fields(self):
        catalog = fixture()
        special = copy.deepcopy(catalog["songs"][0])
        special["id"] = "Song.artist.1"
        special["charts"][0].update(id="chart.Song.artist.1.SP", songId=special["id"], difficulty="SP", level="竹", constant=None, maxScore=None, riztimeHit=None)
        catalog["songs"].append(special)
        template = supplement_template(catalog)
        self.assertEqual(template["songs"]["Song.artist.0"], {"illustrator": None, "durationSeconds": None, "updatedAt": None})
        self.assertEqual(template["charts"], {"chart.Song.artist.1.SP": {"maxScore": None, "riztimeHit": None}})
        self.assertNotIn("constant", template["charts"]["chart.Song.artist.1.SP"])
        self.assertEqual(set(template), set(overrides()))

    def test_override_edit_creates_new_release(self):
        first = build(self.source, self.override, self.output)
        patched = overrides()
        patched["charts"]["chart.Song.artist.0.IN"] = {"designer": "Correct designer"}
        atomic_write(self.override, json_bytes(patched))
        second = build(self.source, self.override, self.output)
        self.assertNotEqual(first["resourceVersion"], second["resourceVersion"])
        self.assertTrue((self.output / "rizline/releases" / first["resourceVersion"] / "catalog.json").exists())

    def test_corruption_is_detected(self):
        build(self.source, self.override, self.output)
        current = read_json(self.output / "rizline/current.json")
        manifest = read_json(self.output / current["manifestPath"])
        (self.output / manifest["files"][0]["path"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "integrity"):
            validate_release(self.output)

    def test_rollback_validates_old_release_before_switching(self):
        first = build(self.source, self.override, self.output)
        value = overrides()
        value["songs"]["Song.artist.0"] = {"title": "New title"}
        atomic_write(self.override, json_bytes(value))
        second = build(self.source, self.override, self.output)
        rollback(self.output, first["resourceVersion"])
        self.assertEqual(validate_release(self.output)["resourceVersion"], first["resourceVersion"])
        before = (self.output / "rizline/current.json").read_bytes()
        (self.output / "rizline/releases" / second["resourceVersion"] / "catalog.json").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "integrity"):
            rollback(self.output, second["resourceVersion"])
        self.assertEqual(before, (self.output / "rizline/current.json").read_bytes())

    def test_paths_and_references_fail_closed(self):
        for path in ("../outside", "/absolute", "a\\b", "a//b", "a/./b", "https://example.com/x", "x?y"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                relative_path(path)
        value = fixture()
        value["songs"][0]["charts"][0]["songId"] = "wrong"
        with self.assertRaisesRegex(ValueError, "mislinked"):
            validate_catalog(value)

    def test_sp_stays_independent_and_cannot_get_constant(self):
        value = fixture()
        special = copy.deepcopy(value["songs"][0])
        special["id"] = "Song.artist.1"
        special["charts"][0].update(id="chart.Song.artist.1.SP", songId=special["id"], difficulty="SP", level="竹", constant=None)
        value["songs"].append(special)
        self.assertEqual(validate_catalog(value)["songs"], 2)
        special["charts"][0]["constant"] = 12
        with self.assertRaisesRegex(ValueError, "SP"):
            validate_catalog(value)

    def test_unknown_override_id_does_not_silently_disappear(self):
        value = overrides()
        value["songs"]["typo"] = {"title": "Edited"}
        with self.assertRaisesRegex(ValueError, "Unknown override"):
            apply_overrides(fixture(), value)

    def test_audio_and_chart_paths_cannot_be_overridden(self):
        songs, charts = overrides(), overrides()
        songs["songs"]["Song.artist.0"] = {"audioPath": "audio/other.acb"}
        charts["charts"]["chart.Song.artist.0.IN"] = {"chartPath": "charts/other.json"}
        for value in (songs, charts):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Unsupported override"):
                apply_overrides(fixture(), value)

    def test_dry_run_needs_no_configuration_or_credentials(self):
        build(self.source, self.override, self.output)
        result = publish(self.output)
        self.assertFalse(result["execute"])
        self.assertEqual(result["workers"], 4)
        self.assertEqual(result["comparison"], "file-identity")
        self.assertEqual(result["onChange"], "date-prefix-delta-copy")
        self.assertEqual(result["uploadOrder"][-1], "rizline/current.json")
        with patch("boto3.Session") as session, self.assertRaisesRegex(RuntimeError, "No AWS credentials"):
            session.return_value.get_credentials.return_value = None
            publish(self.output, execute=True)

    def test_cli_publishes_with_only_two_keys_and_builtin_destination(self):
        build(self.source, self.override, self.output)
        client, output = MemoryS3(), io.StringIO()
        credentials = {"AWS_ACCESS_KEY_ID": "test-access-key", "AWS_SECRET_ACCESS_KEY": "test-secret-key"}
        # Use the real boto3 environment credential provider; replace only its HTTP client.
        with patch.dict(os.environ, credentials, clear=True), patch("boto3.Session.client", return_value=client) as factory, patch("rizline_publisher.publication.verify_conditional_writes", return_value={"conditionalWrites": True}), patch("rizline_publisher.publication.verify_object_copy", return_value={"objectCopy": True}), patch("rizline_publisher.publication.beijing_release_date", return_value="2026-09-14"), redirect_stdout(output):
            self.assertEqual(main(["--output", str(self.output), "publish", "--execute"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["endpoint"], "https://cn-nb1.rains3.com")
        self.assertEqual(result["region"], "us-east-1")
        self.assertEqual(result["bucket"], "rranker-rizline-data")
        self.assertEqual(factory.call_args.kwargs["endpoint_url"], result["endpoint"])
        self.assertEqual(client.order[-1], "rizline/current.json")
        self.assertEqual(client.values["rizline/current.json"]["Body"], (self.output.parent / "work/publication-release/rizline/current.json").read_bytes())

    def test_publish_workers_are_bounded_and_cli_forwards_the_selection(self):
        build(self.source, self.override, self.output)
        for workers in (0, 17, True, 2.5, "4"):
            with self.subTest(workers=workers), self.assertRaisesRegex(ValueError, "between 1 and 16"):
                publish(self.output, workers=workers)
        self.assertEqual(publish(self.output, workers=16)["workers"], 16)
        with patch("rizline_publisher.__main__.publish", return_value={}) as publisher, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["publish", "--workers", "6"]), 0)
            self.assertEqual(publisher.call_args.kwargs["workers"], 6)
        for workers in ("0", "17"):
            with self.subTest(workers=workers), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(["publish", "--workers", workers])
            self.assertEqual(error.exception.code, 2)



class ImportTests(unittest.TestCase):
    def test_combo_boundaries_and_hold_counts(self):
        self.assertEqual([max_combo(n) for n in (0, 5, 6, 8, 9, 11, 12, 537)], [0, 5, 7, 11, 14, 20, 24, 2124])
        self.assertEqual(chart_stats({"bPM": 150, "lines": [{"notes": [{"type": 0}, {"type": 1}, {"type": 2}]}]})[0], {"hit": 4, "combo": 4})

    def test_statistics_are_parsed_as_data_and_hit_must_match(self):
        value = parse_stats(b'let songAllData = [\n// comment\n{"name":"Song","IN":{"mHit":20,"mH":10}},\n];')
        chart = fixture()["songs"][0]["charts"][0]
        self.assertEqual(verified_stats(value[0], chart), {"riztimeHit": 10, "maxScore": 1001000})
        chart["hit"] = 21
        self.assertIsNone(verified_stats(value[0], chart))
        with self.assertRaises(ValueError):
            parse_stats(b'let songAllData = [process.exit()];')

    def test_achievements_do_not_attach_to_sp_by_same_title(self):
        songs = fixture()["songs"]
        special = copy.deepcopy(songs[0])
        special["id"] = "Song.artist.1"
        special["charts"][0]["difficulty"] = "SP"
        songs.append(special)
        attach_achievements(songs, {"ach.song.name": "First", "ach.song.desc": 'Play “Song” at 118%'}, overrides())
        self.assertEqual(len(songs[0]["achievements"]), 1)
        self.assertEqual(songs[1]["achievements"], [])

    def test_general_perfect_achievement_is_not_reported_as_unknown_song(self):
        songs = fixture()["songs"]
        unresolved = attach_achievements(songs, {"ach.any.name": "Any", "ach.any.desc": '游玩任意关卡并获得 “PERFECT” 评价'}, overrides())
        self.assertEqual(unresolved, [])
        self.assertEqual(songs[0]["achievements"], [])

    def test_addressables_handles_long_keys_and_multiple_dependencies(self):
        integer = lambda n: struct.pack("<i", n)
        key = "long-key-" + "x" * 300
        keys = b"\0" + integer(len(key)) + key.encode() + b"\4" + integer(7)
        buckets = integer(2) + integer(0) + integer(1) + integer(0) + integer(5 + len(key)) + integer(2) + integer(1) + integer(2)
        entry = lambda internal, dep: b"".join(integer(n) for n in (internal, 0, dep, 0, 0, 0, 0))
        entries = integer(3) + entry(0, 1) + entry(1, -1) + entry(2, -1)
        data = {"m_InternalIds": ["asset", "https://cdn/default/a.bundle", "https://cdn/default/b.bundle"], "m_KeyDataString": base64.b64encode(keys).decode(), "m_BucketDataString": base64.b64encode(buckets).decode(), "m_EntryDataString": base64.b64encode(entries).decode()}
        self.assertEqual(Addressables(data).bundles(key), data["m_InternalIds"][1:])

    def test_bpm_range_uses_actual_tempo_shifts(self):
        _, bpm = chart_stats({"bPM": 150, "bpmShifts": [{"value": 0.866667}, {"value": 2.1}], "lines": []})
        self.assertEqual(bpm, (130.0, 315.0))


class AudioTests(unittest.TestCase):
    def audio(self, samples=1720):
        return sample_acb(samples)

    def test_duration_uses_real_samples_excluding_codec_padding(self):
        self.assertAlmostEqual(acb_duration(self.audio()), 1720 / 44100, places=6)

    def test_audio_rejects_mismatch_and_truncated_metadata(self):
        with self.assertRaisesRegex(ValueError, "disagrees"):
            acb_duration(self.audio(samples=2048))
        with self.assertRaisesRegex(ValueError, "Invalid CRI UTF"):
            acb_duration(self.audio()[:-1])


if __name__ == "__main__":
    unittest.main()
