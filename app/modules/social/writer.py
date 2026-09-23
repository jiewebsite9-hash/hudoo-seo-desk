# -*- coding: utf-8 -*-
"""AI 写作层 —— **只写文字,一个数字都不许它自己产**。

公司模板的红线原文:「全部数字来自后台截图/导出,AI 只润色文字不生成数字」。
这条不能靠「在 prompt 里叮嘱模型」来保证,必须**机械地拦住**:

  1. facts 里的所有数值先摊平成一张白名单(含 1,234 / 1234 / 12.5% 等写法变体)
  2. AI 产出的每段文字扫一遍数字
  3. 有辨识度的数字(≥50、带小数、带千分位、带百分号)不在白名单里 -> 判违规
  4. 把违规项回喂给模型重写一次;还不过就**丢弃该段,回退到规则模板句**

第 4 步是关键:宁可输出一句干巴巴的模板话,也不能把编造的数字发给客户。

小整数(<50)不拦 —— 「三段式」「前 3 秒」「≤30 秒」这类修辞和建议里天然会出现,
拦了模型就没法说人话了;而真正会造成事故的指标值都是有辨识度的大数或带小数的比率。
"""
import json
import re

# 需要 AI 写的段落。key 会出现在 prose 字典里,render 按 key 取用。
SECTIONS = {
    "conclusion": "第一节「本周结论」的 3 条要点",
    "best_content": "第 4.2 节「表现最好的内容」的有效原因分析",
    "todo": "第 4.3 节「待优化点」",
    "audience": "第五节「访客与受众」的解读段落",
    "review": "第六节「本周复盘」的三段式（走向与归因 / 仍稳定的亮点 / 调整动作）",
    "appendix_a": "附录 A 的解读段落（发群用，一段话）",
    "appendix_b": "附录 B 的群内同步话术",
    # 内容类型是**分类不是数字**,交给 AI 安全,而且模板第二节的 40/30/20/10 配比要用它
    "content_types": "每条贴文的内容类型分类，取值只能是 企业介绍 / 产品 / 活动 / 资质 "
                     "四选一。输出成 JSON 对象：键用该贴文的 url（没有 url 就用标题前 34 个字符），"
                     "值是分类。只分类 posts_in_period 和 posts_next_period 里的贴文。",
}

# 小数点后必须有数字,否则 markdown 有序列表的「1.」「2.」会被当成数字抓出来,
# 实测会把整段复盘误判成违规。
NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?%?")
SAFE_BELOW = 50          # 小于它的纯整数不拦


class GuardFailed(RuntimeError):
    def __init__(self, violations):
        super(GuardFailed, self).__init__("数字守卫未通过")
        self.violations = violations


# ------------------------------------------------------------------ 白名单

def whitelist(facts):
    """facts 里所有数值 -> 允许出现的字符串集合。"""
    allowed = set()

    def add(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            f = float(v)
            for s in ("%d" % int(f) if f == int(f) else None,
                      "{:,}".format(int(f)) if f == int(f) else None,
                      ("%g" % f), ("%.1f" % f), ("%.2f" % f)):
                if s:
                    allowed.add(s)
                    allowed.add(s + "%")      # 1.9% 和 1.90% 两种写法都放行
            # **占比类的事实存的是分数**(性别 0.81、地区 0.249、LinkedIn 的 CTR 0.8327),
            # 而写周报时正确的写法是 81% / 24.9% / 83.3%。这是同一个事实的另一种表达,
            # 必须放行,否则模型写对了反而被拦。
            if 0 < abs(f) < 1:
                p = f * 100
                for s in ("%g" % p, "%.1f" % p, "%.2f" % p,
                          "%d" % int(round(p)) if abs(p - round(p)) < 1e-9 else None):
                    if s:
                        allowed.add(s)
                        allowed.add(s + "%")
        elif isinstance(v, str):
            for m in NUM.findall(v):
                allowed.add(m)

    def walk(o):
        if isinstance(o, dict):
            for x in o.values():
                walk(x)
        elif isinstance(o, (list, tuple)):
            for x in o:
                walk(x)
        else:
            add(o)

    walk(facts)
    return allowed


def _norm(tok):
    return tok.replace(",", "")


# 章节号不是数据。「第 4.3 节」「见 4.1」这类引用会被当成小数抓出来,
# 实测拦下过一整段合格的文字。扫描前先把它们抹掉。
SECTION_REF = re.compile(r"第\s*\d+(?:\.\d+)?\s*[节章]|(?<=[见按详])\s*\d\.\d")


def _strip_refs(text):
    return SECTION_REF.sub(" ", text or "")


def check_text(text, allowed):
    """返回违规的数字列表。"""
    bad = []
    norm = {_norm(a) for a in allowed}
    for tok in NUM.findall(_strip_refs(text)):
        if tok in allowed or _norm(tok) in norm:
            continue
        core = tok.rstrip("%").replace(",", "")
        try:
            val = abs(float(core))
        except ValueError:
            continue
        distinctive = (val >= SAFE_BELOW or "." in core or "," in tok or tok.endswith("%"))
        if distinctive:
            bad.append(tok)
    return sorted(set(bad))


def guard(prose, facts):
    allowed = whitelist(facts)
    out = {}
    for k, v in (prose or {}).items():
        if k == "content_types":
            continue          # 纯分类,不含指标数字,不必过数字守卫
        bad = check_text(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False),
                         allowed)
        if bad:
            out[k] = bad
    return out


# ------------------------------------------------------------------ 提示词

SYSTEM = """你是互旦（HUDOO）的社媒运营分析师，正在为客户写一份对外的周报。

【最高优先级的硬规则 —— 违反即作废】
1. 你**只能使用 facts 里出现过的数字**，原样照抄。不得四舍五入、不得换算单位、
   不得把两个数字相加或相除得出新数字、不得估算。需要某个数字而 facts 里没有，
   就换一种不需要数字的说法。
2. 没有上期数据时，**绝对不能写「较上期上升/回落 X%」**。可以写期内对比
   （某天 vs 某天）或与历史存量内容对比，但必须说清是跟什么比。
3. 不向客户承诺曝光、粉丝、询盘的目标或数值。
4. 数据下降不甩锅、不回避。回落周必须三段式：先客观归因 → 再给仍稳定的亮点 →
   最后给明确动作，三段缺一不可。「有所回落」这类措辞全文不超过 2 次。
5. 跨期比较只看互动率/点击率这类比率，不用绝对值下结论。

【写作要求】
- 对象是客户（对外），口吻专业、具体、不浮夸，不用营销腔。
- 归因要落到可验证的原因（主题贴合痛点/形式/首图/发布时段/平台推荐/节点效应），
  不写「持续发力」「加大力度」这种空话。
- 第六节的调整动作必须具体到形态、主题或时段，因为它要和下周排期一一对应。
- 用简体中文。数字后面带单位或指标名，别裸奔。

【输出格式】
只输出一个 JSON 对象，不要任何解释或代码块标记。字段如下（都是字符串，可含换行和
markdown 列表）：
""" + "\n".join("  %-14s %s" % (k, v) for k, v in SECTIONS.items())
# 注意:上面这段提示词里有大量字面百分号(40 / 30 / 20 / 10 配比、1%~3%),
# 所以必须用拼接,不能用 % 格式化 —— 否则 % 会被当成格式符解析并报错。


def build_prompt(facts):
    return ("下面是这份周报的全部事实数据（facts）。请严格依据它写作。\n\n"
            + json.dumps(facts, ensure_ascii=False, indent=2))


def _parse(text):
    """模型偶尔会套 ```json 代码块,或在 JSON 前后加话,这里都兜住。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except ValueError:
            pass
    raise ValueError("模型没有返回可解析的 JSON（前 200 字：%s）" % t[:200])


FALLBACK = {
    "conclusion": "（本节待补：AI 生成的内容未通过数字校验，已回退。请人工按第三节数据补写结论。）",
    "best_content": "（本节待补：请人工从第 4.1 节挑出表现最好的内容并写明有效原因。）",
    "todo": "（本节待补：请人工列出本期待优化点，每条对应第七节排期里的一条动作。）",
    "audience": "（本节待补：请人工解读第五节的受众数据。）",
    "review": "（本节待补：请人工按三段式写复盘——走向与归因 / 仍稳定的亮点 / 调整动作。）",
    "appendix_a": "（本节待补：请人工撰写发群用的解读段落。）",
    "appendix_b": "（本节待补：请人工撰写群内同步话术。）",
}


def write(facts, log=None, retries=1):
    """调 LLM 写文字段落,过数字守卫。返回 (prose, 报告)。

    报告里带模型、耗时、花费、每轮的违规项 —— 出问题时能直接看出是哪一步。
    """
    from app.modules.llm import client as llm
    log = log or (lambda *a: None)

    report = {"rounds": [], "fallback": [], "cost": 0.0, "model": None}
    user = build_prompt(facts)
    extra = ""
    prose = {}

    for attempt in range(retries + 1):
        r = llm.complete(SYSTEM + extra, user, skill="social_weekly", stream=False, log=log)
        report["model"] = r.get("model")
        report["cost"] += r.get("cost") or 0
        try:
            cand = _parse(r["text"])
        except ValueError as e:
            report["rounds"].append({"attempt": attempt + 1, "error": str(e)})
            log("第 %d 轮：%s" % (attempt + 1, e))
            continue

        bad = guard(cand, facts)
        report["rounds"].append({"attempt": attempt + 1,
                                 "violations": bad,
                                 "sections": sorted(cand.keys())})
        prose = cand
        if not bad:
            log("数字守卫通过（第 %d 轮）" % (attempt + 1))
            break
        log("数字守卫拦下 %d 段：%s" % (len(bad), bad))
        extra = ("\n\n【上一轮被拦下的问题 —— 必须改正】\n"
                 "以下数字没有出现在 facts 里，是你自己算出来或编出来的，禁止使用。"
                 "请改写这些段落，只用 facts 里出现过的数字，或换成不需要数字的说法：\n"
                 + "\n".join("  %s: %s" % (k, "、".join(v)) for k, v in bad.items()))

    # 仍不合格的段落,直接换成模板占位句 —— 宁可空着也不发编造的数字
    final_bad = guard(prose, facts)
    for k in final_bad:
        prose[k] = FALLBACK.get(k, "（本节待补。）")
        report["fallback"].append(k)
    for k in SECTIONS:
        if k == "content_types":
            prose.setdefault(k, {})
            continue
        if not str(prose.get(k) or "").strip():
            prose[k] = FALLBACK.get(k, "（本节待补。）")
            if k not in report["fallback"]:
                report["fallback"].append(k)
    if report["fallback"]:
        log("以下段落回退成人工待补：%s" % "、".join(report["fallback"]))
    return prose, report
