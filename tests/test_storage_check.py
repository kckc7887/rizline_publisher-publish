"""Offline provider capability tests; no network or credentials are used."""

import base64
import hashlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError
from rizline_publisher.storage_check import verify_conditional_writes, verify_object_copy


class ProbeStore:
    def __init__(self):
        self.objects = {"rizline/current.json": b"active", "rizline/releases/old/music.ogg": b"music"}
        self.puts = []
        self.deletes = []
        self.reads = []
        self.bodies = []
        self.copies = []
        self.ignore = set()
        self.unsupported = set()
        self.constant_etag = False
        self.fail_delete = False
        self.reject_correct_match = False
        self.missing_etag = False

    def etag(self, data):
        return '"' + ("0" * 32 if self.constant_etag else hashlib.md5(data).hexdigest()) + '"'

    @staticmethod
    def error(code, status):
        return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject")

    def put_object(self, *, Bucket, Key, Body, ContentMD5, **options):
        assert ContentMD5 == base64.b64encode(hashlib.md5(Body).digest()).decode("ascii")
        self.puts.append((Key, Body, options))
        for name in self.unsupported:
            if name in options:
                raise self.error("NotImplemented", 501)
        if "IfNoneMatch" in options and "IfNoneMatch" not in self.ignore and Key in self.objects:
            raise self.error("PreconditionFailed", 412)
        if "IfMatch" in options and "IfMatch" not in self.ignore:
            if Key not in self.objects or options["IfMatch"] != self.etag(self.objects[Key]):
                raise self.error("PreconditionFailed", 412)
            if self.reject_correct_match:
                raise self.error("PreconditionFailed", 412)
        self.objects[Key] = Body
        return {"ETag": self.etag(Body)}

    def get_object(self, *, Bucket, Key):
        self.reads.append(Key)
        data = self.objects[Key]
        stream = io.BytesIO(data)
        self.bodies.append(stream)
        return {"Body": stream, "ContentLength": len(data),
                "ETag": None if self.missing_etag else self.etag(data)}

    def copy_object(self, *, Bucket, Key, CopySource, **options):
        source = CopySource["Key"] if isinstance(CopySource, dict) else str(CopySource).split("/", 1)[-1]
        self.copies.append((source, Key, options))
        if "copy" in self.unsupported:
            raise self.error("NotImplemented", 501)
        if source not in self.objects:
            raise self.error("NoSuchKey", 404)
        self.objects[Key] = self.objects[source]
        return {}

    def delete_object(self, *, Bucket, Key):
        self.deletes.append(Key)
        if self.fail_delete:
            raise self.error("AccessDenied", 403)
        self.objects.pop(Key, None)
        return {}


class StorageCheckTests(unittest.TestCase):
    def setUp(self):
        self.store = ProbeStore()
        self.original = dict(self.store.objects)

    def verify(self):
        return verify_conditional_writes(self.store, "bucket", "rizline")

    def assert_only_probe_was_touched(self):
        for key, _body, _options in self.store.puts:
            self.assertRegex(key, r"^rizline/publisher-checks/[0-9a-f]{32}$")
        self.assertTrue(all(key.startswith("rizline/publisher-checks/") for key in self.store.reads + self.store.deletes))
        self.assertTrue(all(body.closed for body in self.store.bodies))
        for key, data in self.original.items():
            self.assertEqual(self.store.objects[key], data)

    def test_valid_provider_proves_both_conditions_and_cleans_only_own_probe(self):
        result = self.verify()
        self.assertTrue(result["conditionalWrites"])
        self.assertEqual(self.store.objects, self.original)
        self.assertEqual(self.store.deletes, [result["probeKey"]])
        self.assertEqual(len(self.store.puts), 4)
        self.assertEqual(len(self.store.reads), 4)
        self.assert_only_probe_was_touched()

    def test_provider_ignoring_if_none_match_aborts_and_cleans_probe(self):
        self.store.ignore.add("IfNoneMatch")
        with self.assertRaisesRegex(RuntimeError, "If-None-Match"):
            self.verify()
        self.assertEqual(self.store.objects, self.original)
        self.assertEqual(len(self.store.deletes), 1)
        self.assert_only_probe_was_touched()

    def test_provider_ignoring_if_match_aborts_and_cleans_probe(self):
        self.store.ignore.add("IfMatch")
        with self.assertRaisesRegex(RuntimeError, "If-Match"):
            self.verify()
        self.assertEqual(self.store.objects, self.original)
        self.assertEqual(len(self.store.deletes), 1)
        self.assert_only_probe_was_touched()

    def test_provider_rejecting_all_etag_updates_cannot_pass(self):
        self.store.reject_correct_match = True
        with self.assertRaisesRegex(RuntimeError, "能力检查失败"):
            self.verify()
        self.assertEqual(self.store.objects, self.original)
        self.assert_only_probe_was_touched()

    def test_unsupported_condition_aborts_before_resource_upload(self):
        self.store.unsupported.add("IfMatch")
        with self.assertRaisesRegex(RuntimeError, "不支持所需的 If-Match"):
            self.verify()
        self.assertEqual(self.store.objects, self.original)
        self.assert_only_probe_was_touched()

    def test_readback_requires_etag(self):
        self.store.missing_etag = True
        with self.assertRaisesRegex(RuntimeError, "ETag"):
            self.verify()
        self.assertEqual(self.store.objects, self.original)
        self.assert_only_probe_was_touched()

    def test_etag_must_change_after_successful_content_update(self):
        self.store.constant_etag = True
        with self.assertRaisesRegex(RuntimeError, "匹配 ETag 的条件更新"):
            self.verify()
        self.assertEqual(self.store.objects, self.original)
        self.assert_only_probe_was_touched()

    def test_cleanup_failure_blocks_publication_and_identifies_only_probe(self):
        self.store.fail_delete = True
        with self.assertRaisesRegex(RuntimeError, "探针清理失败"):
            self.verify()
        self.assertEqual(len(self.store.deletes), 1)
        self.assertEqual(set(self.store.objects) - set(self.original), set(self.store.deletes))
        self.assert_only_probe_was_touched()

    def test_existing_probe_is_not_deleted_when_initial_create_fails(self):
        nonce = "a" * 32
        key = f"rizline/publisher-checks/{nonce}"
        self.store.objects[key] = b"existing unrelated probe"
        with patch("rizline_publisher.storage_check.uuid4", return_value=SimpleNamespace(hex=nonce)):
            with self.assertRaisesRegex(RuntimeError, "能力检查失败"):
                self.verify()
        self.assertEqual(self.store.objects[key], b"existing unrelated probe")
        self.assertEqual(self.store.deletes, [])

    def test_invalid_prefix_makes_no_requests(self):
        for prefix in ("", "../phigros", "phigros/releases", "/rizline", None):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                verify_conditional_writes(self.store, "bucket", prefix)
        self.assertEqual(self.store.puts, [])

    def test_checks_use_a_fresh_namespace_each_time(self):
        first = self.verify()
        second = self.verify()
        self.assertNotEqual(first["probeKey"], second["probeKey"])
        self.assertEqual(self.store.objects, self.original)
        self.assert_only_probe_was_touched()

    def test_copy_probe_copies_exact_bytes_and_cleans_both_keys(self):
        result = verify_object_copy(self.store, "bucket", "rizline")
        self.assertTrue(result["objectCopy"])
        self.assertEqual(self.store.objects, self.original)
        self.assertEqual(len(self.store.copies), 1)
        self.assertEqual(len(self.store.deletes), 2)
        self.assertTrue(all(key.startswith("rizline/publisher-checks/") for key in self.store.deletes))

    def test_copy_unsupported_aborts_and_cleans_source(self):
        self.store.unsupported.add("copy")
        with self.assertRaisesRegex(RuntimeError, "CopyObject"):
            verify_object_copy(self.store, "bucket", "rizline")
        self.assertEqual(self.store.objects, self.original)
        self.assertEqual(len(self.store.deletes), 1)


if __name__ == "__main__":
    unittest.main()

