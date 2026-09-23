# -*- coding: utf-8 -*-
"""布词:给每个词一个 URL、一个角色,生成「URL布词视图」。

三段:
  1. crawl_site   抓站点 URL 清单(sitemap 优先)+ 每页 title / H1,按路径猜页面类型
  2. plan_pages   AI 一次调用:给每个现有页定主关键词,给没有承接页的词簇提待建页
  3. assign       AI 分块调用:把其余词分到页、定角色(次词 / 长尾 / FAQ/正文)

AI 的产出一律过代码校验:URL 必须在规划里、主词全表唯一、角色只认那几个。
分不到页的词不删,留在表上标「无法归入任何页面（人工复核）」—— 布词是人的活,
AI 出草稿。
"""
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from . import derive
from ..llm import client as llm

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}
TYPE_ORDER = ["首页", "产品总览", "栏目页", "聚合页", "产品页", "行业页", "指南", "其他"]
ROLES = {"次词", "长尾", "FAQ/正文"}
MAX_PAGES = 200
PLAN_KW = 350          # 给规划那一步看的词数(按优先级 + 月搜排前)
CHUNK = 180            # 分配那一步每次给 AI 的词数


class LayoutError(RuntimeError):
    pass


# ---------------------------------------------------------------- 1. 抓站

def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def guess_type(path):
    """按路径猜页面类型。迅睿 / WordPress 这类站:目录 = 栏目,.html = 内容页。"""
    p = path.strip("/")
    if not p:
        return "首页"
    top = p.split("/")[0]
    leaf = p.endswith(".html") or p.endswith(".htm")
    if top in ("products", "product"):
        if p == top:
            return "产品总览"
        return "产品页" if leaf else "栏目页"
    if top in ("applications", "application", "industries", "industry", "solutions"):
        return "行业页" if leaf else "其他"
    if top in ("news", "blog", "blogs", "guide", "guides", "knowledge", "faq", "resources"):
        return "指南" if leaf else "其他"
    return "其他"


def crawl_site(site, log=None, max_pages=MAX_PAGES):
    """返回 [{url, path, type, title, h1}]。sitemap.xml -> sitemap.txt -> 首页链接。"""
    log = log or (lambda m: None)
    site = site.strip().rstrip("/")
    if not site.startswith("http"):
        site = "https://" + site
    host = urlparse(site).netloc
    urls = []
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        try:
            body = _get(site + path)
        except Exception:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        # sitemap index:再下钻一层
        subs = [u for u in locs if u.endswith(".xml")]
        for sub in subs[:10]:
            try:
                locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", _get(sub))
            except Exception:
                pass
        urls = [u for u in locs if not u.endswith(".xml")]
        if urls:
            log("sitemap 拿到 %d 个 URL" % len(urls))
            break
    if not urls:
        try:
            urls = [u.strip() for u in _get(site + "/sitemap.txt").splitlines() if u.strip().startswith("http")]
            log("sitemap.txt 拿到 %d 个 URL" % len(urls))
        except Exception:
            pass
    if not urls:
        try:
            home = _get(site + "/")
            urls = sorted(set(re.findall(r'href="(https?://%s/[^"#?]+)"' % re.escape(host), home)))
            log("sitemap 没有,从首页链接拿到 %d 个 URL" % len(urls))
        except Exception as e:
            raise LayoutError("抓不到站点结构:%s" % e)
    # 只要本站、非小语种、非文件
    keep = []
    seen = set()
    for u in urls:
        pu = urlparse(u)
        if pu.netloc != host:
            continue
        path = pu.path or "/"
        if re.search(r"^/(de|ru|fr|es|it|pt|ja|ko|ar|zh|cn|tw)(/|$)", path):
            continue
        if re.search(r"\.(jpg|jpeg|png|gif|webp|pdf|xml|txt|css|js)$", path, re.I):
            continue
        if path in seen:
            continue
        seen.add(path)
        keep.append(path)
    keep = keep[:max_pages]
    log("本站页面 %d 个,抓 title / H1…" % len(keep))

    def fetch(path):
        try:
            html = _get(site + path, timeout=15)
        except Exception:
            return {"path": path, "title": "", "h1": ""}
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        h = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S)
        clean = lambda x: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x or "")).strip()
        title = clean(t.group(1) if t else "")
        title = re.split(r"\s[|\-–—]\s", title)[0].strip() if title else ""
        return {"path": path, "title": title[:120], "h1": clean(h.group(1) if h else "")[:120]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        metas = list(ex.map(fetch, keep))
    pages = []
    for m in metas:
        pages.append({"url": m["path"], "path": m["path"], "type": guess_type(m["path"]),
                      "title": m["title"], "h1": m["h1"]})
    got = sum(1 for p in pages if p["title"] or p["h1"])
    log("抓到标题的 %d / %d;类型分布 %s" % (got, len(pages), json.dumps(
        {t: sum(1 for p in pages if p["type"] == t) for t in TYPE_ORDER if any(p["type"] == t for p in pages)},
        ensure_ascii=False)))
    return pages


# ---------------------------------------------------------------- 2. 页面规划

PLAN_SYSTEM = """你是外贸 B2B 网站的 SEO 布词助手。给你:① 站点现有页面清单(URL | 类型 | 标题 | H1 | 候选主词);② 关键词清单(词 | 意图 | 优先级 | 月搜 | 客户级别)。

任务:给每个现有页面定**一个主关键词**;给没有承接页的关键词簇提出待建页。

规则(互旦关键词 SOP V2「一页一主词」):
- 现有页的主词**只能从该页的「候选主词」里选**(候选按与标题 / H1 的相关度排好了);候选都明显不合适就把 main 给空串,不要从别处拿、不要自己编
- 主关键词全表不重复;同一词只归一个 URL
- 首页只吃 manufacturer / supplier 类「品牌 + 品类」词
- 产品总览吃最大的品类词;栏目页吃品类词;产品页吃型号 / 结构 / 材质 / 用途词;行业页吃应用场景词;指南吃信息型词
- 候选里有客户级别 S / A+ / A 的词优先;其次优先级高、月搜高
- 待建页:关键词清单里有明显的簇(横切材质 / 用途 / 信息型问句)却没有承接页时才提,最多 25 个;主词必须是关键词清单里的词;URL 放对应目录:栏目 / 聚合页 /products/<slug>/,产品页 /products/<slug>.html,行业页 /applications/<slug>.html,指南 /news/<slug>.html;slug 用主词的连字符小写
- 页面状态:现有页写「已有」,新页写「待建」

只输出严格 JSON,不要 markdown 标记、不要解释:
{"pages":[{"url":"/products/xxx/","type":"栏目页","status":"已有","main":"主关键词","note":"一句话为什么"}]}"""

HOME_WORDS = {"manufacturer", "manufacturers", "supplier", "suppliers", "factory", "factories", "company", "companies"}
# 行业通用词:候选靠它们重合不算数,页面标题里有更具体的词时必须命中具体词
GENERIC = {"roller", "rollers", "conveyor", "conveyors", "pulley", "pulleys", "idler", "idlers", "drum", "drums", "belt", "belts", "industry", "industries"}


def _fmt_kw(r, market_col):
    lv = r.get("客户级别") or ""
    return "%s | %s | %s | %s%s" % (r["关键词"], r.get("意图", ""), r.get("优先级", ""),
                                     r.get(market_col) or 0, (" | 客户级别 " + lv) if lv else "")


def page_candidates(pages, rows, market_col, n=6):
    """每个现有页按 标题 / H1 / URL 的词面重合算候选主词。

    排序:重合词数 → 客户级别(S / A+ / A 优先)→ 优先级 → 月搜。首页只要
    manufacturer / supplier 类。这是给 AI 的选项,也是它选歪时的回落顺序。
    """
    order = {"P0": 0, "P1": 1, "P2": 2}
    lvl = {"S": 0, "A+": 1, "A": 2, "B": 3, "C": 4}
    cand = [r for r in rows if r.get("优先级") in ("P0", "P1", "P2")]
    toks = [(r, _tokens(r["关键词"])) for r in cand]
    out = {}
    for pg in pages:
        if pg["type"] == "其他":
            continue
        pt = _tokens(pg["title"]) | _tokens(pg["h1"]) | _tokens(pg["url"].replace(".html", "").replace("/", " "))
        pt -= {"products", "product", "news", "applications", "html", "www", "com", "with", "and", "for", "the"}
        specific = pt - GENERIC
        scored = []
        for r, kt in toks:
            ov = len(kt & pt)
            if ov == 0:
                continue
            # 标题里有具体词(pu / coated / warehouse)就必须命中具体词,只靠泛词重合不算
            if specific and not (kt & specific):
                continue
            if pg["type"] == "首页" and not (kt & HOME_WORDS):
                continue
            scored.append((-ov, lvl.get(r.get("客户级别") or "", 9), order.get(r["优先级"], 9),
                           -float(r.get(market_col) or 0), r["关键词"]))
        scored.sort()
        out[pg["url"]] = [x[4] for x in scored[:n]]
    return out


def plan_pages(pages, rows, market_col, extra=None, log=None):
    """AI 一次调用出页面规划。返回 [{url, type, status, main, note}]。"""
    log = log or (lambda m: None)
    cand = [r for r in rows if r.get("优先级") in ("P0", "P1", "P2")]
    order = {"P0": 0, "P1": 1, "P2": 2}
    cand.sort(key=lambda r: (order.get(r["优先级"], 9), -float(r.get(market_col) or 0)))
    cand = cand[:PLAN_KW]
    cands = page_candidates(pages, rows, market_col)
    site_lines = ["%s | %s | %s | %s | 候选主词: %s" % (p["url"], p["type"], p["title"], p["h1"],
                  "; ".join(cands.get(p["url"]) or []) or "(无)")
                  for p in pages if p["type"] != "其他"]
    user = ("【现有页面】(URL | 类型 | 标题 | H1 | 候选主词)\n" + "\n".join(site_lines)
            + "\n\n【关键词】(词 | 意图 | 优先级 | 月搜 | 客户级别;按优先级和月搜排序,共 %d 个,总表另有 %d 个未列)\n" % (len(cand), len(rows) - len(cand))
            + "\n".join(_fmt_kw(r, market_col) for r in cand))
    if extra:
        user += "\n\n【补充说明】\n" + str(extra).strip()
    log("页面规划:%d 个页面(每页 ≤ 6 个候选主词)+ %d 个词,调用 LLM…" % (len(site_lines), len(cand)))
    res = llm.complete(PLAN_SYSTEM, user, skill="layout-plan", stream=False, log=log,
                       max_tokens=12000, thinking=False)
    data = derive._extract_json(res["text"])
    known = {p["url"]: p for p in pages}
    kw_set = {r["关键词"].lower(): r["关键词"] for r in rows}
    ranked = [r["关键词"] for r in cand]

    def nearest(text, used):
        tt = _tokens(text)
        best, score = None, 0
        for kw in ranked:
            if kw.lower() in used:
                continue
            sc = len(tt & _tokens(kw))
            if sc > score:
                best, score = kw, sc
        return best if score >= 2 else None

    def pick(url, used, prefer=None):
        """该页候选里第一个没用过的;prefer 是 AI 选的,在候选里且没用过就依它。"""
        cl = cands.get(url) or []
        if prefer and prefer.lower() in used:
            prefer = None
        if prefer and any(prefer.lower() == c.lower() for c in cl):
            return kw_set.get(prefer.lower())
        for c in cl:
            if c.lower() not in used:
                return c
        return None

    plan, used, bad = [], set(), []
    items = []
    for p in data.get("pages") or []:
        url = str(p.get("url") or "").strip()
        if not url:
            continue
        if not url.startswith("/"):
            url = "/" + url
        items.append((url, p))
    # 现有页先处理、待建页后处理:AI 输出里待建页常排前面,先把好词占了
    items.sort(key=lambda x: 0 if x[0] in known else 1)
    for url, p in items:
        main = str(p.get("main") or "").strip()
        note = str(p.get("note") or "").strip()
        if url in known:
            chosen = pick(url, used, main or None)
            if not chosen:
                # 留在规划里、主词空着:消失的话它的词全变孤词;留着还能接词,第 3 页会提醒人填
                bad.append("现有页候选用尽,主词留空请人工定:%s" % url)
                plan.append({"url": url, "type": known[url]["type"], "status": "已有", "main": "",
                             "note": "候选主词用尽,请人工定主词"})
                continue
            if main and chosen.lower() != main.lower():
                bad.append("现有页主词%s:%s -> 回落到候选 %s(%s)"
                           % ("不在候选" if not any(main.lower() == c.lower() for c in (cands.get(url) or [])) else "重复", main, chosen, url))
            main = chosen
            ptype = known[url]["type"]
            status = "已有"
        else:
            if not main:
                continue
            if main.lower() not in kw_set or main.lower() in used:
                alt = nearest(main, used)
                if not alt:
                    bad.append("待建页主词%s且找不到替代,不建:%s(%s)" % ("不在词表" if main.lower() not in kw_set else "重复", main, url))
                    continue
                bad.append("待建页主词回落:%s -> %s(%s)" % (main, alt, url))
                main = alt
            main = kw_set[main.lower()]
            ptype = str(p.get("type") or "").strip()
            if ptype not in TYPE_ORDER:
                ptype = guess_type(url)
            status = "待建"
        if main:
            used.add(main.lower())
        plan.append({"url": url, "type": ptype, "status": status, "main": main, "note": note})
    # AI 漏掉的现有页:直接用该页第一个没用过的候选
    planned = {p["url"] for p in plan}
    filled = 0
    for pg in pages:
        if pg["type"] == "其他" or pg["url"] in planned:
            continue
        chosen = pick(pg["url"], used)
        if chosen:
            used.add(chosen.lower())
        plan.append({"url": pg["url"], "type": pg["type"], "status": "已有", "main": chosen or "",
                     "note": "AI 漏了这页,按候选回落选的主词,请核" if chosen else "AI 漏了这页且候选用尽,请人工定主词"})
        filled += 1
    if filled:
        bad.append("AI 漏掉 %d 个现有页,已按候选回落补上" % filled)
    for b in bad[:14]:
        log("  [规划校验] " + b)
    n_new = sum(1 for p in plan if p["status"] == "待建")
    log("页面规划:%d 页(已有 %d / 待建 %d),主词全表唯一;约 $%s" % (len(plan), len(plan) - n_new, n_new, res.get("cost")))
    if not plan:
        raise LayoutError("AI 没有产出可用的页面规划。")
    return plan, res


# ---------------------------------------------------------------- 3. 分配

ASSIGN_SYSTEM = """你是外贸 B2B 网站的 SEO 布词助手。给你页面规划(URL | 类型 | 主关键词)和一批关键词(词 | 意图 | 优先级 | 月搜)。
把每个关键词分到**最合适的一个页面**,并定角色:
- 次词:与该页主词同品类、意思相近的词,进 Title 副位 / H2 / 规格表
- 长尾:型号 / 规格 / 修饰词组合,进 H3 / 正文
- FAQ/正文:信息型问句落到产品页 / 栏目页时

规则:主词已经定了,不要再给任何词「主词」角色;信息型词优先分到指南页;品类词分到栏目页 / 聚合页;型号词分到产品页;应用场景词分到行业页。实在分不到任何页的词,url 给空串。

只输出严格 JSON,不要 markdown 标记、不要解释:
{"items":[{"kw":"关键词","url":"/products/xxx/","role":"次词"}]}"""


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", str(s).lower().replace("-", " ")))


def _fallback_page(kw, plan):
    """AI 没给或给错 URL 时,按词面重合度找最近的页;完全不沾边就放弃。"""
    kt = _tokens(kw)
    best, score = None, 0
    for p in plan:
        pt = _tokens(p["main"]) | _tokens(p["url"])
        s = len(kt & pt)
        if s > score:
            best, score = p, s
    return best["url"] if best and score >= 2 else ""


def assign(plan, rows, market_col, log=None, workers=4):
    """AI 分块把词分到页。返回 {关键词: (url, role)}。主词不参与。"""
    log = log or (lambda m: None)
    mains = {p["main"].lower() for p in plan if p["main"]}
    todo = [r for r in rows if r.get("优先级") in ("P0", "P1", "P2") and r["关键词"].lower() not in mains]
    plan_lines = "\n".join("%s | %s | %s" % (p["url"], p["type"], p["main"] or "(主词待定)") for p in plan)
    urls = {p["url"] for p in plan}
    chunks = [todo[i:i + CHUNK] for i in range(0, len(todo), CHUNK)]
    log("分配:%d 个词分 %d 批,每批 ≤ %d,%d 路并行…" % (len(todo), len(chunks), CHUNK, workers))
    total_cost = 0.0

    def run(chunk):
        user = ("【页面规划】(URL | 类型 | 主关键词)\n" + plan_lines
                + "\n\n【关键词】(词 | 意图 | 优先级 | 月搜)\n" + "\n".join(_fmt_kw(r, market_col) for r in chunk))
        res = llm.complete(ASSIGN_SYSTEM, user, skill="layout-assign", stream=False,
                           max_tokens=12000, thinking=False)
        data = derive._extract_json(res["text"])
        return {str(i.get("kw") or "").strip().lower(): (str(i.get("url") or "").strip(), str(i.get("role") or "").strip())
                for i in (data.get("items") or [])}, res.get("cost") or 0

    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got, cost in ex.map(run, chunks):
            out.update(got)
            total_cost += float(cost or 0)
    # ---- 代码校验 ----
    result, fixed, orphan = {}, 0, 0
    for r in todo:
        kw = r["关键词"]
        url, role = out.get(kw.lower(), ("", ""))
        if url and not url.startswith("/"):
            url = "/" + url
        if url not in urls:
            url = _fallback_page(kw, plan)
            fixed += 1
        if role not in ROLES:
            if r.get("意图") == "信息":
                role = "FAQ/正文"
            else:
                role = "次词" if (r.get("优先级") == "P0" or float(r.get(market_col) or 0) >= 100) else "长尾"
        if not url:
            orphan += 1
        result[kw] = (url, role)
    log("分配完成:%d 个词;AI 给的 URL 不在规划里 / 缺失而按词面回落 %d 个;分不到页 %d 个;约 $%.4f"
        % (len(result), fixed, orphan, total_cost))
    return result, total_cost


# ---------------------------------------------------------------- 4. 落表 + 透视

def apply(rows, plan, assigned, market_col, log=None):
    """把布词结果写进总表行(布局角色 / 目标URL / 页面类型 / 页面状态 / 该页主词)。

    变体跟主词走;主词写「主词」;分不到页的写备注不删。
    「长尾降为 P1」在这里执行 —— 口径原文 P0 的括号补充,依赖布局角色。
    """
    log = log or (lambda m: None)
    by_url = {p["url"]: p for p in plan}
    main_of = {p["main"].lower(): p for p in plan if p["main"]}
    by_kw = {r["关键词"].lower(): r for r in rows}
    demoted = orphan = 0
    for r in rows:
        k = r["关键词"].lower()
        if r.get("优先级") == "—":
            continue                       # 变体:下面按主词回填
        if k in main_of:
            p = main_of[k]
            r["布局角色"], r["目标URL"], r["页面类型"], r["页面状态"], r["该页主词"] = "主词", p["url"], p["type"], p["status"], p["main"]
            continue
        url, role = assigned.get(r["关键词"], ("", ""))
        if not url:
            orphan += 1
            r["布局角色"], r["目标URL"] = "", ""
            r["备注"] = ("无法归入任何页面（人工复核）" + ("；" + r["备注"] if r.get("备注") else ""))
            continue
        p = by_url[url]
        r["布局角色"], r["目标URL"], r["页面类型"], r["页面状态"], r["该页主词"] = role, p["url"], p["type"], p["status"], p["main"]
        if role == "长尾" and r.get("优先级") == "P0":
            r["优先级"] = "P1"
            demoted += 1
    # 变体:跟主词(备注里写着「跟随「X」」)
    for r in rows:
        if r.get("优先级") != "—":
            continue
        m = re.search(r"跟随「(.+?)」", str(r.get("备注") or ""))
        lead = by_kw.get(m.group(1).lower()) if m else None
        if lead and lead.get("目标URL"):
            r["目标URL"], r["页面类型"], r["页面状态"], r["该页主词"] = lead["目标URL"], lead["页面类型"], lead["页面状态"], lead["该页主词"]
    log("落表:主词 %d 个,长尾降为 P1 %d 个,分不到页 %d 个(留在表上标备注)" % (len(plan), demoted, orphan))
    return rows


PIVOT_COLUMNS = ["目标URL", "页面类型", "页面状态", "主关键词", "次关键词", "长尾 / FAQ 词",
                 "关键词数(不含变体)", "{市场}月搜合计(不含变体)", "金矿词数", "原始词数", "备注"]


def pivot(rows, plan, market_col):
    """「URL布词视图」:每个 URL 一行。对齐飞书总表第 3 页的 11 列。"""
    groups = {p["url"]: {"p": p, "rows": []} for p in plan}
    for r in rows:
        u = r.get("目标URL")
        if u in groups:
            groups[u]["rows"].append(r)
    out = []
    for u, g in groups.items():
        p, rs = g["p"], g["rows"]
        nonvar = [r for r in rs if r.get("优先级") != "—"]
        sec = [r["关键词"] for r in nonvar if r.get("布局角色") == "次词"]
        tail = [r["关键词"] for r in nonvar if r.get("布局角色") in ("长尾", "FAQ/正文")]
        out.append({
            "目标URL": u, "页面类型": p["type"], "页面状态": p["status"], "主关键词": p["main"],
            "次关键词": " | ".join(sec), "长尾 / FAQ 词": " | ".join(tail),
            "关键词数(不含变体)": len(nonvar),
            "{市场}月搜合计(不含变体)": int(sum(float(r.get(market_col) or 0) for r in nonvar)),
            "金矿词数": sum(1 for r in nonvar if r.get("金矿")),
            "原始词数": sum(1 for r in nonvar if r.get("词源") == "原始词"),
            "备注": p.get("note", ""),
        })
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    out.sort(key=lambda x: (order.get(x["页面类型"], 9), -x["{市场}月搜合计(不含变体)"]))
    return out


def run(site, rows, market_col, extra=None, log=None):
    """整条布词流程。返回 (rows, plan, pivot_rows, info)。"""
    log = log or (lambda m: None)
    pages = crawl_site(site, log=log)
    plan, res1 = plan_pages(pages, rows, market_col, extra=extra, log=log)
    assigned, cost2 = assign(plan, rows, market_col, log=log)
    rows = apply(rows, plan, assigned, market_col, log=log)
    pv = pivot(rows, plan, market_col)
    info = {"pages_crawled": len(pages), "plan": plan,
            "existing": sum(1 for p in plan if p["status"] == "已有"),
            "new": sum(1 for p in plan if p["status"] == "待建"),
            "orphans": sum(1 for r in rows if r.get("优先级") != "—" and not r.get("目标URL")),
            "cost": round(float(res1.get("cost") or 0) + cost2, 4),
            "assigned": assigned}
    return rows, plan, pv, info
