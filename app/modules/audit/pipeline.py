# -*- coding: utf-8 -*-
"""SEO 审计：全站抓取 → 解析 → 技术探测 → 30 项打分 → Lighthouse → 基线 + xlsx。

每个站点每天一个工作目录 data/audit/<域名>/audit-<YYYYMMDD>/，summary.json 是基线，
下次审计自动和上一次对比（「00-基线总览」的变化列）。
findings 规则引擎按实测数据起草 P0/P1/P2(初稿,对外前须人工复核);docmd 生成报告 markdown。
"""
import datetime as dt
import shutil
from urllib.parse import urlparse

from app import config
from app.modules.audit import analyze, crawl, docmd, findings, lighthouse_run, probe, report, score
from app.modules.audit.common import jload, load_cfg, save_cfg

STEPS = [("crawl", crawl), ("analyze", analyze), ("probe", probe), ("score", score),
         ("lighthouse", lighthouse_run), ("findings", findings), ("report", report)]


def site_key(base):
    host = urlparse(base if "://" in base else "https://" + base).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def build(base, brand=(), langs=None, max_pages=1500, lighthouse=True, only_report=False, log=print):
    base = base.strip()
    if "://" not in base:
        base = "https://" + base
    site = site_key(base)
    if not site:
        raise ValueError("网址不对：%s" % base)
    date = dt.date.today().strftime("%Y%m%d")
    out = config.data_dir("audit", site, "audit-" + date)
    cfg = load_cfg(str(out)) or {}
    if only_report and not cfg.get("host"):
        raise ValueError("今天还没有 %s 的抓取数据，先完整跑一次。" % site)
    cfg.update({"site": site, "base": base, "out": str(out), "date": date, "max_pages": int(max_pages),
                "threads": int(config.get("audit.threads", 5)), "th": config.get("audit.thresholds", {}) or {}})
    if brand:
        cfg["brand_words"] = list(brand)
    if langs:
        cfg["langs"] = langs
    save_cfg(cfg)

    steps = [("findings", findings), ("report", report)] if only_report else STEPS
    for name, mod in steps:
        if name == "lighthouse" and not (lighthouse and config.get("audit.lighthouse", True)):
            log("跳过 Lighthouse")
            continue
        log("=== %s ===" % name)
        mod.run(cfg, log=log)
        save_cfg(cfg)

    src = out / ("%s-SEO审计表-%s.xlsx" % (site, date))
    xlsx = config.out_dir() / ("SEO审计表_%s_%s.xlsx" % (site, date))
    shutil.copyfile(src, xlsx)
    summ = jload(str(out), "summary.json", {})
    prev_name, prev_summ = report.previous_summary(cfg)
    prev_issues = jload(str(out.parent / prev_name), "findings.json") if prev_name else None
    return {"site": site, "date": date, "xlsx": xlsx, "summary": summ, "prev": prev_name, "prev_summary": prev_summ,
            "prev_issues": prev_issues, "issues": jload(str(out), "findings.json", []), "cfg": cfg, "workdir": str(out)}


def report_md(r, sheet_url=None, client=""):
    return docmd.build(r["cfg"], r["summary"], r["issues"], r["prev"], r["prev_summary"], r["prev_issues"],
                       sheet_url=sheet_url, client=client)
