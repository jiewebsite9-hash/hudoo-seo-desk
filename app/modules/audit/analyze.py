# -*- coding: utf-8 -*-
"""步骤 2：逐页解析 → pages.json / template_parts.json / odd_links.json"""
import collections, json, os, re
from urllib.parse import urljoin, urlparse

from app.modules.audit.common import hosts, jdump, jload, lang_of, page_type, strip_comments, strip_lang, text_of, wordcount
from app.modules.audit.crawl import make_norm

# 模板块：HTML5 语义标签 + 常见建站模板的头尾 class（迅睿 / WP / Elementor）
TPL_TAGS = r"header|footer|nav"
TPL_CLASSES = (r"header|footer|site-header|site-footer|header_box|footer_box|nav_list_box|head_box|foot_box|"
               r"top_box|elementor-location-header|elementor-location-footer|navbar|main-nav|main-menu")


def cut_block(html, cls_re):
    """按 class 起点做 div 配平剪块"""
    out = html
    for _ in range(50):
        m = re.search(r'<div[^>]+class="(?:[^"]*\s)?(?:%s)(?:\s[^"]*)?"[^>]*>' % cls_re, out, re.I)
        if not m:
            break
        depth, pos = 1, m.end()
        while depth > 0 and pos < len(out):
            n = re.search(r"<div\b|</div>", out[pos:], re.I)
            if not n:
                pos = len(out)
                break
            pos += n.end()
            depth += 1 if n.group(0).lower() == "<div" else -1
        out = out[: m.start()] + " " + out[pos:]
    return out


def schema_types(hs):
    types = []
    for blob in re.findall(r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""", hs, re.S | re.I):
        try:
            j = json.loads(blob.strip())
        except Exception:
            types.append("PARSE_ERROR")
            continue
        stack = j if isinstance(j, list) else [j]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                t = o.get("@type")
                types += t if isinstance(t, list) else ([t] if t else [])
                stack += o.get("@graph", []) or []
    return types


def meta(hs, attr, name):
    m = re.search(r"""<meta[^>]+%s=["']%s["'][^>]*content=["']([^"']*)["']""" % (attr, name), hs, re.I) or \
        re.search(r"""<meta[^>]+content=["']([^"']*)["'][^>]*%s=["']%s["']""" % (attr, name), hs, re.I)
    return m.group(1).strip() if m else ""


NOT_CONTENT_IMG = re.compile(r"captcha|[?&]s=api|\.ico(\?|$)|data:image|/(?:icons?|ico)/", re.I)


def tkey(cfg, page_url, ref):
    """模板件比对键：转绝对 URL、去语言前缀，抹平 相对/协议相对/多语种 写法差异"""
    p = urlparse(urljoin(page_url, ref))
    return strip_lang(cfg, p.path or "/") + ("?" + p.query if p.query else "")


def run(cfg, log=print):
    out = cfg["out"]
    hs_ok = hosts(cfg)
    norm = make_norm(cfg)
    rows = [json.loads(l) for l in open(os.path.join(out, "crawl.jsonl"), encoding="utf-8")]
    sitemap = {norm(u) or u for u in jload(out, "sitemap_urls.json", [])}
    html_pages = [r for r in rows if r.get("html")]
    N = max(len(html_pages), 1)

    # 第一遍：出现在 ≥80% 页面的链接/图片 = 模板件
    raw, link_cnt, img_cnt = {}, collections.Counter(), collections.Counter()
    for r in html_pages:
        p = os.path.join(out, "html", r["html"] + ".html")
        if not os.path.exists(p) or r["final"] in raw:
            continue
        h = open(p, encoding="utf-8", errors="ignore").read()
        raw[r["final"]] = (r, h)
        hs = strip_comments(h)
        link_cnt.update({tkey(cfg, r["final"], x) for x in re.findall(r"""<a[^>]+href=["']([^"']+)["']""", hs, re.I)})
        img_cnt.update({tkey(cfg, r["final"], x) for x in
                        re.findall(r"""<img\b[^>]*?\s(?:data-src|data-lazy-src|src)=["']([^"']+)["']""", hs, re.I)})
    tpl_links = {k for k, v in link_cnt.items() if v >= 0.8 * N}
    tpl_imgs = {k for k, v in img_cnt.items() if v >= 0.8 * N}

    # 全站入链(含导航/页脚):判孤岛页用,和正文入链分开算
    all_in = collections.Counter()
    for url, (r, h) in raw.items():
        tg = set()
        for x in re.findall(r"""<a[^>]+href=["']([^"']+)["']""", strip_comments(h), re.I):
            n = norm(urljoin(url, x))
            if n and n != url:
                tg.add(n)
        all_in.update(tg)

    pages, inlinks = {}, collections.Counter()
    for url, (r, h) in raw.items():
        hs = strip_comments(h)
        path = urlparse(url).path or "/"
        d = {"url": url, "path": path, "lang": lang_of(cfg, path), "type": page_type(cfg, path),
             "status": r["status"], "bytes": r["len"], "elapsed": r["elapsed"], "depth": r["depth"],
             "in_sitemap": url in sitemap, "redirected_from": r["url"] if r["url"] != url else "",
             "x_robots": r.get("headers", {}).get("x-robots-tag", "")}
        m = re.search(r"<title[^>]*>(.*?)</title>", hs, re.S | re.I)
        d["title"] = text_of(m.group(1)) if m else ""
        d["title_count"] = len(re.findall(r"<title[\s>]", hs, re.I))
        d["desc"] = meta(hs, "name", "description")
        d["keywords"] = meta(hs, "name", "keywords")
        d["robots_meta"] = meta(hs, "name", "robots")
        m = re.search(r"""<link[^>]+rel=["']canonical["'][^>]*href=["']([^"']*)["']""", hs, re.I) or \
            re.search(r"""<link[^>]+href=["']([^"']*)["'][^>]*rel=["']canonical["']""", hs, re.I)
        d["canonical"] = m.group(1).strip() if m else ""
        d["canonical_count"] = len(re.findall(r"""rel=["']canonical["']""", hs, re.I))
        d["hreflang"] = re.findall(r"""<link[^>]+hreflang=["']([^"']+)["'][^>]*href=["']([^"']+)["']""", hs, re.I) + \
            [(b, a) for a, b in re.findall(r"""<link[^>]+href=["']([^"']+)["'][^>]*hreflang=["']([^"']+)["']""", hs, re.I)]
        d["og"] = dict(re.findall(r"""<meta[^>]+property=["']og:([^"']+)["'][^>]*content=["']([^"']*)["']""", hs, re.I))
        d["twitter"] = dict(re.findall(r"""<meta[^>]+name=["']twitter:([^"']+)["'][^>]*content=["']([^"']*)["']""", hs, re.I))
        d["schema"] = schema_types(hs)
        d["viewport"] = bool(re.search(r"""name=["']viewport["']""", hs, re.I))
        m = re.search(r"<html[^>]+lang=[\"']([^\"']+)", hs, re.I)
        d["html_lang"] = m.group(1) if m else ""
        d["gtm"] = bool(re.search(r"googletagmanager\.com|gtag\(", hs))
        d["mixed"] = len(re.findall(r"""(?:src|href)=["']http://""", hs, re.I))

        heads = [(int(m.group(1)), text_of(m.group(2))) for m in re.finditer(r"<h([1-6])[^>]*>(.*?)</h\1>", hs, re.S | re.I)]
        d["h1"] = [t for lv, t in heads if lv == 1]
        d["h_levels"] = [lv for lv, _ in heads]

        # 正文：剥掉头尾导航模板
        body = re.sub(r"^.*?<body[^>]*>", " ", hs, flags=re.S | re.I)
        body = re.sub(r"<(%s)\b[^>]*>.*?</\1>" % TPL_TAGS, " ", body, flags=re.S | re.I)
        body = cut_block(body, TPL_CLASSES)
        btxt = text_of(body)
        d["words"] = wordcount(btxt)
        d["body_head200"] = btxt[:400]
        d["body_h2"] = [text_of(m.group(1)) for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", body, re.S | re.I)]

        imgs = []
        for m in re.finditer(r"<img[^>]*>", body, re.I):
            tag = m.group(0)
            srcm = re.search(r"""\s(?:data-src|data-lazy-src|src)=["']([^"']+)["']""", tag, re.I)
            src = srcm.group(1) if srcm else ""
            altm = re.search(r"""\salt=["']([^"']*)["']""", tag, re.I)
            imgs.append({"src": urljoin(url, src) if src and not src.startswith("data:") else "", "raw": src,
                         "alt": altm.group(1) if altm else None,
                         "w": bool(re.search(r"""\swidth=["']?\d""", tag, re.I)),
                         "h": bool(re.search(r"""\sheight=["']?\d""", tag, re.I)),
                         "lazy": bool(re.search(r"""loading=["']lazy|data-src=|lazyload""", tag, re.I)),
                         "tpl": bool(src) and (tkey(cfg, url, src) in tpl_imgs or bool(NOT_CONTENT_IMG.search(src)))})
        d["imgs"] = imgs

        links = []
        for m in re.finditer(r"""<a\b([^>]*)href=["']([^"']+)["']([^>]*)>(.*?)</a>""", body, re.S | re.I):
            href, anchor = m.group(2), text_of(m.group(4))
            if not anchor:
                am = re.search(r"""alt=["']([^"']+)["']""", m.group(4), re.I)
                anchor = "[img] " + am.group(1) if am else ""
            full = urljoin(url, href)
            internal = urlparse(full).netloc.lower() in hs_ok
            links.append({"href": href, "full": norm(full) or full, "anchor": anchor, "internal": internal,
                          "tpl": tkey(cfg, url, href) in tpl_links, "nofollow": "nofollow" in (m.group(1) + m.group(3)).lower()})
        d["links"] = links
        for l in links:
            if l["internal"] and not l["tpl"]:
                inlinks[l["full"]] += 1
        pages[url] = d

    tmap, dmap = collections.defaultdict(list), collections.defaultdict(list)
    for u, d in pages.items():
        d["inlinks"] = inlinks.get(u, 0)
        d["all_inlinks"] = all_in.get(u, 0)
        if d["title"]:
            tmap[d["title"]].append(u)
        if d["desc"]:
            dmap[d["desc"]].append(u)
    for d in pages.values():
        d["dup_title"] = len(tmap.get(d["title"], [])) > 1
        d["dup_desc"] = len(dmap.get(d["desc"], [])) > 1

    jdump(out, "pages.json", pages)
    jdump(out, "template_parts.json", {"tpl_links": sorted(tpl_links), "tpl_imgs": sorted(tpl_imgs)})

    # 断链 / 畸形链接：谁链到了非 200
    statuses = {}
    for r in rows:
        statuses[r["url"]] = r["status"]
        statuses.setdefault(r.get("final", r["url"]), r["status"])
    odd = collections.defaultdict(list)
    for u, d in pages.items():
        for l in d["links"]:
            if l["internal"]:
                st = statuses.get(l["full"])
                if st is not None and st >= 400 and "/cdn-cgi/" not in l["full"]:
                    odd["%s [%s]" % (l["full"], st)].append(u)
            if re.match(r"^\s*(tel|mailto)", l["href"], re.I) and not re.match(r"^(tel:|mailto:)", l["href"], re.I):
                odd["MALFORMED:" + l["href"]].append(u)
    jdump(out, "odd_links.json", odd)
    log("解析 %d 页；模板链接 %d / 模板图片 %d；断链/畸形链接 %d 条" % (len(pages), len(tpl_links), len(tpl_imgs), len(odd)))
