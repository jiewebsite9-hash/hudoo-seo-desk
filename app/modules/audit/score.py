# -*- coding: utf-8 -*-
"""步骤 4：On-page 6 维 30 项逐页打分

阈值走 cfg["th"]（config 的 audit.thresholds 覆盖 DEFAULT_TH）—— 团队内部标准放 config.local.yaml，不进仓库。
关键词密度不作硬性目标，不计分。
主关键词从 Title/H1 剥掉品牌词推断；当天目录放 kw_map.json（{url: 主关键词}）则优先用它。
"""
import collections, re

from app.modules.audit.common import jdump, jload

GENERIC = {"click here", "read more", "more", "learn more", "view more", "details", "view details", "here", "link",
           "this", "see more", "more+", "view", "check now", "know more", "inquire", "inquire now", "submit",
           "mehr", "mehr lesen", "weiterlesen", "подробнее", "читать далее", "детали", "en savoir plus", "leer más", "ver más"}
COMPANY = {"co", "ltd", "inc", "llc", "gmbh", "company", "limited", "group", "corp", "corporation", "industrial",
           "technology", "technologies", "tech", "factory", "manufacturer", "china", "chinese", "official", "website",
           "home", "homepage"}
STOP = set("""a an the and or of for with in on to from by at is are be as your our we you it this that these those
more all new best top can will has have need used use using about into than then per mm
und der die das fur für mit von den im auf ist sind sie wir ihre
и в на для с по из от не что это как все или
le la les des et pour avec en du de el los las y para con""".split())
DEFAULT_TH = {"title_min": 50, "title_max": 60, "desc_min": 120, "desc_max": 160,
              "img_kb_max": 200, "img_kb_ideal": 100, "good": 24, "ok": 15,
              "lcp_ms": 2500, "cls": 0.1, "ttfb_ms": 800}
WORD = re.compile(r"[0-9A-Za-zÀ-ÿА-Яа-яЁё\-]+")
CTA = re.compile(r"contact|quote|inquir|enquir|request|buy|get |order|call|email|shop|download|free sample|"
                 r"kontakt|anfrage|angebot|связ|запрос|заказ|contacte|cotiz|devis", re.I)


def kw_of(d, brand):
    src = re.split(r"\s[|\-–—:]\s|[|_]", d["title"])[0].strip()
    if len(src) < 3:
        src = d["h1"][0] if d["h1"] else ""

    def toks(s):
        return [t for t in WORD.findall(s.lower()) if t not in brand and t not in COMPANY and t not in STOP and len(t) > 1]
    t = toks(src) or toks(d["h1"][0] if d["h1"] else "")
    return " ".join(t[:4]), set(t[:4])


def score_page(u, d, ctx, kw_override=None):
    if kw_override:
        kw, kwt = kw_override, {t for t in WORD.findall(kw_override.lower()) if t not in STOP}
    else:
        kw, kwt = kw_of(d, ctx["brand"])
    title, desc, h1s = d["title"], d["desc"], d["h1"]
    tl, dl = len(title), len(desc)
    first100 = " ".join(d["body_head200"].split()[:100]).lower()
    slug = d["path"].lower()
    content_imgs = [i for i in d["imgs"] if not i["tpl"] and i["src"] and not re.search(r"\.svg(\?|$)", i["src"], re.I)]
    body_links = [l for l in d["links"] if not l["tpl"]]
    inl = [l for l in body_links if l["internal"]]
    exl = [l for l in body_links if not l["internal"] and not re.match(r"^\s*(tel:|mailto:|javascript|#)", l["href"], re.I)]
    words = max(d["words"], 1)
    med = ctx["median_words"].get((d["lang"], d["type"]), 300)
    it = []

    def add(dim, name, ok, val=""):
        it.append((dim, name, 1 if ok else 0, str(val)))

    th = ctx["th"]
    tl_low = title.lower()
    add("1.关键词优化", "Title Tag 含主关键词", bool(kwt) and all(t in tl_low for t in list(kwt)[:2]), kw or "推断失败")
    add("1.关键词优化", "主关键词在 Title 前 30 字符", bool(kwt) and any(t in tl_low[:30] for t in kwt), title[:30])
    add("1.关键词优化", "H1 含主关键词", bool(h1s) and bool(kwt) and any(t in h1s[0].lower() for t in kwt), h1s[0][:40] if h1s else "无 H1")
    add("1.关键词优化", "前 100 字含主关键词", bool(kwt) and any(t in first100 for t in kwt), "")
    add("1.关键词优化", "URL slug 含关键词", bool(kwt) and any(t in slug for t in kwt), slug[:50])
    add("1.关键词优化", "副关键词/语义相关词自然分布", len(d["body_h2"]) >= 2 and any(t in first100 for t in kwt),
        "正文 H2 %d 个" % len(d["body_h2"]))

    add("2.Meta标签", "Title %d-%d 字符" % (th["title_min"], th["title_max"]), th["title_min"] <= tl <= th["title_max"], "%d 字符" % tl)
    add("2.Meta标签", "Description %d-%d 字符" % (th["desc_min"], th["desc_max"]), th["desc_min"] <= dl <= th["desc_max"], "%d 字符" % dl)
    add("2.Meta标签", "Description 含 CTA", bool(CTA.search(desc)), "")
    add("2.Meta标签", "无重复 Title/Description（整站唯一）", not d["dup_title"] and not d["dup_desc"],
        ("Title重复 " if d["dup_title"] else "") + ("Desc重复" if d["dup_desc"] else ""))

    add("3.内容质量", "主题覆盖度 ≥ 同类页中位", words >= med, "%d 词（同类中位 %d）" % (words, med))
    add("3.内容质量", "包含原创图表/图片", len(content_imgs) >= 1, "正文内容图 %d 张" % len(content_imgs))
    add("3.内容质量", "包含数据/统计（有引用来源）",
        bool(re.search(r"\d+\s*(mm|cm|kg|ton|rpm|°|%|φ|Ф|kw|v\b|hz|mpa|bar|psi)", d["body_head200"], re.I)) and len(exl) > 0,
        "外链来源 %d" % len(exl))
    add("3.内容质量", "段落长度适中（≤4句/段）[代理指标]", words < 1200, "%d 词" % words)
    need_vb = max(1, words // 300)
    add("3.内容质量", "有视觉断点（每 300 字至少 1 个）", (len(d["body_h2"]) + len(content_imgs)) >= need_vb,
        "H2+图 %d / 需 %d" % (len(d["body_h2"]) + len(content_imgs), need_vb))
    add("3.内容质量", "内容无明显过时信息 [需人工复核]", True, "脚本不判，默认通过")

    add("4.标题层级", "仅 1 个 H1", len(h1s) == 1, "%d 个" % len(h1s))
    lv = d["h_levels"]
    jump = any(lv[i + 1] - lv[i] > 1 for i in range(len(lv) - 1))
    add("4.标题层级", "H2/H3 层级不跳级", not jump, "有跳级" if jump else "正常")
    add("4.标题层级", "至少 1 个 H2 含副关键词", bool(kwt) and any(any(t in h.lower() for t in kwt) for h in d["body_h2"]),
        "；".join(d["body_h2"][:3])[:50] or "正文无 H2")
    need_h2 = max(1, words // 400)
    add("4.标题层级", "H 标签总数合理（每 300-500 字 1 个 H2）", len(d["body_h2"]) >= need_h2, "%d 个 / 需 %d" % (len(d["body_h2"]), need_h2))

    need_out = max(2, int(words / 1000 * 2))
    add("5.链接", "内链符合密度标准（出链 & 入链≥2）", len(inl) >= need_out and d["inlinks"] >= 2,
        "正文出链 %d（需 %d）／入链 %d" % (len(inl), need_out, d["inlinks"]))
    gen = [l for l in inl if l["anchor"].strip().lower() in GENERIC or not l["anchor"].strip()]
    add("5.链接", "内链锚文本描述性（非“点击这里”）", not gen, "通用/空锚文本 %d 条" % len(gen))
    add("5.链接", "外链 2-3 个权威来源", len(exl) >= 2, "%d 个" % len(exl))
    add("5.链接", "无死链/404", u not in ctx["dead"], "死链 %d 条" % ctx["dead"].get(u, 0))
    add("5.链接", "外链 nofollow 设置合理 [需人工复核]", True, "脚本不判，默认通过")

    noalt = [i for i in content_imgs if not (i["alt"] or "").strip()]
    add("6.技术元素", "所有图片有 ALT 标签", not noalt, "缺 %d / 共 %d 张" % (len(noalt), len(content_imgs)))
    big = [i for i in content_imgs if ctx["img_size"].get(i["src"], 0) > th["img_kb_max"] * 1024]
    add("6.技术元素", "图片已压缩（建议<%dKB，硬上限 %dKB）" % (th["img_kb_ideal"], th["img_kb_max"]), not big,
        "超 %dKB：%d 张" % (th["img_kb_max"], len(big)))
    add("6.技术元素", "有 Schema 标记", bool(d["schema"]), ",".join(d["schema"]) or "无")
    add("6.技术元素", "有 Canonical 标签", bool(d["canonical"]), d["canonical"][-50:] or "无")
    add("6.技术元素", "移动端友好", d["viewport"], "viewport 已声明" if d["viewport"] else "无 viewport")
    return kw, sum(x[2] for x in it), it


def thresholds(cfg):
    return dict(DEFAULT_TH, **(cfg.get("th") or {}))


def rating(s, th=DEFAULT_TH):
    return "良好" if s >= th["good"] else ("需优化" if s >= th["ok"] else "全面整改")


def run(cfg, log=print):
    out = cfg["out"]
    P = jload(out, "pages.json")
    odd = jload(out, "odd_links.json", {})
    img_size = {i[0]: i[2] for i in jload(out, "images_head.json", []) if i[1] == 200}
    kw_map = jload(out, "kw_map.json", {})  # 可选：{url: 主关键词}
    dead = collections.Counter(s for srcs in odd.values() for s in srcs)
    wt = collections.defaultdict(list)
    for d in P.values():
        wt[(d["lang"], d["type"])].append(d["words"])
    median = {k: sorted(v)[len(v) // 2] for k, v in wt.items()}
    brand = set(WORD.findall(cfg["bare"].split(".")[0].lower())) | {b.lower() for b in cfg.get("brand_words", [])}
    th = thresholds(cfg)
    ctx = {"median_words": median, "dead": dead, "img_size": img_size, "brand": brand, "th": th}

    res = {}
    for u, d in P.items():
        kw, total, items = score_page(u, d, ctx, kw_map.get(u))
        res[u] = {"lang": d["lang"], "type": d["type"], "path": d["path"], "kw": kw, "score": total,
                  "rating": rating(total, th), "items": items, "title": d["title"], "words": d["words"]}
    jdump(out, "scores.json", res)

    by_lang = collections.defaultdict(list)
    for v in res.values():
        by_lang[v["lang"]].append(v["score"])
    for lang, v in sorted(by_lang.items()):
        log("  %s：%d 页，平均 %.1f/30（%s）" % (lang, len(v), sum(v) / len(v), rating(sum(v) / len(v), th)))
    main = [v for v in res.values() if v["lang"] == cfg["default_lang"]] or list(res.values())
    fail = collections.Counter((dim, name) for v in main for dim, name, s, _ in v["items"] if s == 0)
    log("主语种失分 Top10：")
    for (dim, name), c in fail.most_common(10):
        log("  %-8s %-30s %d/%d" % (dim, name, c, len(main)))
