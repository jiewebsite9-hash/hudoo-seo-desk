# -*- coding: utf-8 -*-
"""口径体检:把「人工看数据时才会发现的坑」固化成规则。

这些规则不是凭空想的,全部来自 2026-09-22 那次人工整理时真实踩到的坑。
每条都回答三个问题:**现象是什么、会造成什么后果、该怎么处理**。

严重度:
  block  发客户前必须处理,否则会把错的数字发出去
  warn   要知道,但不一定影响本期交付
  info   口径说明,写进周报脚注即可
"""
import statistics

LEVELS = {"block": 0, "warn": 1, "info": 2}


def issue(scope, problem, evidence, advice, level="warn"):
    return {"scope": scope, "problem": problem, "evidence": evidence,
            "advice": advice, "level": level}


def run(bundles, totals_by_key):
    """bundles: {(客户,平台): 归一化数据};totals_by_key: {(客户,平台): 区间汇总}。"""
    from app.modules.social import metrics
    out = []
    out += _period_mismatch(bundles)
    for key, data in bundles.items():
        t = totals_by_key.get(key) or {}
        name = "%s %s" % (key[0], data["platform"])
        out += _flags(name, data)
        out += _negatives(name, data)
        out += _ctr_anomaly(name, data, t)
        out += _incomplete_days(name, data)
        out += _posts_vs_engagement(name, data, t)
    out.sort(key=lambda i: LEVELS.get(i["level"], 9))
    return out


def _period_mismatch(bundles):
    from app.modules.social import metrics
    spans = {}
    for key, data in bundles.items():
        s, e = metrics.period(data)
        if s:
            spans["%s %s" % (key[0], data["platform"])] = (s, e)
    if len(set(spans.values())) <= 1:
        return []
    lo, hi = metrics.common_window(list(bundles.values()))
    detail = "；".join("%s %s~%s" % (k, v[0], v[1]) for k, v in sorted(spans.items()))
    return [issue(
        "全部", "各平台后台导出的时间窗不一致", detail,
        ("客户周报按各平台自己的完整区间出（分表口径已分别标注）；"
         "**跨平台/跨客户对比只能用对齐窗口 %s~%s**，否则是拿不同天数的数字在比。"
         % (lo, hi)) if lo else "各平台区间没有交集，本期无法做跨平台对比。",
        "warn")]


# 平台解析器打的口径标记 -> 人话
FLAG_TEXT = {
    "tiktok_content_cumulative": (
        "TikTok Content 表的播放量是【累计值】不是本期增量",
        "该表列的是每条视频自发布至今的累计播放，历史存量视频动辄上万",
        "本期播放量一律以 Overview 表为准；Content 表只用来看单条内容的累计表现。"
        "**直接对 Total views 求和会把本期播放虚报几十上百倍**",
        "block"),
    "ig_post_level_cumulative": (
        "Instagram 只有帖子级「创建至今」数据，没有账号级日数据",
        "IG 后台该导出入口本身不提供账号级日数据，「数据注释」列写的是「内容创建至今」",
        "IG 的数字不能与 FB/LinkedIn/TikTok 横向比，周报里要单列一节并注明口径；"
        "要做周对比需去 Meta 后台另导账号级日数据",
        "block"),
    "yt_reach_not_additive": (
        "YouTube 独立观看者人数不能按日相加",
        "跨天同一观众会被重复计数",
        "已自动改用后台的区间总计行；若后台没给总计，该指标留空不填",
        "info"),
    "yt_two_dimensions_differ": (
        "YouTube 两个维度的观看次数对不上",
        "「日期」维度与「内容」维度去重口径不同",
        "对外报数统一用日期维度（工具已按日期维度取数）",
        "warn"),
    "tiktok_no_reach": (
        "TikTok 后台不提供触达人数",
        "只有播放量，没有 reach 口径",
        "互动率以播放量为分母，与 FB/LinkedIn 的「互动÷触达」不是一回事，脚注要写明",
        "info"),
}


def _flags(name, data):
    out = []
    for f in dict.fromkeys(data.get("flags") or []):     # 去重且保序
        if f in FLAG_TEXT:
            p, e, a, lv = FLAG_TEXT[f]
            out.append(issue(name, p, e, a, lv))
    return out


def _negatives(name, data):
    hits = []
    for r in data.get("daily") or []:
        for k, v in r.items():
            if k != "date" and isinstance(v, (int, float)) and v < 0:
                hits.append("%s %s=%s" % (r["date"], k, v))
    if not hits:
        return []
    return [issue(name, "逐日数据里有负数", "、".join(hits[:6]),
                  "撤回评论/取关所致，属正常现象；已按后台原值汇总，但逐日求和会低估真实新增",
                  "info")]


def _ctr_anomaly(name, data, t):
    """自然内容的点击率异常高,多半是含推广或后台口径问题,不能直接当成绩报出去。"""
    out = []
    ctr = t.get("ctr")
    if ctr is not None and ctr > 20:
        out.append(issue(
            name, "整体点击率异常偏高（%.2f%%）" % ctr,
            "点击 %s / 曝光 %s" % (t.get("clicks"), t.get("impressions")),
            "自然内容点击率常态 1%~3%，**发客户前必须回后台核实是否含付费推广、"
            "或后台「点击」是否把展开全文/看图重复计入**；确认前不要把点击率当成绩强调",
            "block"))
    for p in data.get("posts") or []:
        imp, clk = p.get("impressions"), p.get("clicks")
        if imp and clk and imp >= 200 and clk / imp > 0.5:
            out.append(issue(
                name, "单帖点击率异常（%.1f%%）" % (clk / imp * 100),
                "%s《%s》展示 %s / 点击 %s" % (p.get("date"), (p.get("title") or "")[:34],
                                              imp, clk),
                "同上，需回后台核实该帖是否含推广", "block"))
    return out


def _incomplete_days(name, data):
    """**最后一天常常是没跑完的。** 后台当天数据只出到某个小时,直接用会把趋势判错。

    两种识别办法:① 有小时级数据(TikTok 活跃时段)时,看尾部是不是连续 0;
    ② 只有日级数据时,看最后一天是不是远低于前几天的中位数。
    """
    out = []
    act = data.get("activity") or {}
    for d, hours in sorted(act.items()):
        vals = [hours.get(h, 0) for h in range(24)]
        tail = 0
        for v in reversed(vals):
            if v:
                break
            tail += 1
        if tail >= 6:
            out.append(issue(
                name, "%s 的小时级数据不完整" % d,
                "24 小时里尾部 %d 个小时全为 0，当日合计 %d" % (tail, sum(vals)),
                "该日不要计入活跃时段分析，否则最佳发布时段会算偏", "warn"))

    rows = [r for r in (data.get("daily") or []) if r.get("impressions") is not None]
    if len(rows) >= 4:
        last, hist = rows[-1]["impressions"], [r["impressions"] for r in rows[:-1]]
        med = statistics.median(hist)
        if med and last < med * 0.25:
            out.append(issue(
                name, "最后一天（%s）的数据可能没出完" % rows[-1]["date"],
                "当日 %s，前几日中位数 %s" % (last, int(med)),
                "后台当天数据通常滞后 1~2 天；确认是真回落还是没跑完，"
                "必要时把统计周期往前收一天", "warn"))
    return out


def _posts_vs_engagement(name, data, t):
    """新帖很少但账号互动不低 —— 说明互动来自存量,不能拿它当新内容的成绩。"""
    posts, eng = t.get("posts") or 0, t.get("engagement")
    if posts and posts <= 3 and eng and eng > posts * 40:
        return [issue(
            name, "本期新帖仅 %d 篇，但账号互动达 %d" % (posts, eng),
            "互动主要来自历史内容与主页存量曝光",
            "评估新帖效果要看单帖数据，不能只看账号级互动总数", "warn")]
    return []
