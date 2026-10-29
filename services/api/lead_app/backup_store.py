"""Copy only authenticated encrypted backup files to an immutable S3 key."""

import base64
import re
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from .backup_crypto import MAGIC
from .backups import digest
from .config import env


def client():
    values = env()
    endpoint = values.get("LEAD_S3_ENDPOINT") or None
    if endpoint:
        p = urlsplit(endpoint)
        local = values.get("LEAD_DEPLOYMENT_MODE") != "production" and p.hostname in (
            "127.0.0.1",
            "localhost",
        )
        if p.scheme != "https" and not (local and p.scheme == "http"):
            raise ValueError("备份存储需要 HTTPS 地址")
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=values.get("LEAD_S3_REGION") or "us-east-1",
        aws_access_key_id=values.get("LEAD_S3_ACCESS_KEY_ID")
        or values.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=values.get("LEAD_S3_SECRET_ACCESS_KEY")
        or values.get("AWS_SECRET_ACCESS_KEY"),
        aws_session_token=values.get("AWS_SESSION_TOKEN"),
        config=Config(
            connect_timeout=10,
            read_timeout=30,
            retries={"total_max_attempts": 3, "mode": "standard"},
        ),
    )


def location(uri):
    p = urlsplit(uri)
    if (
        p.scheme != "s3"
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", p.netloc)
        or p.query
        or p.fragment
        or ".." in p.path.split("/")
    ):
        raise ValueError("备份存储路径不可用")
    return p.netloc, p.path.strip("/")


def upload(path, uri):
    path = Path(path)
    if not re.fullmatch(r"[a-f0-9-]{36}\.leadbackup", path.name) or path.is_symlink():
        raise ValueError("只允许上传加密备份文件")
    with path.open("rb") as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise ValueError("文件不是加密备份")
    bucket, prefix = location(uri)
    key = (prefix + "/" if prefix else "") + path.name
    expected = digest(path)
    s3 = client()
    try:
        try:
            existing = s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        except s3.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") not in (
                "404",
                "NoSuchKey",
                "NotFound",
            ):
                raise
            existing = None
        if existing:
            if (
                existing.get("Metadata", {}).get("cipher-sha256") != expected
                or existing.get("ContentLength") != path.stat().st_size
            ):
                raise ValueError("同一备份标识已存在不同远端内容，拒绝覆盖")
        else:
            extra = {
                "Metadata": {"cipher-sha256": expected},
                "ChecksumAlgorithm": "SHA256",
                "ServerSideEncryption": "AES256",
            }
            kms = env().get("LEAD_S3_KMS_KEY_ID")
            if kms:
                extra.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=kms)
            if path.stat().st_size > 4 * 1024**3:
                raise ValueError("当前远端备份单文件上限为 4 GB")
            with path.open("rb") as stream:
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=stream,
                    ContentLength=path.stat().st_size,
                    ChecksumSHA256=base64.b64encode(bytes.fromhex(expected)).decode(),
                    IfNoneMatch="*",
                    **extra,
                )
            existing = s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        if (
            existing.get("ChecksumSHA256")
            != base64.b64encode(bytes.fromhex(expected)).decode()
        ):
            raise ValueError("远端未返回匹配的 SHA256 校验结果")
        if digest(path) != expected:
            raise ValueError("本地备份在上传时发生变化")
        if (
            existing.get("Metadata", {}).get("cipher-sha256") != expected
            or existing.get("ContentLength") != path.stat().st_size
        ):
            raise ValueError("远端备份校验未通过")
    except ValueError:
        raise
    except Exception:
        raise RuntimeError("远端备份上传未完成") from None
    return {
        "uri": f"s3://{bucket}/{key}",
        "cipher_sha256": expected,
        "size_bytes": path.stat().st_size,
        "version_id": existing.get("VersionId"),
        "verified": "server_sha256",
    }


def download(uri, destination, expected_sha256):
    bucket, key = location(uri)
    target = Path(destination)
    if not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise ValueError("需要原始备份哈希")
    if target.exists():
        raise ValueError("恢复输出必须是新路径")
    partial = target.with_name(target.name + ".partial")
    try:
        if partial.exists():
            raise ValueError("恢复输出必须是新路径")
        client().download_file(
            bucket, key, str(partial), Config=TransferConfig(max_concurrency=2)
        )
        partial.chmod(0o600)
        if digest(partial) != expected_sha256:
            raise ValueError("远端备份内容哈希不匹配")
        partial.rename(target)
    finally:
        partial.unlink(missing_ok=True)
    return str(target)
