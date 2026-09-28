# -*- coding: utf-8 -*-
"""行业速通:给客户官网 / 公司名 / 行业名,按 industry-quick-study skill 的流程出一份 10 模块行业速通文档。

skill 原本是给 Claude Code 里的 agent 用的:agent 自己上网抓官网、搜索、填模板。
这里把「抓」和「搜」两步换成程序做(官网抓取 + DataForSEO 搜索),把材料一次性喂给 LLM,
让它按 skill 的 SKILL.md + template.md 写文档。LLM 拿不到网络,材料里没有的只能标
[推断] / [待验证] —— 这正好和 skill「不虚构」的硬规则一致。

流程(对应 skill 的 Step 1-5):
  1 读 skill 文件      skills/industry-quick-study/SKILL.md + references/template.md
  2 一手信息           sitemap / 首页 → 挑首页、About、产品、行业应用、认证、博客等 ≤14 页,抓正文
  3 二手信息           LLM 先从官网材料里提炼行业词/下游/公司名(小调用),按模板的搜索表跑 ≤12 次搜索,
                       顺带抓 2-3 家竞品首页的 title / H1 / 导航(需求侧用词)
  4-5 写文档           LLM 按模板写;程序核对 10 个模块 / 速览 / 待验证清单 / 营销切入点是否齐

花钱的两处:DataForSEO 搜索(regular live,$0.002/次,一份约 $0.02)和 LLM 两次调用。
"""
import base64
import datetime as dt
import html as htmlmod
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from app import config
from app.modules.keywords import layout
from app.modules.keywords.derive import _extract_json
from app.modules.llm import client as llm

SKILL = "industry-quick-study"
SERP_LIVE = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"
SERP_COST = 0.002
MAX_SEARCH = 12
PAGE_CHARS = 5000          # 每页正文上限
TOTAL_CHARS = 60000        # 官网材料总上限(DeepSeek 128k 上下文,留够写文档的余量)
MAX_PAGES = 14
# 目录站 / 平台 / 社媒:不算竞品,也不当官网
SKIP_HOSTS = ("alibaba", "made-in-china", "globalsources", "amazon", "wikipedia", "linkedin", "facebook",
              "youtube", "indiamart", "thomasnet", "ebay", "aliexpress", "reddit", "quora", "pinterest",
              "instagram", "tiktok", "twitter", "x.com", "tradeindia", "ec21", "yellowpages", "crunchbase",
              "bloomberg", "zoominfo", "dnb.com", "glassdoor", "google.", "baidu", "1688", "taobao",
              # 行业报告 / 资讯聚合站:是搜索证据,但不是竞品,也不当官网
              "cbinsights", "fortunebusinessinsights", "grandviewresearch", "marketsandmarkets", "mordorintelligence",
              "statista", "researchandmarkets", "imarcgroup", "precedenceresearch", "businesswire", "prnewswire",
              "globenewswire", "medium.com", "issuu", "scribd", "slideshare", "yumpu", "manta.com", "kompass",
              "europages", "exportersindia", "go4worldbusiness", "tradekey", "ecplaza", "sourcify", "volza",
              "importgenius", "panjiva", "zauba", "seair", "datantify", "buyerzone", "capterra", "g2.com")

# 页面角色:按路径挑,每类最多几页
ROLES = [
    ("首页", r"^/$", 1),
    ("About", r"about|company|profile|who-we-are|our-story|history", 1),
    ("产品/服务", r"product|service|solution|capabilit|equipment|technolog|machin|manufactur|what-we-do", 5),
    ("行业/应用", r"industr|application|market|sector|case|project|customer", 3),
    ("认证/质量", r"certif|quality|iso|compliance|standard", 1),
    ("下单/FAQ", r"faq|how-to-order|order|quote|rfq|process|lead-time|shipping|payment", 1),
    ("博客", r"blog|news|article|insight|knowledge|resource", 3),
]

REQUIRED = ["30 秒速览", "模块 1", "模块 2", "模块 3", "模块 4", "模块 5", "模块 6", "模块 7", "模块 8",
            "模块 9", "模块 10", "待验证清单", "营销切入点", "置信度图例"]


class StudyError(RuntimeError):
    pass


# ---------------------------------------------------------------- skill 文件

def skill_files():
    d = config.skills_dir() / SKILL
    sk, tp = d / "SKILL.md", d / "references" / "template.md"
    if not sk.exists() or not tp.exists():
        raise StudyError("skills 目录里没有 %s(要有 SKILL.md 和 references/template.md)。"
                         "在「设置」页导入 skill 包后再试。" % SKILL)
    return sk.read_text(encoding="utf-8"), tp.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 搜索

def _serp(query, market, log):
    """DataForSEO regular live:一次一条,返回前 10 条自然结果 [{title,url,host,snippet}] 和花费。"""
    login, pwd = config.get("dataforseo.login", ""), config.get("dataforseo.password", "")
    if not login or not pwd:
        raise StudyError("没配 DataForSEO 账号密码,做不了搜索。")
    from app.modules.ranks.engine import GL_TO_LOCATION
    body = [{"keyword": query, "location_name": GL_TO_LOCATION.get(market, "United States"),
             "language_code": "en", "depth": 10}]
    req = urllib.request.Request(SERP_LIVE, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Basic " + base64.b64encode(("%s:%s" % (login, pwd)).encode()).decode(),
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise StudyError("搜索失败:HTTP %s(账号密码不对或余额不足)" % e.code)
    except Exception as e:
        raise StudyError("搜索失败:%s" % e)
    if d.get("status_code") != 20000:
        raise StudyError("搜索失败:%s" % d.get("status_message"))
    task = (d.get("tasks") or [{}])[0]
    cost = float(task.get("cost") or 0)
    if task.get("status_code") != 20000:
        log("  搜索「%s」没结果:%s" % (query, task.get("status_message")))
        return [], cost
    items = ((task.get("result") or [{}])[0].get("items") or [])
    out = []
    for it in items:
        if it.get("type") != "organic" or not it.get("url"):
            continue
        out.append({"title": it.get("title") or "", "url": it["url"],
                    "host": (it.get("domain") or urllib.parse.urlparse(it["url"]).netloc).lower().replace("www.", ""),
                    "snippet": (it.get("description") or "")[:300]})
    return out, cost


def _is_skip(host):
    h = (host or "").lower()
    return any(s in h for s in SKIP_HOSTS)


# ---------------------------------------------------------------- 抓页面

def _html_text(raw):
    """HTML -> {title, desc, h1, h2, nav, text}。不上 BeautifulSoup,正则够用,而且只要「能读」不要「精确」。"""
    s = re.sub(r"(?is)<(script|style|noscript|svg|iframe|template)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    t = lambda x: htmlmod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x))).strip()
    title = t((re.search(r"(?is)<title[^>]*>(.*?)</title>", s) or [None, ""])[1])
    m = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)', s)
    desc = htmlmod.unescape(m.group(1)).strip() if m else ""
    h1 = [t(x) for x in re.findall(r"(?is)<h1[^>]*>(.*?)</h1>", s)][:3]
    h2 = [t(x) for x in re.findall(r"(?is)<h2[^>]*>(.*?)</h2>", s)][:12]
    navs = re.findall(r"(?is)<nav[^>]*>(.*?)</nav>", s) or re.findall(r"(?is)<header[^>]*>(.*?)</header>", s)
    nav = []
    for n in navs[:2]:
        nav += [t(x) for x in re.findall(r"(?is)<a[^>]*>(.*?)</a>", n)]
    nav = [x for x in dict.fromkeys(nav) if 1 < len(x) < 40][:40]
    body = re.search(r"(?is)<body[^>]*>(.*)</body>", s)
    body = body.group(1) if body else s
    body = re.sub(r"(?is)<(nav|header|footer)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|br|section|article)>|<br\s*/?>", "\n", body)
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return {"title": title, "desc": desc, "h1": [x for x in h1 if x], "h2": [x for x in h2 if x], "nav": nav, "text": text}


def _fetch(url):
    try:
        return _html_text(layout._get(url, timeout=15))
    except Exception:
        return None


def pick_pages(pages):
    """crawl_site 的结果 -> [(角色, path)],按 ROLES 配额挑,最多 MAX_PAGES。"""
    chosen, used = [], set()
    for role, pat, n in ROLES:
        k = 0
        for p in pages:
            path = p["path"]
            if path in used or k >= n:
                continue
            hit = (path == "/") if pat == r"^/$" else bool(re.search(pat, path, re.I))
            if hit:
                chosen.append((role, path))
                used.add(path)
                k += 1
    # 配额没用满就按顺序补几页(小站常常路径不规范)
    for p in pages:
        if len(chosen) >= MAX_PAGES:
            break
        if p["path"] not in used:
            chosen.append(("其他", p["path"]))
            used.add(p["path"])
    return chosen[:MAX_PAGES]


def collect_site(site, log, max_pages=MAX_PAGES, label="客户官网"):
    """抓一个站:sitemap/首页发现 → 挑页 → 抓正文。返回 {site, host, pages, chars, n_found, thin, js}。"""
    site = site.strip().rstrip("/")
    if not site.startswith("http"):
        site = "https://" + site
    try:
        found = layout.crawl_site(site, log=lambda m: log("  " + m), max_pages=150)
    except Exception as e:
        raise StudyError("抓不到 %s:%s" % (site, e))
    chosen = pick_pages(found)[:max_pages]
    log("%s:发现 %d 页,抓 %d 页正文…" % (label, len(found), len(chosen)))
    with ThreadPoolExecutor(max_workers=4) as ex:
        got = list(ex.map(lambda rp: (rp[0], rp[1], _fetch(site + rp[1])), chosen))
    pages, total = [], 0
    for role, path, d in got:
        if not d:
            continue
        text = d["text"][:PAGE_CHARS]
        if total + len(text) > TOTAL_CHARS:
            text = text[:max(0, TOTAL_CHARS - total)]
        total += len(text)
        pages.append(dict(d, role=role, url=site + path, text=text))
    js = len(pages) > 0 and sum(len(p["text"]) for p in pages) < 800 * max(1, len(pages)) / 4
    thin = len(found) < 5 or total < 3000
    log("%s:拿到 %d 页,正文 %d 字%s%s" % (label, len(pages), total,
                                        ";疑似 JS 渲染,正文很少" if js else "", ";信息量薄" if thin else ""))
    return {"site": site, "host": urllib.parse.urlparse(site).netloc.replace("www.", ""),
            "pages": pages, "chars": total, "n_found": len(found), "thin": thin, "js": js}


def _site_block(info, tag):
    L = ["## %s:%s(%s)" % (tag, info["host"], "共 %d 页" % info["n_found"])]
    for p in info["pages"]:
        L.append("### [%s] %s" % (p["role"], p["url"]))
        if p["title"]:
            L.append("title: " + p["title"])
        if p["desc"]:
            L.append("description: " + p["desc"])
        if p["h1"]:
            L.append("H1: " + " | ".join(p["h1"]))
        if p["h2"]:
            L.append("H2: " + " | ".join(p["h2"]))
        if p["nav"] and p["role"] == "首页":
            L.append("导航: " + " | ".join(p["nav"]))
        if p["text"]:
            L.append("正文:\n" + p["text"])
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- 第 1 次 LLM:提炼行业坐标

PROFILE_SYS = """你是 B2B 行业研究员。下面是一家公司官网的抓取材料。只根据材料,输出一个 JSON 对象,不要别的字:
{
 "company": "公司名(官网写法)",
 "industry_en": "行业英文名,用海外买家搜索时会用的说法,2-5 个词,如 conveyor roller manufacturing",
 "industry_zh": "行业中文名,4-10 字",
 "core_terms": ["3-6 个核心产品/服务英文词,买家搜索用的自然说法"],
 "downstream": ["2-4 个主要下游行业英文名"],
 "regions": ["官网提到的目标市场/国家,没有就空数组"],
 "competitor_hints": ["官网提到的同行/对标品牌,没有就空数组"]
}
材料里没有的不要编;行业名要外行也能懂。"""


def profile(site_info, log):
    mat = _site_block(site_info, "客户官网")[:40000]
    r = llm.complete(PROFILE_SYS, mat, skill=SKILL, stream=False, log=log, max_tokens=800, thinking=False)
    d = _extract_json(r["text"]) or {}
    if not d.get("industry_en"):
        raise StudyError("LLM 没从官网材料里提炼出行业名,材料可能太少。")
    for k in ("core_terms", "downstream", "regions", "competitor_hints"):
        d[k] = [str(x) for x in (d.get(k) or []) if x][:6]
    d["company"] = d.get("company") or site_info["host"]
    d["industry_zh"] = d.get("industry_zh") or d["industry_en"]
    log("行业坐标:%s / %s;公司 %s;下游 %s" % (d["industry_zh"], d["industry_en"], d["company"], "、".join(d["downstream"]) or "—"))
    return d, r["cost"]


# ---------------------------------------------------------------- 搜索计划(对应模板 Step 3 的搜索表)

def plan_queries(prof, market):
    ind, comp = prof["industry_en"], prof["company"]
    ds = prof["downstream"][:2]
    q = []
    for d in ds:
        q.append(("模块4 下游要求", "%s %s requirements supplier qualification standards" % (d, ind)))
    q += [
        ("模块5 用户画像", "who buys %s procurement buyer" % ind),
        ("模块5 目标市场", "%s top importing countries market by region" % ind),
        ("模块6 上游", "%s supply chain raw materials cost" % ind),
        ("模块7 竞争", "%s competitors alternatives" % comp),
        ("模块7 海外本土玩家", "%s manufacturers in USA Germany" % ind),
        ("模块7 中国出海同行", "%s china supplier manufacturer" % ind),
        ("模块8 交易结构", "%s MOQ pricing how to order RFQ lead time" % ind),
        ("模块10 信息源", "%s trade show association industry magazine" % ind),
    ]
    return q[:MAX_SEARCH]


def research(site_info, prof, market, log):
    """跑搜索 + 竞品首页速览。返回 (evidence_markdown, 花费, 统计)。"""
    queries = plan_queries(prof, market)
    log("按模板搜索表跑 %d 次搜索(DataForSEO,约 $%.3f)…" % (len(queries), len(queries) * SERP_COST))
    cost, blocks, comp_hosts = 0.0, [], []
    my_host = site_info["host"] if site_info else ""
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(lambda mq: (mq[0], mq[1]) + _serp(mq[1], market, log), queries))
    for mod, q, rows, c in results:
        cost += c
        blocks.append("## 搜索「%s」(%s)" % (q, mod))
        for i, r in enumerate(rows[:8], 1):
            blocks.append("%d. %s — %s\n   %s" % (i, r["title"], r["url"], r["snippet"]))
        blocks.append("")
        if mod in ("模块7 海外本土玩家", "模块7 中国出海同行"):
            for r in rows:
                if r["host"] != my_host and not _is_skip(r["host"]) and r["host"] not in comp_hosts:
                    comp_hosts.append(r["host"])
        log("  %s:%d 条" % (mod, len(rows)))
    comp_hosts = comp_hosts[:3]
    comps = []
    if comp_hosts:
        log("抓 %d 家竞品首页的 title / H1 / 导航:%s" % (len(comp_hosts), "、".join(comp_hosts)))
        with ThreadPoolExecutor(max_workers=3) as ex:
            got = list(ex.map(lambda h: (h, _fetch("https://" + h + "/")), comp_hosts))
        for h, d in got:
            if not d:
                continue
            comps.append(h)
            blocks.append("## 竞品官网速览:%s" % h)
            blocks.append("title: %s\nH1: %s\n导航: %s\n首页摘录: %s\n" % (
                d["title"], " | ".join(d["h1"]), " | ".join(d["nav"]), d["text"][:1200]))
    return "\n".join(blocks), cost, {"searches": len(queries), "competitors": comps, "comp_hosts": comp_hosts}


# ---------------------------------------------------------------- 第 2 次 LLM:写文档

WRITE_RULES = """
# 本次执行的附加规则(程序化环境)
- 你现在就是这个 skill 的执行者。上面 skill 里「抓官网」「搜索」两步已经由程序做完,材料全部在用户消息里;你没有网络,**不要假装搜索**。
- 材料里没有的信息,一律标 [推断] 或 [待验证],禁止编造市场规模、营收、增长率等数字。
- 一手信息(客户官网材料)才能标 [官网];搜索结果里来自竞品官网、行业协会、标准文档的才能标 [行业];搜索结果里的目录站/平台文案不算可靠来源。
- 输出**只有最终的 markdown 文档**,从「# 行业速通:」这一行开始,不要前言、不要结尾说明、不要代码块围栏。
- 严格按模板的结构和顺序:文档头(含置信度图例表)→ 30 秒速览 → Part A 模块 1-4 → Part B 模块 5-8 → Part C 模块 9-10 → 【待验证清单】→【营销切入点】。标题写法固定为「## 模块 N:xxx」。
- 中文写作,行业术语与缩写保留英文并在**首次出现处**(包括 30 秒速览里)加 3-8 字中文括注,如 BHS(机场行李系统)。
- 篇幅硬上限:正文汉字 2500-4000 字。这是「速通」,宁可表格化、短句化,也不要展开成报告;超出就删解释性文字,保留事实与标记。
- 研究日期用材料里给的日期。
"""


def write_doc(skill_md, template_md, material, log):
    system = skill_md + "\n\n---\n\n# references/template.md\n\n" + template_md + "\n\n---\n" + WRITE_RULES
    r = llm.complete(system, material, skill=SKILL, stream=True, log=log, max_tokens=12000, thinking=False)
    md = (r["text"] or "").strip()
    md = re.sub(r"^```(?:markdown|md)?\s*", "", md)
    md = re.sub(r"\s*```$", "", md)
    i = md.find("# 行业速通")
    if i > 0:
        md = md[i:]
    return md, r["cost"]


def check_doc(md):
    return [k for k in REQUIRED if k not in md]


# ---------------------------------------------------------------- 入口

def resolve(inp, kind, market, log):
    """输入 -> (客户官网 或 None, 锚点站列表, 说明)。kind: auto / site / company / industry。"""
    s = (inp or "").strip()
    if not s:
        raise StudyError("先给一个客户官网、公司名或行业名。")
    looks_url = ("." in s and " " not in s and not re.search(r"[一-鿿]", s))
    if kind == "site" or (kind == "auto" and looks_url):
        return s, [], ""
    cost = 0.0
    if kind == "industry":
        rows, c = _serp("%s manufacturer supplier" % s, market, log)
        anchors = [r["host"] for r in rows if not _is_skip(r["host"])][:2]
        if not anchors:
            raise StudyError("搜「%s」没找到可做锚点的同行官网。" % s)
        log("行业名输入:以 %s 为行业锚点(skill 规则:行业名先找 2-3 家典型公司)" % "、".join(anchors))
        return None, anchors, "输入为行业名,无客户官网;信息主源为同行官网 %s" % "、".join(anchors)
    rows, c = _serp("%s official website" % s, market, log)
    cand = [r for r in rows if not _is_skip(r["host"])]
    if not cand:
        raise StudyError("搜「%s official website」没找到像官网的结果;直接给官网 URL 更稳。" % s)
    log("公司名输入:以搜索第 1 条 %s 为官网 —— 请核对是不是同名公司" % cand[0]["host"])
    return "https://" + cand[0]["host"], [], "官网由搜索「%s official website」确定,存在同名公司风险,请核对" % s


def build(inp, kind="auto", market="us", log=None):
    """跑完整流程。返回 {title, filename, md, prof, stats, cost, missing}。"""
    log = log or (lambda m: None)
    skill_md, template_md = skill_files()
    site, anchors, note = resolve(inp, kind, market, log)
    cost = 0.0
    today = dt.date.today().isoformat()

    site_info = None
    if site:
        site_info = collect_site(site, log)
    anchor_infos = []
    anchor_mode = bool(anchors) or (site_info is not None and site_info["thin"])
    if site_info is not None and site_info["thin"] and not anchors:
        log("官网信息量薄(skill 规则:切换行业锚点模式,以海外头部同行官网为信息主源)")

    # 行业坐标:有官网从官网提;只有行业名就用输入本身
    if site_info is not None:
        prof, c = profile(site_info, log)
        cost += c
    else:
        prof = {"company": "", "industry_en": inp.strip(), "industry_zh": inp.strip(),
                "core_terms": [], "downstream": [], "regions": [], "competitor_hints": []}

    # 锚点站:行业名输入给的,或官网太薄时从「海外本土玩家」搜索里挑
    if anchor_mode and not anchors:
        rows, c = _serp("%s manufacturers" % prof["industry_en"], market, log)
        cost += c
        anchors = [r["host"] for r in rows if not _is_skip(r["host"]) and r["host"] != site_info["host"]][:2]
    for h in anchors:
        try:
            anchor_infos.append(collect_site("https://" + h, log, max_pages=8, label="锚点站 " + h))
        except StudyError as e:
            log("  %s" % e)
    if site_info is None and anchor_infos and not prof["downstream"]:
        # 没有客户官网时,从锚点站材料里提炼行业坐标,搜索才有靠谱的词
        p2, c = profile(anchor_infos[0], log)
        cost += c
        prof.update({k: v for k, v in p2.items() if k != "company"})
        prof["company"] = "(无客户官网)"

    evidence, c, rs = research(site_info, prof, market, log)
    cost += c

    head = ["# 研究对象",
            "- 输入:%s(类型:%s)" % (inp, kind),
            "- 客户官网:%s" % (site_info["site"] if site_info else "无"),
            "- 研究日期:%s;目标市场:%s" % (today, market.upper()),
            "- 行业坐标(程序初判,可修正):%s / %s;核心词 %s;下游 %s" % (
                prof["industry_zh"], prof["industry_en"], "、".join(prof["core_terms"]) or "—", "、".join(prof["downstream"]) or "—"),
            "- 抓取情况:%s" % ("官网 %d 页、正文 %d 字%s" % (len(site_info["pages"]), site_info["chars"],
                                                    "(疑似 JS 渲染,一手信息受限,文档头要声明)" if site_info["js"] else "") if site_info else "无官网"),
            "- 行业锚点模式:%s" % ("是 —— 信息主源为同行官网 %s,非客户官网,文档头整体置信度要注明" % "、".join(a["host"] for a in anchor_infos)
                                  if anchor_infos else "否"),
            "- " + note if note else "",
            "", "# 一手信息(客户官网)—— 只有这里的内容能标 [官网]", ""]
    parts = ["\n".join(x for x in head if x is not None)]
    if site_info:
        parts.append(_site_block(site_info, "客户官网"))
    else:
        parts.append("(无客户官网)")
    if anchor_infos:
        parts.append("\n# 行业锚点站(同行官网)—— 可标 [行业]\n")
        parts += [_site_block(a, "锚点站") for a in anchor_infos]
    parts.append("\n# 二手信息(搜索结果 + 竞品首页速览)—— 竞品官网/协会/标准可标 [行业],其余谨慎\n")
    parts.append(evidence)
    material = "\n".join(parts)
    log("材料合计 %d 字,交给 LLM 按模板写文档…" % len(material))

    md, c = write_doc(skill_md, template_md, material, log)
    cost += c
    missing = check_doc(md)
    words = len(re.sub(r"\s", "", md))
    if missing:
        log("[当心] 文档缺少:%s —— 已按原样保留,请人工补" % "、".join(missing))
    log("文档 %d 字,%s" % (words, "结构齐全" if not missing else "结构不全"))

    comp = prof["company"] or (site_info["host"] if site_info else "")
    safe = lambda s: re.sub(r"[\\/:*?\"<>|\s]+", "-", s).strip("-")[:40]
    filename = "行业速通_%s_%s_%s.md" % (safe(prof["industry_zh"]), safe(site_info["host"] if site_info else comp) or "行业", today.replace("-", ""))
    title = "行业速通：%s（%s）" % (prof["industry_zh"], comp or inp)
    stats = {"行业": prof["industry_zh"], "公司": comp, "官网页数": len(site_info["pages"]) if site_info else 0,
             "搜索次数": rs["searches"], "竞品速览": len(rs["competitors"]), "字数": words,
             "锚点模式": "是" if anchor_infos else "否", "花费(USD)": round(cost, 4)}
    return {"title": title, "filename": filename, "md": md, "prof": prof, "stats": stats,
            "cost": round(cost, 4), "missing": missing, "site": site_info["site"] if site_info else None}
