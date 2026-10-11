"""结构指纹（T2.3 / E6）：SHOW CREATE TABLE 归一化哈希。

去除 AUTO_INCREMENT=N、DEFINER=... 等易变/环境相关项后取 SHA-256，
供「每表开拷前复核」与「收尾复检」检测全量期源库 DDL。
"""
from __future__ import annotations

import hashlib
import re

# 易变项：自增计数器、DEFINER、SQL SECURITY、行格式统计注释
_RE_AUTO_INCREMENT = re.compile(r"\bAUTO_INCREMENT\s*=\s*\d+", re.IGNORECASE)
_RE_DEFINER = re.compile(r"\bDEFINER\s*=\s*`[^`]*`@`[^`]*`(\s*\(.*?\))?", re.IGNORECASE)
_RE_SQL_SECURITY = re.compile(r"\bSQL\s+SECURITY\s+(DEFINER|INVOKER)", re.IGNORECASE)
_RE_COLLATE_VERSION = re.compile(r"COLLATE\s+(utf8mb4_0900_ai_ci)", re.IGNORECASE)


def normalize_ddl(ddl: str) -> str:
    """DDL 兼容预处理（T2.3）：去 DEFINER 等；用于结构与指纹归一化。"""
    s = ddl or ""
    s = _RE_DEFINER.sub("", s)
    s = _RE_SQL_SECURITY.sub("", s)
    s = _RE_AUTO_INCREMENT.sub("", s)
    # 归一化空白与引号风格差异
    s = re.sub(r"\s+", " ", s).strip()
    return s


def fingerprint(ddl: str) -> str:
    return hashlib.sha256(normalize_ddl(ddl).encode("utf-8")).hexdigest()


def clean_definer(ddl: str) -> str:
    """仅做 DEFINER 去除（目标端执行用），保留原始排版其余部分。"""
    s = ddl or ""
    s = _RE_DEFINER.sub("", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()
