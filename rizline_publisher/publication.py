"""File-identity comparison, date-prefix delta publication and leftover release cleanup."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import (BUCKET, PUBLIC_BASE, S3_ENDPOINT, S3_REGION, atomic_write,
                   check_workers, contained_path, json_bytes, log_progress, parallel_map, read_json,
                   sha256, validate_catalog_release, validate_current,
                   validate_manifest, validate_release)
from .storage_check import verify_conditional_writes, verify_object_copy

CURRENT = "rizline/current.json"
RELEASES_PREFIX = "rizline/releases/"
BEIJING = timezone(timedelta(hours=8))
_RETRYABLE = {
    "ConnectionClosedError", "EndpointConnectionError", "ConnectTimeoutError",
    "ReadTimeoutError", "TimeoutError", "ProtocolError", "ConnectionError",
}


def beijing_release_date(now=None):
    clock = now or datetime.now(BEIJING)
    return clock.astimezone(BEIJING).date().isoformat()


def next_date_name(live_name, today):
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:-([1-9]\d*))?", live_name or "")
    if not match or match.group(1) != today:
        return today
    return f"{today}-{int(match.group(2) or 1) + 1}"


def _retryable(error):
    names, current, seen = [], error, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return any(name in _RETRYABLE for name in names) or "timed out" in str(error).lower()


def _precondition_failed(error):
    response = getattr(error, "response", None) or {}
    code = (response.get("Error") or {}).get("Code")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("PreconditionFailed", "412") or status == 412


def _retry(operation, attempts=4):
    last = None
    for index in range(attempts):
        try:
            return operation()
        except BaseException as error:
            last = error
            if not _retryable(error) or index == attempts - 1:
                raise
            time.sleep(min(2 ** index, 8))
    raise last


def make_client(endpoint, region, workers):
    if not endpoint.startswith("https://"):
        raise ValueError("S3 endpoint must use HTTPS")
    import boto3
    from botocore.config import Config
    from botocore.session import get_session
    supported = get_session().get_service_model("s3").operation_model("PutObject").input_shape.members
    if not {"ContentMD5", "IfNoneMatch", "IfMatch"}.issubset(supported):
        raise ValueError("Installed boto3/botocore must support ContentMD5, IfNoneMatch and IfMatch; upgrade the SDK")
    session = boto3.Session(region_name=region)
    if session.get_credentials() is None:
        raise ValueError("No AWS credentials are configured")
    client = session.client("s3", endpoint_url=endpoint, config=Config(
        signature_version="s3v4", max_pool_connections=workers,
        connect_timeout=20, read_timeout=90, retries={"mode": "standard", "total_max_attempts": 4}))
    client.meta.events.register("before-sign.s3.DeleteObjects", _delete_objects_content_md5,
                                unique_id="rizline-delete-content-md5")
    return client


def remote_bytes(client, path, maximum=64 * 1024 * 1024, allow_missing=False):
    from botocore.exceptions import ClientError
    try:
        response = client.get_object(Bucket=BUCKET, Key=path)
    except ClientError as error:
        if allow_missing and error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return None
        raise
    body = response["Body"]
    try:
        size = response.get("ContentLength")
        if type(size) is not int or not 0 <= size <= maximum:
            raise ValueError(f"Invalid remote metadata object size: {path}")
        data = body.read(maximum + 1)
        if len(data) != size:
            raise ValueError(f"Remote metadata object size mismatch: {path}")
    finally:
        body.close()
    return data, response.get("ETag")


def verify_remote_object(client, path, expected_size, expected_digest, allow_missing=False, allow_mismatch=False):
    from botocore.exceptions import ClientError
    try:
        response = client.get_object(Bucket=BUCKET, Key=path)
    except ClientError as error:
        if allow_missing and error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return False
        raise
    body = response["Body"]
    digest, size = hashlib.sha256(), 0
    try:
        if response.get("ContentLength") != expected_size:
            if allow_mismatch:
                return False
            raise ValueError(f"Remote resource size mismatch: {path}")
        while chunk := body.read(1024 * 1024):
            size += len(chunk)
            if size > expected_size:
                break
            digest.update(chunk)
        if size != expected_size or digest.hexdigest() != expected_digest:
            if allow_mismatch:
                return False
            raise ValueError(f"Remote resource content digest mismatch: {path}")
    finally:
        body.close()
    return True


def list_keys(client, prefix):
    if not isinstance(prefix, str) or not prefix.startswith("rizline/") or not prefix.endswith("/") or "//" in prefix:
        raise ValueError("A rizline object prefix is required")
    keys, seen_tokens = set(), set()
    request = {"Bucket": BUCKET, "Prefix": prefix, "MaxKeys": 1000}
    while True:
        page = client.list_objects_v2(**request)
        for item in page.get("Contents", []):
            key = item.get("Key")
            if not isinstance(key, str) or not key.startswith(prefix):
                raise ValueError("Remote listing returned a key outside the selected prefix")
            keys.add(key)
        if type(page.get("IsTruncated")) is not bool:
            raise ValueError("Remote listing did not confirm pagination state")
        if not page["IsTruncated"]:
            return keys
        token = page.get("NextContinuationToken")
        if not isinstance(token, str) or not token or token in seen_tokens:
            raise ValueError("Remote listing returned invalid or repeated pagination token")
        seen_tokens.add(token)
        request["ContinuationToken"] = token


def release_keys(client, prefix):
    # Callers select one exact committed version, never an unrelated tree.
    if not prefix.startswith(RELEASES_PREFIX) or not prefix.endswith("/") or len(prefix.split("/")) != 4:
        raise ValueError("An exact release prefix is required")
    return list_keys(client, prefix)


def _delete_objects_content_md5(request, **kwargs):
    if not isinstance(request.body, (bytes, bytearray)):
        raise ValueError("DeleteObjects must serialize to bytes before signing")
    if "Content-MD5" in request.headers:
        del request.headers["Content-MD5"]
    request.headers["Content-MD5"] = base64.b64encode(hashlib.md5(request.body, usedforsecurity=False).digest()).decode("ascii")


def rebase_resource(value, old_prefix, new_prefix, label):
    if value is None:
        return None
    if not value.startswith(old_prefix):
        raise ValueError(f"{label} is outside its release")
    return new_prefix + value[len(old_prefix):]


def move_catalog(catalog, old_prefix, new_prefix, revision):
    result = copy.deepcopy(catalog)
    result["resourceVersion"] = revision
    for song in result["songs"]:
        song["coverPath"] = rebase_resource(song["coverPath"], old_prefix, new_prefix, "Cover")
        song["audioPath"] = rebase_resource(song["audioPath"], old_prefix, new_prefix, "Audio")
        for chart in song["charts"]:
            chart["chartPath"] = rebase_resource(chart["chartPath"], old_prefix, new_prefix, "Chart")
    return result


def manifests_equal(local_current, local_manifest, local_catalog, remote_current, remote_manifest, remote_catalog):
    """Only release identity/paths are normalized; every resource must still match."""
    old_prefix, new_prefix = validate_current(remote_current), validate_current(local_current)
    catalog = move_catalog(remote_catalog, old_prefix, new_prefix, local_current["resourceVersion"])
    if catalog != local_catalog:
        return False
    data = json_bytes(catalog)
    normalized = copy.deepcopy(remote_manifest)
    normalized["resourceVersion"] = local_current["resourceVersion"]
    normalized["catalogPath"] = new_prefix + remote_manifest["catalogPath"][len(old_prefix):]
    for asset in normalized["files"]:
        if asset["path"] == remote_manifest["catalogPath"]:
            asset["size"], asset["sha256"] = len(data), sha256(data)
        asset["path"] = new_prefix + asset["path"][len(old_prefix):]
    normalized["files"].sort(key=lambda asset: asset["path"])
    expected = copy.deepcopy(local_manifest)
    expected["files"].sort(key=lambda asset: asset["path"])
    return normalized == expected


def rebase_release(root, destination, revision, workers=4, remote=None, *, source_current=None):
    """Archive exact selected bytes in a separate output; offline build stays unchanged."""
    root, destination = Path(root), Path(destination)
    if root.resolve() == destination.resolve():
        raise ValueError("Publication output must differ from the deterministic build")
    current = source_current if source_current is not None else read_json(root / CURRENT)
    old_prefix = validate_current(current)
    manifest = validate_manifest(current, contained_path(root, current["manifestPath"]).read_bytes())
    source_catalog_data = contained_path(root, manifest["catalogPath"]).read_bytes()
    catalog_asset = next(asset for asset in manifest["files"] if asset["path"] == manifest["catalogPath"])
    if len(source_catalog_data) != catalog_asset["size"] or sha256(source_catalog_data) != catalog_asset["sha256"]:
        raise ValueError("Local catalog changed during publication preparation")
    catalog = json.loads(source_catalog_data)
    validate_catalog_release(catalog, manifest)
    prefix = f"rizline/releases/{revision}/"
    catalog_data = json_bytes(move_catalog(catalog, old_prefix, prefix, revision))
    rebased = copy.deepcopy(manifest)
    rebased["resourceVersion"] = revision
    rebased["catalogPath"] = prefix + manifest["catalogPath"][len(old_prefix):]
    if remote:
        catalog_data = remote["catalogRaw"]
    def copy_asset(asset):
        data = catalog_data if asset["path"] == manifest["catalogPath"] else contained_path(root, asset["path"]).read_bytes()
        if asset["path"] != manifest["catalogPath"] and (len(data) != asset["size"] or sha256(data) != asset["sha256"]):
            raise ValueError("Local release changed during publication preparation")
        path = prefix + asset["path"][len(old_prefix):]
        target = contained_path(destination, path)
        if target.exists() and target.read_bytes() != data:
            raise ValueError("Refusing to overwrite an immutable publication artifact")
        atomic_write(target, data)
        return {"path": path, "size": len(data), "sha256": sha256(data)}
    def report(done, total, asset):
        log_progress(f"rebase {done}/{total} {asset['path']}")
    rebased["files"] = parallel_map(copy_asset, manifest["files"], workers, progress=report)
    manifest_data = remote["manifestRaw"] if remote else json_bytes(rebased)
    selected = {"schemaVersion": 1, "resourceVersion": revision, "manifestPath": prefix + "manifest.json", "manifestSha256": sha256(manifest_data)}
    current_data = remote["currentRaw"] if remote else json_bytes(selected)
    atomic_write(contained_path(destination, selected["manifestPath"]), manifest_data)
    validate_release(destination, selected, workers)
    atomic_write(destination / CURRENT, current_data)
    return selected, validate_manifest(selected, manifest_data)


def read_remote_release(client, pointer):
    current_raw, _ = pointer
    current = json.loads(current_raw)
    validate_current(current)
    fetched = remote_bytes(client, current["manifestPath"], allow_missing=True)
    if fetched is None:
        return None
    manifest_raw, _ = fetched
    manifest = validate_manifest(current, manifest_raw)
    asset = next(a for a in manifest["files"] if a["path"] == manifest["catalogPath"])
    fetched = remote_bytes(client, manifest["catalogPath"], allow_missing=True)
    if fetched is None:
        return None
    catalog_raw, _ = fetched
    if len(catalog_raw) != asset["size"] or sha256(catalog_raw) != asset["sha256"]:
        raise ValueError("Remote catalog digest mismatch")
    catalog = json.loads(catalog_raw)
    validate_catalog_release(catalog, manifest)
    return {"current": current, "manifest": manifest, "catalog": catalog,
            "currentRaw": current_raw, "manifestRaw": manifest_raw, "catalogRaw": catalog_raw}


def verify_current(client, expected_data, expected_etag=None):
    fetched = remote_bytes(client, CURRENT, maximum=65536)
    data, etag = fetched
    if data != expected_data or expected_etag is not None and etag != expected_etag:
        raise ValueError("Remote current changed; no further old resources will be deleted")
    if not isinstance(etag, str) or not etag:
        raise ValueError("S3 must return current ETag for conditional publication")
    return etag


def validate_receipt(receipt):
    if not isinstance(receipt, dict) or type(receipt.get("schemaVersion")) is not int or receipt.get("schemaVersion") != 1 or receipt.get("bucket") != BUCKET:
        raise ValueError("Invalid cleanup receipt")
    if not isinstance(receipt.get("endpoint"), str) or not receipt["endpoint"].startswith("https://") or not isinstance(receipt.get("region"), str) or not receipt["region"]:
        raise ValueError("Cleanup receipt must identify its original S3 destination")
    current = receipt.get("expectedCurrent")
    new_prefix = validate_current(current)
    old_current = receipt.get("previousCurrent")
    old_prefix = validate_current(old_current) if old_current else None
    keys = receipt.get("snapshotKeys")
    remaining = receipt.get("remainingKeys")
    if not isinstance(keys, list) or not isinstance(remaining, list) or any(not isinstance(key, str) for key in keys + remaining):
        raise ValueError("Invalid cleanup receipt keys")
    if len(keys) != len(set(keys)) or len(remaining) != len(set(remaining)) or not set(remaining).issubset(keys):
        raise ValueError("Cleanup receipt may not expand its original snapshot")
    if old_prefix == new_prefix:
        raise ValueError("Cleanup receipt previous release must differ from the published prefix")
    if any(not key.startswith(RELEASES_PREFIX) or key.startswith(new_prefix) or key == RELEASES_PREFIX for key in keys):
        raise ValueError("Cleanup receipt keys must be confined to leftover release prefixes")
    return new_prefix


def unused_receipt_path(path):
    path = Path(path)
    if not path.exists():
        return path
    index = 2
    while True:
        candidate = path.with_name(f"{path.stem}.retry-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def cleanup_snapshot(client, receipt, receipt_path):
    validate_receipt(receipt)
    expected = json_bytes(receipt["expectedCurrent"])
    deleted = 0
    try:
        verify_current(client, expected, receipt.get("expectedEtag"))
        while receipt["remainingKeys"]:
            verify_current(client, expected, receipt.get("expectedEtag"))
            batch = receipt["remainingKeys"][:1000]
            response = client.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": False})
            confirmed = {item.get("Key") for item in response.get("Deleted", [])}
            errors = response.get("Errors", [])
            if not confirmed.issubset(batch):
                raise ValueError("S3 confirmed an unrequested deletion")
            existing = list_keys(client, RELEASES_PREFIX)
            absent = set(batch) - existing
            receipt["remainingKeys"] = [key for key in receipt["remainingKeys"] if key not in absent]
            deleted += len(absent)
            atomic_write(receipt_path, json_bytes(receipt))
            if errors or confirmed != set(batch) or absent != set(batch):
                raise ValueError("Remote cleanup failed or did not confirm every requested deletion")
        verify_current(client, expected, receipt.get("expectedEtag"))
        receipt["status"] = "complete"
        receipt.pop("error", None)
        atomic_write(receipt_path, json_bytes(receipt))
        return deleted
    except Exception as error:
        receipt["status"], receipt["error"] = "failed", str(error)
        atomic_write(receipt_path, json_bytes(receipt))
        raise


def retry_cleanup(receipt_path, execute=False, endpoint=None, region=None):
    receipt_path = Path(receipt_path)
    receipt = read_json(receipt_path)
    validate_receipt(receipt)
    endpoint, region = endpoint or receipt["endpoint"], region or receipt["region"]
    if (endpoint, region) != (receipt["endpoint"], receipt["region"]):
        raise ValueError("Cleanup retry must use the receipt's original S3 destination")
    result = {"execute": execute, "remainingKeys": receipt["remainingKeys"], "expectedCurrent": receipt["expectedCurrent"]}
    if execute:
        client = make_client(endpoint, region, 1)
        result["deleted"] = cleanup_snapshot(client, receipt, receipt_path)
        result["remainingKeys"] = receipt["remainingKeys"]
    return result


def object_content_type(path):
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".json"):
        return "application/json; charset=utf-8"
    return "application/octet-stream"


def put_verified(client, path, data, *, etag=None):
    request = {"Bucket": BUCKET, "Key": path, "Body": data,
               "ContentType": object_content_type(path),
               "CacheControl": "no-cache" if path == CURRENT else "public, max-age=31536000, immutable",
               "Metadata": {"sha256": sha256(data)},
               "ContentMD5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")}
    if etag is not None:
        request["IfMatch"] = etag
    else:
        request["IfNoneMatch"] = "*"
    digest = sha256(data)
    def put():
        from botocore.exceptions import ClientError
        try:
            client.put_object(**request)
        except ClientError as error:
            # Lost PUT responses and leftover objects in a reused date prefix both
            # surface as 412. current still fails closed; resources may already match.
            if etag is not None or path == CURRENT or not _precondition_failed(error):
                raise
            log_progress(f"publish put-exists {path}")
            verify_remote_object(client, path, len(data), digest)
            return None
        if path == CURRENT:
            return verify_current(client, data)
        verify_remote_object(client, path, len(data), digest)
        return None
    return _retry(put)


def copy_verified(client, source, dest, size):
    if not source.startswith(RELEASES_PREFIX) or not dest.startswith(RELEASES_PREFIX) or source == dest:
        raise ValueError("Refusing to copy outside isolated release prefixes")
    def copy_one():
        client.copy_object(Bucket=BUCKET, Key=dest, CopySource={"Bucket": BUCKET, "Key": source})
        response = client.head_object(Bucket=BUCKET, Key=dest)
        if response.get("ContentLength") != size:
            raise ValueError(f"Copied object size mismatch: {dest}")
    _retry(copy_one)


def delete_keys(client, keys):
    remaining = list(keys)
    while remaining:
        batch = remaining[:1000]
        response = client.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": False})
        confirmed = {item.get("Key") for item in response.get("Deleted", [])}
        errors = response.get("Errors", [])
        remaining = [key for key in remaining if key not in (confirmed - {item.get("Key") for item in errors})]
        if errors or confirmed != set(batch):
            raise ValueError("Remote leftover cleanup failed")


def allocate_revision(client, live_prefix, today):
    live_name = live_prefix.rstrip("/").rsplit("/", 1)[-1] if live_prefix else None
    name = next_date_name(live_name, today)
    while True:
        prefix = f"{RELEASES_PREFIX}{name}/"
        existing = list_keys(client, prefix)
        if not existing:
            return name, prefix
        if live_prefix and prefix == live_prefix:
            name = next_date_name(name, today)
            continue
        delete_keys(client, sorted(existing))
        if list_keys(client, prefix):
            raise RuntimeError(f"Failed leftover prefix was not emptied: {prefix}")
        return name, prefix


def publish_release(root, execute=False, endpoint=None, region=None, workers=4, *, report_path=None, publication_output=None, cleanup_receipt=None):
    check_workers(workers)
    root = Path(root)
    endpoint, region = endpoint or S3_ENDPOINT, region or S3_REGION
    report_path = Path(report_path or root.parent / "work/publication-report.json")
    destination = Path(publication_output or root.parent / "work/publication-release")
    receipt_path = Path(cleanup_receipt) if cleanup_receipt else None
    summary = validate_release(root, workers=workers)
    current = read_json(root / CURRENT)
    manifest = validate_manifest(current, contained_path(root, current["manifestPath"]).read_bytes())
    catalog = read_json(contained_path(root, manifest["catalogPath"]))
    plan = {"bucket": BUCKET, "publicBase": PUBLIC_BASE, "execute": execute, "endpoint": endpoint, "region": region,
            "workers": workers, "uploadOrder": [a["path"] for a in manifest["files"]] + [current["manifestPath"], CURRENT],
            "comparison": "file-identity", "onChange": "date-prefix-delta-copy", "summary": summary}
    if not execute:
        return plan
    publication = {"status": "running", "phase": "compare", "sourceResourceVersion": current["resourceVersion"],
                   "uploaded": 0, "copied": 0, "skipped": 0, "deleted": 0, "currentSwitched": False,
                   "remainingDeletionKeys": [], "publicationOutput": str(destination), "cleanupReceipt": str(receipt_path) if receipt_path else None}
    plan["publication"] = publication
    try:
        log_progress(f"publish compare current; execute={execute} workers={workers}")
        client = make_client(endpoint, region, workers)
        pointer = remote_bytes(client, CURRENT, maximum=65536, allow_missing=True)
        old_current, etag, remote = None, None, None
        if pointer:
            old_current = json.loads(pointer[0])
            validate_current(old_current)
            etag = pointer[1]
            if not isinstance(etag, str) or not etag:
                raise ValueError("S3 must return current ETag for conditional publication")
            try:
                remote = read_remote_release(client, pointer)
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                publication["comparisonReason"] = "invalid-remote-manifest-or-catalog: " + str(error)
        if remote and manifests_equal(current, manifest, catalog, remote["current"], remote["manifest"], remote["catalog"]):
            rebase_release(root, destination, remote["current"]["resourceVersion"], workers, remote, source_current=current)
            publication.update(status="unchanged", phase="complete", resourceVersion=remote["current"]["resourceVersion"],
                               skipped=len(manifest["files"]) + 2)
            log_progress("publish unchanged; skip upload")
            return plan
        publication.setdefault("comparisonReason", "different-resource-set" if remote else "missing-remote-manifest-or-catalog")
        publication["phase"] = "verify-storage"
        log_progress("publish verify-storage")
        publication["storageCheck"] = verify_conditional_writes(client, BUCKET, "rizline")
        publication["storageCheck"].update(verify_object_copy(client, BUCKET, "rizline"))
        publication["phase"] = "prepare"
        log_progress("publish prepare date prefix")
        live_prefix = validate_current(old_current) if old_current else None
        revision, new_prefix = allocate_revision(client, live_prefix, beijing_release_date())
        selected, selected_manifest = rebase_release(root, destination, revision, workers, source_current=current)
        publication["phase"] = "snapshot-old-release"
        snapshot = sorted(key for key in list_keys(client, RELEASES_PREFIX) if not key.startswith(new_prefix))
        requested_receipt = receipt_path
        receipt_path = receipt_path or root.parent / "work/cleanup-receipts" / (revision + ".json")
        if receipt_path.exists():
            if requested_receipt is not None:
                raise ValueError("Cleanup receipt already exists; select a fresh receipt path to preserve its retry snapshot")
            receipt_path = unused_receipt_path(receipt_path)
        publication["cleanupReceipt"] = str(receipt_path)
        log_progress(f"publish cleanup-receipt {receipt_path}")
        publication["resourceVersion"] = revision
        plan["uploadOrder"] = [a["path"] for a in selected_manifest["files"]] + [selected["manifestPath"], CURRENT]
        receipt = {"schemaVersion": 1, "bucket": BUCKET, "endpoint": endpoint, "region": region, "status": "prepared", "previousCurrent": old_current,
                   "expectedCurrent": selected, "expectedEtag": None, "snapshotKeys": snapshot, "remainingKeys": snapshot.copy()}
        atomic_write(receipt_path, json_bytes(receipt))
        publication["phase"] = "upload-resources"
        log_progress(f"publish upload-resources {len(selected_manifest['files'])} files")
        old_rel = {}
        if remote and live_prefix:
            old_rel = {asset["path"][len(live_prefix):]: asset for asset in remote["manifest"]["files"]}
        def publish_asset(asset):
            relative = asset["path"][len(new_prefix):]
            previous = old_rel.get(relative)
            if previous and previous["sha256"] == asset["sha256"] and previous["size"] == asset["size"]:
                copy_verified(client, previous["path"], asset["path"], asset["size"])
                return "copy"
            data = contained_path(destination, asset["path"]).read_bytes()
            if len(data) != asset["size"] or sha256(data) != asset["sha256"]:
                raise ValueError("Local publication artifact changed during upload")
            put_verified(client, asset["path"], data)
            return "upload"
        def report(done, total, asset):
            log_progress(f"publish {done}/{total} {asset['path']}")
        kinds = parallel_map(publish_asset, selected_manifest["files"], workers, progress=report)
        publication["copied"] = kinds.count("copy")
        publication["uploaded"] = kinds.count("upload")
        publication["phase"] = "upload-manifest"
        log_progress("publish upload-manifest")
        data = contained_path(destination, selected["manifestPath"]).read_bytes()
        if sha256(data) != selected["manifestSha256"]:
            raise ValueError("Local manifest changed during publication")
        put_verified(client, selected["manifestPath"], data)
        publication["uploaded"] += 1
        publication["phase"] = "switch-current"
        log_progress("publish switch-current")
        # An exception after PUT can leave the pointer outcome uncertain. Only
        # mark it true after actual bytes have been read back successfully.
        publication["currentSwitched"] = None
        receipt["expectedEtag"] = put_verified(client, CURRENT, json_bytes(selected), etag=etag)
        publication["currentSwitched"] = True
        publication["uploaded"] += 1
        atomic_write(receipt_path, json_bytes(receipt))
        publication["phase"] = "cleanup"
        log_progress("publish cleanup leftovers")
        publication["deleted"] = cleanup_snapshot(client, receipt, receipt_path)
        publication.update(status="published", phase="complete")
        return plan
    except Exception as error:
        publication["status"], publication["error"] = "failed", str(error)
        if "receipt" in locals():
            publication["remainingDeletionKeys"] = receipt["remainingKeys"]
            publication["deleted"] = len(receipt["snapshotKeys"]) - len(receipt["remainingKeys"])
        raise RuntimeError(f"Publication failed during {publication['phase']}: {error}; report: {report_path}") from error
    finally:
        atomic_write(report_path, json_bytes(plan))
