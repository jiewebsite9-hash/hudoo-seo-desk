# -*- coding: utf-8 -*-
"""规则引擎:逐页「本轮问题」+ 站级 P0/P1/P2 问题清单 + 与上一轮核对

定级口径:P0 = 阻断爬取/收录;P1 = 明显失分;P2 = 优化项。
这里出的是**初稿**:每条都带实测证据和影响页数,但"要不要做、先做哪条"仍需审计负责人复核。
"""
import collections, json, os, re, statistics
from urllib.parse import urlparse

from app.modules.audit.common import jload, jdump
from app.modules.audit.crawl import make_norm
from app.modules.audit.score import thresholds

TYPE_ZH = {"home": "首页"}
# CMS 常识性"不算重复入口"的路径
SHADOW_IGNORE = {"wordpress": {"/feed/", "/wp-json/", "/wp-login.php", "/?s=test"}}
CMS_HINT = {
    "xunrui": {"sitemap": "迅睿 Sitemap 模块 / Seokit 插件", "tdk": "迅睿后台 SEO 字段(或 Seokit TDK 规则)",
               "schema": "Seokit Schema 模块或模板 head 注入", "redirect": "Seokit 301 规则(入库后重编译缓存)或 nginx"},
    "wordpress": {"sitemap": "Yoast / Rank Math 的站点地图设置", "tdk": "Yoast / Rank Math 的 SEO 标题与描述",
                  "schema": "Yoast / Rank Math 的 Schema 设置", "redirect": "Redirection 插件或 nginx"},
    "shopify": {"sitemap": "Shopify 自动生成(检查是否被隐藏)", "tdk": "后台各页 Search engine listing / 主题 meta-tags",
                "schema": "主题 JSON-LD 片段", "redirect": "后台 URL Redirects"},
}


def hint(cfg, k):
    return CMS_HINT.get(cfg.get("cms") or "", {}).get(k, "CMS 后台 / 模板 / 服务器配置")


def label(d):
    return TYPE_ZH.get(d["type"], d["type"])


def is_noindex(d):
    return "noindex" in (d["robots_meta"] + " " + d["x_robots"]).lower()


def canon_state(d):
    c = d["canonical"]
    if not c:
        return "缺"
    if not c.startswith("http"):
        return "相对路径"
    return "Y" if c.rstrip("/") == d["url"].rstrip("/") else "指向他页"


def content_imgs(d):
    return [i for i in d["imgs"] if not i["tpl"] and i["src"] and not re.search(r"\.svg(\?|$)", i["src"], re.I)]


def context(cfg):
    out = cfg["out"]
    P = jload(out, "pages.json", {})
    S = jload(out, "scores.json", {})
    odd = jload(out, "odd_links.json", {})
    imgs = {i[0]: i[2] for i in jload(out, "images_head.json", []) if i[1] == 200}
    dead = collections.Counter(s for v in odd.values() for s in v)
    wt = collections.defaultdict(list)
    for d in P.values():
        wt[(d["lang"], d["type"])].append(d["words"])
    med = {k: statistics.median(v) for k, v in wt.items()}
    return {"P": P, "S": S, "img_size": imgs, "dead": dead, "median": med, "th": thresholds(cfg),
            "multi_lang": bool(cfg.get("langs"))}


def page_flags(d, ctx):
    th, f = ctx["th"], []
    if is_noindex(d):
        f.append("noindex")
    cs = canon_state(d)
    if cs != "Y":
        f.append("canonical" + cs)
    tl, dl = len(d["title"]), len(d["desc"])
    if not tl:
        f.append("缺Title")
    elif tl > th["title_max"]:
        f.append("Title %d字符>%d" % (tl, th["title_max"]))
    elif tl < th["title_min"]:
        f.append("Title %d字符<%d" % (tl, th["title_min"]))
    if d["dup_title"]:
        f.append("Title重复")
    if not dl:
        f.append("缺Meta描述")
    elif not th["desc_min"] <= dl <= th["desc_max"]:
        f.append("Meta %d字符" % dl)
    if d["dup_desc"]:
        f.append("Meta重复")
    if not d["h1"]:
        f.append("无H1")
    elif len(d["h1"]) > 1:
        f.append("%d个H1" % len(d["h1"]))
    if not d["schema"]:
        f.append("无结构化数据")
    ci = content_imgs(d)
    na = sum(1 for i in ci if not (i["alt"] or "").strip())
    if na:
        f.append("%d张图空alt" % na)
    big = sum(1 for i in ci if ctx["img_size"].get(i["src"], 0) > th["img_kb_max"] * 1024)
    if big:
        f.append("%d张图>%dKB" % (big, th["img_kb_max"]))
    if not d["in_sitemap"] and not is_noindex(d):
        f.append("不在sitemap")
    if d.get("all_inlinks", 1) == 0 and d["type"] != "home":
        f.append("全站0入链(孤岛)")
    if d["depth"] > 3:
        f.append("点击深度%d" % d["depth"])
    if ctx["dead"].get(d["url"]):
        f.append("含%d条死链" % ctx["dead"][d["url"]])
    m = ctx["median"].get((d["lang"], d["type"]), 0)
    if d["words"] < 300 and d["words"] < m * 0.5:
        f.append("正文偏薄(%d词,同类中位%d)" % (d["words"], m))
    if ctx["multi_lang"] and not d["hreflang"]:
        f.append("缺hreflang")
    if d["mixed"]:
        f.append("含http资源")
    if not d["og"].get("image"):
        f.append("无og:image")
    return f


def detail_rows(cfg, ctx):
    """在线表格「逐页明细」:一页一行,问题多的排前面"""
    S = ctx["S"]
    rows = []
    for u, d in ctx["P"].items():
        ci = content_imgs(d)
        fl = page_flags(d, ctx)
        rows.append([label(d), d["path"], d["lang"], d["status"], d["title"], len(d["title"]), len(d["desc"]),
                     len(d["h1"]), " / ".join(d["h1"])[:120], canon_state(d), " ".join(sorted(set(d["schema"]))),
                     "%d/%d" % (sum(1 for i in ci if not (i["alt"] or "").strip()), len(ci)),
                     d.get("all_inlinks", ""), d["inlinks"], d["words"], (S.get(u) or {}).get("score", ""),
                     "Y" if d["in_sitemap"] else "N", "；".join(fl)])
    order = {"首页": 0}
    rows.sort(key=lambda r: (order.get(r[0], 1), -len(r[-1].split("；")) if r[-1] else 0, r[2], r[1]))
    return ["类型", "路径", "语种", "状态码", "当前Title", "Title字符", "Meta字符", "H1数", "H1", "Canonical正确",
            "Schema类型", "正文图空alt/总数", "全站入链数", "正文入链数", "正文词数", "On-page得分", "在sitemap", "本轮问题"], rows


# ------------------------------------------------------------------ 站级问题

def site_issues(cfg, ctx):
    out, th, P = cfg["out"], ctx["th"], ctx["P"]
    probe = jload(out, "probe.json", {})
    lh = jload(out, "lighthouse.json", {})
    rows = [json.loads(l) for l in open(os.path.join(out, "crawl.jsonl"), encoding="utf-8")]
    sm_files = jload(out, "sitemap_files.json", [])
    norm = make_norm(cfg)
    sm = {norm(u) or u for u in jload(out, "sitemap_urls.json", [])}
    by_url = {r["url"]: r for r in rows}
    issues = []

    def add(key, level, dim, title, evidence, action, pages=(), unit="页"):
        pages = list(dict.fromkeys(pages))
        issues.append({"key": key, "level": level, "dim": dim, "title": title, "evidence": evidence,
                       "action": action, "count": len(pages), "examples": pages[:5], "unit": unit})

    def pages_where(fn):
        return [d["url"] for d in P.values() if fn(d)]

    n = len(P)
    # ---- 爬取性
    rb = probe.get("robots", {})
    if rb.get("status") != 200:
        add("robots_missing", "P1", "爬取性", "robots.txt 不可访问", "状态 %s" % rb.get("status"),
            "放置 robots.txt,至少声明 Sitemap 地址")
    else:
        txt = rb.get("text", "")
        blocks = re.split(r"(?im)^\s*user-agent:", txt)
        for b in blocks:
            if re.match(r"\s*\*", b) and re.search(r"(?im)^\s*disallow:\s*/\s*$", b):
                add("robots_block_all", "P0", "爬取性", "robots.txt 对所有爬虫 Disallow: /",
                    "User-agent: * 段含 Disallow: /", "立即删除该行(常见于测试环境上线后忘改)")
        if not re.search(r"(?im)^\s*sitemap:", txt):
            add("robots_no_sitemap", "P2", "爬取性", "robots.txt 未声明 Sitemap", "无 Sitemap: 行",
                "robots.txt 末尾加 Sitemap: https://%s/sitemap.xml" % cfg["host"])
    good_sm = [f for f in sm_files if f.get("status") == 200 and f.get("locs")]
    if not good_sm:
        add("sitemap_missing", "P0", "爬取性", "没有可用的 XML Sitemap",
            "robots 声明及 /sitemap.xml、/sitemap_index.xml、/wp-sitemap.xml 均无有效条目",
            "在 %s 开启并提交 GSC" % hint(cfg, "sitemap"))
    else:
        lang_pages = collections.Counter(d["lang"] for d in P.values() if not is_noindex(d))
        lang_sm = collections.Counter(d["lang"] for d in P.values() if d["in_sitemap"])
        for lang, c in lang_pages.items():
            if c >= 5 and lang_sm.get(lang, 0) == 0:
                add("sitemap_lang_" + lang, "P0", "爬取性", "%s 语种 %d 个页面完全不在 sitemap" % (lang, c),
                    "sitemap 共 %d 条,%s 语种 0 条" % (len(sm), lang),
                    "%s 输出多语言(或按语种各出一份 + sitemap index)" % hint(cfg, "sitemap"),
                    pages_where(lambda d, l=lang: d["lang"] == l))
        miss = pages_where(lambda d: not d["in_sitemap"] and not is_noindex(d)
                           and not any(i["key"] == "sitemap_lang_" + d["lang"] for i in issues))
        if len(miss) > max(3, n * 0.05):
            add("sitemap_gap", "P1", "爬取性", "%d 个可索引页面不在 sitemap" % len(miss),
                "站内链接能抓到,但 sitemap 未收录", "核对 %s 的模块/栏目开关" % hint(cfg, "sitemap"), miss)
        bad = [u for u in sm if u in by_url and (by_url[u]["status"] != 200 or by_url[u].get("chain"))]
        if bad:
            add("sitemap_dirty", "P1", "爬取性", "sitemap 含 %d 条非 200 / 跳转 URL" % len(bad),
                "sitemap 只应列最终 200 的规范 URL", "从 sitemap 剔除或改成跳转后的目标 URL", bad)
    # ---- 索引性
    ni_sm = pages_where(lambda d: is_noindex(d) and d["in_sitemap"])
    if ni_sm:
        add("noindex_in_sitemap", "P0", "索引性", "%d 个 noindex 页面在 sitemap 里" % len(ni_sm),
            "信号互相矛盾:提交了却要求不索引", "确认是否误标 noindex;确属不索引的移出 sitemap", ni_sm)
    for key, state, lv, title, act in [
        ("canonical_missing", "缺", "P1", "缺 canonical", "模板 head 统一输出绝对路径自指 canonical"),
        ("canonical_relative", "相对路径", "P2", "canonical 用相对路径", "改为绝对 HTTPS 路径"),
        ("canonical_other", "指向他页", "P1", "canonical 指向其它 URL", "逐个确认是有意合并还是模板取错 URL")]:
        ps = pages_where(lambda d, s=state: canon_state(d) == s)
        if ps:
            add(key, lv, "索引性", "%d 页%s" % (len(ps), title), "抓取到的 <link rel=canonical>", act, ps)
    red = probe.get("redirects", {})
    open_hosts = []
    for u, ch in red.items():
        if not ch or "status" not in ch[0]:
            continue
        p = urlparse(u)
        if ch[0]["status"] == 200 and (p.scheme == "http" or p.netloc != cfg["host"]):
            open_hosts.append(u)
        elif len([h for h in ch if h.get("status") in (301, 302, 307, 308)]) > 1:
            add("redirect_chain_" + u, "P2", "索引性", "%s 多跳跳转" % u,
                " → ".join("%s %s" % (h.get("status"), h.get("url")) for h in ch), "合并为一跳 301 到最终 URL")
    if open_hosts:
        add("host_dup", "P0", "索引性", "主机/协议未归一:%d 个入口直接 200" % len(open_hosts),
            "；".join(open_hosts), "统一 301 到 https://%s/(%s)" % (cfg["host"], hint(cfg, "redirect")), open_hosts, unit="个")
    ign = SHADOW_IGNORE.get(cfg.get("cms") or "", set())
    dup_in = [u for u, ch in red.items() if re.search(r"index\.(html|php)", u) and ch and ch[0].get("status") == 200]
    shadow = ["https://%s%s" % (cfg["host"], p) for p, r in probe.get("shadow", {}).items()
              if p not in ign and r.get("chain") and r["chain"][0].get("status") == 200 and r["chain"][0].get("title")]
    dups = list(dict.fromkeys(dup_in + shadow))
    if dups:
        add("shadow_urls", "P1", "索引性", "%d 个重复入口返回 200" % len(dups), "；".join(dups)[:600],
            "301 到正式 URL 或返回 404(%s);若服务器日志显示已被搜索引擎抓取,升为 P0" % hint(cfg, "redirect"),
            dups, unit="个")
    soft = [u for u, ch in probe.get("not_found", {}).items() if ch and ch[-1].get("status") == 200]
    if soft:
        add("soft404", "P1", "索引性", "不存在的 URL 返回 200(软 404)", "；".join(soft),
            "不存在的地址返回 404 状态码 + 品牌化 404 页", soft)
    if ctx["multi_lang"]:
        nh = pages_where(lambda d: not d["hreflang"])
        if nh:
            add("hreflang_missing", "P1", "索引性", "%d 页缺 hreflang" % len(nh), "多语种站点", "模板输出全语种互指 hreflang", nh)
        nx = pages_where(lambda d: d["hreflang"] and not any(l.lower() == "x-default" for l, _ in d["hreflang"]))
        if nx:
            add("hreflang_xdefault", "P2", "索引性", "hreflang 缺 x-default(%d 页)" % len(nx), "", "补 x-default 指向默认语种", nx)
    # ---- 速度
    for u, r in lh.items():
        path = urlparse(u).path or "/"
        if r.get("LCP_ms") and r["LCP_ms"] > th["lcp_ms"]:
            add("lcp_" + path, "P1", "网站速度", "移动端 LCP %.1fs(%s)" % (r["LCP_ms"] / 1000.0, path),
                "Lighthouse 移动端实验室值" + ("；LCP 元素 " + lcp_snippet(r) if lcp_snippet(r) else ""),
                "压缩/换 WebP 首屏主图、首屏图加 fetchpriority=high 且不懒加载")
        if r.get("CLS") is not None and r["CLS"] > th["cls"]:
            add("cls_" + path, "P1", "网站速度", "移动端 CLS %s(%s)" % (r["CLS"], path), "Lighthouse 移动端",
                "图片补 width/height、轮播与字体预留尺寸")
    slow = []
    for u, ts in probe.get("ttfb", {}).items():
        good = [t for t in ts if t > 0]
        if good and statistics.median(good) > th["ttfb_ms"]:
            slow.append("%s 中位 %dms(5 次 %s)" % (urlparse(u).path or "/", statistics.median(good), ts))
    if slow:
        hh = probe.get("home_headers", {})
        add("ttfb", "P1", "网站速度", "TTFB 超过 %dms" % th["ttfb_ms"],
            "；".join(slow) + ";Cache-Control=%s;cf-cache-status=%s" % (hh.get("cache-control"), hh.get("cf-cache-status")),
            "先区分源站慢还是回源慢:HTML 不可缓存时考虑页面缓存 / CDN 缓存 HTML(动态表单页除外)")
    imgs = [i for i in jload(out, "images_head.json", []) if i[1] == 200]
    big = sorted([i for i in imgs if i[2] > th["img_kb_max"] * 1024], key=lambda x: -x[2])
    if big:
        add("img_heavy", "P1", "网站速度", "%d 张正文图超过 %dKB" % (len(big), th["img_kb_max"]),
            "最大:" + "；".join("%s %.0fKB" % (i[0].rsplit("/", 1)[-1], i[2] / 1024.0) for i in big[:5]),
            "重新压缩并转 WebP(建议 <%dKB)" % th["img_kb_ideal"], [i[0] for i in big], unit="张")
    bi = [i for d in P.values() for i in content_imgs(d)]
    if bi:
        nw = sum(1 for i in bi if not re.search(r"\.(webp|avif)(\?|$)", i["src"], re.I))
        if nw / len(bi) > 0.5:
            add("img_format", "P2", "网站速度", "正文图 %d%% 不是 WebP/AVIF" % (nw * 100 // len(bi)),
                "%d / %d 处引用" % (nw, len(bi)), "批量转 WebP(保留原图旁路)")
        nd = sum(1 for i in bi if not (i["w"] and i["h"]))
        if nd / len(bi) > 0.2:
            add("img_dims", "P2", "网站速度", "正文图 %d%% 无 width/height" % (nd * 100 // len(bi)),
                "%d / %d 处引用" % (nd, len(bi)), "模板层补 width/height,防 CLS")
    hh = probe.get("home_headers", {})
    if re.search(r"no-store|no-cache|max-age=0", hh.get("cache-control") or "") and (hh.get("cf-cache-status") or "").upper() in ("DYNAMIC", "BYPASS"):
        add("html_nocache", "P2", "网站速度", "HTML 不进 CDN 缓存",
            "Cache-Control=%s;cf-cache-status=%s" % (hh.get("cache-control"), hh.get("cf-cache-status")),
            "与客户确认表单/验证码页不缓存后,对 HTML 开边缘缓存")
    # ---- 移动端
    nv = pages_where(lambda d: not d["viewport"])
    if nv:
        add("viewport", "P1", "移动端", "%d 页无 viewport" % len(nv), "", "模板 head 加 viewport", nv)
    # ---- 安全性
    mixed = pages_where(lambda d: d["mixed"])
    if mixed:
        add("mixed", "P1", "安全性", "%d 页引用 http:// 资源" % len(mixed), "混合内容", "资源地址改 https 或协议相对", mixed)
    miss = probe.get("security_missing", [])
    if "strict-transport-security" in miss:
        add("hsts", "P2", "安全性", "未开 HSTS", "", "nginx 加 Strict-Transport-Security")
    rest = [m for m in miss if m != "strict-transport-security"]
    if rest:
        add("sec_headers", "P2", "安全性", "缺安全响应头:%s" % ", ".join(rest), "首页响应头", "nginx 层补齐")
    # ---- 结构化数据
    ns = pages_where(lambda d: not d["schema"])
    if len(ns) == n and n:
        add("schema_none", "P1", "结构化数据", "全站 %d 页 0 个 JSON-LD" % n, "",
            "首页 Organization + WebSite;产品 Product(询价制不填假价格);文章 Article;全站 BreadcrumbList(%s)" % hint(cfg, "schema"), ns)
    elif ns:
        add("schema_gap", "P2", "结构化数据", "%d 页无 JSON-LD" % len(ns), "", "按页型补齐 Schema(%s)" % hint(cfg, "schema"), ns)
    types = collections.Counter(t for d in P.values() for t in d["schema"])
    if n and types and "Organization" not in types:
        add("schema_org", "P2", "结构化数据", "全站无 Organization", "已有类型:%s" % ", ".join(types), "首页补 Organization(含 logo、sameAs、联系方式)")
    # ---- 站点架构
    st = collections.Counter(r["status"] for r in rows)
    e5 = [r["url"] for r in rows if r["status"] >= 500]
    if e5:
        add("5xx", "P0", "站点架构", "%d 个 URL 返回 5xx" % len(e5), "抓取时", "查服务器错误日志", e5)
    odd = jload(out, "odd_links.json", {})
    if odd:
        add("broken_links", "P1", "站点架构", "%d 条站内死链 / 畸形链接" % len(odd),
            "；".join(list(odd)[:5]), "逐条修正链接目标(明细见审计表「断链」页)", list(odd), unit="条")
    orphan = pages_where(lambda d: d.get("all_inlinks", 1) == 0 and d["type"] != "home")
    if orphan:
        add("orphan", "P1", "站点架构", "%d 个页面全站 0 入链(孤岛)" % len(orphan), "只能从 sitemap 发现",
            "加进导航/栏目页/相关推荐", orphan)
    deep = pages_where(lambda d: d["depth"] > 3)
    if deep:
        add("deep", "P2", "站点架构", "%d 页点击深度 >3" % len(deep), "", "栏目页/聚合页增加直达链接", deep)
    # ---- On-page 站级
    for key, fn, lv, title, act in [
        ("title_long", lambda d: len(d["title"]) > th["title_max"], "P1", "Title 超过 %d 字符" % th["title_max"],
         "砍修饰词再砍品牌位;品牌后缀只在放得下时追加(%s)" % hint(cfg, "tdk")),
        ("title_dup", lambda d: d["dup_title"], "P1", "Title 重复", "逐页写差异化 Title;分页/筛选页用变量模板"),
        ("title_missing", lambda d: not d["title"], "P0", "缺 Title", "补 Title"),
        ("desc_missing", lambda d: not d["desc"], "P1", "缺 Meta 描述", "逐页写 %d-%d 字符描述并带 CTA" % (th["desc_min"], th["desc_max"])),
        ("desc_dup", lambda d: d["dup_desc"], "P1", "Meta 描述重复", "重复的多为模板默认描述,逐页重写"),
        ("h1_missing", lambda d: not d["h1"], "P1", "无 H1", "补含核心词的 H1(首页用业务定位句)"),
        ("h1_multi", lambda d: len(d["h1"]) > 1, "P2", "多个 H1", "保留一个,其余降为 H2"),
        ("alt_missing", lambda d: any(not (i["alt"] or "").strip() for i in content_imgs(d)), "P1", "正文图缺 ALT",
         "按「产品名 + 用途 + 品牌」补写"),
        ("og_missing", lambda d: not d["og"].get("image"), "P2", "无 og:image", "补 1200×630 分享图并标注 og:image:width/height"),
        ("thin", lambda d: d["words"] < 300 and d["words"] < ctx["median"].get((d["lang"], d["type"]), 0) * 0.5, "P2",
         "正文偏薄", "按页型补应用场景 / 规格选型 / FAQ 段")]:
        ps = pages_where(fn)
        if ps:
            add(key, lv, "On-page", "%d 页%s" % (len(ps), title), "占 %d%%" % (len(ps) * 100 // max(n, 1)), act, ps)

    rank = {"P0": 0, "P1": 1, "P2": 2}
    issues.sort(key=lambda i: (rank[i["level"]], -i["count"]))
    for lv in rank:
        for k, i in enumerate([x for x in issues if x["level"] == lv], 1):
            i["id"] = "%s-%d" % (lv, k)
    return issues


def lcp_snippet(r):
    v = r.get("lcp_element") or ""
    return "" if v.startswith("[") else v[:160]   # 老数据是截断的 JSON,不展示


def tech_dims(issues):
    """7 维状态:有 P0/P1 → ❌;只有 P2 → ⚠️;无 → ✅"""
    dims = ["爬取性", "索引性", "网站速度", "移动端", "安全性", "结构化数据", "站点架构"]
    out = []
    for dm in dims:
        lv = {i["level"] for i in issues if i["dim"] == dm}
        st = "❌" if lv & {"P0", "P1"} else ("⚠️" if lv else "✅")
        out.append((dm, st, "；".join(i["title"] for i in issues if i["dim"] == dm) or "未发现问题"))
    return out


def strengths(cfg, ctx, summary):
    """做对了的(保持):只列实测通过且有数字的"""
    P, n, s = ctx["P"], len(ctx["P"]), []
    if n and all(canon_state(d) == "Y" for d in P.values()):
        s.append("canonical %d/%d 页绝对路径自指" % (n, n))
    if n and not any(is_noindex(d) for d in P.values()):
        s.append("0 个 noindex 误标")
    if summary.get("5xx 数") == 0 and summary.get("4xx 数", 1) == 0:
        s.append("抓取 %d 个 URL 无 4xx/5xx" % summary.get("抓取 URL 数", 0))
    if n and not any(d["dup_title"] for d in P.values()):
        s.append("Title 全站唯一")
    if n and all(d["h1"] for d in P.values()):
        s.append("H1 全覆盖")
    if n and all(d["schema"] for d in P.values()):
        s.append("JSON-LD %d/%d 页覆盖(%s)" % (n, n, ", ".join(list((summary.get("Schema 类型分布") or {}).keys())[:5])))
    if ctx["multi_lang"] and all(d["hreflang"] for d in P.values()):
        s.append("hreflang %d/%d 页齐全" % (n, n))
    if n and not any(d["depth"] > 3 for d in P.values()):
        s.append("全部页面点击深度 ≤3")
    if n and all(d["og"].get("image") for d in P.values()):
        s.append("og:image 全覆盖")
    if summary.get("正文图 WebP/AVIF") and summary.get("正文图(全站引用次数)"):
        r = summary["正文图 WebP/AVIF"] / summary["正文图(全站引用次数)"]
        if r >= 0.8:
            s.append("正文图 %d%% 已是 WebP/AVIF" % (r * 100))
    return s


def compare(prev, now):
    """按 key 核对:已修复 / 仍存在 / 新出现"""
    pm, nm = {i["key"]: i for i in prev}, {i["key"]: i for i in now}
    fixed = [pm[k] for k in pm if k not in nm]
    still = [(pm[k], nm[k]) for k in pm if k in nm]
    new = [nm[k] for k in nm if k not in pm]
    return fixed, still, new


def run(cfg, log=print):
    ctx = context(cfg)
    issues = site_issues(cfg, ctx)
    jdump(cfg["out"], "findings.json", issues)
    c = collections.Counter(i["level"] for i in issues)
    log("问题清单初稿:P0 %d / P1 %d / P2 %d" % (c["P0"], c["P1"], c["P2"]))
    return issues
