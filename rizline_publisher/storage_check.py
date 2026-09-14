"""Probe conditional writes on an isolated key before publishing resources.

Keep this small protocol helper identical in the two independent publishers.
No release object or public pointer is read, overwritten, or deleted here.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any
from uuid import uuid4


def verify_conditional_writes(client: Any, bucket: str, prefix: str) -> dict[str, Any]:
    """Require working create-only and ETag compare-and-swap on this endpoint."""
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", prefix):
        raise ValueError("存储能力检查需要独立的游戏名称前缀")
    nonce = uuid4().hex
    key = f"{prefix}/publisher-checks/{nonce}"
    original = f"publisher-conditional-check:{nonce}:initial".encode("ascii")
    replacement = f"publisher-conditional-check:{nonce}:replacement".encode("ascii")
    owned = False
    failure = None

    def put(data: bytes, **condition: str) -> None:
        client.put_object(
            Bucket=bucket, Key=key, Body=data, ContentType="application/octet-stream",
            CacheControl="no-store",
            ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode("ascii"),
            **condition,
        )

    def read() -> tuple[bytes, str]:
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            data = body.read(1025)
        finally:
            body.close()
        etag = response.get("ETag")
        if (len(data) > 1024 or response.get("ContentLength") != len(data)
                or not isinstance(etag, str) or not etag):
            raise RuntimeError("S3 条件写入探针回读缺少可靠的内容或 ETag")
        return data, etag

    def must_reject(label: str, expected_etag: str, **condition: str) -> None:
        rejected = None
        try:
            put(replacement, **condition)
        except Exception as error:
            rejected = error
        data, etag = read()
        if data != original or etag != expected_etag:
            raise RuntimeError(f"S3 未正确执行 {label}，条件失败后探针内容或 ETag 已改变") from rejected
        if rejected is None:
            raise RuntimeError(f"S3 未拒绝 {label}，不能保证发布指针安全切换")
        response = getattr(rejected, "response", {})
        if (response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412
                and response.get("Error", {}).get("Code") != "PreconditionFailed"):
            raise RuntimeError(f"S3 不支持所需的 {label} 条件写入") from rejected

    try:
        put(original, IfNoneMatch="*")
        owned = True
        data, etag = read()
        if data != original:
            raise RuntimeError("S3 条件写入探针首次回读内容不一致")
        must_reject("If-None-Match", etag, IfNoneMatch="*")
        wrong_etag = '"' + ("1" if etag == '"' + "0" * 32 + '"' else "0") * 32 + '"'
        must_reject("If-Match", etag, IfMatch=wrong_etag)
        put(replacement, IfMatch=etag)
        updated, updated_etag = read()
        if updated != replacement or updated_etag == etag:
            raise RuntimeError("S3 未正确执行匹配 ETag 的条件更新")
    except Exception as error:
        failure = error
        raise RuntimeError(f"S3 条件写入能力检查失败，发布已停止（{key}）：{error}") from error
    finally:
        if owned:
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception as error:
                detail = f"；原始错误：{failure}" if failure is not None else ""
                raise RuntimeError(f"S3 条件写入探针清理失败，发布已停止（{key}）{detail}") from error
    return {"conditionalWrites": True, "probeKey": key}

