# -*- coding: utf-8 -*-
"""步骤 3：技术侧定向实测 → probe.json / images_head.json

跳转链与主机归一、404 行为、CMS 兜底/影子 URL、robots、安全与缓存响应头、TTFB、正文图体积。
"""
import collections, re, time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

from app.modules.audit.common import UA, jdump, jload

SEC_HEADERS = ["strict-transport-security", "x-content-type-options", "x-frame-options",
               "referrer-policy", "content-security-policy", "permissions-policy"]


def session():
    # 只有装了 brotli 才敢声明 br:否则服务器回 br 压缩体,requests 解不开,拿到的是二进制
    try:
        import brotli  # noqa: F401
        enc = "gzip, deflate, br"
    except ImportError:
        enc = "gzip, deflate"
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Encoding": enc})
    return s


def follow(S, url, hops=6):
    """手动跟跳转，记录每一跳"""
    chain = []
    for _ in range(hops):
        try:
            r = S.get(url, timeout=40, allow_redirects=False)
        except Exception as e:
            chain.append({"url": url, "error": str(e)[:120]})
            return chain
        t = re.search(r"<title[^>]*>(.*?)</title>", r.text[:20000], re.S | re.I) if "html" in r.headers.get("Content-Type", "") else None
        chain.append({"url": url, "status": r.status_code, "loc": r.headers.get("Location", ""),
                      "len": len(r.content), "title": t.group(1).strip()[:80] if t else ""})
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
        else:
            break
    return chain


def detect_cms(html):
    if re.search(r"wp-content|wp-includes", html):
        return "wordpress"
    if re.search(r"/static/default/pc/|dayrui|Dolphingoo|xunruicms", html, re.I):
        return "xunrui"
    if re.search(r"Shopify\.theme|cdn\.shopify", html):
        return "shopify"
    return "unknown"


def ttfb(S, url, n=5):
    ts = []
    for _ in range(n):
        t0 = time.time()
        try:
            r = S.get(url, timeout=40, stream=True)
            next(r.iter_content(1), None)
            ts.append(round((time.time() - t0) * 1000))
            r.close()
        except Exception:
            ts.append(-1)
        time.sleep(0.4)
    return ts


def run(cfg, log=print):
    out, host, bare = cfg["out"], cfg["host"], cfg["bare"]
    base = "https://" + host
    S, res = session(), {}
    P = jload(out, "pages.json", {})
    home = S.get(base + "/", timeout=45)
    cms = cfg.get("cms_override") or detect_cms(home.text)  # 每次重判;cfg["cms"] 只是上次结果,可能是坏数据
    res["cms"] = cms
    log("CMS 识别：" + cms)

    # 1. 主机归一与跳转链
    variants = ["http://%s/" % bare, "http://www.%s/" % bare, "https://%s/" % bare, "https://www.%s/" % bare,
                base + "/index.html", base + "/index.php", base + "/?utm_source=audit"]
    res["redirects"] = {u: follow(S, u) for u in variants}

    # 2. 404 行为（软 404 是重点）
    res["not_found"] = {u: follow(S, u) for u in [base + "/seo-audit-404-check-zz/", base + "/seo-audit-404-check-zz.html",
                                                  base + "/products/seo-audit-404-check-zz.html"]}

    # 3. CMS 兜底/影子 URL：返回 200 且 canonical 自指 = 重复入口
    sections = collections.Counter(d["path"].strip("/").split("/")[0] for d in P.values()
                                   if d["lang"] == cfg["default_lang"] and d["path"].strip("/") and "." not in d["path"].strip("/").split("/")[0])
    secs = [s for s, _ in sections.most_common(4)]
    shadow = []
    if cms == "xunrui":
        shadow += ["/index.html", "/?c=category&id=1", "/index.php?s=api"]
        for s in secs:
            shadow += ["/%s/p999.html" % s, "/%s/show-1.html" % s, "/%s/1.html" % s, "/list-%s.html" % s, "/index.php?s=%s" % s]
        if "en" not in cfg["langs"]:
            shadow.append("/en/")  # 迅睿多语言常见：默认语种再挂一套 /en/ 前缀
    elif cms == "wordpress":
        shadow += ["/?p=1", "/?page_id=1", "/?s=test", "/author/admin/", "/feed/", "/wp-json/", "/page/999/",
                   "/category/uncategorized/", "/tag/test/", "/?attachment_id=1", "/wp-login.php"]
        for s in secs:
            shadow += ["/%s/page/999/" % s]
    else:
        for s in secs:
            shadow += ["/%s/page/999/" % s, "/%s?page=999" % s]
    res["shadow"] = {}
    for p in dict.fromkeys(shadow):
        ch = follow(S, base + p)
        last = ch[-1] if ch else {}
        res["shadow"][p] = {"chain": ch, "final_status": last.get("status"), "title": last.get("title", "")}

    # 4. robots.txt
    try:
        rb = S.get(base + "/robots.txt", timeout=30)
        res["robots"] = {"status": rb.status_code, "text": rb.text[:4000] if rb.status_code == 200 else ""}
    except Exception as e:
        res["robots"] = {"error": str(e)}

    # 5. 响应头：安全 / 缓存 / 压缩
    hh = {k.lower(): v for k, v in home.headers.items()}
    res["home_headers"] = {k: hh.get(k, "") for k in SEC_HEADERS + ["cache-control", "cf-cache-status", "server",
                                                                      "content-encoding", "x-cache", "age", "x-powered-by"]}
    res["security_missing"] = [k for k in SEC_HEADERS if not hh.get(k)]
    assets = re.findall(r"""<link[^>]+href=["']([^"']+\.css[^"']*)["']""", home.text)[:2] + \
        re.findall(r"""<script[^>]+src=["']([^"']+\.js[^"']*)["']""", home.text)[:2]
    res["asset_cache"] = {}
    for a in assets:
        u = urljoin(base + "/", a)
        try:
            r = S.head(u, timeout=30, allow_redirects=True)
            res["asset_cache"][u] = {"status": r.status_code, "cc": r.headers.get("Cache-Control", ""),
                                     "cf": r.headers.get("cf-cache-status", ""), "enc": r.headers.get("Content-Encoding", "")}
        except Exception as e:
            res["asset_cache"][u] = {"error": str(e)[:100]}

    # 6. TTFB：首页 + 主语种最多的详情页类型取一页
    detail = [d for d in P.values() if d["lang"] == cfg["default_lang"] and d["type"].endswith("_detail")]
    top_type = collections.Counter(d["type"] for d in detail).most_common(1)
    sample = [base + "/"] + ([next(d["url"] for d in detail if d["type"] == top_type[0][0])] if top_type else [])
    res["ttfb"] = {u: ttfb(S, u) for u in sample}
    res["sample_urls"] = sample
    for u, ts in res["ttfb"].items():
        log("  TTFB %s %s" % (u, ts))

    # 7. 正文图体积（主语种，非模板图，最多 800 张）
    srcs = list(dict.fromkeys(i["src"] for d in P.values() if d["lang"] == cfg["default_lang"]
                              for i in d["imgs"] if i["src"] and not i["tpl"]))[:800]

    def head(u):
        try:
            r = S.head(u, timeout=30, allow_redirects=True)
            n = r.headers.get("Content-Length")
            if n is None or r.status_code == 405:
                r = S.get(u, timeout=40)
                n = len(r.content)
            return [u, r.status_code, int(n), r.headers.get("Content-Type", ""), r.headers.get("Cache-Control", "")]
        except Exception as e:
            return [u, -1, 0, "", str(e)[:80]]

    with ThreadPoolExecutor(max_workers=8) as ex:
        imgs = list(ex.map(head, srcs))
    jdump(out, "images_head.json", imgs)
    ok = [i for i in imgs if i[1] == 200]
    log("  正文图 %d 张，合计 %.1f MB，>200KB %d 张" % (len(ok), sum(i[2] for i in ok) / 1048576.0,
                                                    sum(1 for i in ok if i[2] > 200 * 1024)))
    jdump(out, "probe.json", res)
    cfg["cms"] = cms
