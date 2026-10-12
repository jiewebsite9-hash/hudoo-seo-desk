# -*- coding: utf-8 -*-
"""步骤 1：全站抓取 = sitemap（含索引嵌套）+ 同域 BFS；顺带识别多语言前缀"""
import hashlib, json, os, queue, re, threading, time
from urllib.parse import urldefrag, urljoin, urlparse

import requests

from app.modules.audit.common import SKIP_EXT, UA, hosts, jdump, save_cfg


def make_norm(cfg):
    hs, base = hosts(cfg), "https://" + cfg["host"]

    def norm(u):
        u, _ = urldefrag((u or "").strip())
        if not u:
            return None
        p = urlparse(u)
        if p.scheme not in ("http", "https", ""):
            return None
        if p.netloc and p.netloc.lower() not in hs:
            return None
        u = urljoin(base + "/", u)
        p = urlparse(u)
        u = p._replace(scheme="https", netloc=cfg["host"]).geturl()
        if SKIP_EXT.search(p.path):
            return None
        return u
    return norm


def fetch_sitemaps(cfg, sess):
    """robots.txt 声明的 sitemap 优先，再兜底常见路径；sitemapindex 递归展开"""
    base = "https://" + cfg["host"]
    todo, seen_sm, urls, files = [], set(), [], []
    try:
        rb = sess.get(base + "/robots.txt", timeout=30)
        todo += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rb.text) if rb.status_code == 200 else []
    except Exception:
        pass
    todo += [base + p for p in ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")]
    while todo and len(seen_sm) < 60:
        sm = todo.pop(0)
        if sm in seen_sm:
            continue
        seen_sm.add(sm)
        try:
            r = sess.get(sm, timeout=40)
        except Exception as e:
            files.append({"url": sm, "status": -1, "error": str(e)})
            continue
        locs = re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]]+?)\s*(?:\]\]>)?\s*</loc>", r.text) if r.status_code == 200 else []
        is_index = "<sitemapindex" in r.text[:2000]
        files.append({"url": sm, "status": r.status_code, "final": r.url, "index": is_index, "locs": len(locs)})
        if is_index:
            todo += locs
        else:
            urls += locs
        if urls and not is_index and sm.endswith(("/sitemap.xml",)) and r.status_code == 200:
            # 主 sitemap 已拿到实体 URL，就不再去撞其余兜底路径
            todo = [t for t in todo if not t.endswith(("/sitemap_index.xml", "/wp-sitemap.xml"))]
    return list(dict.fromkeys(u.strip() for u in urls)), files


def detect_langs(cfg, html):
    """从首页 hreflang 推断语言前缀：{"de": "/de", ...}；默认语种取 <html lang> / x-default"""
    langs = {}
    for lang, href in re.findall(r"""<link[^>]+hreflang=["']([^"']+)["'][^>]*href=["']([^"']+)["']""", html, re.I) + \
            [(b, a) for a, b in re.findall(r"""<link[^>]+href=["']([^"']+)["'][^>]*hreflang=["']([^"']+)["']""", html, re.I)]:
        seg = [s for s in urlparse(href).path.split("/") if s]
        if lang.lower() != "x-default" and seg and re.fullmatch(r"[a-z]{2}(-[a-z]{2,4})?", seg[0], re.I):
            langs[lang.split("-")[0].lower()] = "/" + seg[0]
    m = re.search(r"<html[^>]+lang=[\"']([a-zA-Z]{2})", html)
    default = m.group(1).lower() if m else "en"
    langs.pop(default, None)
    return langs, default


def run(cfg, log=print):
    out = cfg["out"]
    htmldir = os.path.join(out, "html")
    os.makedirs(htmldir, exist_ok=True)
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    # 以首页最终落点确定主机名（www / 裸域）
    r = sess.get(cfg["base"], timeout=45, allow_redirects=True)
    cfg["host"] = urlparse(r.url).netloc.lower()
    cfg["bare"] = cfg["host"][4:] if cfg["host"].startswith("www.") else cfg["host"]
    if not cfg.get("langs"):
        cfg["langs"], cfg["default_lang"] = detect_langs(cfg, r.text)
    cfg.setdefault("default_lang", "en")
    save_cfg(cfg)
    log("主机 %s  默认语种 %s  语言前缀 %s" % (cfg["host"], cfg["default_lang"], cfg["langs"] or "无"))

    norm = make_norm(cfg)
    sm_raw, sm_files = fetch_sitemaps(cfg, sess)
    log("sitemap 文件 %d 个，URL %d 条" % (len(sm_files), len(sm_raw)))

    lock, seen, results, q = threading.Lock(), set(), [], queue.Queue()
    limit = cfg.get("max_pages", 1500)
    base = "https://" + cfg["host"]
    seeds = [base + "/"] + [base + p + "/" for p in cfg["langs"].values()] + sm_raw
    for u in seeds:
        n = norm(u)
        if n and n not in seen:
            seen.add(n)
            q.put((n, 0))

    def worker():
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        while True:
            try:
                url, depth = q.get(timeout=8)
            except queue.Empty:
                return
            try:
                t0 = time.time()
                r = s.get(url, timeout=45, allow_redirects=True)
                rec = {"url": url, "final": r.url, "status": r.status_code,
                       "chain": [(h.status_code, h.headers.get("Location", "")) for h in r.history],
                       "elapsed": round(time.time() - t0, 3), "ctype": r.headers.get("Content-Type", ""),
                       "len": len(r.content), "depth": depth,
                       "headers": {k.lower(): v for k, v in r.headers.items()}}
                if "text/html" in rec["ctype"] and r.status_code == 200:
                    h = hashlib.md5(r.url.encode()).hexdigest()
                    rec["html"] = h
                    with open(os.path.join(htmldir, h + ".html"), "w", encoding="utf-8", errors="ignore") as f:
                        f.write(r.text)
                    if depth < 8:
                        for m in re.finditer(r"""<a[^>]+href=["']([^"']+)["']""", r.text, re.I):
                            n = norm(m.group(1))
                            if n:
                                with lock:
                                    if n not in seen and len(seen) < limit:
                                        seen.add(n)
                                        q.put((n, depth + 1))
                with lock:
                    results.append(rec)
                    if len(results) % 50 == 0:
                        log("  已抓 %d 页" % len(results), flush=True)
            except Exception as e:
                with lock:
                    results.append({"url": url, "status": -1, "error": str(e), "depth": depth})
            finally:
                q.task_done()

    ths = [threading.Thread(target=worker, daemon=True) for _ in range(cfg.get("threads", 5))]
    for t in ths:
        t.start()
    for t in ths:
        t.join()

    with open(os.path.join(out, "crawl.jsonl"), "w", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    jdump(out, "sitemap_urls.json", sm_raw)
    jdump(out, "sitemap_files.json", sm_files)
    log("抓取完成：%d 条（上限 %d%s）" % (len(results), limit, "，已触顶，结果不完整" if len(seen) >= limit else ""))
