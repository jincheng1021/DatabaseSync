"""凭据加密器（T1.3 / E4）：AES-256-GCM，主密钥独立存放 data/keys/master.key。"""
from __future__ import annotations

import base64
import hashlib
import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import MASTER_KEY_PATH


class MasterKeyManager:
    """首启生成 256 位主密钥；权限 600；提供密钥指纹（供系统设置页展示）。"""

    def __init__(self, key_path: Path = MASTER_KEY_PATH):
        self.key_path = key_path
        self._key: bytes | None = None

    def ensure(self) -> bytes:
        if self._key is None:
            if self.key_path.exists():
                self._key = self.key_path.read_bytes()
                if len(self._key) != 32:
                    raise RuntimeError("主密钥文件损坏（长度不为 256 位），请从备份恢复 data/keys/master.key")
            else:
                self._key = AESGCM.generate_key(bit_length=256)
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                self.key_path.write_bytes(self._key)
                self._restrict_permissions(self.key_path)
        return self._key

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600
        except OSError:
            pass  # Windows 上 chmod 语义受限，尽力而为

    def fingerprint(self) -> str:
        """SHA-256 指纹前 16 位（仅用于展示比对，不泄露密钥）。"""
        key = self.ensure()
        return hashlib.sha256(key).hexdigest()[:16]


class CredentialCipher:
    """AES-256-GCM 加解密封装。密文格式：base64(nonce(12) + ciphertext+tag)。"""

    def __init__(self, key_manager: MasterKeyManager):
        self.key_manager = key_manager

    def encrypt(self, plaintext: str) -> str:
        key = self.key_manager.ensure()
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        key = self.key_manager.ensure()
        raw = base64.b64decode(token.encode("ascii"))
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")

    def try_decrypt(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self.decrypt(token)
        except Exception:
            return None
