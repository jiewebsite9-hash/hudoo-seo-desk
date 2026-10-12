# -*- coding: utf-8 -*-
"""SEO 审计：全站抓取 → 解析 → 技术探测 → 30 项打分 → Lighthouse → 基线 + xlsx。

每个站点每天一个工作目录 data/audit/<域名>/audit-<YYYYMMDD>/，summary.json 是基线，
下次审计自动和上一次对比（「00-基线总览」的变化列）。
这里只出数据和审计表；P0/P1/P2 定级、整改建议要人按审计标准判 ——
判完把 issues.json 放进当天目录、点「只重出表格」就会加进「01-问题清单」页。
"""
import datetime as dt
import shutil
from urllib.parse import urlparse

from app import config
from app.modules.audit import analyze, crawl, lighthouse_run, probe, report, score
from app.modules.audit.common import load_cfg, save_cfg

STEPS = [("crawl", crawl), ("analyze", analyze), ("probe", probe), ("score", score),
         ("lighthouse", lighthouse_run), ("report", report)]


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

    steps = [("report", report)] if only_report else STEPS
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
    summ = report.jload(str(out), "summary.json", {})
    prev_name, _ = report.previous_summary(cfg)
    return {"site": site, "date": date, "xlsx": xlsx, "summary": summ, "prev": prev_name, "workdir": str(out)}
