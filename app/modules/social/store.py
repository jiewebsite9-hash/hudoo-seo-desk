# -*- coding: utf-8 -*-
"""周报存档:每期每个 客户×平台 存一份汇总,下期拿它算环比。

**为什么必须存**:模板第三节要「上期 / 本期 / 环比」三列,而后台导出只给当期。
不存档就永远只能填「—」。存的是**算好的汇总**不是原始文件 —— 原始文件太大,
而且环比只需要汇总值。

放在 config.data_dir(),内网共享部署时指向同一个目录,全组共用一条基线。
"""
import json
import time
from pathlib import Path


def _dir():
    from app import config
    d = Path(config.data_dir()) / "social"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(s):
    """文件名里不能出现路径分隔符和盘符 —— 客户名是用户可控的。"""
    bad = '\\/:*?"<>|'
    out = "".join("_" if c in bad else c for c in str(s)).strip(". ")
    return out[:60] or "_"


def key_of(client, platform, end):
    return "%s__%s__%s" % (_safe(client), _safe(platform), _safe(end))


def save(client, platform, start, end, totals, meta=None):
    rec = {"client": client, "platform": platform, "start": start, "end": end,
           "totals": totals, "meta": meta or {}, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    p = _dir() / (key_of(client, platform, end) + ".json")
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def previous(client, platform, before):
    """找 before 之前最近的一期。找不到返回 None —— 让模板填「—」,不猜。"""
    pref = "%s__%s__" % (_safe(client), _safe(platform))
    best = None
    for p in _dir().glob(pref + "*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if rec.get("end") and rec["end"] < before:
            if best is None or rec["end"] > best["end"]:
                best = rec
    return best


def history(client=None, platform=None, limit=40):
    out = []
    for p in _dir().glob("*.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if client and rec.get("client") != client:
            continue
        if platform and rec.get("platform") != platform:
            continue
        out.append(rec)
    out.sort(key=lambda r: (r.get("client", ""), r.get("platform", ""), r.get("end", "")),
             reverse=True)
    return out[:limit]
