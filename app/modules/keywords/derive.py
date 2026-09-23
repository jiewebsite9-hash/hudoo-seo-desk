# -*- coding: utf-8 -*-
"""从客户资料产出三份清单(调一次 LLM)。

和「学清单」是一对:
  learn.py   从**做过的项目**统计学 —— 纯规则,零 AI,但要先有标注数据
  derive.py  从**客户资料**生成    —— 调一次 LLM,新项目开工时用

两者产出同一种格式,存进同一个清单库。之后筛词全程零 AI。

**这里 LLM 的输出一律当作「草稿」,不当作结论。** 拿到之后:
  1. 结构化解析(要求 JSON),解析不了就报错,不猜
  2. 给了样本词的话,当场算出每条模式会杀掉哪些词 —— 让人肉眼验证再入库
  3. 永远不自动存库
"""
import json
import re

from . import sop
from ..llm import client as llm

SYSTEM = """你是外贸 SEO 关键词筛选助手。根据客户资料,产出三份清单,用于自动筛掉
不该做的关键词。

输出**严格的 JSON**,不要有任何其他文字,结构如下:

{
  "exclude": [{"pattern": "...", "reason": "...", "hard": true}],
  "mixed":   [{"pattern": "...", "reason": "..."}],
  "strategy":[{"keyword": "...", "reason": "..."}],
  "core":    ["...", "..."]
}

各字段含义:

- exclude 剔除清单:命中的词直接移出词表。对应这几类
  ① 泛词(单独成词、没有行业限定)
  ② 跨行业混杂(同一个词在别的行业里意思完全不同)
  ③ 系统级/整机词(客户只做零部件时)
  ④ 相邻但不做的品类
  ⑤ 客户明确不生产的产品线
  ⑥ 品牌词、电商平台词
- mixed 混杂泛词清单:命中的词**不剔除,但优先级最高只给 P1**。
  指那些意图混杂、既可能是客户的产品也可能不是的词。
- strategy 策略词:客户产品线里**确实有、但搜索量可能很低**的词。
  这类词要保留并人工提权,靠搜索量筛会被误杀。
- core 产品核心词:客户产品的核心名词(英文,3-8 个)。
  用作豁免层 —— 命中 exclude 但含 core 词的词会被放行。

pattern 的写法:
- 不带星号 = 整词匹配,只命中这个词组本身。例:`roller manufacturer`
- `abc*` = 以 abc 开头
- `*abc*` = 包含 abc

## 最重要的两条要求

**一、必须泛化,不许逐词列举。**
exclude 最多 40 条。每一条都应当覆盖**一类词**,而不是一个词。
反例(错):
  machinery manufacturer / machinery industry / machinery company / machinery products
正解:一条 `*machinery*` 就够了。
如果你发现自己在写好几条只差一两个字的模式,就说明该合并成一条带星号的。
给的候选词样本是用来**帮你归纳规律**的,不是让你把它们抄一遍。

**二、exclude 的 hard 标志要分清楚。**
`hard: true` = **客户明确不生产/不做的品类**。这类词无条件剔掉,豁免层对它无效。
`hard: false` = 跨行业混杂、相邻品类、泛词。这类可以被 core 豁免救回。

为什么要分:客户不做电动滚筒,所以有 `*motorized*`;但 `motorized roller` 含
core 里的 `roller`,如果不标 hard 就会被豁免层错误地救回来 —— 而它恰恰是
客户压根不做的东西。反过来 `*conveyor belt*` 要标 hard: false,因为
`conveyor belt rollers`(输送带用的滚筒)是客户产品,必须能被救回。

判断口径:资料里写了「不生产」「不做」「未确认生产」的 -> hard: true;
「泛词」「其他行业」「相邻品类」-> hard: false。

**三、core 必须是单个英文名词,不能是词组。**
正解:`roller` / `pulley` / `drum` / `idler`
反例(错):`conveyor roller` / `roller conveyor` / `steel conveyor roller`
理由:core 是豁免层 —— 命中 exclude 但含 core 词的词会被放行。
写成词组的话几乎豁免不了任何东西,这一层就白做了。
举例:客户不做输送带,所以有 `*conveyor belt*`;但 `conveyor belt rollers`
是"输送带用的滚筒",属于客户产品,要靠 core 里的 `roller` 把它救回来。

宁可漏剔,不可误杀。拿不准的词放 mixed,不要放 exclude。

reason 用中文,一句话说清为什么。
只输出 JSON,不要 markdown 代码块标记,不要解释。"""


class DeriveError(RuntimeError):
    pass


def _extract_json(text):
    """LLM 常把 JSON 包在 ```json 块里,也可能前后带话。尽量捞出来。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            pass
    raise DeriveError("LLM 没有返回可解析的 JSON。原样前 300 字:\n" + t[:300])


def _clean(rows, key):
    out, seen = [], set()
    for r in rows or []:
        if isinstance(r, str):
            r = {key: r, "reason": ""}
        v = re.sub(r"\s+", " ", str((r or {}).get(key) or "").strip())
        if not v or v.lower() in seen:
            continue
        seen.add(v.lower())
        item = {key: v, "reason": str(r.get("reason") or "").strip()}
        if "hard" in (r or {}):
            item["hard"] = bool(r.get("hard"))
        out.append(item)
    return out


def derive(material, sample_words=None, extra=None, job=None):
    """跑一次。返回 {exclude, mixed, strategy, core, preview, usage}。"""
    log = job.log if job else (lambda m: None)
    material = (material or "").strip()
    if len(material) < 30:
        raise DeriveError("客户资料太短了(少于 30 字),这样产不出有用的清单。"
                          "把启动会纪要、产品手册、产品线说明贴进来。")

    user = "【客户资料】\n" + material
    if extra:
        user += "\n\n【补充说明】\n" + str(extra).strip()
    sample = [w for w in (sample_words or []) if str(w).strip()][:300]
    if sample:
        user += ("\n\n【候选词样本(仅供参考,帮助你判断该剔什么)】\n"
                 + "\n".join(sample))
        log("带了 %d 个候选词样本给模型参考" % len(sample))

    log("资料 %d 字,调用 LLM…" % len(material))
    # 这是结构化抽取,不是推理题 —— 关掉深思并给足输出额度。
    # 开着深思时实测会把 16000 输出额度全烧在推理上,content 返回空串。
    r = llm.complete(SYSTEM, user, skill="derive-lists", stream=False, log=log,
                     max_tokens=12000, thinking=False)
    data = _extract_json(r["text"])

    res = {
        "exclude": _clean(data.get("exclude"), "pattern"),
        "mixed": _clean(data.get("mixed"), "pattern"),
        "strategy": _clean(data.get("strategy"), "keyword"),
        "core": [str(c).strip() for c in (data.get("core") or []) if str(c).strip()][:12],
        "usage": r["usage"], "cost": r["cost"], "model": r["model"],
    }
    log("产出:剔除 %d 条 / 混杂 %d 条 / 策略词 %d 个 / 核心词 %d 个"
        % (len(res["exclude"]), len(res["mixed"]), len(res["strategy"]), len(res["core"])))

    # ---- 代码层兜底:prompt 写得再清楚,模型也不保证听话 ----
    res["warnings"] = []
    if len(res["exclude"]) > 60:
        res["warnings"].append(
            "剔除清单 %d 条,超出预期(应 ≤40)。多半是在逐词列举而不是归纳成模式,"
            "已截断到前 60 条,建议看一眼再入库。" % len(res["exclude"]))
        res["exclude"] = res["exclude"][:60]
    phrase_core = [c for c in res["core"] if " " in c]
    if phrase_core and len(phrase_core) >= len(res["core"]) / 2:
        res["warnings"].append(
            "核心词大多是词组(%s…)而不是单个名词。**豁免层基本会失效** —— "
            "它靠 roller / pulley 这种短名词把误伤的词救回来,词组几乎救不到东西。"
            % "、".join(phrase_core[:3]))
    # 混杂清单里的「裸核心词通配」必须拿掉:*roller* 进了 mixed = 几乎所有产品词
    # 都被压到 P1,整张表的 P0 全没了。实测模型会把 core 原样再抄一遍进 mixed。
    core_l = {c.lower() for c in res["core"]}
    bad = [m for m in res["mixed"]
           if m["pattern"].replace("*", "").strip().lower() in core_l]
    if bad:
        res["mixed"] = [m for m in res["mixed"] if m not in bad]
        res["warnings"].append(
            "混杂清单里有 %d 条是裸核心词的通配(%s),会把几乎所有产品词压到 P1,已自动拿掉。"
            % (len(bad), "、".join(b["pattern"] for b in bad[:3])))
    for w in res["warnings"]:
        log("[当心] " + w)

    # ---- 当场验证:每条模式会杀掉样本里的哪些词 ----
    # **LLM 的输出是草稿不是结论。** 没有这一步,用户没法判断该不该信。
    if sample:
        res["preview"] = dry_run(res["exclude"], res["mixed"], res["core"], sample)
        p = res["preview"]
        log("对 %d 个样本词试跑:会剔掉 %d 个,其中 %d 个被核心词豁免救回"
            % (len(sample), p["cut"], p["saved"]))
        if p["cut"] > len(sample) * 0.6:
            res["warnings"].append("剔除比例超过 60%,清单可能过宽 —— 逐条看下再入库。")
            log("[当心] 剔除比例超过 60%,清单可能过宽 —— 逐条看下再入库。")
        # 「每条只命中一个词」= 在背样本而不是归纳规律
        singles = sum(1 for r in p["rows"] if r["命中"] <= 1)
        if p["rows"] and singles >= len(p["rows"]) * 0.7:
            res["warnings"].append(
                "%d/%d 条模式只命中 1 个样本词 —— 模型多半在逐词列举而不是归纳规律,"
                "这种清单换个项目就没用了。建议重跑一次。" % (singles, len(p["rows"])))
            log("[当心] " + res["warnings"][-1])
    else:
        res["preview"] = None
        log("没给候选词样本,跳过试跑 —— 建议补一批样本词再跑一次,好判断清单准不准。")
    return res


def dry_run(exclude, mixed, core, words):
    """拿样本词试跑清单,返回每条模式命中了什么 + 总体影响。"""
    core_m = sop.make_mixed_matcher(["*%s*" % c for c in core]) if core else (lambda w: False)
    rows, cut_set, saved = [], set(), 0
    for e in exclude:
        m = sop.make_mixed_matcher([e["pattern"]])
        hit = [w for w in words if m(w)]
        # hard = 客户明确不做的品类,豁免层对它无效。
        # 否则 motorized roller 会因为含 roller 被错误救回 —— 而客户压根不生产电动滚筒。
        hard = bool(e.get("hard"))
        kept = [] if hard else [w for w in hit if core_m(w)]
        killed = [w for w in hit if w not in kept]
        cut_set.update(killed)
        saved += len(kept)
        rows.append({"模式": e["pattern"], "硬剔": "是" if hard else "",
                     "命中": len(hit), "实剔": len(killed),
                     "被豁免": len(kept), "理由": e.get("reason", ""),
                     "样例": " / ".join(killed[:3]) or (" / ".join(kept[:2]) + "(全被豁免)" if kept else "—")})
    mx = sop.make_mixed_matcher([m["pattern"] for m in mixed]) if mixed else (lambda w: False)
    return {"rows": rows, "cut": len(cut_set), "saved": saved,
            "mixed_hit": sum(1 for w in words if mx(w)),
            "total": len(words),
            "survivors": [w for w in words if w not in cut_set][:50]}


def to_list_payload(res, name, note=""):
    """转成「学清单」那套的存库格式,两条路进同一个库。"""
    rows = [{"模式": e["pattern"], "剔除中命中": "", "误杀保留词": "",
             "样例": e.get("reason", "")} for e in res["exclude"]]
    metrics = {"来源": "客户资料(LLM)", "模型": res.get("model"),
               "剔除条数": len(res["exclude"]), "混杂条数": len(res["mixed"]),
               "策略词": len(res["strategy"])}
    core = [{"词": c, "保留词中出现": ""} for c in res["core"]]
    return rows, metrics, core, note
