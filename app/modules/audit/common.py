# -*- coding: utf-8 -*-
"""SEO 审计流水线公共件：配置读写、路径、HTML 小工具"""
import json, os, re

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|avif|svg|ico|css|js|pdf|zip|rar|7z|doc|docx|xls|xlsx|ppt|pptx|mp4|mp3|webm|woff2?|ttf|eot|xml|txt|json)(\?|$)", re.I)
CN = re.compile(r"[一-鿿]")



def jload(out, name, default=None):
    p = os.path.join(out, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def jdump(out, name, obj):
    with open(os.path.join(out, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def load_cfg(out):
    return jload(out, "audit.json")


def save_cfg(cfg):
    jdump(cfg["out"], "audit.json", cfg)


def hosts(cfg):
    return {cfg["host"], cfg["bare"], "www." + cfg["bare"]}


def strip_comments(h):
    return re.sub(r"<!--.*?-->", " ", h, flags=re.S)


def text_of(h):
    h = re.sub(r"<(script|style|noscript|template)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<[^>]+>", " ", h)
    h = (h.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
         .replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", h).strip()


def wordcount(t):
    cn = len(CN.findall(t))
    en = len(re.findall(r"[A-Za-zÀ-ÿА-Яа-яЁё]+", t))
    return cn + en if cn > 20 else en


def lang_of(cfg, path):
    for lang, pre in sorted(cfg.get("langs", {}).items(), key=lambda x: -len(x[1])):
        if path == pre or path.startswith(pre + "/"):
            return lang
    return cfg.get("default_lang", "en")


def strip_lang(cfg, path):
    lang = lang_of(cfg, path)
    pre = cfg.get("langs", {}).get(lang)
    if pre and (path == pre or path.startswith(pre + "/")):
        return path[len(pre):] or "/"
    return path


def page_type(cfg, path):
    """通用页型：home / <一级栏目>_index / <一级栏目>_detail，用于同类页字数中位数对比"""
    p = strip_lang(cfg, path)
    seg = [s for s in p.strip("/").split("/") if s]
    if not seg or seg[0] in ("index.html", "index.php"):
        return "home"
    s0 = re.sub(r"\.html?$", "", seg[0])
    if len(seg) == 1:
        return s0 if not seg[0].endswith((".html", ".htm")) else "page:" + s0
    return s0 + "_detail" if re.search(r"\.html?$", seg[-1]) or len(seg) > 2 else s0 + "_sub"
