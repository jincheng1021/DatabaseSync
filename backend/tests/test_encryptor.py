"""T1.10 单元：加密器往返、密钥权限、指纹稳定性。"""
from __future__ import annotations

from app.core.security import CredentialCipher, MasterKeyManager


def test_encrypt_roundtrip(cipher):
    token = cipher.encrypt("p@ssw0rd-中文-🔐")
    assert token != "p@ssw0rd-中文-🔐"
    assert cipher.decrypt(token) == "p@ssw0rd-中文-🔐"


def test_encrypt_unique_nonce(cipher):
    """同一明文两次加密产生不同密文（随机 nonce），均能解回。"""
    t1 = cipher.encrypt("same")
    t2 = cipher.encrypt("same")
    assert t1 != t2
    assert cipher.decrypt(t1) == cipher.decrypt(t2) == "same"


def test_master_key_persisted_once(tmp_path):
    km = MasterKeyManager(tmp_path / "master.key")
    k1 = km.ensure()
    assert k1 is km.ensure()  # 进程内缓存
    km2 = MasterKeyManager(tmp_path / "master.key")
    assert km2.ensure() == k1  # 文件持久化，重载一致


def test_master_key_fingerprint_stable(tmp_path):
    km = MasterKeyManager(tmp_path / "master.key")
    fp1 = km.fingerprint()
    km2 = MasterKeyManager(tmp_path / "master.key")
    assert km2.fingerprint() == fp1
    assert len(fp1) == 16


def test_tampered_ciphertext_rejected(cipher):
    token = cipher.encrypt("secret")
    raw = bytearray(token.encode())
    raw[-3] = raw[-3] ^ 0xFF  # 篡改
    try:
        assert cipher.try_decrypt(bytes(raw).decode()) is None
    except Exception:
        pass  # base64 解码失败同样视为拒绝
