# -*- coding: utf-8 -*-
"""GSC 周报出稿:按《SEO 客户周同步（含数据解读）》模板。

分工和社媒周报一样:**数字全由代码给,AI 只写字** —— 本周结论、三段式解读、本周计划。
AI 拿到的是 facts 里的事实,只许引用其中的数字;写不出来就退回代码模板句,不空着。
"""
import json
import re

from ..keywords import derive
from ..llm import client as llm
from . import analyze

SYSTEM = """你是外贸 B2B 网站的 SEO 顾问,给客户写每周同步。你会拿到一份 JSON 事实(GSC 近 7 天 vs 前 7 天的指标、判定、背离、点击差值 Top 榜、新发页面等)。

按下面规则写,只输出严格 JSON,不要 markdown 标记、不要解释:
{
  "status": "正常 | 关注 | 异常",
  "conclusion": ["整体状态:… —— 一句话说主因", "本周最重要的一个变化 + 它对客户意味着什么", "下周最重要的一件事"],
  "what": "① 发生了什么:1–2 句概括五个指标的组合形态,点出背离,不复述表格数字",
  "why": "② 为什么:给出下钻证据,查询词维度和页面维度必须覆盖;说清增减最多的词是品牌词 / 产品词 / 信息型长尾;解释平均排名(曝光加权:新词进入前 10 但停在 5–10 位会推高曝光、拉高平均排名、压低整体 CTR);每条结论后面跟数据",
  "next": "③ 下一步:由归因推出的动作,每条写清对象和验证时点;不确定的写「观察至【日期】再判」",
  "plans": [{"事项": "…", "具体对象": "…", "预期产出": "…", "来源": "第二节③ / 第三节 / 例行"}]
}

硬规则:
- 只能引用事实里出现过的数字,不得自己编任何数字、百分比、排名
- CTR 的环比用 pp(百分点);平均排名用「位」
- 小样本(周点击 <100)不写百分比结论,写绝对数变动,并说明要看连续 3 周趋势
- 标「关注」「异常」「背离」的指标必须给词级或页级证据(来自 queries_up / queries_down / pages_up / pages_down)
- 不向客户承诺具体排名位次与达成时限
- 解读里写到的动作,plans 里必须有对应一条;plans 至少含一条例行(如「核对上周新发页面收录与首周数据」)
- 用中文,口吻是给客户看的同步,不是内部备忘"""


def write(facts, status, client_name, use_ai=True, log=None):
    """AI 写字。失败或关闭 AI 时退回代码模板句。"""
    log = log or (lambda m: None)
    fb = fallback(facts, status)
    if not use_ai:
        return fb, None
    slim = {k: facts[k] for k in ("window", "cur", "prev", "small", "snapshot", "diverge",
                                  "queries_up", "queries_down", "pages_up", "pages_down",
                                  "top_queries", "brand_clicks", "n_queries", "new_pages",
                                  "quick_wins", "country", "device")}
    slim["client"] = client_name
    slim["overall_status_by_rule"] = status
    user = "【事实】\n" + json.dumps(slim, ensure_ascii=False, indent=1)
    try:
        log("调用 LLM 写结论 / 解读 / 计划…")
        r = llm.complete(SYSTEM, user, skill="gsc_weekly", stream=False, log=log,
                         max_tokens=6000, thinking=False)
        data = derive._extract_json(r["text"])
        out = {
            "status": data.get("status") or status,
            "conclusion": [str(x) for x in (data.get("conclusion") or [])][:3] or fb["conclusion"],
            "what": str(data.get("what") or fb["what"]),
            "why": str(data.get("why") or fb["why"]),
            "next": str(data.get("next") or fb["next"]),
            "plans": [p for p in (data.get("plans") or []) if isinstance(p, dict)] or fb["plans"],
        }
        # 状态由规则定,AI 只能写字;它改了就以规则为准并记一笔
        if out["status"] != status:
            log("AI 给的整体状态「%s」和规则判定「%s」不一致,以规则为准" % (out["status"], status))
            out["status"] = status
        return out, r.get("cost")
    except Exception as e:
        log("AI 写字失败(%s: %s),用模板句" % (type(e).__name__, str(e)[:120]))
        return fb, None


def fallback(facts, status):
    """不调 AI 的兜底文案:把事实按模板句式串起来,数字全来自 facts。"""
    c, p, w = facts["cur"], facts["prev"], facts["window"]
    snap = {s["指标"]: s for s in facts["snapshot"]}
    seg = lambda n: "%s %s(%s)" % (n, _fmt(snap[n]["本周"], n), snap[n]["环比"])
    what = "本周(%s–%s)%s、%s、%s、%s。" % (w["start"][5:], w["end"][5:], seg("曝光"), seg("点击"), seg("CTR"), seg("平均排名"))
    if facts["diverge"]:
        what += "形成「%s」的背离形态。" % "、".join(facts["diverge"])
    if facts["small"]:
        what += "周点击不足 100 属小样本,只看绝对数变动,不下百分比结论。"
    why = []
    if facts["queries_up"]:
        r = facts["queries_up"][0]
        why.append("点击增量最大的词是「%s」(%s → %s,%s)" % (r["对象"], r["上周点击"], r["本周点击"], r["词性"]))
    if facts["queries_down"]:
        r = facts["queries_down"][0]
        why.append("点击降幅最大的词是「%s」(%s → %s,%s,本周排名 %s)" % (r["对象"], r["上周点击"], r["本周点击"], r["词性"], r["本周排名"]))
    if facts["pages_up"]:
        r = facts["pages_up"][0]
        why.append("贡献增量最多的页面 %s(%s → %s)" % (r["对象"], r["上周点击"], r["本周点击"]))
    if facts["pages_down"]:
        r = facts["pages_down"][0]
        why.append("回落最多的页面 %s(%s → %s)" % (r["对象"], r["上周点击"], r["本周点击"]))
    if facts["new_pages"]:
        why.append("本周新增 %d 个有曝光的页面,首周曝光合计 %d" % (len(facts["new_pages"]), sum(x["首周曝光"] for x in facts["new_pages"])))
    why_txt = ";".join(why) + "。" if why else "本周词级 / 页级没有超过阈值的变动。"
    nxt = []
    if facts["queries_down"]:
        nxt.append("核查「%s」所在页面的 SERP 标题展示与位次,必要时优化 Title / Description,下周核对 CTR" % facts["queries_down"][0]["对象"])
    if facts["quick_wins"]:
        nxt.append("11–20 位的词(如「%s」)补内链与内容深度,观察至下周" % facts["quick_wins"][0]["查询词"])
    if facts["new_pages"]:
        nxt.append("新发页面回填首周曝光 / 点击,核对收录")
    nxt_txt = ";".join(nxt) + "。" if nxt else "维持例行:核对新发内容收录,观察连续 3 周趋势。"
    plans = [{"事项": x.split(",")[0][:30], "具体对象": "见第二节③", "预期产出": "下周同步里核对结果", "来源": "第二节③"} for x in nxt]
    plans.append({"事项": "核对上周新发页面收录与首周数据", "具体对象": "4.1 表内页面", "预期产出": "回填首周曝光 / 点击", "来源": "例行"})
    lead = snap["点击"]
    conclusion = [
        "整体状态:%s —— 点击 %s(%s),曝光 %s(%s),%s。" % (status, _fmt(c["clicks"], "点击"), snap["点击"]["环比"], _fmt(c["impressions"], "曝光"), snap["曝光"]["环比"],
                                                     ("存在 " + "、".join(facts["diverge"]) + " 的背离") if facts["diverge"] else "各指标方向一致"),
        why[0] + "。" if why else "本周没有超过阈值的词级变动。",
        nxt[0] + "。" if nxt else "下周维持例行核对。",
    ]
    return {"status": status, "conclusion": conclusion, "what": what, "why": why_txt, "next": nxt_txt, "plans": plans}


def _fmt(v, metric):
    if metric == "CTR":
        return "%.1f%%" % v
    if metric == "平均排名":
        return "%.1f" % v
    return "{:,}".format(int(v))


def markdown(facts, text, client_name):
    """按模板出 markdown。使用说明 / 解读示例 / 自检三个「发出前删除」块不带。"""
    w = facts["window"]
    md = lambda d: d[5:].replace("-", ".")
    title = "%s SEO 周同步（%s-%s）" % (client_name, md(w["start"]), md(w["end"]))
    L = ["# " + title, ""]
    L += ["## 一、本周结论", ""]
    for i, s in enumerate(text["conclusion"][:3]):
        L.append("%d. %s" % (i + 1, s))
    L += ["", "## 二、核心指标快照（GSC，近 7 天 vs 前 7 天）", "",
          "| 指标 | 本周 | 上周 | 环比 | 判定 | 备注 |", "|---|---|---|---|---|---|"]
    note = {"曝光": "效果报告", "点击": "效果报告", "CTR": "效果报告", "平均排名": "效果报告（曝光加权）"}
    for s in facts["snapshot"]:
        L.append("| %s | %s | %s | %s | %s | %s |" % (s["指标"], _fmt(s["本周"], s["指标"]), _fmt(s["上周"], s["指标"]), s["环比"], s["判定"], note[s["指标"]]))
    sm = facts.get("sitemaps") or []
    submitted = sum(x["submitted"] for x in sm)
    L.append("| 已收录页面数 | 【】 | 【】 | 【±x】 | 【】 | 页面索引报告手填；sitemap 已提交 %s 条 |" % (submitted if sm else "—"))
    L.append("| 真实询盘数 | 【】 | 【】 | 【±x 条】 | 【】 | 表单后台，已剔除垃圾询盘 |")
    L += ["",
          "> 判定规则：±10% 以内正常；10%–30% 关注（须下钻到词 / 页）；≥30% 或连续 2 周同向为异常；指标方向相反为背离；周点击 <100 为小样本，不定级、看绝对数。",
          "", "### 数据解读", "",
          "**① 发生了什么**：" + text["what"], "",
          "**② 为什么**：" + text["why"], "",
          "**③ 下一步**：" + text["next"], "",
          "### 2.1 下钻：变动最大的查询词 / 页面（点击差值 Top）", "",
          "| 维度 | 对象（词 / URL） | 本周点击 | 上周点击 | 差值 | 本周排名 | 词性 / 判断 |", "|---|---|---|---|---|---|---|"]
    rows = [("查询词", r) for r in facts["queries_up"]] + [("查询词", r) for r in facts["queries_down"]] \
         + [("页面", r) for r in facts["pages_up"]] + [("页面", r) for r in facts["pages_down"]]
    if rows:
        for dim, r in rows:
            L.append("| %s | %s | %s | %s | %+d | %s | %s%s |" % (
                dim, r["对象"], r["本周点击"], r["上周点击"], r["差值"], r["本周排名"], r["词性"],
                ("（" + r["状态"] + "）") if r["状态"] else ""))
    else:
        L.append("| — | 本周无超过阈值的变动 | | | | | |")
    if facts["top_queries"]:
        L += ["", "本周点击 Top 10 查询词（品牌词点击 %d，占 %s）：" % (
            facts["brand_clicks"], ("%.0f%%" % (facts["brand_clicks"] / facts["cur"]["clicks"] * 100)) if facts["cur"]["clicks"] else "—"), "",
              "| 查询词 | 点击 | 曝光 | CTR | 排名 | 词性 |", "|---|---|---|---|---|---|"]
        for r in facts["top_queries"]:
            L.append("| %s | %s | %s | %s%% | %s | %s |" % (r["查询词"], r["点击"], r["曝光"], r["CTR"], r["排名"], r["词性"]))
    L += ["", "## 三、收录与技术健康", "",
          "| 项目 | 本周 | 上周 | 变动 | 说明 |", "|---|---|---|---|---|",
          "| 已收录页面（页面索引） | 【】 | 【】 | 【±x】 | GSC 页面索引报告手填（接口不提供） |",
          "| 未收录页面 | 【】 | 【】 | 【±x】 | 主要原因桶：【404 / 已抓取未编入索引 / 重复网页 / 被 robots 屏蔽…】 |",
          "| 本周新发内容收录 | %s | — | — | 本周首次获得曝光的页面数（按 GSC 效果数据） |" % (("%d 个页面首次有曝光" % len(facts["new_pages"])) if facts["new_pages"] else "本周无"),
          "| sitemap | %s | — | — | %s |" % (("已提交 %d 条" % submitted) if sm else "【】", "；".join("%s（错误 %d / 警告 %d）" % (x["path"].split("/")[-1], x["errors"], x["warnings"]) for x in sm[:4]) if sm else "接口未读到 sitemap"),
          "| GSC 验证中的问题 | 【本周无 / 桶名】 | — | — | 【发起日期 + 当前状态；验证发起日必须晚于修复日】 |",
          "| 技术事件 | 【本周无 / 事件】 | — | — | 【证书 / CDN / 服务器 / robots / 301 变更及影响】 |",
          "",
          "> 判断修复是否生效以 GSC「网址检查 → 测试实际网址」实时抓取为准；改过 robots / sitemap 必须先确认 CDN 边缘已更新再发起验证。",
          "", "## 四、上周计划完成情况", "",
          "| 事项 | 具体内容 / 链接 | 结果 | 效果或后续 |", "|---|---|---|---|",
          "| 【】 | 【】 | 【已完成 / 进行中 / 未完成（原因）】 | 【带来的数据变化，或下一步】 |",
          "", "### 4.1 本周发布内容", "",
          "| 标题 | URL | 目标关键词 | 收录 | 首周曝光 / 点击 |", "|---|---|---|---|---|"]
    if facts["new_pages"]:
        for r in facts["new_pages"]:
            L.append("| 【】 | %s | 【】 | 已有曝光（排名 %s） | %s / %s |" % (r["URL"], r["排名"], r["首周曝光"], r["首周点击"]))
    else:
        L.append("| 【本周无】 | | | | |")
    L += ["", "> 上周发布的文章本周回填「首周曝光 / 点击」，让客户看到内容起效的过程，而不是只报「已收录」。",
          "", "## 五、本周计划", "",
          "| 事项 | 具体对象 | 预期产出 | 来源 | 预计完成 |", "|---|---|---|---|---|"]
    for p in text["plans"]:
        L.append("| %s | %s | %s | %s | 【MM.DD】 |" % (p.get("事项", ""), p.get("具体对象", ""), p.get("预期产出", ""), p.get("来源", "")))
    if facts["quick_wins"]:
        L += ["", "11–20 位的词（离首页一步之遥，可作本周计划的候选对象）：", "",
              "| 查询词 | 排名 | 曝光 | 点击 |", "|---|---|---|---|"]
        for r in facts["quick_wins"]:
            L.append("| %s | %s | %s | %s |" % (r["查询词"], r["排名"], r["曝光"], r["点击"]))
    L += ["", "## 六、需客户配合 / 风险提示", "",
          "| 事项 | 需要客户做什么 | 不做的影响 | 截止 |", "|---|---|---|---|",
          "| 【本周无 / 事项】 | 【】 | 【】 | 【】 |", "",
          "---", "",
          "数据口径：流量与排名来自 Google Search Console，与第三方工具存在正常差异；GSC 数据约滞后 2–3 天，本期统计区间为 %s 至 %s（对比 %s 至 %s）。平均排名为曝光加权平均。真实询盘＝经人工判定的有效商业询盘。"
          % (w["start"], w["end"], w["prev_start"], w["prev_end"])]
    if facts["country"] or facts["device"]:
        L += ["", "附：本周点击按国家 / 设备 —— " +
              "、".join("%s %d" % (r["国家"], r["点击"]) for r in facts["country"]) + "；" +
              "、".join("%s %d" % (r["设备"], r["点击"]) for r in facts["device"])]
    return title, "\n".join(L)


def build(site, client_name, end=None, brand=None, use_ai=True, log=None):
    """一条龙:取数 → 判定 → AI 写字 → markdown。返回 (标题, markdown, facts, text, ai_cost)。"""
    log = log or (lambda m: None)
    facts = analyze.weekly(site, end=end, brand=brand, log=log)
    status = analyze.overall_status(facts)
    log("规则判定:整体 %s;各指标 %s;背离 %s" % (status, " / ".join("%s %s" % (s["指标"], s["判定"]) for s in facts["snapshot"]), "、".join(facts["diverge"]) or "无"))
    text, cost = write(facts, status, client_name, use_ai=use_ai, log=log)
    title, md = markdown(facts, text, client_name)
    return title, md, facts, text, cost
