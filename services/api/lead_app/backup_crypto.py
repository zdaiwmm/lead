"""Versioned chunk-authenticated backup format; verify before exposing plaintext."""

import hashlib
import json
import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"LEADBK02"
CHUNK = 1024**2


def key_init(path):
    from .config import ROOT, STATE

    path = Path(path).resolve()
    if path.is_relative_to(ROOT) and not path.is_relative_to(STATE):
        raise ValueError("备份密钥只能保存在私有状态目录或仓库之外")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(os.urandom(32))
        stream.flush()
        os.fsync(stream.fileno())
    return key_id(load_key(path))


def load_key(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("备份密钥必须是权限为 0600 的独立文件")
    key = path.read_bytes()
    if len(key) != 32:
        raise ValueError("备份密钥格式不可用")
    return key


def key_id(key):
    return hashlib.sha256(key).hexdigest()[:24]


class EncryptedWriter:
    def __init__(self, target, key, metadata):
        self.target = Path(target)
        self.partial = self.target.with_name(self.target.name + ".partial")
        self.file = self.partial.open("xb")
        os.chmod(self.partial, 0o600)
        self.metadata = {**metadata, "key_id": key_id(key), "format": 2}
        encoded = json.dumps(
            self.metadata, sort_keys=True, separators=(",", ":")
        ).encode()
        if len(encoded) > 16384:
            self.file.close()
            self.partial.unlink()
            raise ValueError("Backup header too large")
        self.prefix = os.urandom(8)
        header = MAGIC + struct.pack(">I", len(encoded)) + encoded + self.prefix
        self.file.write(header)
        self.aad = hashlib.sha256(header).digest()
        self.aead = AESGCM(key)
        self.buffer = bytearray()
        self.index = 0
        self.closed = False

    def writable(self):
        return True

    def write(self, value):
        self.buffer.extend(value)
        while len(self.buffer) >= CHUNK:
            chunk = bytes(self.buffer[:CHUNK])
            del self.buffer[:CHUNK]
            self.frame(chunk, False)
        return len(value)

    def frame(self, value, final):
        from .resources import ensure_capacity

        ensure_capacity(64)
        if self.index >= 2**32:
            raise ValueError("Backup exceeds format limit")
        index = struct.pack(">I", self.index)
        cipher = self.aead.encrypt(
            self.prefix + index, value, self.aad + index + bytes([final])
        )
        self.file.write(struct.pack(">I", len(cipher)) + bytes([final]) + cipher)
        self.index += 1

    def finish(self):
        if self.buffer:
            self.frame(bytes(self.buffer), False)
            self.buffer.clear()
        self.frame(b"", True)
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        # Publish only a fully authenticated file; never replace an existing copy.
        try:
            os.link(self.partial, self.target)
        finally:
            self.partial.unlink(missing_ok=True)
        self.closed = True

    def abort(self):
        if not self.file.closed:
            self.file.close()
        self.partial.unlink(missing_ok=True)
        self.closed = True


def encrypt_stream(source, target, key, metadata):
    writer = EncryptedWriter(target, key, metadata)
    try:
        while chunk := source.read(CHUNK):
            writer.write(chunk)
        writer.finish()
    except BaseException:
        writer.abort()
        raise
    return writer.metadata


def read_exact(stream, size):
    value = stream.read(size)
    if len(value) != size:
        raise ValueError("备份不完整")
    return value


def decrypt_to_file(source, target, key, maximum=16 * 1024**3):
    target = Path(target)
    partial = target.with_name(target.name + ".partial")
    if target.exists() or partial.exists():
        raise ValueError("恢复输出路径必须为空")
    try:
        with Path(source).open("rb") as encrypted, partial.open("xb") as plain:
            os.chmod(partial, 0o600)
            magic = read_exact(encrypted, 8)
            if magic != MAGIC:
                raise ValueError("备份格式不可用")
            length_raw = read_exact(encrypted, 4)
            length = struct.unpack(">I", length_raw)[0]
            if not 0 < length <= 16384:
                raise ValueError("备份头不可用")
            encoded = read_exact(encrypted, length)
            metadata = json.loads(encoded)
            if metadata.get("key_id") != key_id(key) or metadata.get("format") != 2:
                raise ValueError("备份密钥不匹配")
            prefix = read_exact(encrypted, 8)
            aad = hashlib.sha256(magic + length_raw + encoded + prefix).digest()
            aead = AESGCM(key)
            total = 0
            for index in range(2**32):
                length = struct.unpack(">I", read_exact(encrypted, 4))[0]
                final = read_exact(encrypted, 1)
                if not 16 <= length <= CHUNK + 16 or final not in (b"\0", b"\1"):
                    raise ValueError("备份帧不可用")
                cipher = read_exact(encrypted, length)
                counter = struct.pack(">I", index)
                block = aead.decrypt(prefix + counter, cipher, aad + counter + final)
                if final == b"\1":
                    if block or encrypted.read(1):
                        raise ValueError("备份结尾不可用")
                    break
                if not block:
                    raise ValueError("备份帧为空")
                total += len(block)
                if total > maximum:
                    raise ValueError("备份超出恢复容量限制")
                plain.write(block)
            else:
                raise ValueError("备份帧过多")
            plain.flush()
            os.fsync(plain.fileno())
        os.link(partial, target)
        return metadata
    finally:
        partial.unlink(missing_ok=True)
