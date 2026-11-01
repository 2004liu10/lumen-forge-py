"""敏感字段（API Key）的落盘加密。

对应 Node 版 `server/secret.js`。原版设计成「编解码器插槽」：
  · 桌面版：注入 Electron safeStorage（系统钥匙串托管）
  · 浏览器版：没注入 → 明文落盘，保证老数据和纯 Node 环境照常跑

Python 版同样保留这个插槽语义：
  · 桌面版：注入 keyring 实现
  · 纯服务版：没注入 → 明文落盘（行为与原版浏览器模式一致）
"""

from __future__ import annotations

import base64
import logging
from typing import Protocol

from .config import SECRET_FIELDS, SECRET_PREFIX

log = logging.getLogger(__name__)


class SecretCodec(Protocol):
    """加密器契约，与 Node 版 setSecretCodec 的 {seal, open} 一致"""

    def seal(self, plain: str) -> str: ...

    def open(self, blob: str) -> str: ...


_codec: SecretCodec | None = None


def set_secret_codec(next_codec: SecretCodec | None) -> None:
    """注入加解码器。桌面版在启动时、拉起服务之前调一次。

    对应 secret.js:23-25。传入的对象必须同时有 seal 和 open。
    """
    global _codec
    if next_codec and hasattr(next_codec, "seal") and hasattr(next_codec, "open"):
        _codec = next_codec
    else:
        _codec = None


def has_secret_codec() -> bool:
    """当前有没有可用的加密（没有就是纯服务版，落盘是明文）"""
    return _codec is not None


def seal_secret(plain: str) -> str:
    """明文 → 落盘形态。已经是密文或没装编解码器时原样返回。"""
    text = plain if isinstance(plain, str) else ""
    if not text or text.startswith(SECRET_PREFIX) or not _codec:
        return text
    try:
        return SECRET_PREFIX + _codec.seal(text)
    except Exception as err:  # noqa: BLE001
        # 加密失败也不能把用户的 Key 吞掉：宁写明文，也不能写空
        log.error("[secret] 加密失败，改为明文落盘：%s", err)
        return text


def open_secret(stored: str) -> str:
    """落盘形态 → 明文。不是密文直接返回；解密失败返回原值，让上层报错时能看见是什么"""
    text = stored if isinstance(stored, str) else ""
    if not text or not text.startswith(SECRET_PREFIX):
        return text
    if not _codec:
        return text
    try:
        return _codec.open(text[len(SECRET_PREFIX):]) or ""
    except Exception as err:  # noqa: BLE001
        log.error("[secret] 解密失败（换了电脑/换了系统用户会这样），保留原值：%s", err)
        return text


def seal_row(table: str, row: dict) -> dict:
    """写库前把某张表的敏感字段换成密文（对应 sealDbSecrets 的单行版本）"""
    out = dict(row)
    for t, field in SECRET_FIELDS:
        if t != table:
            continue
        if out.get(field):
            out[field] = seal_secret(out[field])
    return out


def open_row(table: str, row: dict) -> dict:
    """读库后把某张表的密文还原成明文（原地改，保持引用语义）"""
    for t, field in SECRET_FIELDS:
        if t != table:
            continue
        if row.get(field):
            row[field] = open_secret(row[field])
    return row


class FernetCodec:
    """默认实现：cryptography Fernet（密钥来自系统钥匙串或环境变量）。

    桌面版用它替代 Electron safeStorage；纯服务版不注入，保持明文。
    """

    def __init__(self, key: bytes) -> None:
        from cryptography.fernet import Fernet  # 延迟导入，纯服务版不需要

        self._fernet = Fernet(key)

    def seal(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def open(self, blob: str) -> str:
        return self._fernet.decrypt(blob.encode("ascii")).decode("utf-8")

    @classmethod
    def from_passphrase(cls, passphrase: str) -> FernetCodec:
        """把任意口令派生成 Fernet 需要的 32 字节 base64 密钥"""
        import hashlib

        digest = hashlib.sha256(passphrase.encode("utf-8")).digest()
        return cls(base64.urlsafe_b64encode(digest))
