# -*- coding: utf-8 -*-
"""编排:上传的字节流 -> 一份或多份客户周报。

链路(每一步的产出都能单独看,出问题好定位):
    ingest   解包/解码/多 sheet
    platforms 识别 + 归一化
    metrics  区间汇总 + 环比(从 store 取上期)
    checks   口径体检 -> 数据问题清单
    facts    冻结成事实包 —— **周报里的数字到此为止**
    writer   DeepSeek 写文字 + 数字守卫
    render   拼成八节 markdown
    feishu   建 docx(可选)
"""
from app.modules.social import checks, ingest, metrics, platforms, render, store, writer

TOP_AUDIENCE = 5          # 每个画像维度给 AI 看几条,太多会稀释提示词


def _infer_year(groups):
    """TikTok 的日期是「9月13日」,不带年份,得从别的平台推。

    **不能扫原始文本里的 20xx** —— 帖子 ID、视频链接里全是长数字,
    `7506243041360515072` 这种一扫一个准,实测推出过 2099 年。
    正确做法是只看**其他平台已经解析出来的真实日期**:它们都自带年份。
    """
    years = set()
    for (_client, plat), sheets in groups.items():
        if plat == "tiktok":
            continue
        data = platforms.normalize(_client, plat, sheets, 0)
        for r in (data.get("daily") or []):
            years.add(int(r["date"][:4]))
        for p in (data.get("posts") or []):
            if p.get("date"):
                years.add(int(p["date"][:4]))
    years = {y for y in years if 2000 <= y <= 2100}
    if years:
        return max(years)
    import datetime
    return datetime.date.today().year


CAVEAT = {
    "ig_post_level_cumulative":
        "Instagram 后台本次未导出账号级日数据，上表为帖子级「创建至今」累计值，"
        "**与其余平台不同口径，不做横向比较**。下期起补齐账号级日数据后纳入统一口径。",
    "tiktok_no_reach":
        "TikTok 后台不提供触达人数，互动率以播放量为分母计算，与 Facebook / LinkedIn 的"
        "「互动 ÷ 触达」不是一回事。",
    "yt_reach_not_additive":
        "独立观看者为区间去重值，不可按日相加。",
}


def analyze(raw, filename, year=None, log=None):
    """解析 + 计算 + 体检。不调 AI,所以很快,可以先给界面预览。"""
    log = log or (lambda *a: None)
    sheets = ingest.read_upload(raw, filename)
    log("解析出 %d 张工作表" % len(sheets))

    groups = platforms.group(sheets)
    if not groups:
        raise ingest.IngestError(
            "没能识别出任何平台。请按「客户名/平台名/导出文件」的目录结构打包，"
            "平台名用 Facebook / Instagram / YouTube / LinkedIn / TikTok。")
    year = year or _infer_year(groups)
    log("识别出 %d 个 客户×平台（年份口径 %d）" % (len(groups), year))

    bundles, totals = {}, {}
    for key, sh in groups.items():
        client, plat = key
        data = platforms.normalize(client, plat, sh, year)
        s, e = metrics.period(data)
        bundles[key] = data
        totals[key] = metrics.totals(data, s, e)
        log("  %s / %s：%s~%s，%d 篇内容" % (client, platforms.PLATFORM_CN[plat],
                                             s or "—", e or "—", totals[key].get("posts", 0)))

    issues = checks.run(bundles, totals)
    log("口径体检：%d 条（必须处理 %d 条）"
        % (len(issues), sum(1 for i in issues if i["level"] == "block")))
    return {"bundles": bundles, "totals": totals, "issues": issues, "year": year}


def build_facts(client, analysis):
    """一个客户的事实包。**周报里允许出现的数字,全集就是它。**"""
    keys = [k for k in analysis["bundles"] if k[0] == client]
    keys.sort(key=lambda k: list(platforms.PLATFORMS).index(k[1]))

    plats, starts, ends = [], [], []
    has_prev = False
    for key in keys:
        data, t = analysis["bundles"][key], analysis["totals"][key]
        s, e = metrics.period(data)
        if s:
            starts.append(s)
            ends.append(e)

        prev = store.previous(client, key[1], s or "")
        delta = metrics.compare(t, (prev or {}).get("totals")) if prev else {}
        if prev:
            has_prev = True

        # 三个桶,别混:
        #   inside  本期发的        -> 第二节标「是」、第四节单帖表
        #   after   截止日之后发的  -> 第二节标「下期计入」
        #   before  本期之前发的    -> **不进第二节**,只作为存量表现的参考
        # 早期版本把 before 也标成「下期计入」,结果 2024 年的老视频出现在「本周发布」里。
        inside, after, before = [], [], []
        for p in data.get("posts") or []:
            if not p.get("date"):
                continue
            slim = _slim(p)
            if s and p["date"] < s:
                before.append(slim)
            elif e and p["date"] > e:
                after.append(slim)
            else:
                inside.append(slim)
        # 内容太多时按表现截断,避免把 79 条 YouTube 存量视频全塞给 AI
        inside = sorted(inside, key=lambda x: x.get("impressions") or 0, reverse=True)[:12]
        after = sorted(after, key=lambda x: x.get("date") or "")[:6]
        before = sorted(before, key=lambda x: x.get("impressions") or 0, reverse=True)[:6]

        caveats = [CAVEAT[f] for f in dict.fromkeys(data.get("flags") or []) if f in CAVEAT]
        aud = {d: [(k2, v) for k2, v in items[:TOP_AUDIENCE]]
               for d, items in (data.get("audience") or {}).items()}

        tt = dict(t)
        if tt.get("followers_net") is None and tt.get("followers_net") != 0:
            tt["followers_net"] = t.get("followers_net")
        plats.append({
            "platform": key[1], "cn": platforms.PLATFORM_CN[key[1]],
            "period": {"start": s, "end": e},
            "totals": tt, "delta": delta,
            "prev_period": {"start": (prev or {}).get("start"),
                            "end": (prev or {}).get("end")} if prev else None,
            "posts_in_period": inside, "posts_next_period": after,
            "posts_backlog": before,
            "audience": aud,
            "caveat": " ".join(caveats) if caveats else "",
            "daily": [{k2: v for k2, v in r.items() if v is not None}
                      for r in (data.get("daily") or [])],
        })

    label = plats[0]["cn"] + " " if len(plats) == 1 else "社媒"
    issues = [i for i in analysis["issues"]
              if i["scope"] == "全部" or i["scope"].startswith(client)]

    return {
        "client": client,
        "platform_label": label,
        "period": {"start": min(starts) if starts else None,
                   "end": max(ends) if ends else None},
        "has_prev": has_prev,
        "platforms": plats,
        "issues": issues,
        "footnote": _footnote(client, plats),
    }


def _slim(p):
    """只把 AI 真正用得上的字段给它 —— 字段越少,跑偏的余地越小。"""
    keep = ("date", "kind", "title", "impressions", "reach", "engagement", "likes",
            "comments", "shares", "saves", "clicks", "ctr", "duration", "avg_watch",
            "thumb_impr", "thumb_ctr", "subs", "cumulative", "url")
    return {k: p[k] for k in keep if p.get(k) is not None}


SOURCE_CN = {
    "facebook": "Meta Business Suite 后台导出",
    "instagram": "Meta Business Suite 后台导出",
    "youtube": "YouTube Studio 分析后台导出",
    "linkedin": "LinkedIn 主页后台「分析」导出",
    "tiktok": "TikTok 后台「分析」导出",
}


def _footnote(client, plats):
    src = "；".join("%s 数据来自 %s，统计周期 %s 至 %s"
                    % (p["cn"], SOURCE_CN[p["platform"]],
                       p["period"]["start"] or "—", p["period"]["end"] or "—")
                    for p in plats)
    tail = ("后台数据有 1–2 天延迟，与移动端 App 显示可能有小幅差异。"
            "本期无广告投放。")
    extra = " ".join(p["caveat"] for p in plats if p.get("caveat"))
    return "数据口径：%s。%s %s" % (src, extra, tail)


def generate(client, analysis, log=None, use_ai=True):
    """出一份客户周报。返回 {markdown, facts, prose, ai}。"""
    log = log or (lambda *a: None)
    facts = build_facts(client, analysis)
    if use_ai:
        log("调用 AI 写文字段落（数字不经过 AI）…")
        prose, ai = writer.write(facts, log=log)
    else:
        prose, ai = dict(writer.FALLBACK), {"skipped": True}
    md = render.report(facts, prose)
    return {"markdown": md, "facts": facts, "prose": prose, "ai": ai}


def archive(client, analysis):
    """把本期汇总存档,下期才有环比。**出稿成功后才调,避免半截数据污染基线。**"""
    saved = []
    for key, t in analysis["totals"].items():
        if key[0] != client:
            continue
        s, e = metrics.period(analysis["bundles"][key])
        if not e:
            continue
        saved.append(store.save(client, key[1], s, e, t))
    return saved


def clients_of(analysis):
    out = []
    for c, _p in analysis["bundles"]:
        if c not in out:
            out.append(c)
    return out
