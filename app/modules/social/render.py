# -*- coding: utf-8 -*-
"""facts + AI 文字 -> 八节周报 markdown。**这里出现的每个数字都来自 facts,零计算。**

板块顺序和小标题必须跟公司《社媒 LinkedIn 客户周报模板》完全一致 ——
团队拿到手是要直接发客户的,板块对不上就白做了。
"""
from app.modules.social import metrics

# 第三节每个平台展示哪些指标、叫什么、脚注写什么
SPEC = {
    "facebook": [
        ("posts", "发布篇数", ""),
        ("impressions", "浏览量（Impressions）", "含历史贴文与主页自然曝光"),
        ("reach", "触达人数（Reach）", ""),
        ("engagement", "互动量（Engagement）", "心情 + 评论 + 分享"),
        ("engagement_rate", "互动率（Eng. Rate）", "互动量 ÷ 触达人数"),
        ("clicks", "链接点击（Link Clicks）", ""),
        ("ctr", "链接点击率", "链接点击 ÷ 浏览量"),
        ("profile_views", "主页访问（Page Visits）", ""),
        ("new_followers", "新增关注（New Followers）", ""),
    ],
    "linkedin": [
        ("posts", "发布篇数", ""),
        ("impressions", "展示量（Impressions）", "内容被展示总次数，含历史贴文"),
        ("reach", "独立展示量（Unique Impressions）", ""),
        ("engagement", "互动量（Engagement）", "回应 + 评论 + 转发"),
        ("engagement_rate", "互动率（Eng. Rate）", "互动量 ÷ 展示量"),
        ("clicks", "点击量（Clicks）", "后台 Clicks，含看图、展开全文，非仅链接点击"),
        ("ctr", "点击率（CTR）", "点击 ÷ 展示"),
        ("profile_views", "主页浏览量（Page Views）", ""),
        ("profile_uniques", "独立访客（Unique Visitors）",
         "后台只给逐日值，此处为逐日相加，跨天重复访客会重复计数"),
        ("new_followers", "新增关注（New Followers）", ""),
        ("followers", "关注者总数（Followers）", "期末值"),
    ],
    "youtube": [
        ("posts", "发布篇数", ""),
        ("impressions", "观看次数（Views）", ""),
        ("reach", "独立观看者（Unique Viewers）", "区间去重值，不可按日相加"),
        ("watch_hours", "观看时长（小时）", ""),
        ("avg_watch", "平均观看时长", ""),
        ("thumb_impr", "缩略图展示（Impressions）", ""),
        ("thumb_ctr", "缩略图点击率（CTR）", ""),
        ("subs", "新增订阅", ""),
    ],
    "tiktok": [
        ("posts", "发布篇数", ""),
        ("impressions", "视频播放量（Video Views）", "账号级，含历史视频"),
        ("engagement", "互动量（Engagement）", "点赞 + 评论 + 分享"),
        ("engagement_rate", "互动率（Eng. Rate）", "互动量 ÷ 播放量"),
        ("profile_views", "主页访问（Profile Views）", ""),
        ("followers_net", "粉丝净增（Net Followers）", ""),
        ("followers", "粉丝总数（Followers）", "期末值"),
    ],
    "instagram": [
        ("posts", "发布篇数", ""),
        ("impressions", "浏览量", ""),
        ("reach", "触达人数", ""),
        ("engagement", "互动量", "赞 + 评论 + 分享 + 收藏"),
        ("engagement_rate", "互动率", "互动量 ÷ 触达人数"),
    ],
}

RATE_KEYS = ("engagement_rate", "ctr", "thumb_ctr")


def n(v):
    """数字渲染。None 一律「—」,**不补 0**。"""
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".") if abs(v) < 1000 else "{:,.0f}".format(v)
    if isinstance(v, int):
        return "{:,}".format(v)
    return str(v)


def pct(v):
    return "—" if v is None else "%.2f%%" % v


def _cell(key, val):
    return pct(val) if key in RATE_KEYS else n(val)


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(out)


# ------------------------------------------------------------------ 各节

def _internal_block(facts):
    """内部核对块 —— 沿用模板自己的「使用说明（发出前删除本块）」写法。

    把体检层的 block/warn 级问题放这里,而不是混进正文:
    正文是给客户看的,这块是给运营专员看的,删掉即可交付。
    """
    items = [i for i in facts["issues"] if i["level"] in ("block", "warn")]
    if not facts.get("has_prev"):
        items = [{"problem": "本期无环比",
                  "advice": "只拿到一期后台导出，第三节「上期」「环比」列按模板规则填「—」，"
                            "不强算；下期起工具会自动带出环比。"}] + items
    items = items + [{"problem": "第七节为建议排期",
                      "advice": "下周计划是按第六节的调整动作推的，需运营专员换成真实内容日历后再发客户。"}]
    lines = ["> **内部核对（发客户前删除本块）**"]
    for i, it in enumerate(items, 1):
        tag = "⚠ " if it.get("level") == "block" else ""
        ev = ("：%s" % it["evidence"]) if it.get("evidence") else ""
        lines.append("> %d. %s**%s**%s。%s" % (i, tag, it["problem"], ev, it["advice"]))
    return "\n".join(lines)


def _sec2_posts(facts, prose=None):
    """只列**本期发的**和**截止日之后发的**;本期之前的存量内容不属于「本周发布」。"""
    types = (prose or {}).get("content_types") or {}
    rows = []
    for p in facts["platforms"]:
        for post, mark in ([(x, "是") for x in p["posts_in_period"]]
                           + [(x, "否（下期计入）") for x in p.get("posts_next_period") or []]):
            title = (post.get("title") or "")[:34] or "—"
            rows.append([post.get("date") or "—", p["cn"], post.get("kind") or "视频",
                         title, types.get(post.get("url") or title, "—"), mark])
    rows.sort(key=lambda r: r[0])
    tbl = md_table(["日期", "平台", "形态", "主题", "内容类型", "计入本期数据"], rows) \
        if rows else "本周无新发内容。"
    total = len(rows)
    inside = sum(1 for r in rows if r[5] == "是")
    note = ("> 本周共发布 %d 篇，其中 %d 篇在本期统计周期内。内容类型按公司 40 / 30 / 20 / 10 "
            "配比口径归类。\n> 统计周期：%s–%s（后台数据截至 %s）。"
            % (total, inside, facts["period"]["start"], facts["period"]["end"],
               facts["period"]["end"]))
    return tbl + "\n\n" + note


def _metric_table(p):
    rows = []
    for key, label, note in SPEC.get(p["platform"], []):
        cur = p["totals"].get(key)
        if key == "followers_net":
            cur = p["totals"].get("followers_net")
        if cur is None and key not in ("posts",):
            continue
        d = p["delta"].get(key)
        rows.append([label, _cell(key, (d or {}).get("prev")), _cell(key, cur),
                     metrics.fmt_delta(d), note])
    return md_table(["指标", "上期", "本期", "环比", "说明"], rows)


def _sec3(facts):
    out = []
    multi = len(facts["platforms"]) > 1
    for i, p in enumerate(facts["platforms"], 1):
        if multi:
            out.append("### 3.%d %s（%s–%s）\n" % (i, p["cn"], p["period"]["start"] or "—",
                                                   p["period"]["end"] or "—"))
        out.append(_metric_table(p))
        if p.get("caveat"):
            out.append("\n> %s" % p["caveat"])
        out.append("")
    out.append("> 环比 =（本期 − 上期）÷ 上期 × 100%，保留一位小数；上期为 0 或后台没记录时"
               "写「—」，不强算。比率类指标（互动率、点击率）环比用**百分点（pp）**表示。")
    return "\n".join(out)


def _sec41(facts):
    rows = []
    for p in facts["platforms"]:
        for post in p["posts_in_period"]:
            er = None
            base = post.get("reach") or post.get("impressions")
            if post.get("engagement") is not None and base:
                er = round(post["engagement"] / base * 100, 2)
            rows.append([post.get("date") or "—", p["cn"],
                         (post.get("title") or "")[:30] or "—",
                         n(post.get("impressions")), n(post.get("reach")),
                         n(post.get("engagement")), pct(er),
                         n(post.get("clicks"))])
    if not rows:
        return "本期统计周期内无新发内容。"
    return md_table(["日期", "平台", "主题", "曝光/播放", "触达", "互动", "互动率", "点击"], rows)


def _sec5(facts, prose):
    out = []
    for p in facts["platforms"]:
        for dim, items in (p.get("audience") or {}).items():
            top = items[:5]
            if not top:
                continue
            out.append("- **%s·%s**：%s" % (p["cn"], dim,
                                            " ｜ ".join("%s %s" % (k, n(v)) for k, v in top)))
    if not out:
        out.append("- 本期后台导出未包含受众画像（行业 / 职能 / 地区）数据。")
    out.append("")
    out.append(prose.get("audience", ""))
    return "\n".join(out)


PLAN_HINT = ("| 日期 | 平台 | 形态 | 主题 | 内容类型 | 目的 | 素材 / 审核状态 |\n"
             "|---|---|---|---|---|---|---|\n"
             "| 【MM/DD】 | 【】 | 【】 | 【】 | 【企业介绍 / 产品 / 活动 / 资质】 | "
             "【承接节点受众 / 强化专业定位 / 新品曝光…】 | 【待客户审核】 |\n\n"
             "> 发布窗口默认周二至周五；贴文发布前须客户审核通过，未过审的写「待审核」，"
             "不写「将发布」。\n"
             "> **本节需运营专员按真实内容日历填写**，并确保第六节的每条调整动作"
             "在这里都有对应贴文。")

NEED_HINT = ("| 事项 | 用途 | 希望提供时间 |\n|---|---|---|\n"
             "| 【例：展位现场照片】 | 【展后回顾贴】 | 【MM/DD 前】 |\n")


def report(facts, prose):
    f = facts
    parts = [
        "# %s %s运营周报 —— %s–%s" % (f["client"], f["platform_label"],
                                       f["period"]["start"], f["period"]["end"]),
        "",
        _internal_block(f),
        "",
        "---",
        "",
        "## 一、本周结论",
        "",
        prose.get("conclusion", ""),
        "",
        "## 二、本周发布",
        "",
        _sec2_posts(f, prose),
        "",
        "## 三、核心数据",
        "",
        _sec3(f),
        "",
        "## 四、内容表现",
        "",
        "### 4.1 单帖数据（本期统计周期内的新贴）",
        "",
        _sec41(f),
        "",
        "### 4.2 表现最好的内容",
        "",
        prose.get("best_content", ""),
        "",
        "### 4.3 待优化点",
        "",
        prose.get("todo", ""),
        "",
        "## 五、访客与受众",
        "",
        _sec5(f, prose),
        "",
        "## 六、本周复盘（三段式：归因 → 亮点 → 动作）",
        "",
        prose.get("review", ""),
        "",
        "## 七、下周计划（建议排期 · 待确认）",
        "",
        PLAN_HINT,
        "",
        "## 八、需客户配合",
        "",
        NEED_HINT,
        "",
        "---",
        "",
        "*%s*" % f["footnote"],
        "",
        "---",
        "",
        "## 附录 A · 解读段落（发群用）",
        "",
        prose.get("appendix_a", ""),
        "",
        "## 附录 B · 群内同步话术",
        "",
        prose.get("appendix_b", ""),
        "",
    ]
    return "\n".join(parts)


def issues_table(issues):
    """给界面用的数据问题清单。"""
    lv = {"block": "必须处理", "warn": "注意", "info": "口径说明"}
    return md_table(["级别", "涉及对象", "问题", "具体表现", "处理建议"],
                    [[lv.get(i["level"], i["level"]), i["scope"], i["problem"],
                      i["evidence"], i["advice"]] for i in issues])
