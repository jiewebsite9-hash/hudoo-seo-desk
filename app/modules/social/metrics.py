# -*- coding: utf-8 -*-
"""指标计算。**这一层只做加减乘除,不做任何判断,也绝不调 AI。**

周报里出现的每一个数字都必须来自这里 —— writer.py 的数字守卫就是拿这里的产出当白名单。

两条口径硬规则(来自公司月报模板,别改):
  · 跨期比较看**比率**不看绝对值;比率类的环比用**百分点 pp**,不算相对增幅
  · 求和口径的指标才可以按日相加;**去重类指标(独立观看者/独立访客)按日相加是错的**,
    后台给了区间总计就用总计,没给就留空,不自己算
"""

# 可以按日相加的量
ADDITIVE = ("impressions", "reach", "engagement", "clicks", "profile_views",
            "new_followers", "likes", "comments", "shares", "saves",
            "reactions", "watch_hours", "pv_web", "pv_mobile", "jobs_views")

# 去重量:后台按日给的值相加会重复计人,只能取区间总计
NON_ADDITIVE = ("profile_uniques",)

# 期末值:取区间最后一天
SNAPSHOT = ("followers",)

CN = {
    "impressions": "曝光/展示", "reach": "触达人数", "engagement": "互动量",
    "clicks": "点击量", "profile_views": "主页访问", "new_followers": "新增关注",
    "followers": "粉丝总数", "profile_uniques": "独立访客", "watch_hours": "观看时长(小时)",
    "posts": "发布篇数", "engagement_rate": "互动率", "ctr": "点击率",
}


def period(data):
    """(起, 止)。没有日数据的平台(IG)退回用帖子日期,再没有返回 (None, None)。"""
    ds = [r["date"] for r in data.get("daily") or [] if r.get("date")]
    if not ds:
        ds = [p["date"] for p in data.get("posts") or [] if p.get("date")]
    return (min(ds), max(ds)) if ds else (None, None)


def slice_daily(data, start=None, end=None):
    return [r for r in data.get("daily") or []
            if (not start or r["date"] >= start) and (not end or r["date"] <= end)]


def totals(data, start=None, end=None):
    """区间汇总。返回的 key 只包含**真的有数据**的指标,不补 0。

    补 0 会让「后台没给这个指标」和「这个指标真的是 0」混在一起,
    周报里就会出现凭空的 0,这是对客户交付里最不能犯的错。
    """
    rows = slice_daily(data, start, end)
    out = {}
    for f in ADDITIVE:
        vals = [r[f] for r in rows if r.get(f) is not None]
        if vals:
            out[f] = round(sum(vals), 4) if f == "watch_hours" else sum(vals)
    for f in SNAPSHOT:
        vals = [r[f] for r in rows if r.get(f) is not None]
        if vals:
            out[f] = vals[-1]
            out[f + "_start"] = vals[0]
            out[f + "_net"] = vals[-1] - vals[0]

    # 去重类:优先用后台给的区间总计。后台只给逐日值时(LinkedIn 的独立访客就是这样),
    # 给出按日相加的结果但打上标记 —— 跨天重复访客会被重复计数,渲染时要写明口径,
    # 不能让它冒充真正的区间去重值。
    for f in NON_ADDITIVE:
        if (data.get("totals") or {}).get(f) is not None:
            out[f] = data["totals"][f]
        else:
            vals = [r[f] for r in rows if r.get(f) is not None]
            if vals:
                out[f] = sum(vals)
                out[f + "_is_daily_sum"] = True
    for f in ("reach", "impressions"):
        # 后台给了区间总计就以总计为准(YouTube 的独立观看者就是这种)
        if f in (data.get("totals") or {}) and data["totals"][f] is not None:
            if f == "reach" and "yt_reach_not_additive" in (data.get("flags") or []):
                out[f] = data["totals"][f]

    posts = [p for p in data.get("posts") or []
             if p.get("date") and (not start or p["date"] >= start)
             and (not end or p["date"] <= end)]
    out["posts"] = len(posts)

    # 没有账号级日数据的平台(Instagram 的导出入口就不给),退回按本期帖子汇总。
    # 注意这是**帖子级累计口径**,platforms 已经打了 flag,checks 会提醒不可横向比。
    if not rows and posts:
        for f in ("impressions", "reach", "engagement", "likes", "comments",
                  "shares", "saves", "clicks"):
            vals = [p[f] for p in posts if p.get(f) is not None]
            if vals:
                out[f] = sum(vals)

    for k in ("subs", "thumb_impr", "thumb_ctr", "avg_watch", "views_by_content"):
        if (data.get("totals") or {}).get(k) is not None:
            out[k] = data["totals"][k]
    out.update(rates(out, data.get("platform")))
    return out


# 互动率的分母**按平台定,不能一刀切**。
#   Facebook / Instagram：触达人数(Reach)
#   LinkedIn：展示量(Impressions) —— 公司模板明确写的是「互动量 ÷ 展示量」
#   TikTok：播放量 —— 后台根本不给触达
# 早期版本统一「有 reach 就用 reach」,LinkedIn 的互动率因此被算成 5.88%(正确是 2.25%),
# 而表格脚注写的又是「÷ 展示量」—— 数和说明自相矛盾,是会直接发错给客户的那类错。
ENGAGEMENT_BASE = {
    "facebook": "reach", "instagram": "reach",
    "linkedin": "impressions", "tiktok": "impressions", "youtube": "reach",
}


def rates(t, platform=None):
    """比率。分母缺失或为 0 一律不给值,不拿 0 当分母也不拿别的指标凑数。"""
    out = {}
    want = ENGAGEMENT_BASE.get(platform or "", "reach")
    base = t.get(want)
    if not base and want != "impressions":
        base = t.get("impressions")
        want = "impressions"
    if t.get("engagement") is not None and base:
        out["engagement_rate"] = round(t["engagement"] / base * 100, 2)
        out["engagement_rate_base"] = want
    if t.get("clicks") is not None and t.get("impressions"):
        out["ctr"] = round(t["clicks"] / t["impressions"] * 100, 2)
    return out


def compare(cur, prev):
    """环比。计数类给百分比,比率类给百分点(pp)。

    上期缺失、为 0、或本期缺失,一律返回 None —— 让模板填「—」,**不强算**。
    """
    out = {}
    if not prev:
        return out
    for k, v in cur.items():
        if not isinstance(v, (int, float)) or k.endswith("_base"):
            continue
        p = prev.get(k)
        if not isinstance(p, (int, float)):
            continue
        if k.endswith("_rate") or k == "ctr":
            out[k] = {"kind": "pp", "value": round(v - p, 2), "prev": p}
        elif p:
            out[k] = {"kind": "pct", "value": round((v - p) / abs(p) * 100, 1), "prev": p}
    return out


def fmt_delta(d):
    """环比渲染成模板要的写法。"""
    if not d:
        return "—"
    if d["kind"] == "pp":
        return "%+.2f pp" % d["value"]
    return "%+.1f%%" % d["value"]


def common_window(bundles):
    """多平台对齐窗口:所有平台都覆盖到的那段。

    只用于**跨平台总览**;各客户自己的周报一律用各平台自己的完整区间,
    强行裁齐会白白丢掉客户的数据。
    """
    spans = [period(b) for b in bundles]
    spans = [s for s in spans if s[0] and s[1]]
    if not spans:
        return (None, None)
    lo, hi = max(s[0] for s in spans), min(s[1] for s in spans)
    return (lo, hi) if lo <= hi else (None, None)
