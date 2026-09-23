# -*- coding: utf-8 -*-
"""平台识别 + 把各家后台导出归一化成同一套字段。

每个平台后台的字段名、口径、甚至日期写法都不一样,这一层的职责是**只做翻译不做判断**:
把 Facebook 的「浏览人数」、LinkedIn 的「独立展示量」、YouTube 的「唯一身份观看者人数」
统一成 `reach`,但**不去决定哪个能跟哪个比** —— 那是 checks.py 的事。

归一化后的结构(每个 客户×平台 一份):
    {
      "client": "客户A", "platform": "facebook",
      "daily":    [{"date": "2026-09-14", "impressions": 21805, "reach": 19220, ...}, ...],
      "posts":    [{"date","kind","title","impressions","reach","engagement",...}, ...],
      "audience": {"地区": [("班加罗尔地区, 印度", 5736), ...], ...},
      "totals":   {"impressions": 130417, ...},     # 后台自己给的总计行,有才填
      "flags":    ["tiktok_content_cumulative", ...] # 交给 checks.py 的口径事实
    }
"""
import datetime as dt
import re

PLATFORMS = ("facebook", "instagram", "youtube", "linkedin", "tiktok")

ALIASES = {
    "facebook": ("facebook", "fb", "脸书"),
    "instagram": ("instagram", "ins", "ig"),
    "youtube": ("youtube", "ytb", "yt", "油管"),
    "linkedin": ("linkedin", "领英"),
    "tiktok": ("tiktok", "tk", "抖音国际版"),
}

PLATFORM_CN = {"facebook": "Facebook", "instagram": "Instagram", "youtube": "YouTube",
               "linkedin": "LinkedIn", "tiktok": "TikTok"}

# Facebook 的每个指标单独一个 csv,靠文件里第一行的指标名认
FB_METRIC = {
    "浏览量": "impressions", "浏览人数": "reach", "内容互动次数": "engagement",
    "facebook 关注次数": "new_followers", "facebook 访问量": "profile_views",
    "facebook 链接点击量": "clicks",
}


class ParseError(RuntimeError):
    pass


# ---------------------------------------------------------------- 小工具

def _num(v):
    """'1,234' / '12.5%' / '' -> 数字或 None。认不出一律 None,不猜。"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "—", "N/A", "nan"):
        return None
    pct = s.endswith("%")
    if pct:
        s = s[:-1]
    try:
        f = float(s)
    except ValueError:
        return None
    if pct:
        f = f / 100.0
    return int(f) if (not pct and f == int(f) and abs(f) < 1e15) else f


def _date(v, year_hint=None):
    """各平台的日期写法统一成 ISO。认不出返回 None。

    见过的写法:2026-09-14T00:00:00(FB) / 2026-09-14(YouTube) / 09/12/2026(LinkedIn 文本列)
    / 2026-09-12T00:00:00(LinkedIn 日期单元格) / 9月13日(TikTok,**不带年份**)
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "%s-%s-%s" % m.groups()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)          # 09/12/2026 = 月/日/年
    if m:
        return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d{1,2})月(\d{1,2})日$", s)                # TikTok,年份靠外部给
    if m and year_hint:
        return "%d-%02d-%02d" % (year_hint, int(m.group(1)), int(m.group(2)))
    m = re.match(r"^([A-Za-z]{3})[a-z]* (\d{1,2}), (\d{4})$", s)   # Sep 17, 2026
    if m:
        mon = ["jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec"].index(m.group(1).lower()) + 1
        return "%s-%02d-%02d" % (m.group(3), mon, int(m.group(2)))
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})[ T]", s)            # 09/21/2026 00:58
    if m:
        return "%s-%s-%s" % (m.group(3), m.group(1), m.group(2))
    return None


def _find(header, *cands):
    """按表头找列号。先精确后前缀,找不到返回 None(调用方自己决定要不要报错)。"""
    low = [str(h).strip().lower() for h in header]
    for c in cands:
        c = c.lower()
        if c in low:
            return low.index(c)
    for c in cands:
        c = c.lower()
        for i, h in enumerate(low):
            if h.startswith(c) or c in h:
                return i
    return None


def _cell(row, idx):
    return row[idx] if (idx is not None and idx < len(row)) else None


def _merge_daily(bucket, date, field, value):
    if not date or value is None:
        return
    bucket.setdefault(date, {"date": date})[field] = value


# ---------------------------------------------------------------- 识别

def group(sheets):
    """[Sheet] -> {(客户, 平台): [Sheet]}。

    平台靠路径里的目录名认(专员的目录习惯就是 客户/平台/文件),客户取平台目录的上一级。
    路径里认不出平台的,退回按表头特征认,客户名标「未分组」交给人工选。
    """
    out = {}
    for s in sheets:
        parts = s.parts
        plat = idx = None
        for i, p in enumerate(parts[:-1]):
            hit = _platform_of(p)
            if hit:
                plat, idx = hit, i
                break
        if plat:
            client = parts[idx - 1] if idx >= 1 else "未分组"
        else:
            plat = _sniff(s)
            client = parts[-2] if len(parts) >= 2 else "未分组"
        if not plat:
            continue
        out.setdefault((client, plat), []).append(s)
    return out


def _platform_of(name):
    low = str(name).strip().lower()
    for plat, keys in ALIASES.items():
        for k in keys:
            if low == k or low.startswith(k):
                return plat
    return None


def _sniff(sheet):
    """路径认不出时,按内容特征兜底。"""
    head = " ".join(str(c) for c in (sheet.rows[0] if sheet.rows else []))
    name = (sheet.sheet or "") + " " + sheet.path
    if "帖子编号" in head and "账户账号" in head:
        return "instagram"
    if "帖子编号" in head and "公共主页名称" in head:
        return "facebook"
    if "唯一身份观看者人数" in head or "缩略图展示次数" in head:
        return "youtube"
    if "展示量" in head or "动态标题" in head or "简介页面访问量" in head:
        return "linkedin"
    if "Video Views" in head or "Follower" in name:
        return "tiktok"
    if any(k in head for k in FB_METRIC):
        return "facebook"
    return None


def normalize(client, platform, sheets, year_hint=None):
    """把一组 Sheet 归一化。year_hint 供 TikTok 这种不带年份的日期用。"""
    fn = {"facebook": _facebook, "instagram": _instagram, "youtube": _youtube,
          "linkedin": _linkedin, "tiktok": _tiktok}[platform]
    data = {"client": client, "platform": platform, "daily": [], "posts": [],
            "audience": {}, "totals": {}, "flags": [], "sources": []}
    for s in sheets:
        data["sources"].append(s.path + (("#" + s.sheet) if s.sheet else ""))
    fn(data, sheets, year_hint or dt.date.today().year)
    data["daily"].sort(key=lambda r: r["date"])
    data["posts"].sort(key=lambda r: r.get("date") or "")
    return data


# ---------------------------------------------------------------- Facebook

def _facebook(data, sheets, year):
    daily = {}
    for s in sheets:
        rows = s.rows
        head0 = str(rows[0][0]).strip().lower() if rows and rows[0] else ""

        # 指标 csv:第1行指标名、第2行表头(日期/Primary)、第3行起数据
        if head0 in FB_METRIC:
            field = FB_METRIC[head0]
            for r in rows[2:]:
                _merge_daily(daily, _date(_cell(r, 0)), field, _num(_cell(r, 1)))
            continue

        # 帖子 csv
        hdr = rows[0] if rows else []
        if _find(hdr, "帖子编号") is not None and _find(hdr, "公共主页名称") is not None:
            _fb_posts(data, hdr, rows[1:])
    data["daily"] = list(daily.values())


def _fb_posts(data, hdr, rows):
    ix = {k: _find(hdr, *v) for k, v in {
        "date": ("发布时间",), "kind": ("帖子类型",), "title": ("标题", "描述"),
        "impressions": ("观看量",), "reach": ("覆盖人数",), "likes": ("心情",),
        "comments": ("评论数",), "shares": ("分享次数",), "clicks": ("总点击量",),
        "duration": ("时长（秒）", "时长"), "avg_watch": ("平均观看时长（秒）",),
        "url": ("固定链接",)}.items()}
    for r in rows:
        p = {k: (_date(_cell(r, i)) if k == "date" else
                 _cell(r, i) if k in ("kind", "title", "url") else _num(_cell(r, i)))
             for k, i in ix.items()}
        if not p.get("date"):
            continue
        p["engagement"] = sum(v for v in (p.get("likes"), p.get("comments"),
                                          p.get("shares")) if v) or 0
        data["posts"].append(p)


# ---------------------------------------------------------------- Instagram

def _instagram(data, sheets, year):
    """IG 后台只导得出帖子级、且是「内容创建至今」的累计口径,没有账号级日数据。

    这不是导出漏了,是 IG 后台该入口本身就不给 —— 所以这里必然只有 posts,
    并且打上 flag 让 checks.py 提醒「不可与其他平台横向比」。
    """
    for s in sheets:
        hdr = s.rows[0] if s.rows else []
        if _find(hdr, "帖子编号") is None:
            continue
        ix = {k: _find(hdr, *v) for k, v in {
            "date": ("发布时间",), "kind": ("帖子类型",), "title": ("描述",),
            "impressions": ("浏览量",), "reach": ("覆盖人数",), "likes": ("赞",),
            "comments": ("评论",), "shares": ("分享",), "saves": ("收藏次数",),
            "new_followers": ("关注者数",), "duration": ("时长（秒）",),
            "url": ("固定链接",), "window": ("数据注释",)}.items()}
        for r in s.rows[1:]:
            p = {k: (_date(_cell(r, i)) if k == "date" else
                     _cell(r, i) if k in ("kind", "title", "url", "window") else
                     _num(_cell(r, i))) for k, i in ix.items()}
            if not p.get("date"):
                continue
            p["engagement"] = sum(v for v in (p.get("likes"), p.get("comments"),
                                              p.get("shares"), p.get("saves")) if v) or 0
            data["posts"].append(p)
    if data["posts"]:
        data["flags"].append("ig_post_level_cumulative")


# ---------------------------------------------------------------- YouTube

def _youtube(data, sheets, year):
    daily = {}
    for s in sheets:
        rows = s.rows
        if not rows:
            continue
        hdr = rows[0]
        low = s.path.lower()

        # 「日期」维度的表格数据:日期 / 观看次数 / 观看时长 / 平均观看时长,首行是总计
        if _find(hdr, "观看次数") is not None and _find(hdr, "日期") is not None \
                and _find(hdr, "内容") is None:
            iv, ih, ia = (_find(hdr, "观看次数"), _find(hdr, "观看时长（小时）"),
                          _find(hdr, "平均观看时长"))
            for r in rows[1:]:
                key = str(_cell(r, 0)).strip()
                if key == "总计":
                    data["totals"]["impressions"] = _num(_cell(r, iv))
                    data["totals"]["watch_hours"] = _num(_cell(r, ih))
                    data["totals"]["avg_watch"] = _cell(r, ia)
                    continue
                d = _date(key)
                _merge_daily(daily, d, "impressions", _num(_cell(r, iv)))
                _merge_daily(daily, d, "watch_hours", _num(_cell(r, ih)))
            continue

        # 图表数据:一行一个「视频×日期」,用来给每个视频补发布日期。
        # 必须排在下面的总计分支前面 —— 它同时有「日期」和「唯一身份观看者人数」两列,
        # 会被那个分支抢走,结果所有视频的发布日期全丢。
        if _find(hdr, "视频发布时间") is not None:
            it, ip = _find(hdr, "内容"), _find(hdr, "视频发布时间")
            pub = {}
            for r in rows[1:]:
                vid = _cell(r, it)
                if vid and vid not in pub:
                    pub[vid] = _date(_cell(r, ip))
            data.setdefault("_pub", {}).update(pub)
            continue

        # 「内容」维度的总计:只有 日期 / 唯一身份观看者人数 两列
        if _find(hdr, "唯一身份观看者人数") is not None and _find(hdr, "日期") is not None \
                and len(hdr) == 2:
            iu = _find(hdr, "唯一身份观看者人数")
            for r in rows[1:]:
                _merge_daily(daily, _date(_cell(r, 0)), "reach", _num(_cell(r, iu)))
            data["flags"].append("yt_reach_not_additive")
            continue

        # 「内容」维度的表格数据:一行一个视频,首行总计
        if _find(hdr, "内容") is not None and _find(hdr, "视频标题") is not None \
                and _find(hdr, "缩略图展示次数") is not None:
            ix = {k: _find(hdr, *v) for k, v in {
                "title": ("视频标题",), "reach": ("唯一身份观看者人数",),
                "impressions": ("观看次数",), "subs": ("订阅人数",),
                "thumb_impr": ("缩略图展示次数",), "thumb_ctr": ("缩略图点击率 (%)",)}.items()}
            for r in rows[1:]:
                rec = {k: (_cell(r, i) if k == "title" else _num(_cell(r, i)))
                       for k, i in ix.items()}
                if str(_cell(r, 0)).strip() == "总计":
                    for k in ("reach", "subs", "thumb_impr", "thumb_ctr"):
                        data["totals"][k] = rec.get(k)
                    data["totals"]["views_by_content"] = rec.get("impressions")
                    continue
                rec["id"] = _cell(r, 0)
                rec["date"] = None
                data["posts"].append(rec)
            continue

    for p in data["posts"]:
        if p.get("id") in data.get("_pub", {}):
            p["date"] = data["_pub"][p["id"]]
    data.pop("_pub", None)
    data["daily"] = list(daily.values())
    if data["totals"].get("views_by_content") is not None \
            and data["totals"].get("impressions") is not None \
            and data["totals"]["views_by_content"] != data["totals"]["impressions"]:
        data["flags"].append("yt_two_dimensions_differ")


# ---------------------------------------------------------------- LinkedIn

LI_AUDIENCE = {"地点": "地区", "职能类别": "职能", "高级": "职级",
               "所属行业": "行业", "公司规模": "公司规模"}


def _linkedin(data, sheets, year):
    daily = {}
    for s in sheets:
        rows, name = s.rows, (s.sheet or "")
        if not rows:
            continue
        hdr = rows[0]

        if name in LI_AUDIENCE and len(hdr) >= 2:
            # 访客表和关注者表都有同名 sheet;关注者的是粉丝池,访客的是本期浏览
            kind = "粉丝" if "follower" in s.path.lower() else "访客"
            key = "%s-%s" % (kind, LI_AUDIENCE[name])
            data["audience"][key] = [(str(r[0]), _num(r[1])) for r in rows[1:]
                                     if len(r) >= 2 and _num(r[1]) is not None]
            continue

        if _find(hdr, "展示量") is not None:                     # 内容分析·数据
            ix = {"impressions": _find(hdr, "展示量"), "reach": _find(hdr, "独立展示量"),
                  "clicks": _find(hdr, "点击量"), "reactions": _find(hdr, "回应量"),
                  "comments": _find(hdr, "评论量"), "shares": _find(hdr, "转发")}
            for r in rows[1:]:
                d = _date(_cell(r, 0))
                for k, i in ix.items():
                    _merge_daily(daily, d, k, _num(_cell(r, i)))
                got = [_num(_cell(r, ix[k])) for k in ("reactions", "comments", "shares")]
                _merge_daily(daily, d, "engagement", sum(v for v in got if v is not None))
            continue

        if _find(hdr, "主页访问总量 (总计)") is not None:            # 访客统计
            ix = {"profile_views": _find(hdr, "主页访问总量 (总计)"),
                  "profile_uniques": _find(hdr, "主页独立访问总量 (总计)"),
                  "pv_web": _find(hdr, "主页访问总量 (网页版)"),
                  "pv_mobile": _find(hdr, "主页访问总量 (移动版)"),
                  "jobs_views": _find(hdr, "职位页面访问量 (总计)")}
            for r in rows[1:]:
                d = _date(_cell(r, 0))
                for k, i in ix.items():
                    _merge_daily(daily, d, k, _num(_cell(r, i)))
            continue

        if name == "新关注者" or _find(hdr, "关注者总数") is not None and _find(hdr, "日期") is not None:
            i = _find(hdr, "关注者总数")
            for r in rows[1:]:
                _merge_daily(daily, _date(_cell(r, 0)), "new_followers", _num(_cell(r, i)))
            continue

        if name == "全部动态" or _find(hdr, "动态标题") is not None:
            # 首行是一句说明文字,真表头在第二行
            h2 = rows[1] if _find(hdr, "动态标题") is None else hdr
            body = rows[2:] if _find(hdr, "动态标题") is None else rows[1:]
            _li_posts(data, h2, body)
    data["daily"] = list(daily.values())


def _li_posts(data, hdr, rows):
    ix = {k: _find(hdr, *v) for k, v in {
        "date": ("创建日期",), "title": ("动态标题",), "author": ("发布者",),
        "impressions": ("展示次数",), "clicks": ("点击次数",), "ctr": ("点击率 (CTR)", "点击率"),
        "likes": ("点赞量",), "comments": ("评论",), "shares": ("转发",),
        "rate": ("参与率",), "url": ("动态链接",)}.items()}
    for r in rows:
        p = {k: (_date(_cell(r, i)) if k == "date" else
                 _cell(r, i) if k in ("title", "author", "url") else _num(_cell(r, i)))
             for k, i in ix.items()}
        if not p.get("date"):
            continue
        p["engagement"] = sum(v for v in (p.get("likes"), p.get("comments"),
                                          p.get("shares")) if v) or 0
        if p.get("title"):
            p["title"] = str(p["title"]).split("\n")[0][:90]
        data["posts"].append(p)


# ---------------------------------------------------------------- TikTok

def _tiktok(data, sheets, year):
    daily = {}
    for s in sheets:
        rows, name = s.rows, (s.sheet or "")
        if not rows:
            continue
        hdr = rows[0]

        if _find(hdr, "Video Views") is not None:                      # Overview
            ix = {"impressions": _find(hdr, "Video Views"),
                  "profile_views": _find(hdr, "Profile Views"),
                  "likes": _find(hdr, "Likes"), "comments": _find(hdr, "Comments"),
                  "shares": _find(hdr, "Shares")}
            for r in rows[1:]:
                d = _date(_cell(r, 0), year)
                for k, i in ix.items():
                    _merge_daily(daily, d, k, _num(_cell(r, i)))
                got = [_num(_cell(r, ix[k])) for k in ("likes", "comments", "shares")]
                _merge_daily(daily, d, "engagement", sum(v for v in got if v is not None))
            data["flags"].append("tiktok_no_reach")
            continue

        # 必须排在 FollowerHistory 前面:「Active followers」含有「followers」,
        # _find 的模糊匹配会让它被下面那个分支抢走,粉丝数就变成了活跃人数。
        if _find(hdr, "Active followers") is not None:                 # FollowerActivity
            act = {}
            for r in rows[1:]:
                d = _date(_cell(r, 0), year)
                if d:
                    act.setdefault(d, {})[int(_num(_cell(r, 1)) or 0)] = \
                        _num(_cell(r, 2)) or 0
            data["activity"] = act
            continue

        if _find(hdr, "Followers") is not None:                        # FollowerHistory
            i, j = _find(hdr, "Followers"), _find(hdr, "Difference in followers")
            for r in rows[1:]:
                d = _date(_cell(r, 0), year)
                _merge_daily(daily, d, "followers", _num(_cell(r, i)))
                _merge_daily(daily, d, "new_followers", _num(_cell(r, j)))
            continue

        if _find(hdr, "Top territories") is not None:
            data["audience"]["粉丝-地区"] = [(str(r[0]), _num(r[1])) for r in rows[1:]
                                              if len(r) >= 2]
            continue
        if _find(hdr, "Gender") is not None:
            data["audience"]["粉丝-性别"] = [(str(r[0]), _num(r[1])) for r in rows[1:]
                                              if len(r) >= 2]
            continue

        if _find(hdr, "Total views") is not None:                      # Content
            ix = {k: _find(hdr, *v) for k, v in {
                "date": ("Post time",), "title": ("Video title",),
                "impressions": ("Total views",), "likes": ("Total likes",),
                "comments": ("Total comments",), "shares": ("Total shares",),
                "url": ("Video link",)}.items()}
            for r in rows[1:]:
                p = {k: (_date(_cell(r, i), year) if k == "date" else
                         _cell(r, i) if k in ("title", "url") else _num(_cell(r, i)))
                     for k, i in ix.items()}
                if not p.get("date"):
                    continue
                p["engagement"] = sum(v for v in (p.get("likes"), p.get("comments"),
                                                  p.get("shares")) if v) or 0
                p["cumulative"] = True          # 关键:这是累计值不是本期增量
                if p.get("title"):
                    p["title"] = str(p["title"]).split("\n")[0][:90]
                data["posts"].append(p)
            data["flags"].append("tiktok_content_cumulative")
    data["daily"] = list(daily.values())
