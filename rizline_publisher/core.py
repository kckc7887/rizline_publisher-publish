from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

PUBLIC_BASE = "https://rranker-rizline-data.cn-nb1.rains3.com"
BUCKET = "rranker-rizline-data"
S3_ENDPOINT = "https://cn-nb1.rains3.com"
# The bucket's GetBucketLocation returns an empty LocationConstraint (us-east-1).
S3_REGION = "us-east-1"
DIFFICULTIES = ("EZ", "HD", "IN", "AT", "SP")
SONG_FIELDS = {"id", "title", "artist", "illustrator", "packId", "packName", "bpm", "durationSeconds", "updatedAt", "coverPath", "charts", "achievements"}
CHART_FIELDS = {"id", "songId", "difficulty", "level", "constant", "designer", "hit", "combo", "maxScore", "riztimeHit"}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or any(c in value for c in ":?#") or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid relative resource path: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(value).is_absolute():
        raise ValueError(f"Invalid relative resource path: {value!r}")
    return value


def contained_path(root, value):
    relative_path(value)
    root = Path(root).resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Resource path escapes its directory")
    return path


def numeric(value, label, integer=False, minimum=0):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"Invalid {label}: {value!r}")
    if integer and int(value) != value:
        raise ValueError(f"{label} must be an integer")


def string(value, label, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {label}")


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or set(catalog) != {"schemaVersion", "resourceVersion", "gameVersion", "songs"} or type(catalog.get("schemaVersion")) is not int or catalog["schemaVersion"] != 1:
        raise ValueError("Unsupported catalog schemaVersion")
    string(catalog.get("resourceVersion"), "resourceVersion")
    string(catalog.get("gameVersion"), "gameVersion")
    if not isinstance(catalog.get("songs"), list) or not catalog["songs"]:
        raise ValueError("Catalog must contain songs")
    song_ids, chart_ids = set(), set()
    for song in catalog["songs"]:
        if set(song) != SONG_FIELDS:
            raise ValueError(f"Unexpected/missing song fields: {song.get('id')}: {set(song) ^ SONG_FIELDS}")
        for field in ("id", "title", "packId", "packName"):
            string(song[field], field)
        if song["id"] in song_ids:
            raise ValueError(f"Duplicate song id: {song['id']}")
        song_ids.add(song["id"])
        for field in ("artist", "illustrator", "bpm"):
            string(song[field], field, nullable=True)
        numeric(song["durationSeconds"], "durationSeconds", minimum=0.001)
        if song["updatedAt"] is not None:
            from datetime import date
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", song["updatedAt"]):
                raise ValueError("updatedAt must be a verified game update date YYYY-MM-DD or null")
            date.fromisoformat(song["updatedAt"])
        if song["coverPath"] is not None:
            relative_path(song["coverPath"])
        if not isinstance(song["charts"], list) or not song["charts"]:
            raise ValueError(f"Song has no charts: {song['id']}")
        kinds = set()
        for chart in song["charts"]:
            if set(chart) != CHART_FIELDS:
                raise ValueError(f"Unexpected/missing chart fields: {chart.get('id')}")
            string(chart["id"], "chart id")
            if chart["id"] in chart_ids or chart["songId"] != song["id"]:
                raise ValueError(f"Duplicate/mislinked chart: {chart['id']}")
            chart_ids.add(chart["id"])
            if chart["difficulty"] not in DIFFICULTIES or chart["difficulty"] in kinds:
                raise ValueError(f"Invalid/duplicate difficulty: {chart['id']}")
            kinds.add(chart["difficulty"])
            string(chart["level"], "level")
            string(chart["designer"], "designer", nullable=True)
            for field in ("hit", "combo", "maxScore", "riztimeHit"):
                numeric(chart[field], field, integer=True)
            numeric(chart["constant"], "constant")
            if chart["difficulty"] == "SP" and chart["constant"] is not None:
                raise ValueError("SP does not contribute RKS; its constant must be null")
            if chart["hit"] is not None and chart["combo"] is not None and chart["combo"] != max_combo(chart["hit"]):
                raise ValueError(f"HIT/COMBO mismatch: {chart['id']}")
            if chart["riztimeHit"] is not None:
                if chart["hit"] is None or chart["riztimeHit"] > chart["hit"]:
                    raise ValueError(f"Invalid Riztime Hit: {chart['id']}")
                if chart["maxScore"] != 1_000_000 + 100 * chart["riztimeHit"]:
                    raise ValueError(f"Max Score/Riztime Hit mismatch: {chart['id']}")
        if not isinstance(song["achievements"], list):
            raise ValueError("Achievements must be an array")
        achievement_ids = set()
        for achievement in song["achievements"]:
            if set(achievement) != {"id", "title", "condition"}:
                raise ValueError("Invalid achievement fields")
            for field in achievement:
                string(achievement[field], "achievement " + field)
            if achievement["id"] in achievement_ids:
                raise ValueError("Duplicate song achievement")
            achievement_ids.add(achievement["id"])
    return {"songs": len(song_ids), "charts": len(chart_ids), "covers": sum(s["coverPath"] is not None for s in catalog["songs"]), "missingUpdateDates": sum(s["updatedAt"] is None for s in catalog["songs"]), "missingDurations": sum(s["durationSeconds"] is None for s in catalog["songs"]), "missingMaxScores": sum(c["maxScore"] is None for s in catalog["songs"] for c in s["charts"])}


def max_combo(hit):
    if hit <= 5:
        return hit
    if hit <= 8:
        return 2 * hit - 5
    if hit <= 11:
        return 3 * hit - 13
    return 4 * hit - 24


def load_overrides(path):
    result = read_json(path)
    if result.get("schemaVersion") != 1:
        raise ValueError("Unsupported override schema")
    for name in ("songs", "charts", "statAliases", "achievementSongs"):
        if not isinstance(result.get(name), dict):
            raise ValueError(f"overrides.{name} must be an object")
    for identity, alias in result["statAliases"].items():
        string(identity, "statAliases ID")
        string(alias, "statAliases title")
    return result


def apply_overrides(catalog, overrides):
    result = copy.deepcopy(catalog)
    songs = {s["id"]: s for s in result["songs"]}
    charts = {c["id"]: c for s in result["songs"] for c in s["charts"]}
    for kind, items, allowed in (("songs", songs, SONG_FIELDS - {"id", "charts", "coverPath"}), ("charts", charts, CHART_FIELDS - {"id", "songId", "difficulty"})):
        for identity, patch in overrides[kind].items():
            if identity not in items:
                raise ValueError(f"Unknown override {kind} id: {identity}")
            if not isinstance(patch, dict) or not set(patch).issubset(allowed):
                raise ValueError(f"Unsupported override fields: {kind}/{identity}")
            items[identity].update(patch)
    validate_catalog(result)
    return result


def supplement_template(catalog):
    result = {"schemaVersion": 1, "songs": {}, "charts": {}, "statAliases": {}, "achievementSongs": {}}
    for song in catalog["songs"]:
        missing = {field: None for field in ("artist", "illustrator", "bpm", "durationSeconds", "updatedAt") if song[field] is None}
        if missing:
            result["songs"][song["id"]] = missing
        for chart in song["charts"]:
            fields = ("designer", "hit", "combo", "maxScore", "riztimeHit") + (() if chart["difficulty"] == "SP" else ("constant",))
            missing = {field: None for field in fields if chart[field] is None}
            if missing:
                result["charts"][chart["id"]] = missing
    return result


def build(source, overrides, output):
    source, output = Path(source), Path(output)
    catalog = apply_overrides(read_json(source), load_overrides(overrides))
    atomic_write(source.parent / "supplement-template.json", json_bytes(supplement_template(catalog)))
    cover_bytes = {}
    for song in catalog["songs"]:
        if song["coverPath"]:
            data = contained_path(source.parent, song["coverPath"]).read_bytes()
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Imported covers must be PNG images")
            digest = sha256(data)
            cover_bytes[digest] = data
            song["coverPath"] = digest
    fingerprint = sha256(json_bytes(catalog))[:16]
    official_version = re.sub(r"[^a-zA-Z0-9_.-]", "_", catalog["resourceVersion"])
    revision = f"{official_version}-{fingerprint}"
    prefix = f"rizline/releases/{revision}"
    catalog["resourceVersion"] = revision
    payloads = {}
    for song in catalog["songs"]:
        if song["coverPath"]:
            digest = song["coverPath"]
            song["coverPath"] = f"{prefix}/covers/{digest}.png"
            payloads[song["coverPath"]] = cover_bytes[digest]
    catalog_path = f"{prefix}/catalog.json"
    payloads[catalog_path] = json_bytes(catalog)
    manifest = {"schemaVersion": 1, "resourceVersion": revision, "gameVersion": catalog["gameVersion"], "files": [{"path": path, "size": len(data), "sha256": sha256(data)} for path, data in sorted(payloads.items())], "catalogPath": catalog_path}
    manifest_path = f"{prefix}/manifest.json"
    manifest_bytes = json_bytes(manifest)
    for path, data in {**payloads, manifest_path: manifest_bytes}.items():
        target = contained_path(output, path)
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Refusing to overwrite an immutable release: {path}")
        atomic_write(target, data)
    current = {"schemaVersion": 1, "resourceVersion": revision, "manifestPath": manifest_path, "manifestSha256": sha256(manifest_bytes)}
    summary = validate_release(output, current)
    atomic_write(output / "rizline/current.json", json_bytes(current))
    return summary


def validate_release(root, current=None):
    root = Path(root)
    current = read_json(root / "rizline/current.json") if current is None else current
    if set(current) != {"schemaVersion", "resourceVersion", "manifestPath", "manifestSha256"} or current["schemaVersion"] != 1:
        raise ValueError("Invalid current.json")
    if not isinstance(current["resourceVersion"], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,200}", current["resourceVersion"]):
        raise ValueError("Invalid resource version")
    if not isinstance(current["manifestSha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", current["manifestSha256"]):
        raise ValueError("Invalid manifest digest")
    raw = contained_path(root, current["manifestPath"]).read_bytes()
    if sha256(raw) != current["manifestSha256"]:
        raise ValueError("Manifest digest mismatch")
    manifest = json.loads(raw)
    if set(manifest) != {"schemaVersion", "resourceVersion", "gameVersion", "files", "catalogPath"} or manifest["schemaVersion"] != 1 or manifest["resourceVersion"] != current["resourceVersion"]:
        raise ValueError("Manifest version/fields mismatch")
    prefix = f"rizline/releases/{current['resourceVersion']}/"
    if current["manifestPath"] != prefix + "manifest.json":
        raise ValueError("Manifest is outside the selected release")
    paths = set()
    if not isinstance(manifest["files"], list) or not manifest["files"]:
        raise ValueError("Manifest must contain files")
    for asset in manifest["files"]:
        if not isinstance(asset, dict) or set(asset) != {"path", "size", "sha256"} or type(asset["size"]) is not int or asset["size"] <= 0 or not isinstance(asset["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
            raise ValueError("Invalid manifest asset fields")
        path = relative_path(asset["path"])
        if path in paths or not path.startswith(prefix):
            raise ValueError("Duplicate or cross-release manifest path")
        paths.add(path)
        data = contained_path(root, path).read_bytes()
        if len(data) != asset["size"] or sha256(data) != asset["sha256"]:
            raise ValueError(f"Resource integrity mismatch: {path}")
    if manifest["catalogPath"] not in paths:
        raise ValueError("Catalog is not in the manifest")
    catalog = read_json(contained_path(root, manifest["catalogPath"]))
    if catalog["resourceVersion"] != current["resourceVersion"] or catalog["gameVersion"] != manifest["gameVersion"]:
        raise ValueError("Catalog release mismatch")
    summary = validate_catalog(catalog)
    for song in catalog["songs"]:
        if song["coverPath"] and song["coverPath"] not in paths:
            raise ValueError("Cover is not in the manifest")
    return {"resourceVersion": current["resourceVersion"], "files": len(paths), "bytes": sum(a["size"] for a in manifest["files"]), **summary}


def rollback(root, revision):
    if not isinstance(revision, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,200}", revision):
        raise ValueError("Invalid rollback resource version")
    root = Path(root)
    path = f"rizline/releases/{revision}/manifest.json"
    current = {"schemaVersion": 1, "resourceVersion": revision, "manifestPath": path, "manifestSha256": sha256(contained_path(root, path).read_bytes())}
    summary = validate_release(root, current)
    atomic_write(root / "rizline/current.json", json_bytes(current))
    return {"selectedLocalRelease": revision, "published": False, "summary": summary}


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
                if allow_mismatch:
                    return False
                raise ValueError(f"Remote resource size mismatch: {path}")
            digest.update(chunk)
        if size != expected_size or digest.hexdigest() != expected_digest:
            if allow_mismatch:
                return False
            raise ValueError(f"Remote resource content digest mismatch: {path}")
    finally:
        body.close()
    return True


def release_keys(client):
    prefix, keys, seen_tokens = "rizline/releases/", set(), set()
    request = {"Bucket": BUCKET, "Prefix": prefix, "MaxKeys": 1000}
    while True:
        page = client.list_objects_v2(**request)
        for item in page.get("Contents", []):
            key = item.get("Key")
            if not isinstance(key, str) or not key.startswith(prefix):
                raise ValueError("Remote listing returned a key outside rizline/releases/")
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


def _delete_objects_content_md5(request, **kwargs):
    # New SDKs choose CRC32 by default. S3-compatible general-purpose buckets
    # can still require Content-MD5 over the SDK's exact serialized XML body.
    if not isinstance(request.body, (bytes, bytearray)):
        raise ValueError("DeleteObjects must serialize to bytes before signing")
    if "Content-MD5" in request.headers:
        del request.headers["Content-MD5"]
    request.headers["Content-MD5"] = base64.b64encode(hashlib.md5(request.body, usedforsecurity=False).digest()).decode("ascii")


def cleanup_releases(client, keep, current_data):
    # Only visible keys are managed. Bucket versioning and historical VersionIds
    # are intentionally outside this publication policy.
    if not keep or any(not key.startswith("rizline/releases/") for key in keep):
        raise ValueError("Invalid release cleanup keep set")

    def verify_current():
        verify_remote_object(client, "rizline/current.json", len(current_data), sha256(current_data))

    verify_current()
    existing = release_keys(client)
    if missing := keep - existing:
        raise ValueError(f"Remote release is incomplete before cleanup: {sorted(missing)}")
    obsolete = sorted(existing - keep)
    for offset in range(0, len(obsolete), 1000):
        # Detect a pointer change before every destructive request. Publishers must
        # also run serially: S3 cannot atomically condition a delete on another key.
        verify_current()
        batch = obsolete[offset:offset + 1000]
        response = client.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": False})
        if errors := response.get("Errors"):
            details = ", ".join(f"{error.get('Key')} ({error.get('Code')})" for error in errors)
            raise ValueError(f"Remote release cleanup failed: {details}")
        if {item.get("Key") for item in response.get("Deleted", [])} != set(batch):
            raise ValueError("Remote release cleanup did not confirm every requested deletion")
    remaining = release_keys(client)
    if remaining != keep:
        raise ValueError(f"Remote release cleanup verification failed: {len(remaining - keep)} extra keys, {len(keep - remaining)} missing keys")
    verify_current()
    return len(obsolete)


def publish(root, execute=False, endpoint=None, region=None, workers=4):
    if type(workers) is not int or not 1 <= workers <= 16:
        raise ValueError("Publishing workers must be an integer between 1 and 16")
    endpoint = endpoint or S3_ENDPOINT
    region = region or S3_REGION
    root = Path(root)
    summary = validate_release(root)
    current = read_json(root / "rizline/current.json")
    manifest = read_json(contained_path(root, current["manifestPath"]))
    resources = [a["path"] for a in manifest["files"]]
    paths = resources + [current["manifestPath"], "rizline/current.json"]
    plan = {"bucket": BUCKET, "publicBase": PUBLIC_BASE, "execute": execute, "endpoint": endpoint, "region": region, "workers": workers, "uploadOrder": paths, "cleanupPrefix": "rizline/releases/", "keepKeys": resources + [current["manifestPath"]], "summary": summary}
    if not execute:
        return plan
    if not endpoint.startswith("https://"):
        raise ValueError("S3 endpoint must use HTTPS")
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    from botocore.session import get_session
    supported = get_session().get_service_model("s3").operation_model("PutObject").input_shape.members
    if not {"ContentMD5", "IfNoneMatch"}.issubset(supported):
        raise ValueError("Installed boto3/botocore must support ContentMD5 and conditional IfNoneMatch uploads; upgrade the SDK")
    session = boto3.Session(region_name=region)
    if session.get_credentials() is None:
        raise ValueError("No AWS credentials are configured")
    # Construct the client before starting threads; sessions are not shared with workers.
    # https://docs.aws.amazon.com/boto3/latest/guide/clients.html#multithreading-or-multiprocessing-with-clients
    client = session.client("s3", endpoint_url=endpoint, config=Config(signature_version="s3v4", max_pool_connections=workers))
    client.meta.events.register("before-sign.s3.DeleteObjects", _delete_objects_content_md5, unique_id="rizline-delete-content-md5")
    expected = {asset["path"]: (asset["size"], asset["sha256"]) for asset in manifest["files"]}
    expected[current["manifestPath"]] = (None, current["manifestSha256"])
    def upload_and_verify(path):
        is_current = path == "rizline/current.json"
        # Use the selected pointer even if another local build runs during this upload.
        data = json_bytes(current) if is_current else contained_path(root, path).read_bytes()
        digest = sha256(data)
        if not is_current:
            expected_size, expected_digest = expected[path]
            if digest != expected_digest or expected_size is not None and len(data) != expected_size:
                raise ValueError(f"Local release changed during publishing: {path}")
        # Object metadata is user-controlled. Only actual GET bytes prove content integrity.
        # The mutable pointer may differ; immutable resources must never be replaced.
        if verify_remote_object(client, path, len(data), digest, allow_missing=True, allow_mismatch=is_current):
            return "skipped"
        request = {"Bucket": BUCKET, "Key": path, "Body": data, "ContentType": "image/png" if path.endswith(".png") else "application/json; charset=utf-8", "CacheControl": "no-cache" if is_current else "public, max-age=31536000, immutable", "Metadata": {"sha256": digest}, "ContentMD5": base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii")}
        if not is_current:
            request["IfNoneMatch"] = "*"
        outcome = "uploaded"
        try:
            client.put_object(**request)
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if is_current or status not in (409, 412):
                raise
            # A competing publisher may have created this key since GET. Never overwrite it;
            # reuse it only after reading its actual contents. Missing/conflicting objects abort.
            outcome = "skipped"
        verify_remote_object(client, path, len(data), digest)
        return outcome

    # Only immutable file objects run concurrently. Exiting the pool joins all in-flight
    # jobs, including on failure, before the manifest or current pointer can be touched.
    publication = {"resourceVersion": current["resourceVersion"], "uploaded": 0, "skipped": 0, "deleted": 0}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(upload_and_verify, path) for path in resources]
        try:
            for future in as_completed(futures):
                publication[future.result()] += 1
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    publication[upload_and_verify(current["manifestPath"])] += 1
    publication[upload_and_verify("rizline/current.json")] += 1
    publication["deleted"] = cleanup_releases(client, set(plan["keepKeys"]), json_bytes(current))
    plan["publication"] = publication
    return plan
