# -*- coding: utf-8 -*-
"""GSC 周报的数字层:近 7 天 vs 前 7 天,再往前 7 天用来判「连续两周同向」。

全是算术,不调 AI。判定规则照搬《SEO 客户周同步》模板:
  ±10% 以内 正常;10–30% 关注;≥30% 或连续两周同向 异常;
  指标方向相反 背离(曝光↑点击↓、排名↑CTR↓);周点击 <100 小样本不定级。
"""
import datetime as dt
import re
from urllib.parse import urlparse

from . import client
from ..keywords.sop import classify_intent

LAG = 3            # GSC 数据滞后 2–3 天,默认统计截止 = 今天 - 3
TOP_MOVERS = 5     # 2.1 下钻:升 / 降各取几条
MIN_ABS = 3        # 点击差值小于这个数的词不当「变动」看


def windows(end=None):
    """返回 (本周, 上周, 上上周) 三个 (起, 止) 日期对,各 7 天。"""
    if isinstance(end, str) and end:
        end = dt.date.fromisoformat(end)
    end = end or (dt.date.today() - dt.timedelta(days=LAG))
    w1 = (end - dt.timedelta(days=6), end)
    w0 = (w1[0] - dt.timedelta(days=7), w1[0] - dt.timedelta(days=1))
    wp = (w0[0] - dt.timedelta(days=7), w0[0] - dt.timedelta(days=1))
    return w1, w0, wp


def _iso(d):
    return d.isoformat()


def _agg(rows):
    clicks = sum(r.get("clicks", 0) for r in rows)
    impr = sum(r.get("impressions", 0) for r in rows)
    pos = (sum((r.get("position") or 0) * r.get("impressions", 0) for r in rows) / impr) if impr else 0.0
    return {"clicks": clicks, "impressions": impr,
            "ctr": round(clicks / impr * 100, 2) if impr else 0.0, "position": round(pos, 1)}


def _pct(cur, prev):
    if not prev:
        return None
    return (cur - prev) / prev * 100


def judge(metric, cur, prev, prev2=None, small=False):
    """一个指标的 (环比文本, 判定, 幅度%)。位次和 CTR 的方向按「变好为正」。"""
    if metric == "ctr":
        d = cur - prev
        text = "%+.1f pp" % d
        p = _pct(cur, prev)
        p2 = _pct(prev, prev2) if prev2 is not None else None
    elif metric == "position":
        good = prev - cur                       # 数字变小 = 排名变好
        text = ("%+.1f 位" % good) if prev else "—"
        p = (good / prev * 100) if prev else None
        p2 = ((prev2 - prev) / prev2 * 100) if (prev2 and prev) else None
    else:
        p = _pct(cur, prev)
        text = ("%+.1f%%" % p) if p is not None else "—"
        p2 = _pct(prev, prev2) if prev2 is not None else None
    if small:
        return text, "不定级", p
    if p is None:
        return text, "—", p
    ap = abs(p)
    if ap >= 30:
        level = "异常"
    elif ap >= 10:
        level = "关注"
    else:
        level = "正常"
    # 连续两周同向且两周都不小:升级为异常
    if level == "关注" and p2 is not None and abs(p2) >= 10 and (p > 0) == (p2 > 0):
        level = "异常"
    return text, level, p


def _brand_tokens(site, brand=None):
    host = site.replace("sc-domain:", "")
    host = urlparse(host).netloc if "://" in host else host
    host = host.lower().replace("www.", "")
    stem = host.split(".")[0]
    toks = {stem}
    if len(stem) > 6:
        toks.add(stem[:6])
    for b in (brand or []):
        if b and b.strip():
            toks.add(b.strip().lower())
    return {t for t in toks if len(t) >= 4}


def word_kind(q, brand_toks):
    ql = q.lower()
    if any(t in ql for t in brand_toks):
        return "品牌词"
    intent = classify_intent(q)
    if intent == "信息":
        return "信息型长尾"
    if intent == "交易":
        return "采购词"
    return "产品词"


def _movers(cur, prev, key_name, brand_toks=None, is_page=False):
    """本周 vs 上周按点击差值排,升 / 降各 TOP_MOVERS 条。"""
    keys = set(cur) | set(prev)
    rows = []
    for k in keys:
        a, b = cur.get(k), prev.get(k)
        c1, c0 = (a or {}).get("clicks", 0), (b or {}).get("clicks", 0)
        d = c1 - c0
        if abs(d) < MIN_ABS:
            continue
        rows.append({
            "对象": k, "本周点击": c1, "上周点击": c0, "差值": d,
            "本周曝光": (a or {}).get("impressions", 0),
            "本周排名": round((a or {}).get("position") or 0, 1) if a else "—",
            "上周排名": round((b or {}).get("position") or 0, 1) if b else "—",
            "词性": ("页面" if is_page else word_kind(k, brand_toks or set())),
            "状态": ("新进" if not b else ("消失" if not a else "")),
        })
    up = sorted([r for r in rows if r["差值"] > 0], key=lambda r: -r["差值"])[:TOP_MOVERS]
    down = sorted([r for r in rows if r["差值"] < 0], key=lambda r: r["差值"])[:TOP_MOVERS]
    return up, down


def weekly(site, end=None, brand=None, log=None):
    """拉数 + 算完。返回 facts(纯数字与列表,给出稿和 AI 用)。"""
    log = log or (lambda m: None)
    w1, w0, wp = windows(end)
    log("统计区间:本周 %s ~ %s,上周 %s ~ %s(GSC 数据滞后约 %d 天,已避开)"
        % (_iso(w1[0]), _iso(w1[1]), _iso(w0[0]), _iso(w0[1]), LAG))

    # ---- 逐日 21 天:三周的账号级指标 ----
    daily = client.query(site, _iso(wp[0]), _iso(w1[1]), ["date"])
    by_day = {r["keys"][0]: r for r in daily}

    def wrows(w):
        return [by_day[_iso(w[0] + dt.timedelta(days=i))] for i in range(7)
                if _iso(w[0] + dt.timedelta(days=i)) in by_day]

    cur, prev, prev2 = _agg(wrows(w1)), _agg(wrows(w0)), _agg(wrows(wp))
    days_cur = len(wrows(w1))
    log("本周 %d 天有数据:点击 %d,曝光 %d,CTR %.2f%%,平均排名 %.1f"
        % (days_cur, cur["clicks"], cur["impressions"], cur["ctr"], cur["position"]))
    small = cur["clicks"] < 100 or prev["clicks"] < 100

    snapshot = []
    for key, cn in (("impressions", "曝光"), ("clicks", "点击"), ("ctr", "CTR"), ("position", "平均排名")):
        text, level, p = judge(key, cur[key], prev[key], prev2.get(key), small)
        snapshot.append({"指标": cn, "本周": cur[key], "上周": prev[key], "环比": text, "判定": level, "幅度": p})

    # ---- 背离 ----
    diverge = []
    pi, pc = _pct(cur["impressions"], prev["impressions"]), _pct(cur["clicks"], prev["clicks"])
    if pi is not None and pc is not None and pi >= 5 and pc <= -5:
        diverge.append("曝光↑点击↓")
    if pi is not None and pc is not None and pi <= -5 and pc >= 5:
        diverge.append("曝光↓点击↑")
    if prev["position"] and cur["position"] < prev["position"] and cur["ctr"] < prev["ctr"]:
        diverge.append("排名↑CTR↓")
    if prev["position"] and cur["position"] > prev["position"] and cur["ctr"] > prev["ctr"]:
        diverge.append("排名↓CTR↑")
    # 只给参与背离的那两个指标打标:排名↑CTR↓ 标平均排名和 CTR,曝光↑点击↓ 标曝光和点击
    involved = set()
    for d in diverge:
        involved |= {"曝光", "点击"} if d.startswith("曝光") else {"平均排名", "CTR"}
    for s in snapshot:
        if s["指标"] in involved and s["判定"] in ("正常", "关注"):
            s["判定"] += "·背离"

    # ---- 查询词 / 页面:本周 vs 上周 ----
    brand_toks = _brand_tokens(site, brand)
    q1 = {r["keys"][0]: r for r in client.query(site, _iso(w1[0]), _iso(w1[1]), ["query"], row_limit=5000)}
    q0 = {r["keys"][0]: r for r in client.query(site, _iso(w0[0]), _iso(w0[1]), ["query"], row_limit=5000)}
    p1 = {r["keys"][0]: r for r in client.query(site, _iso(w1[0]), _iso(w1[1]), ["page"], row_limit=5000)}
    p0 = {r["keys"][0]: r for r in client.query(site, _iso(w0[0]), _iso(w0[1]), ["page"], row_limit=5000)}
    pp = {r["keys"][0]: r for r in client.query(site, _iso(wp[0]), _iso(wp[1]), ["page"], row_limit=5000)}
    log("查询词:本周 %d 个 / 上周 %d 个;页面:本周 %d / 上周 %d" % (len(q1), len(q0), len(p1), len(p0)))
    q_up, q_down = _movers(q1, q0, "query", brand_toks)
    pg_up, pg_down = _movers(p1, p0, "page", is_page=True)

    top_q = sorted(q1.values(), key=lambda r: -r["clicks"])[:10]
    top_queries = [{"查询词": r["keys"][0], "点击": r["clicks"], "曝光": r["impressions"],
                    "CTR": round(r["ctr"] * 100, 1), "排名": round(r["position"], 1),
                    "词性": word_kind(r["keys"][0], brand_toks)} for r in top_q]
    brand_clicks = sum(r["clicks"] for r in q1.values() if word_kind(r["keys"][0], brand_toks) == "品牌词")

    # 新发页面:本周有曝光、前两周都没有 —— 4.1「本周发布内容」的首周数据
    new_pages = [{"URL": k, "首周曝光": v["impressions"], "首周点击": v["clicks"], "排名": round(v["position"], 1)}
                 for k, v in p1.items() if k not in p0 and k not in pp]
    new_pages.sort(key=lambda r: -r["首周曝光"])

    # 11–20 位:离首页一步之遥的词(SSOT Quick Wins 11–30,这里取更近的一档)
    quick = sorted([r for r in q1.values() if 10 < (r.get("position") or 0) <= 20 and r["impressions"] >= 20],
                   key=lambda r: -r["impressions"])[:10]
    quick_wins = [{"查询词": r["keys"][0], "排名": round(r["position"], 1), "曝光": r["impressions"], "点击": r["clicks"]}
                  for r in quick]

    # ---- 国家 / 设备(本周)----
    country = sorted(client.query(site, _iso(w1[0]), _iso(w1[1]), ["country"]), key=lambda r: -r["clicks"])[:6]
    device = client.query(site, _iso(w1[0]), _iso(w1[1]), ["device"])

    # ---- sitemap(已收录数 GSC 接口不给,只能给提交数)----
    try:
        sm = client.sitemaps(site)
    except client.GscError as e:
        log("读 sitemap 失败:%s" % e)
        sm = []

    return {
        "site": site, "window": {"start": _iso(w1[0]), "end": _iso(w1[1]), "prev_start": _iso(w0[0]),
                                 "prev_end": _iso(w0[1]), "days": days_cur},
        "cur": cur, "prev": prev, "prev2": prev2, "small": small,
        "snapshot": snapshot, "diverge": diverge,
        "levels": [s["判定"] for s in snapshot],
        "queries_up": q_up, "queries_down": q_down, "pages_up": pg_up, "pages_down": pg_down,
        "top_queries": top_queries, "brand_clicks": brand_clicks, "n_queries": len(q1), "n_pages": len(p1),
        "new_pages": new_pages[:10], "quick_wins": quick_wins,
        "country": [{"国家": r["keys"][0].upper(), "点击": r["clicks"], "曝光": r["impressions"]} for r in country],
        "device": [{"设备": r["keys"][0], "点击": r["clicks"], "曝光": r["impressions"]} for r in device],
        "sitemaps": sm,
    }


def overall_status(facts):
    """整体状态:任一指标异常 → 异常;有关注或背离 → 关注;否则正常。小样本按绝对数看,不升级。"""
    levels = facts["levels"]
    if any(l.startswith("异常") for l in levels):
        return "异常"
    if facts["diverge"] or any(l.startswith("关注") for l in levels):
        return "关注"
    return "正常"
