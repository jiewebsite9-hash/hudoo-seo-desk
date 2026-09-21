# -*- coding: utf-8 -*-
"""排名检查主流程:取数 -> 匹配名次 -> 落台账 -> 算涨跌。"""
from datetime import datetime
from urllib.parse import urlparse

from app import config
from . import storage
from .engine import DataForSeo, RankError, Blocked  # noqa: F401  (server 用 DataForSeo 报价)


def norm_host(host):
    """归一化成裸主机名。

    ⚠ 这个函数写松了会**静默毁掉整个项目的数据**:域名匹配不上时不会报错,
    只会把每个词都记成「未进前 N」,表现是「这个站一个词都没排名」。
    实际踩过三次,起因都是 config 里 domain 的写法:

        example.com            裸域名          正常
        example.com/           带尾斜杠        248 词全记假 0
        https://example.com/   完整 URL        65 词全记假 0

    **判断信号:某项目「有排名数」恰好为 0 而词量很大,先查 domain 怎么写的,
    别急着下 SEO 结论。**
    """
    host = (host or "").strip().lower()
    if "//" in host:
        host = host.split("//", 1)[1]
    host = host.split("/", 1)[0]
    host = host.split("?", 1)[0].split("#", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def match_position(results, domain):
    target = norm_host(domain)
    if not target:
        return None, None
    for idx, r in enumerate(results, start=1):
        host = norm_host(urlparse(r["url"]).hostname or "")
        if host == target or host.endswith("." + target):
            return idx, r["url"]
    return None, None


def projects():
    """项目都放在 config.local.yaml 里 —— 公开仓库不带任何客户信息。"""
    out = []
    for p in (config.get("rank_projects") or []):
        if not isinstance(p, dict) or not p.get("domain"):
            continue
        kws = p.get("keywords") or []
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.splitlines() if k.strip()]
        out.append({"name": p.get("name") or p["domain"],
                    "domain": str(p["domain"]).strip(),
                    "host": norm_host(p["domain"]),
                    "keywords": kws,
                    "gl": p.get("gl") or config.get("defaults.geo", "US"),
                    "hl": p.get("hl") or config.get("defaults.lang", "en"),
                    "device": p.get("device") or "desktop",
                    "depth": int(p.get("depth") or 3)})
    return out


def find_project(domain):
    want = norm_host(domain)
    for p in projects():
        if p["host"] == want:
            return p
    return None


def make_engine(gl="us", hl="en", device="desktop", depth=3, mode="standard", log=None):
    return DataForSeo(
        config.get("dataforseo.login"), config.get("dataforseo.password"),
        gl=(gl or "us").lower(), hl=hl, device=device, depth=depth, mode=mode,
        batch=int(config.get("ranks.batch_size", 100)),
        poll_sec=int(config.get("ranks.poll_sec", 15)),
        timeout_sec=int(config.get("ranks.timeout_sec", 1800)),
        journal=config.ROOT / "data" / "rank_pending.json",
        log=log)


def estimate(n, depth=3, mode="standard"):
    return DataForSeo.unit_price(depth, mode) * max(0, n)


def check(domain, keywords=None, gl=None, hl=None, device=None, depth=None,
          mode="standard", only_failed=False, job=None):
    log = job.log if job else (lambda m: None)
    proj = find_project(domain)
    host = norm_host(domain)
    if not host:
        raise RankError("域名不能为空。")
    gl = gl or (proj or {}).get("gl") or "us"
    hl = hl or (proj or {}).get("hl") or "en"
    device = device or (proj or {}).get("device") or "desktop"
    depth = int(depth or (proj or {}).get("depth") or 3)

    conn = storage.connect()
    if only_failed:
        words = storage.failed_keywords(conn, host)
        log("只补查出错词:%d 个" % len(words))
    else:
        # **`None` 和 `[]` 必须区别对待**:None = 没给,用项目词表;
        # [] = 调用方明确给了空,那就是空,不能偷偷回落到项目词表。
        # 原先用 `keywords or 项目词` 混为一谈,结果「测试空输入」变成了真实付费调用。
        src = keywords if keywords is not None else (proj or {}).get("keywords") or []
        words = [w.strip() for w in src if str(w).strip()]
    words = list(dict.fromkeys(words))
    if not words:
        raise RankError("没有要查的词 —— 选个项目,或者把词填进来。")

    eng = make_engine(gl, hl, device, depth, mode, log=log)
    log("域名 %s(归一化后 %s)| %d 个词 | 前 %d 名 | %s 模式 | 预估 $%.4f"
        % (domain, host, len(words), depth * 10, mode, eng.estimate(len(words))))
    if host != str(domain).strip().lower().replace("www.", ""):
        log("提示:domain 已归一化,匹配用的是 %s" % host)

    state = {"n": 0}

    def progress(done, total, kw, status):
        state["n"] = done
        if done % 25 == 0 or done == total:
            log("  取回 %d/%d" % (done, total))

    try:
        got = eng.fetch_many(words, progress=progress)
    except Blocked as e:
        raise RankError("整轮已停:%s" % e)

    run_date = datetime.now().strftime("%Y-%m-%d")
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows, out = [], []
    for kw in words:
        g = got.get(kw) or {"status": "error", "results": [], "msg": "没有返回"}
        pos, url = (None, None)
        if g["status"] == "ok":
            pos, url = match_position(g["results"], host)
        prev = storage.prev_position(conn, kw, host, run_date)
        rows.append({"run_date": run_date, "checked_at": checked_at, "keyword": kw,
                     "domain": host, "gl": gl, "hl": hl, "device": device,
                     "position": pos, "url": url, "depth": depth * 10,
                     "status": g["status"], "cost": eng.cost_by_kw.get(kw),
                     "engine": mode})
        out.append({"关键词": kw, "排名": pos or "", "上轮": prev or "",
                    "变化": _delta(pos, prev), "URL": url or "",
                    "状态": "正常" if g["status"] == "ok" else (g.get("msg") or "出错")[:60]})
    storage.save(conn, rows)

    ok = sum(1 for r in rows if r["status"] == "ok")
    ranked = sum(1 for r in rows if r["position"])
    top10 = sum(1 for r in rows if r["position"] and r["position"] <= 10)
    log("成功 %d/%d | 有排名 %d | 前 10 名 %d | 本轮花费 $%.4f%s"
        % (ok, len(rows), ranked, top10, eng.total_cost,
           "(复用 %d 个已付费任务)" % eng.reused if eng.reused else ""))
    if len(rows) >= 20 and ranked == 0:
        log("[当心] 词量不小却一个排名都没有 —— 先确认 domain 写法,"
            "带尾斜杠或完整 URL 会让匹配永远落空。")

    out.sort(key=lambda r: (r["排名"] == "", r["排名"] or 9999))
    stats = {"总词数": len(rows), "成功": ok, "有排名": ranked, "前10名": top10,
             "花费": round(eng.total_cost, 4), "复用": eng.reused,
             "轮次": run_date, "域名": host}
    conn.close()
    return out, stats


def _delta(pos, prev):
    if not pos or not prev:
        return ""
    d = prev - pos
    return ("↑%d" % d) if d > 0 else (("↓%d" % -d) if d < 0 else "—")


def overview(domain):
    host = norm_host(domain)
    conn = storage.connect()
    rows = storage.latest(conn, host)
    out = []
    for r in rows:
        prev = storage.prev_position(conn, r["keyword"], host, r["run_date"])
        out.append({"关键词": r["keyword"], "排名": r["position"] or "",
                    "上轮": prev or "", "变化": _delta(r["position"], prev),
                    "URL": r["url"] or "", "轮次": r["run_date"]})
    cost = storage.run_cost(conn, host)
    dates = storage.run_dates(conn, host, 12)
    failed = storage.failed_keywords(conn, host)
    conn.close()
    out.sort(key=lambda r: (r["排名"] == "", r["排名"] or 9999))
    return {"rows": out, "cost": cost, "run_dates": dates,
            "failed": len(failed), "domain": host}
