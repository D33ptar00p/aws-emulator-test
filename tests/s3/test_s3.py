"""S3: buckets, objects, pagination, multipart, presigned URLs, versioning."""

from __future__ import annotations

import io

import pytest
from boto3.s3.transfer import TransferConfig

from emulator_poc.testing import http_request


@pytest.fixture
def s3(aws):
    return aws.client("s3")


@pytest.fixture
def bucket(s3, name, cleanup) -> str:
    s3.create_bucket(Bucket=name)

    def _empty_and_delete() -> None:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=name):
            keys = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", [])]
            keys += [{"Key": m["Key"], "VersionId": m["VersionId"]} for m in page.get("DeleteMarkers", [])]
            if keys:
                s3.delete_objects(Bucket=name, Delete={"Objects": keys})
        s3.delete_bucket(Bucket=name)

    cleanup.append(_empty_and_delete)
    return name


def test_bucket_is_listed(s3, bucket) -> None:
    assert bucket in [b["Name"] for b in s3.list_buckets()["Buckets"]]


def test_put_get_with_metadata(s3, bucket) -> None:
    s3.put_object(
        Bucket=bucket,
        Key="takes/a.fbx",
        Body=b"fbx-bytes",
        ContentType="application/octet-stream",
        Metadata={"capture-id": "cap-1"},
    )
    obj = s3.get_object(Bucket=bucket, Key="takes/a.fbx")
    assert obj["Body"].read() == b"fbx-bytes"
    assert obj["Metadata"] == {"capture-id": "cap-1"}
    assert obj["ContentLength"] == 9


def test_missing_key_raises_no_such_key(s3, bucket) -> None:
    with pytest.raises(s3.exceptions.NoSuchKey):
        s3.get_object(Bucket=bucket, Key="nope")


def test_missing_bucket_raises_no_such_bucket(s3, name) -> None:
    with pytest.raises(s3.exceptions.NoSuchBucket):
        s3.list_objects_v2(Bucket=f"{name}-missing")


def test_list_objects_paginates_with_prefix(s3, bucket) -> None:
    for i in range(12):
        s3.put_object(Bucket=bucket, Key=f"exports/{i:02d}.bvh", Body=b"x")
    s3.put_object(Bucket=bucket, Key="other/z.bvh", Body=b"x")

    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix="exports/", PaginationConfig={"PageSize": 5}
    )
    keys = list(pages.search("Contents[].Key"))
    assert keys == [f"exports/{i:02d}.bvh" for i in range(12)]


def test_multipart_upload_via_transfer_manager(s3, bucket) -> None:
    payload = bytes(range(256)) * 50_000  # ~12.8 MB -> 3 parts at 5 MiB
    cfg = TransferConfig(multipart_threshold=5 * 1024 * 1024, multipart_chunksize=5 * 1024 * 1024)
    s3.upload_fileobj(io.BytesIO(payload), bucket, "big.bin", Config=cfg)

    head = s3.head_object(Bucket=bucket, Key="big.bin")
    assert head["ContentLength"] == len(payload)
    assert head["ETag"].strip('"').endswith("-3")  # multipart ETag suffix = part count

    out = io.BytesIO()
    s3.download_fileobj(bucket, "big.bin", out)
    assert out.getvalue() == payload


def test_presigned_get_and_put(s3, bucket) -> None:
    put_url = s3.generate_presigned_url("put_object", Params={"Bucket": bucket, "Key": "up.txt"})
    status, _ = http_request(put_url, method="PUT", body=b"uploaded")
    assert status == 200

    get_url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": "up.txt"})
    assert http_request(get_url) == (200, b"uploaded")


def test_unsigned_request_is_rejected(bucket) -> None:
    import os

    status, _ = http_request(f"{os.environ['AWS_ENDPOINT_URL']}/{bucket}/whatever")
    assert status in (403, 404)


def test_copy_and_delete(s3, bucket) -> None:
    s3.put_object(Bucket=bucket, Key="src", Body=b"data")
    s3.copy_object(Bucket=bucket, Key="dst", CopySource={"Bucket": bucket, "Key": "src"})
    s3.delete_object(Bucket=bucket, Key="src")
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])]
    assert keys == ["dst"]


def test_versioning_keeps_old_versions(s3, bucket) -> None:
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    v1 = s3.put_object(Bucket=bucket, Key="rig.json", Body=b"v1")["VersionId"]
    s3.put_object(Bucket=bucket, Key="rig.json", Body=b"v2")

    assert s3.get_object(Bucket=bucket, Key="rig.json")["Body"].read() == b"v2"
    assert s3.get_object(Bucket=bucket, Key="rig.json", VersionId=v1)["Body"].read() == b"v1"
    versions = s3.list_object_versions(Bucket=bucket, Prefix="rig.json")["Versions"]
    assert len(versions) == 2


def test_object_tagging(s3, bucket) -> None:
    s3.put_object(Bucket=bucket, Key="t", Body=b"x", Tagging="team=platform&env=local")
    tags = {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket=bucket, Key="t")["TagSet"]}
    assert tags == {"team": "platform", "env": "local"}


def test_default_encryption_config(s3, bucket) -> None:
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    rules = s3.get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"]
    assert rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    s3.put_object(Bucket=bucket, Key="enc", Body=b"x")
    assert s3.head_object(Bucket=bucket, Key="enc")["ServerSideEncryption"] == "AES256"
