# -*- coding: utf-8 -*-
"""审计报告 markdown(转飞书文档用)。结构对齐团队审计报告样板:
总览 → 上轮核验 → P0/P1/P2 清单 → 各维度详情 → 数据来源与局限。
"""
import collections, datetime as dt, statistics
from urllib.parse import urlparse

from app.modules.audit import findings as F
from app.modules.audit.common import jload


def _cell(v):
    return str(v if v is not None else "").replace("|", "／").replace("\n", " ").strip() or "—"


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _d(date):
    return "%s-%s-%s" % (date[:4], date[4:6], date[6:8])


def _paths(urls, k=5):
    ps = [urlparse(u).path or u if "://" in u else u for u in urls[:k]]
    return "、".join(ps) + (" 等" if len(urls) > k else "")


def build(cfg, summary, issues, prev_name=None, prev_summary=None, prev_issues=None, sheet_url=None, client=""):
    out, th = cfg["out"], F.thresholds(cfg)
    S = jload(out, "scores.json", {})
    lh = jload(out, "lighthouse.json", {})
    probe = jload(out, "probe.json", {})
    ctx = F.context(cfg)
    date = _d(cfg["date"])
    rnd = "复审" if prev_name else "首轮"
    L = []
    L.append("# %s SEO 审计报告(%s %s)" % (cfg["site"], date, rnd))
    L.append("> 工作台自动生成的**初稿**:数字均为实测,P0/P1/P2 由规则引擎按实测数据起草。对外发送前,请审计负责人逐条复核、"
             "补充 GSC / 外链 / 内容质量等脚本测不到的维度。")
    L.append("**客户**:%s　**站点**:https://%s　**CMS**:%s" % (client or "—", cfg["host"], probe.get("cms") or "未识别"))
    L.append("**基线**:%s" % ("上一轮 %s 审计" % _d(prev_name.replace("audit-", "")) if prev_name else "首轮审计,本次数据即开工基线"))
    L.append("**本轮范围**:全量抓取 %s 个 URL(sitemap %s 条 + 站内链接),HTML 页面 %s 个%s;主机归一 / 404 / CMS 重复入口定向探测;"
             "%s正文图逐张测重。" % (summary.get("抓取 URL 数"), summary.get("sitemap URL 数"), summary.get("HTML 200 页数"),
                                 "(" + "、".join("%s %s" % (k.split("-")[-1], v) for k, v in summary.items() if k.startswith("页面数-")) + ")"
                                 if cfg.get("langs") else "",
                                 "Lighthouse 移动端实测;" if lh else ""))
    if sheet_url:
        L.append("**逐页明细表**(%s 行,含每页问题标记):%s" % (summary.get("HTML 200 页数"), sheet_url))

    # ---------------- 总览
    L.append("## 总览")
    langs = sorted(k.split("-")[-1] for k in summary if k.startswith("Onpage 平均分-") and not k.endswith("全站"))
    main = cfg.get("default_lang", "en")
    rate = lambda v: "—" if v is None else ("良好" if v >= th["good"] else ("需优化" if v >= th["ok"] else "全面整改"))
    tdims = F.tech_dims(issues)
    tdesc = lambda ds: "%d 达标 · %d 部分 · %d 不达标" % tuple(sum(1 for d in ds if d[1] == s) for s in ("✅", "⚠️", "❌"))
    lv = collections.Counter(i["level"] for i in issues)
    rows = []
    k = "Onpage 平均分-%s" % main
    pv = (prev_summary or {}).get(k)
    rows.append(["On-page(%s %s 页均值,30 分制)" % (main, summary.get("页面数-" + main, summary.get("HTML 200 页数"))),
                 "%s/30" % pv if pv is not None else "—", "%s/30" % summary.get(k), rate(summary.get(k)),
                 "全站含小语种 %s/30" % summary.get("Onpage 平均分-全站") if len(langs) > 1 else ""])
    rows.append(["Technical(7 维)", "—", tdesc(tdims), "P0 %d · P1 %d · P2 %d" % (lv["P0"], lv["P1"], lv["P2"]),
                 "；".join(d[0] for d in tdims if d[1] == "❌") and "不达标:" + "、".join(d[0] for d in tdims if d[1] == "❌")])
    rows.append(["Content(CORE-EEAT)", "—", "未测", "—", "脚本不覆盖,需按 CORE-EEAT 抽 5-10 页人工评"])
    rows.append(["Backlink + 域名权威(CITE)", "—", "未测", "—", "需 Semrush / 外链侦察"])
    L.append(table(["维度", "上轮", "本轮", "评级", "说明"], rows))

    p0 = [i for i in issues if i["level"] == "P0"]
    p1 = [i for i in issues if i["level"] == "P1"]
    if p0:
        diag = "有 %d 项阻断爬取/收录的问题必须先解决:%s。" % (len(p0), "；".join(i["title"] for i in p0))
    else:
        diag = "未发现阻断爬取/收录的问题。"
    if p1:
        diag += "主要失分在:%s。" % "；".join(i["title"] for i in sorted(p1, key=lambda x: -x["count"])[:4])
    L.append("**一句话诊断**:" + diag)
    st = F.strengths(cfg, ctx, summary)
    if st:
        L.append("**做对了的(保持)**:" + "；".join(st) + "。")

    # ---------------- 上轮核验
    if prev_name and prev_issues is not None:
        L.append("## 一、上轮问题核验(%s)" % _d(prev_name.replace("audit-", "")))
        fixed, still, new = F.compare(prev_issues, issues)
        rows = [[i["title"], "%s · %d 页" % (i["level"], i["count"]), "本轮未再出现", "✅"] for i in fixed]
        for a, b in still:
            better = b["count"] < a["count"]
            rows.append([b["title"], "%s · %d 页" % (a["level"], a["count"]), "%s · %d 页" % (b["level"], b["count"]),
                         "⚠️ 好转" if better else ("❌ 未改" if b["count"] == a["count"] else "❌ 恶化")])
        rows += [[i["title"], "—", "%s · %d 页" % (i["level"], i["count"]), "🆕 新出现"] for i in new]
        L.append(table(["问题", "上轮", "本轮", "结论"], rows))
        if prev_summary:
            keys = ["HTML 200 页数", "Onpage 平均分-全站", "可抓页面不在 sitemap", "缺 H1", "Title 重复页", "Title 超 60 字符",
                    "Description 重复页", "有 JSON-LD 的页", "正文图缺 ALT", "正文图 WebP/AVIF", "唯一正文图合计 MB"]
            rows = [[k, prev_summary.get(k), summary.get(k)] for k in keys if k in summary]
            L.append("**关键指标对比**")
            L.append(table(["指标", "上轮", "本轮"], rows))
    sec = ["一", "二", "三", "四"] if prev_name else ["", "一", "二", "三"]

    # ---------------- 问题清单
    L.append("## %s、优先级修复清单" % sec[1])
    if not issues:
        L.append("本轮规则检查未发现问题。")
    for level, name in (("P0", "阻断爬取/收录,先做"), ("P1", "明显失分"), ("P2", "优化项")):
        items = [i for i in issues if i["level"] == level]
        if not items:
            continue
        L.append("### %s(%s,%d 项)" % (level, name, len(items)))
        if level == "P2":
            L.append(table(["#", "问题", "影响", "动作"],
                           [[i["id"], i["title"], _paths(i["examples"]) if i["count"] else i["evidence"], i["action"]] for i in items]))
            continue
        for i in items:
            L.append("**%s %s**" % (i["id"], i["title"]))
            if i["evidence"]:
                L.append("证据:" + i["evidence"])
            if i["count"]:
                L.append("影响:%d %s,如 %s" % (i["count"], i.get("unit", "页"), _paths(i["examples"])))
            L.append("动作:" + i["action"])

    # ---------------- 各维度详情
    L.append("## %s、各维度详情" % sec[2])
    L.append("### Technical(7 维)")
    L.append(table(["维度", "结果", "证据"], tdims))
    if lh:
        L.append("**Lighthouse 移动端**(实验室数据,非真实用户 CrUX)")
        L.append(table(["页面", "性能", "SEO", "LCP", "CLS", "TBT"],
                       [[urlparse(u).path or "/", r.get("performance"), r.get("seo"),
                         "%.1fs" % (r["LCP_ms"] / 1000.0) if r.get("LCP_ms") else "—", r.get("CLS"),
                         "%sms" % r.get("TBT_ms")] for u, r in lh.items() if "error" not in r]))
    ttfb = probe.get("ttfb", {})
    if ttfb:
        L.append("**TTFB**(5 次实测,标准 ≤%dms):" % th["ttfb_ms"] +
                 "；".join("%s 中位 %dms" % (urlparse(u).path or "/", statistics.median([t for t in ts if t > 0] or [0]))
                          for u, ts in ttfb.items()))

    L.append("### On-page(30 分制)")
    ms = [v for v in S.values() if v["lang"] == main] or list(S.values())
    agg = collections.OrderedDict()
    for v in ms:
        for dim, name, s, _ in v["items"]:
            a = agg.setdefault(dim, [0, 0])
            a[0] += s
            a[1] += 1
    nper = collections.Counter(dim for dim, _, _, _ in (ms[0]["items"] if ms else []))
    L.append(table(["维度", "平均得分", "满分"],
                   [[dim, "%.1f" % (a[0] / max(len(ms), 1)), nper[dim]] for dim, a in agg.items()]))
    fail = collections.Counter((dim, name) for v in ms for dim, name, s, _ in v["items"] if s == 0)
    L.append("**失分最多的检查项**(%s %d 页)" % (main, len(ms)))
    L.append(table(["检查项", "失分页"], [["%s · %s" % k, "%d/%d" % (c, len(ms))] for k, c in fail.most_common(10)]))
    dist = collections.Counter(v["rating"] for v in ms)
    L.append("评级分布:良好 %d · 需优化 %d · 全面整改 %d。得分最低:%s。" % (
        dist["良好"], dist["需优化"], dist["全面整改"],
        "；".join("%s %d" % (v["path"], v["score"]) for v in sorted(ms, key=lambda x: x["score"])[:5])))
    if len(langs) > 1:
        L.append("各语种均分:" + "；".join("%s %s" % (l, summary.get("Onpage 平均分-" + l)) for l in langs))

    # ---------------- 数据来源与局限
    L.append("## %s、数据来源与局限" % sec[3])
    L.append("- 页面实况:%s 无 cookie 抓取(桌面 Chrome UA),sitemap + 同域链接,上限 %s 页%s。" % (
        date, cfg.get("max_pages"), ";**已触顶,结果不完整**" if summary.get("抓取 URL 数", 0) >= cfg.get("max_pages", 1e9) else ""))
    L.append("- 阈值:Title %d–%d、Meta %d–%d 字符;图片建议 <%dKB、上限 %dKB;LCP ≤%dms、CLS ≤%s、TTFB ≤%dms;On-page ≥%d 良好 / %d–%d 需优化。" % (
        th["title_min"], th["title_max"], th["desc_min"], th["desc_max"], th["img_kb_ideal"], th["img_kb_max"],
        th["lcp_ms"], th["cls"], th["ttfb_ms"], th["good"], th["ok"], th["good"] - 1))
    L.append("- 主关键词从 Title/H1 推断,On-page 关键词类检查项仅供参考;「内容无过时信息」「外链 nofollow」脚本不判、默认通过。")
    L.append("- 未覆盖:收录量与查询词(GSC)、真实用户 CWV / INP(CrUX)、外链与域名权威、内容质量(CORE-EEAT)、AI 可见度。")
    if not lh:
        L.append("- 本轮未跑 Lighthouse,速度只看 TTFB 与图片体积。")
    L.append("- 定向探测(重复入口、404)会在客户服务器日志留下 4xx/5xx,属本次审计触发,勿与真实爬虫问题混淆。")
    L.append("- FAQ 富摘要仅对政府/健康类权威站展示,FAQPage 的作用是机器可读与 AI 引用,不向客户承诺富结果;询价制产品 Product Schema 不得填假价格。")
    L.append("\n生成时间:%s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    return "\n\n".join(L)
