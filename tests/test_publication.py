import base64
import copy
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.etree import ElementTree

from test_publisher import MemoryS3, fake_transcode, fixture, overrides, sample_acb, write_fixture_assets
from rizline_publisher.core import (atomic_write, build, json_bytes, parallel_map,
                                   publish, read_json, sha256, validate_release)
from rizline_publisher.audio import acb_duration
from rizline_publisher.publication import (CURRENT, _delete_objects_content_md5,
                                          beijing_release_date, cleanup_snapshot, manifests_equal,
                                          make_client, next_date_name, retry_cleanup, verify_remote_object)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.override, self.output = self.root / "source/catalog.json", self.root / "overrides.json", self.root / "dist"
        atomic_write(self.source, json_bytes(fixture()))
        write_fixture_assets(self.source.parent)
        atomic_write(self.override, json_bytes(overrides()))
        self.transcode = patch("rizline_publisher.transcode.transcode_acb", side_effect=fake_transcode)
        self.transcode.start()
        self.addCleanup(self.transcode.stop)
        build(self.source, self.override, self.output)
        self.client = MemoryS3()

    def execute(self, **kwargs):
        with patch("rizline_publisher.publication.make_client", return_value=self.client), \
                patch("rizline_publisher.publication.verify_conditional_writes", return_value={"conditionalWrites": True}), \
                patch("rizline_publisher.publication.verify_object_copy", return_value={"objectCopy": True}), \
                patch("rizline_publisher.publication.beijing_release_date", return_value="2026-09-14"):
            return publish(self.output, execute=True, workers=2, **kwargs)

    def update(self, title="Changed"):
        data = overrides()
        data["songs"] = {"Song.artist.0": {"title": title}}
        atomic_write(self.override, json_bytes(data))
        build(self.source, self.override, self.output)

    def report(self):
        return read_json(self.root / "work/publication-report.json")["publication"]

    def pointer(self):
        return json.loads(self.client.values[CURRENT]["Body"])

    def seed_build(self):
        selected = read_json(self.output / CURRENT)
        manifest = read_json(self.output / selected["manifestPath"])
        for path in [a["path"] for a in manifest["files"]] + [selected["manifestPath"], CURRENT]:
            self.client.seed(path, (self.output / path).read_bytes())
        return selected, manifest

    def rewrite_remote_audio_extension(self, current, manifest, new_suffix):
        catalog_path = manifest["catalogPath"]
        catalog = json.loads(self.client.values[catalog_path]["Body"])
        files = []
        for asset in manifest["files"]:
            asset = dict(asset)
            path = asset["path"]
            if path.endswith(".m4a") or path.endswith(".acb"):
                new_path = path.rsplit(".", 1)[0] + new_suffix
                self.client.values[new_path] = self.client.values.pop(path)
                asset["path"] = new_path
            files.append(asset)
        for song in catalog["songs"]:
            song["audioPath"] = song["audioPath"].rsplit(".", 1)[0] + new_suffix
        catalog_bytes = json_bytes(catalog)
        catalog_asset = next(asset for asset in files if asset["path"] == catalog_path)
        catalog_asset["size"] = len(catalog_bytes)
        catalog_asset["sha256"] = sha256(catalog_bytes)
        self.client.seed(catalog_path, catalog_bytes)
        rewritten = dict(manifest)
        rewritten["files"] = files
        raw = json_bytes(rewritten)
        pointer = dict(current)
        pointer["manifestSha256"] = sha256(raw)
        self.client.seed(pointer["manifestPath"], raw)
        self.client.seed(CURRENT, json_bytes(pointer))
        return pointer, rewritten

    def test_full_upload_is_isolated_conditional_and_archives_actual_release(self):
        original = (self.output / CURRENT).read_bytes()
        result = self.execute()
        selected = self.pointer()
        self.assertEqual(selected["resourceVersion"], "2026-09-14")
        self.assertNotEqual(selected["resourceVersion"], json.loads(original)["resourceVersion"])
        self.assertEqual((self.output / CURRENT).read_bytes(), original)
        self.assertEqual(validate_release(self.root / "work/publication-release")["resourceVersion"], selected["resourceVersion"])
        self.assertEqual(result["publication"]["uploaded"], 6)
        self.assertEqual(result["publication"]["copied"], 0)
        self.assertEqual(self.client.order[-2:], [selected["manifestPath"], CURRENT])
        for request in self.client.requests:
            self.assertEqual(request["IfNoneMatch"], "*")
            self.assertIn("ContentMD5", request)
            self.assertEqual(request["CacheControl"], "no-cache" if request["Key"] == CURRENT else "public, max-age=31536000, immutable")
            if request["Key"].endswith(".m4a"):
                self.assertEqual(request["ContentType"], "audio/mp4")
        for path in self.client.order[:-2]:
            self.assertLess(self.client.events.index(("put", path)), self.client.events.index(("get", path)))
            self.assertLess(self.client.events.index(("get", path)), self.client.events.index(("put", selected["manifestPath"])))
            self.assertTrue(path.startswith("rizline/releases/2026-09-14/"))

    def test_storage_probe_failure_stops_before_resources_and_noop_does_not_probe(self):
        with patch("rizline_publisher.publication.make_client", return_value=self.client), patch("rizline_publisher.publication.verify_conditional_writes", side_effect=RuntimeError("conditional writes unsupported")) as probe:
            with self.assertRaisesRegex(RuntimeError, "conditional writes unsupported"):
                publish(self.output, execute=True)
        probe.assert_called_once_with(self.client, "rranker-rizline-data", "rizline")
        self.assertFalse(self.client.order)
        self.assertEqual(self.report()["phase"], "verify-storage")
        self.execute()
        with patch("rizline_publisher.publication.make_client", return_value=self.client), patch("rizline_publisher.publication.verify_conditional_writes", side_effect=AssertionError("No probe for unchanged release")) as probe:
            result = publish(self.output, execute=True)
        self.assertEqual(result["publication"]["status"], "unchanged")
        probe.assert_not_called()

    def test_equal_manifest_skips_all_cover_get_put_and_listing_after_rebase(self):
        self.execute()
        selected = self.pointer()
        self.client.events.clear()
        self.client.requests.clear()
        self.client.list_requests.clear()
        result = self.execute()
        self.assertEqual(result["publication"]["status"], "unchanged")
        self.assertEqual(result["publication"]["uploaded"], 0)
        self.assertEqual(result["publication"]["copied"], 0)
        self.assertFalse(self.client.requests)
        self.assertFalse(self.client.list_requests)
        self.assertEqual(self.client.events, [("get", CURRENT), ("get", selected["manifestPath"]), ("get", selected["manifestPath"].replace("manifest.json", "catalog.json"))])
        self.assertEqual((self.root / "work/publication-release" / CURRENT).read_bytes(), self.client.values[CURRENT]["Body"])

    def test_catalog_change_copies_cover_uses_date_suffix_and_sweeps_leftovers(self):
        first = self.execute()
        live = "rizline/releases/" + first["publication"]["resourceVersion"] + "/"
        previous = set(self.client.values) - {CURRENT}
        leftover = "rizline/releases/another-staged-release/cover.png"
        self.client.seed(leftover)
        self.client.seed("other/object")
        self.update()
        self.client.requests.clear()
        self.client.events.clear()
        self.client.order.clear()
        result = self.execute()
        selected = self.pointer()
        self.assertEqual(selected["resourceVersion"], "2026-09-14-2")
        self.assertEqual(result["publication"]["copied"], 3)
        self.assertEqual(result["publication"]["uploaded"], 3)
        self.assertEqual(len([r for r in self.client.requests if r["Key"].endswith(".png")]), 0)
        copies = [event for event in self.client.events if event[0] == "copy"]
        self.assertEqual(len(copies), 3)
        self.assertEqual(sorted(event[1].rsplit(".", 1)[-1] for event in copies), ["json", "m4a", "png"])
        self.assertTrue(copies[0][2].startswith("rizline/releases/2026-09-14-2/"))
        self.assertTrue(previous.isdisjoint(self.client.values))
        self.assertNotIn(leftover, self.client.values)
        self.assertIn("other/object", self.client.values)
        self.assertEqual(result["publication"]["deleted"], len(previous) + 1)
        self.assertIn("IfMatch", self.client.requests[-1])
        self.assertFalse(any(key.startswith(live) for key in self.client.order))
        self.assertFalse(any(event[0] == "copy" and event[2].startswith(live) for event in self.client.events))
        self.assertTrue(any(request["Prefix"] == "rizline/releases/" for request in self.client.list_requests))

    def test_missing_or_corrupt_manifest_at_same_deterministic_version_full_uploads_fresh_prefix(self):
        for corrupted in (False, True):
            with self.subTest(corrupted=corrupted):
                self.client = MemoryS3()
                old, _ = self.seed_build()
                if corrupted:
                    self.client.seed(old["manifestPath"], b"not the manifest")
                else:
                    del self.client.values[old["manifestPath"]]
                result = self.execute(
                    cleanup_receipt=self.root / "work/cleanup-receipts" / f"corrupt-{corrupted}.json",
                    publication_output=self.root / "work" / f"publication-corrupt-{corrupted}",
                    report_path=self.root / "work" / f"publication-report-corrupt-{corrupted}.json",
                )
                self.assertEqual(result["publication"]["uploaded"], 6)
                self.assertNotEqual(self.pointer()["resourceVersion"], old["resourceVersion"])
                self.assertTrue(all(old["resourceVersion"] + "/" not in key for key in self.client.order))

    def test_remote_catalog_bytes_must_verify_before_equal_comparison(self):
        old, manifest = self.seed_build()
        self.client.seed(manifest["catalogPath"], b"unverified catalogue")
        result = self.execute()
        self.assertEqual(result["publication"]["status"], "published")
        self.assertIn("digest mismatch", result["publication"]["comparisonReason"])
        self.assertNotEqual(self.pointer(), old)

    def test_changed_non_catalog_hash_in_valid_manifest_forces_full_upload(self):
        old, manifest = self.seed_build()
        next(asset for asset in manifest["files"] if asset["path"].endswith(".png"))["sha256"] = "0" * 64
        raw = json_bytes(manifest)
        old["manifestSha256"] = sha256(raw)
        self.client.seed(old["manifestPath"], raw)
        self.client.seed(CURRENT, json_bytes(old))
        result = self.execute()
        self.assertEqual(result["publication"]["uploaded"], 4)
        self.assertEqual(result["publication"]["copied"], 2)
        self.assertEqual(result["publication"]["comparisonReason"], "different-resource-set")

    def test_remote_permission_error_does_not_mean_missing_manifest(self):
        old, _ = self.seed_build()
        original_get = self.client.get_object
        def denied(Bucket, Key):
            if Key == old["manifestPath"]:
                raise MemoryS3.error(403)
            return original_get(Bucket, Key)
        with patch.object(self.client, "get_object", side_effect=denied), self.assertRaisesRegex(RuntimeError, "403"):
            self.execute()
        self.assertFalse(self.client.order)

    def test_parallel_resource_verifications_are_a_barrier_before_manifest_and_pointer(self):
        active, release = threading.Barrier(3), threading.Event()
        def blocked(client, path, size, digest, *args, **kwargs):
            if path.endswith((".png", "catalog.json")):
                active.wait(timeout=5)
                self.assertTrue(release.wait(5))
            return verify_remote_object(client, path, size, digest, *args, **kwargs)
        with patch("rizline_publisher.publication.verify_remote_object", side_effect=blocked), ThreadPoolExecutor(max_workers=1) as runner:
            pending = runner.submit(self.execute)
            try:
                active.wait(timeout=5)
                self.assertNotIn(CURRENT, self.client.values)
                self.assertFalse(any(key.endswith("manifest.json") for key in self.client.values))
            finally:
                release.set()
            pending.result(timeout=5)

    def test_parallel_failure_joins_inflight_and_does_not_publish_manifest(self):
        running, fail_now, release = threading.Event(), threading.Event(), threading.Event()
        def blocked(client, path, size, digest, *args, **kwargs):
            if path.endswith(".png"):
                running.set()
                self.assertTrue(release.wait(5))
            elif path.endswith("catalog.json"):
                self.assertTrue(running.wait(5))
                fail_now.set()
                raise OSError("resource verification failed")
            return verify_remote_object(client, path, size, digest, *args, **kwargs)
        with patch("rizline_publisher.publication.verify_remote_object", side_effect=blocked), ThreadPoolExecutor(max_workers=1) as runner:
            pending = runner.submit(self.execute)
            try:
                self.assertTrue(fail_now.wait(5))
                with self.assertRaises(TimeoutError):
                    pending.result(timeout=0.05)
            finally:
                release.set()
            with self.assertRaisesRegex(RuntimeError, "resource verification failed"):
                pending.result(timeout=5)
        self.assertNotIn(CURRENT, self.client.values)
        self.assertFalse(any(key.endswith("manifest.json") for key in self.client.values))
        self.assertEqual(self.report()["phase"], "upload-resources")

    def test_failed_leftover_date_prefix_is_reused_after_wipe(self):
        leftover = "rizline/releases/2026-09-14/covers/leftover.png"
        self.client.seed(leftover)
        result = self.execute()
        self.assertEqual(result["publication"]["resourceVersion"], "2026-09-14")
        self.assertNotIn(leftover, self.client.values)
        self.assertTrue(any(key.startswith("rizline/releases/2026-09-14/") for key in self.client.values))

    def test_delta_only_refuses_full_upload_when_remote_cannot_be_compared(self):
        old, manifest = self.seed_build()
        self.client.seed(manifest["catalogPath"], b"unverified catalogue")
        with self.assertRaisesRegex(RuntimeError, "Delta-only publication requires a comparable remote release"):
            self.execute(delta_only=True)
        self.assertEqual(self.pointer(), old)
        self.assertFalse(self.client.order)
        self.assertEqual(self.report()["phase"], "compare")

    def test_delta_only_still_copies_unchanged_files_when_catalog_changes(self):
        self.execute()
        previous = set(self.client.values) - {CURRENT}
        self.update()
        result = self.execute(delta_only=True)
        self.assertEqual(result["publication"]["copied"], 3)
        self.assertGreater(result["publication"]["uploaded"], 0)
        self.assertTrue(previous.isdisjoint(self.client.values))

    def test_delta_only_accepts_previous_acb_catalog_and_uploads_m4a(self):
        current, manifest = self.seed_build()
        self.rewrite_remote_audio_extension(current, manifest, ".acb")
        result = self.execute(delta_only=True)
        selected = self.pointer()
        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(result["publication"]["copied"], 2)
        self.assertGreaterEqual(result["publication"]["uploaded"], 2)
        catalog = json.loads(self.client.values[selected["manifestPath"].replace("manifest.json", "catalog.json")]["Body"])
        self.assertTrue(all(song["audioPath"].endswith(".m4a") for song in catalog["songs"]))
        self.assertTrue(any(key.endswith(".m4a") for key in self.client.values))
        self.assertFalse(any(key.endswith(".acb") for key in self.client.values))

    def test_identical_resource_put_precondition_is_treated_as_already_uploaded(self):
        self.client.race = "identical"
        result = self.execute()
        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(self.pointer()["resourceVersion"], "2026-09-14")

    def test_conflicting_resource_put_precondition_still_fails_before_pointer(self):
        self.client.race = "conflict"
        with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
            self.execute()
        self.assertNotIn(CURRENT, self.client.values)
        self.assertEqual(self.report()["phase"], "upload-resources")

    def test_date_suffix_advances_only_for_the_same_beijing_day(self):
        self.assertEqual(next_date_name(None, "2026-09-14"), "2026-09-14")
        self.assertEqual(next_date_name("3.20.0-uuid", "2026-09-14"), "2026-09-14")
        self.assertEqual(next_date_name("2026-09-14", "2026-09-14"), "2026-09-14-2")
        self.assertEqual(next_date_name("2026-09-14-2", "2026-09-14"), "2026-09-14-3")
        self.assertEqual(next_date_name("2026-09-13", "2026-09-14"), "2026-09-14")
        self.assertEqual(beijing_release_date(), beijing_release_date())

    def test_corrupt_uploaded_bytes_or_manifest_never_switch_old_current(self):
        png = self.source.parent / "covers/test.png"
        original_png = png.read_bytes()
        for target in (".png", "manifest.json"):
            with self.subTest(target=target):
                png.write_bytes(original_png)
                atomic_write(self.override, json_bytes(overrides()))
                build(self.source, self.override, self.output)
                self.client = MemoryS3()
                old, _ = self.seed_build()
                if target == ".png":
                    png.write_bytes(original_png + b"\x00")
                    build(self.source, self.override, self.output)
                else:
                    self.update("Changed")
                original_put = self.client.put_object
                def corrupt(**kwargs):
                    original_put(**kwargs)
                    if kwargs["Key"].endswith(target):
                        value = self.client.values[kwargs["Key"]]
                        value["Body"] = bytes([value["Body"][0] ^ 1]) + value["Body"][1:]
                label = "png" if target == ".png" else "manifest"
                with patch.object(self.client, "put_object", side_effect=corrupt), self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                    self.execute(
                        cleanup_receipt=self.root / "work/cleanup-receipts" / f"corrupt-{label}.json",
                        publication_output=self.root / "work" / f"publication-corrupt-{label}",
                        report_path=self.root / "work" / f"publication-report-corrupt-{label}.json",
                    )
                self.assertEqual(self.pointer(), old)
                self.assertFalse(self.client.delete_requests)

    def test_competing_pointer_update_rejects_cas_and_preserves_other_publisher(self):
        self.seed_build()
        self.update()
        original_put = self.client.put_object
        rival = b"another publisher won"
        def raced(**kwargs):
            if kwargs["Key"] == CURRENT:
                self.client.seed(CURRENT, rival)
            return original_put(**kwargs)
        with patch.object(self.client, "put_object", side_effect=raced), self.assertRaisesRegex(RuntimeError, "412"):
            self.execute()
        self.assertEqual(self.client.values[CURRENT]["Body"], rival)
        self.assertFalse(self.client.delete_requests)
        self.assertIsNone(self.report()["currentSwitched"])

    def test_first_pointer_race_also_requires_if_none_match(self):
        original_put = self.client.put_object
        def raced(**kwargs):
            if kwargs["Key"] == CURRENT:
                self.client.seed(CURRENT, b"first writer")
            return original_put(**kwargs)
        with patch.object(self.client, "put_object", side_effect=raced), self.assertRaisesRegex(RuntimeError, "412"):
            self.execute()
        self.assertFalse(self.client.delete_requests)

    def test_failed_current_readback_keeps_all_old_keys_and_retry_receipt(self):
        self.seed_build()
        old_keys = set(self.client.values) - {CURRENT}
        self.update()
        original_put = self.client.put_object
        def corrupt(**kwargs):
            original_put(**kwargs)
            if kwargs["Key"] == CURRENT:
                self.client.seed(CURRENT, b"corrupt current")
        with patch.object(self.client, "put_object", side_effect=corrupt), self.assertRaisesRegex(RuntimeError, "current changed"):
            self.execute()
        self.assertTrue(old_keys.issubset(self.client.values))
        self.assertEqual(set(self.report()["remainingDeletionKeys"]), old_keys)
        receipt = read_json(self.report()["cleanupReceipt"])
        self.assertEqual(set(receipt["snapshotKeys"]), old_keys)

    def test_partial_cleanup_can_retry_exact_remaining_keys_without_upload(self):
        self.seed_build()
        old_keys = set(self.client.values) - {CURRENT}
        failure_key = next(key for key in old_keys if key.endswith(".png"))
        self.client.failed_deletions.add(failure_key)
        self.update()
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            self.execute()
        report = self.report()
        self.assertTrue(report["currentSwitched"])
        self.assertEqual(report["remainingDeletionKeys"], [failure_key])
        self.client.failed_deletions.clear()
        self.client.order.clear()
        with patch("rizline_publisher.publication.make_client", return_value=self.client):
            result = retry_cleanup(report["cleanupReceipt"], execute=True)
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(result["remainingKeys"])
        self.assertFalse(self.client.order)
        self.assertEqual(read_json(report["cleanupReceipt"])["status"], "complete")

    def test_cleanup_retry_rejects_different_destination_expanded_keys_and_changed_current(self):
        self.seed_build()
        self.update()
        self.client.failed_deletions = set(self.client.values) - {CURRENT}
        with self.assertRaises(RuntimeError):
            self.execute()
        receipt_path = self.report()["cleanupReceipt"]
        with self.assertRaisesRegex(ValueError, "original S3 destination"):
            retry_cleanup(receipt_path, endpoint="https://different.example")
        self.client.seed(CURRENT, b"newer release")
        before = len(self.client.delete_requests)
        with patch("rizline_publisher.publication.make_client", return_value=self.client), self.assertRaisesRegex(ValueError, "current changed"):
            retry_cleanup(receipt_path, execute=True)
        self.assertEqual(len(self.client.delete_requests), before)
        receipt = read_json(receipt_path)
        receipt["remainingKeys"].append("rizline/releases/foreign/key")
        atomic_write(receipt_path, json_bytes(receipt))
        with self.assertRaisesRegex(ValueError, "expand"):
            retry_cleanup(receipt_path)

    def test_snapshot_pagination_and_delete_batches_sweep_foreign_staging(self):
        old, _ = self.seed_build()
        prefix = old["manifestPath"].removesuffix("manifest.json")
        for index in range(1001):
            self.client.seed(prefix + f"orphan-{index:04}.png")
        self.client.page_size = 300
        self.client.seed("rizline/releases/foreign-staging/file")
        self.update()
        self.execute()
        self.assertEqual([len(r["Delete"]["Objects"]) for r in self.client.delete_requests], [1000, 7])
        self.assertNotIn("rizline/releases/foreign-staging/file", self.client.values)
        self.assertTrue(any("ContinuationToken" in r for r in self.client.list_requests))
        self.assertTrue(any(request["Prefix"] == "rizline/releases/" for request in self.client.list_requests))

    def test_pointer_change_during_final_delete_is_reported_and_not_clean_success(self):
        self.seed_build()
        self.update()
        original_delete = self.client.delete_objects
        def changed(**kwargs):
            result = original_delete(**kwargs)
            self.client.seed(CURRENT, b"newer current")
            return result
        with patch.object(self.client, "delete_objects", side_effect=changed), self.assertRaisesRegex(RuntimeError, "current changed"):
            self.execute()
        self.assertEqual(self.report()["status"], "failed")

    def test_delete_md5_is_for_exact_serialized_xml_body(self):
        from botocore.awsrequest import AWSRequest
        request = AWSRequest(method="POST", url="https://s3.example/?delete", data=b"<Delete><Key>a&amp;b</Key></Delete>")
        _delete_objects_content_md5(request)
        expected = base64.b64encode(hashlib.md5(request.body, usedforsecurity=False).digest()).decode("ascii")
        self.assertEqual(request.headers["Content-MD5"], expected)

    def test_real_sdk_delete_signs_md5_of_serialized_xml_without_network(self):
        from botocore.awsrequest import AWSResponse
        from botocore.config import Config
        from botocore.session import Session
        session = Session()
        session.set_config_variable("config_file", os.devnull)
        session.set_config_variable("credentials_file", os.devnull)
        session.set_credentials("offline-test-id", "offline-test-secret")
        client = session.create_client("s3", region_name="us-east-1", endpoint_url="https://example.invalid", config=Config(signature_version="s3v4"))
        key = 'rizline/releases/old/cover & <test>.png'
        requests = []
        def respond(request, **kwargs):
            requests.append(request)
            response = ElementTree.Element("DeleteResult", xmlns="http://s3.amazonaws.com/doc/2006-03-01/")
            ElementTree.SubElement(ElementTree.SubElement(response, "Deleted"), "Key").text = key
            data = ElementTree.tostring(response, encoding="utf-8")
            return AWSResponse(request.url, 200, {"content-type": "application/xml"}, SimpleNamespace(stream=lambda: iter([data])))
        client.meta.events.register("before-send.s3.DeleteObjects", respond)
        with patch("boto3.Session") as factory, patch.object(client._endpoint.http_session, "send", side_effect=AssertionError("Network is forbidden")) as network:
            factory.return_value.client.return_value = client
            configured = make_client("https://example.invalid", "us-east-1", 2)
            configured.delete_objects(Bucket="rranker-rizline-data", Delete={"Objects": [{"Key": key}], "Quiet": False})
        network.assert_not_called()
        self.assertEqual(len(requests), 1)
        request = requests[0]
        expected = base64.b64encode(hashlib.md5(request.body, usedforsecurity=False).digest())
        self.assertEqual(request.headers["Content-MD5"], expected)
        self.assertIn(b"content-md5", request.headers["Authorization"].split(b"SignedHeaders=", 1)[1].split(b",", 1)[0].split(b";"))
        self.assertEqual([node.text for node in ElementTree.fromstring(request.body).findall("{*}Object/{*}Key")], [key])

    def test_retry_publish_keeps_existing_receipt_and_uses_a_sibling_path(self):
        self.seed_build()
        self.update()
        with patch.object(self.client, "put_object", side_effect=RuntimeError("upload interrupted")), self.assertRaisesRegex(RuntimeError, "upload interrupted"):
            self.execute()
        first = Path(self.report()["cleanupReceipt"])
        self.assertEqual(first.name, "2026-09-14.json")
        self.assertEqual(read_json(first)["status"], "prepared")
        result = self.execute()
        second = Path(result["publication"]["cleanupReceipt"])
        self.assertEqual(second.name, "2026-09-14.retry-2.json")
        self.assertTrue(first.exists())
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(result["publication"]["status"], "published")
        self.assertEqual(self.pointer()["resourceVersion"], "2026-09-14")

    def test_explicit_cleanup_receipt_path_still_refuses_overwrite(self):
        path = self.root / "work/cleanup-receipts/custom.json"
        atomic_write(path, b"{}\n")
        with self.assertRaisesRegex(RuntimeError, "Cleanup receipt already exists"):
            self.execute(cleanup_receipt=path)
        self.assertEqual(path.read_bytes(), b"{}\n")


class WorkerTests(unittest.TestCase):
    def test_import_chart_statistics_run_in_parallel_and_failed_import_preserves_catalog(self):
        from rizline_publisher.upstream import chart_stats, import_catalog
        official = {"musics": [{"id": "music", "musicName": "Song"}],
                    "illustrations": [{"id": "cover"}], "resourceReplacements": [], "discOLevels": [],
                    "charts": [{"id": "chart-" + kind, "level": kind, "difficulty": 10.0} for kind in ("EZ", "IN")],
                    "levels": [{"id": "song", "musicId": "music", "illustrationId": "cover", "discName": "Disc 1", "chartIds": ["chart-EZ", "chart-IN"]}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_write(root / "overrides.json", json_bytes(overrides()))
            importer = Mock()
            importer.default.return_value = official
            importer.config = {"version": "1.0"}
            importer.version = "v1"
            importer.cover.return_value = b"PNG bytes"
            acb = sample_acb()
            importer.acb.return_value = acb
            chart_raw = json_bytes({"bPM": 150, "lines": [{"notes": [{"type": 0}, {"type": 2}]}]})
            importer.text.side_effect = lambda key: b"" if key.startswith("local.") else chart_raw
            names = set()
            barrier = threading.Barrier(2)
            def parallel_stats(value):
                names.add(threading.current_thread().name)
                barrier.wait(timeout=5)
                return chart_stats(value)
            with patch("rizline_publisher.upstream.Importer", return_value=importer), patch("rizline_publisher.upstream.chart_stats", side_effect=parallel_stats):
                import_catalog(root / "work", root / "cache", root / "overrides.json", workers=2, stats_url=None, log=Mock())
            self.assertEqual(len(names), 2)
            catalog = read_json(root / "work/catalog.json")
            self.assertEqual([chart["hit"] for chart in catalog["songs"][0]["charts"]], [3, 3])
            self.assertEqual(catalog["songs"][0]["durationSeconds"], acb_duration(acb))
            self.assertEqual((root / "work" / catalog["songs"][0]["audioPath"]).read_bytes(), acb)
            self.assertEqual((root / "work" / catalog["songs"][0]["charts"][0]["chartPath"]).read_bytes(), chart_raw)
            before = (root / "work/catalog.json").read_bytes()
            importer.cover.side_effect = OSError("cover unavailable")
            with patch("rizline_publisher.upstream.Importer", return_value=importer), self.assertRaisesRegex(ValueError, "catalog unchanged"):
                import_catalog(root / "work", root / "cache", root / "overrides.json", workers=2, stats_url=None, log=Mock())
            self.assertEqual((root / "work/catalog.json").read_bytes(), before)
            self.assertEqual(read_json(root / "work/import-failures.json")[0]["kind"], "cover")

    def test_bounded_queue_and_running_workers_preserve_order(self):
        release, all_running = threading.Event(), threading.Barrier(3)
        produced = []
        def values():
            for value in range(20):
                produced.append(value)
                yield value
        def work(value):
            if value < 2:
                all_running.wait(timeout=5)
                self.assertTrue(release.wait(5))
            return value * 2
        with ThreadPoolExecutor(max_workers=1) as runner:
            pending = runner.submit(parallel_map, work, values(), 2)
            try:
                all_running.wait(timeout=5)
                self.assertLessEqual(len(produced), 4)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5), [i * 2 for i in range(20)])

    def test_parallel_map_reports_progress_for_each_completed_item(self):
        seen = []
        def progress(done, total, item):
            seen.append((done, total, item))
        self.assertEqual(parallel_map(lambda value: value * 2, [3, 1, 2], 2, progress=progress), [6, 2, 4])
        self.assertEqual(len(seen), 3)
        self.assertEqual({item for _, _, item in seen}, {1, 2, 3})
        self.assertEqual({total for _, total, _ in seen}, {3})
        self.assertEqual(seen[-1][0], 3)

    def test_build_and_validate_workers_reject_invalid_values(self):
        from rizline_publisher.upstream import import_catalog
        for workers in (0, 17, True, "4", 1.5):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                parallel_map(lambda value: value, [], workers)
        for workers in (0, 9, True):
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                import_catalog("missing", "missing", "missing", workers=workers)


if __name__ == "__main__":
    unittest.main()
