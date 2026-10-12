# -*- coding: utf-8 -*-
"""步骤 5：Lighthouse 移动端实跑 → lighthouse.json（原始报告在 lh/）

实验室数据，不等于 CrUX 实况；INP 只能从 CrUX / GSC 拿，这里用 TBT 作代理。
"""
import json, os, re, shutil, subprocess

from app.modules.audit.common import jdump, jload

KEEP = {"largest-contentful-paint": "LCP_ms", "cumulative-layout-shift": "CLS", "total-blocking-time": "TBT_ms",
        "server-response-time": "TTFB_ms", "speed-index": "SI_ms", "first-contentful-paint": "FCP_ms"}


def run(cfg, log=print):
    out = cfg["out"]
    exe = shutil.which("lighthouse")
    if not exe:
        log("未找到 lighthouse（npm i -g lighthouse），跳过")
        return
    probe = jload(out, "probe.json", {})
    urls = cfg.get("lh_urls") or probe.get("sample_urls") or ["https://%s/" % cfg["host"]]
    os.makedirs(os.path.join(out, "lh"), exist_ok=True)
    res = {}
    for u in urls:
        name = re.sub(r"[^\w]+", "_", u.split("://", 1)[-1]).strip("_")[:80]
        path = os.path.join(out, "lh", name + ".json")
        cmd = [exe, u, "--quiet", "--output=json", "--output-path=" + path, "--form-factor=mobile",
               "--only-categories=performance,seo,accessibility,best-practices",
               "--chrome-flags=--headless=new --no-sandbox"]
        log("  Lighthouse: " + u)
        try:
            subprocess.run(cmd, timeout=240, capture_output=True)
            rep = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            res[u] = {"error": str(e)[:200]}
            continue
        a = rep.get("audits", {})
        row = {k: round((rep["categories"].get(k, {}).get("score") or 0) * 100) for k in
               ("performance", "seo", "accessibility", "best-practices")}
        for aid, key in KEEP.items():
            v = a.get(aid, {}).get("numericValue")
            row[key] = round(v, 3) if key == "CLS" and v is not None else (round(v) if v is not None else None)
        row["lcp_element"] = (a.get("largest-contentful-paint-element", {}).get("details", {}) or {}).get("items", [{}])
        snip = re.search(r'"snippet":\s*"((?:[^"\\]|\\.)*)"', json.dumps(row["lcp_element"], ensure_ascii=False))
        row["lcp_element"] = json.loads('"%s"' % snip.group(1))[:200] if snip else ""
        row["seo_fails"] = [a[x["id"]]["title"] for x in rep["categories"]["seo"]["auditRefs"]
                            if x["id"] in a and a[x["id"]].get("score") is not None and a[x["id"]]["score"] < 1]
        row["lighthouse_version"] = rep.get("lighthouseVersion")
        res[u] = row
        log("    性能 %s  LCP %sms  CLS %s  TBT %sms" % (row["performance"], row["LCP_ms"], row["CLS"], row["TBT_ms"]))
    jdump(out, "lighthouse.json", res)
