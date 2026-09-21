# -*- coding: utf-8 -*-
"""本地 HTTP 服务。只监听 127.0.0.1,不对外。

前端是普通网页,后端是标准库 http.server —— 零额外依赖,打包体积小,
和 rank-tracker 一脉相承。
"""
import json
import mimetypes
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, quote

from app import config, jobs
from app.modules.keywords import gkp

VERSION = "0.1.0"
REPO = "jiewebsite9-hash/hudoo-seo-desk"


def web_dir():
    # 打包后网页资源解包在 sys._MEIPASS
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "web"


class Handler(BaseHTTPRequestHandler):
    server_version = "HudooSeoDesk/" + VERSION

    # 静音默认访问日志,控制台只留我们自己的输出
    def log_message(self, fmt, *args):
        pass

    # ---------------------------------------------------------- 响应工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, download_name=None):
        if not path.exists() or not path.is_file():
            return self._json({"error": "找不到文件"}, 404)
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download_name:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + download_name)
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            return self._file(web_dir() / "index.html")
        if p == "/app.js":
            return self._file(web_dir() / "app.js")

        if p == "/api/status":
            st = config.status()
            st["version"] = VERSION
            st["repo"] = REPO
            return self._json(st)

        if p == "/api/template":
            from app.modules.keywords import templates
            kind = (q.get("kind") or [""])[0]
            try:
                data, name = templates.build(kind)
            except KeyError as e:
                return self._json({"error": str(e)}, 404)
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument."
                             "spreadsheetml.sheet")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + quote(name))
            self.end_headers()
            return self.wfile.write(data)

        if p == "/api/ranks/projects":
            from app.modules.ranks import tracker
            return self._json({"projects": tracker.projects(),
                               "unit": {"standard": tracker.DataForSeo.unit_price(3, "standard"),
                                        "live": tracker.DataForSeo.unit_price(3, "live")}})

        if p == "/api/ranks/overview":
            from app.modules.ranks import tracker
            d = (q.get("domain") or [""])[0]
            if not d:
                return self._json({"error": "要指定域名"}, 400)
            return self._json(tracker.overview(d))

        if p == "/api/ranks/history":
            from app.modules.ranks import storage, tracker
            kw = (q.get("keyword") or [""])[0]
            d = tracker.norm_host((q.get("domain") or [""])[0])
            conn = storage.connect()
            rows = storage.history(conn, kw, d)
            conn.close()
            return self._json({"keyword": kw, "domain": d, "rows": rows})

        if p == "/api/lists":
            from app.modules.keywords import learn
            name = (q.get("load") or [""])[0]
            if name:
                try:
                    return self._json(learn.load_list(name))
                except KeyError as e:
                    return self._json({"error": str(e)}, 404)
            return self._json({"lists": learn.list_saved()})

        if p == "/api/options":
            from app.modules.keywords import locations
            return self._json(locations.options())

        if p == "/api/jobs":
            return self._json(jobs.listing())

        if p == "/api/job":
            job = jobs.get((q.get("id") or [""])[0])
            if not job:
                return self._json({"error": "作业不存在(可能已被清理)"}, 404)
            return self._json(job.snapshot(int((q.get("since") or ["0"])[0])))

        if p == "/api/download":
            name = unquote((q.get("file") or [""])[0])
            # 只允许下载 out 目录下的文件,挡掉 ../ 穿越
            target = (config.out_dir() / name).resolve()
            if not str(target).startswith(str(config.out_dir().resolve())):
                return self._json({"error": "路径不合法"}, 400)
            return self._file(target, download_name=name)

        return self._json({"error": "没有这个接口"}, 404)

    # ---------------------------------------------------------- 文件解析
    def _parse_file(self):
        """上传的词表文件 -> 行数组。

        放在服务端做,是因为真实文件很脏:GKP 网页版导出的 CSV 是 **UTF-16 + Tab 分隔**,
        Excel 另存的可能是 GBK,还有 xlsx。这些在浏览器里都处理不了。
        """
        from urllib.parse import unquote
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return self._json({"error": "没收到文件内容"}, 400)
        if n > 20 * 1024 * 1024:
            return self._json({"error": "文件超过 20MB"}, 400)
        raw = self.rfile.read(n)
        name = unquote(self.headers.get("X-Filename") or "上传文件")
        try:
            rows, note = parse_table_bytes(raw, name)
        except Exception as e:
            return self._json({"error": "解析失败:%s: %s" % (type(e).__name__, e)}, 400)
        if not rows:
            return self._json({"error": "文件里没读到内容"}, 400)
        return self._json({"name": name, "count": len(rows), "note": note,
                           "rows": rows[:20000]})

    # ---------------------------------------------------------- POST
    def do_POST(self):
        p = urlparse(self.path).path

        # 必须在 _body() 之前 —— 文件上传的请求体是二进制,
        # 一旦被 _body() 按 JSON 读掉,这里再读 Content-Length 就会永久阻塞。
        if p == "/api/parse-file":
            return self._parse_file()

        b = self._body()

        if p == "/api/keywords/check":
            job = jobs.start("自检 Google Ads 连接", lambda j: gkp.check(j))
            return self._json({"job": job.id})

        if p == "/api/keywords/ideas":
            geos = [g for g in (b.get("geos") or []) if g]
            seeds = gkp.parse_keyword_text(b.get("seeds"))
            url = (b.get("url") or "").strip() or None
            site = bool(b.get("site"))
            lang = b.get("lang") or "en"
            minv = int(b.get("min_volume") or 0)

            def run(j):
                rows = gkp.ideas(seeds=seeds, url=url, site=site, geos=geos,
                                 lang=lang, min_volume=minv, job=j)
                return _finish(j, rows, "ideas", bool(b.get("usd")), b.get("usd_rate"))

            return self._json({"job": jobs.start("拓词", run).id})

        if p == "/api/keywords/derive":
            from app.modules.keywords import derive, learn

            def run(j):
                res = derive.derive(
                    material=b.get("material"),
                    sample_words=gkp.parse_keyword_text(b.get("sample")),
                    extra=b.get("extra"), job=j)
                saved = None
                if (b.get("save_as") or "").strip():
                    rows, metrics, core, note = derive.to_list_payload(
                        res, b["save_as"], b.get("note") or "")
                    saved = learn.save_list(b["save_as"].strip(), rows, metrics, core,
                                            note=note or "从客户资料生成")
                    j.log("已存进清单库:%s" % saved)
                pv = res.get("preview") or {}
                return {"count": len(res["exclude"]),
                        "columns": ["模式", "硬剔", "命中", "实剔", "被豁免", "理由", "样例"],
                        "preview": pv.get("rows") or
                                   [{"模式": e["pattern"], "理由": e.get("reason", "")}
                                    for e in res["exclude"]],
                        "csv": None, "truncated": False,
                        "stats": {"剔除条数": len(res["exclude"]),
                                  "混杂条数": len(res["mixed"]),
                                  "策略词": len(res["strategy"]),
                                  "核心词": len(res["core"]),
                                  "样本实剔": pv.get("cut"),
                                  "核心词救回": pv.get("saved"),
                                  "本次花费": ("$%s" % res["cost"]) if res.get("cost") is not None else "—"},
                        "lists": {"exclude": [e["pattern"] for e in res["exclude"]],
                                  "mixed": [m["pattern"] for m in res["mixed"]],
                                  "strategy": [s["keyword"] for s in res["strategy"]],
                                  "core": res["core"]},
                        "saved": saved}

            return self._json({"job": jobs.start("从客户资料产出清单", run).id})

        if p == "/api/llm/check":
            from app.modules.llm import client as llm

            def run(j):
                r = llm.check(log=j.log)
                j.log("回复:%s" % r["reply"])
                return {"count": 0, "preview": [], "csv": None,
                        "stats": {"provider": r["provider"], "模型": r["model"],
                                  "耗时": "%ss" % r["seconds"],
                                  "输入token": r["usage"].get("in"),
                                  "缓存命中": r["usage"].get("cache_in"),
                                  "输出token": r["usage"].get("out"),
                                  "本次花费": ("$%s" % r["cost"]) if r["cost"] is not None else "—"}}

            return self._json({"job": jobs.start("LLM 连通性自检", run).id})

        if p == "/api/ranks/sync":
            from app.modules.ranks import tracker

            def run(j):
                r = tracker.sync_keywords(domain=b.get("domain"), url=b.get("url"), job=j)
                return {"count": len(r["keywords"]), "columns": ["关键词"],
                        "preview": [{"关键词": k} for k in r["keywords"][:300]],
                        "csv": None, "truncated": len(r["keywords"]) > 300,
                        "keywords": r["keywords"], "resolved_url": r["resolved_url"],
                        "stats": {"飞书行数": r["rows"], "拉到词数": len(r["keywords"]),
                                  "未标Y被过滤": r["skipped"]}}

            return self._json({"job": jobs.start("同步飞书词库", run).id})

        if p == "/api/ranks/writeback":
            from app.modules.ranks import tracker

            def run(j):
                r = tracker.writeback(b.get("domain") or "", field=b.get("field"),
                                      url=b.get("url"), job=j)
                return {"count": r["written"], "columns": [], "preview": [],
                        "csv": None, "stats": {"写入行数": r["written"],
                                               "字段": r["field"],
                                               "列类型": r["field_type"],
                                               "表里无数据行": r["no_data"],
                                               "轮次": r["run_date"]}}

            return self._json({"job": jobs.start("写回飞书排名列", run).id})

        if p == "/api/ranks/check":
            from app.modules.ranks import tracker

            def run(j):
                rows, stats = tracker.check(
                    domain=b.get("domain") or "",
                    keywords=gkp.parse_keyword_text(b.get("keywords")),
                    gl=b.get("gl"), hl=b.get("hl"), device=b.get("device"),
                    depth=int(b.get("depth") or 3),
                    mode=b.get("mode") or "standard",
                    only_failed=bool(b.get("only_failed")), job=j)
                if b.get("writeback"):
                    try:
                        wb = tracker.writeback(b.get("domain") or "", job=j)
                        stats["写回"] = "%d 行 -> %s" % (wb["written"], wb["field"])
                    except Exception as e:
                        j.log("[写回失败] %s(排名数据已存好,可以单独重试写回)" % str(e)[:120])
                if b.get("push"):
                    try:
                        from app.modules.ranks import feishu
                        feishu.push(tracker.report_text(b.get("domain") or "", stats, rows),
                                    log=j.log)
                    except Exception as e:
                        j.log("[推送失败] %s" % str(e)[:120])
                return {"count": len(rows),
                        "columns": ["关键词", "排名", "上轮", "变化", "URL", "状态"],
                        "preview": rows[:300], "csv": None,
                        "truncated": len(rows) > 300, "stats": stats}

            return self._json({"job": jobs.start("排名检查", run).id})

        if p == "/api/keywords/learn":
            from app.modules.keywords import learn

            def run(j):
                cut = gkp.parse_keyword_text(b.get("cut"))
                keep = gkp.parse_keyword_text(b.get("keep"))
                j.log("剔除词 %d 个,保留词 %d 个" % (len(cut), len(keep)))
                if not keep:
                    j.log("[注意] 没给保留词,没法验证误杀 —— 学出来的清单可能过宽")
                rows, metrics, core, rejected = learn.learn(
                    cut, keep,
                    min_support=int(b.get("min_support") or 8),
                    allow_false_kill=int(b.get("allow_false_kill") or 0))
                j.log("候选 %d 条 -> 过验证 + 去冗余后留下 %d 条"
                      % (metrics["候选数"], metrics["学出模式数"]))
                j.log("自检:召回 %.1f%%(%d/%d),误杀 %.1f%%(%d/%d)"
                      % (metrics["召回"], metrics["命中剔除词"], metrics["剔除词总数"],
                         metrics["误杀"], metrics["误杀数"], metrics["保留词总数"]))
                path = learn.save_xlsx(rows, metrics, core, rejected)
                j.log("已导出 -> %s" % path.name)
                saved = None
                if (b.get("save_as") or "").strip():
                    saved = learn.save_list(b["save_as"].strip(), rows, metrics, core,
                                            note=b.get("note") or "")
                    j.log("已存进清单库:%s" % saved)
                return {"count": len(rows), "columns": ["模式", "剔除中命中", "误杀保留词", "样例"],
                        "preview": rows[:200], "csv": None, "xlsx": path.name,
                        "truncated": len(rows) > 200, "stats": metrics,
                        "patterns": [r["模式"] for r in rows],
                        "core": [c["词"] for c in core], "saved": saved}

            return self._json({"job": jobs.start("学剔除清单", run).id})

        if p == "/api/keywords/sop":
            from app.modules.keywords import sop
            geos = [g for g in (b.get("geos") or []) if g]

            def run(j):
                market = (geos[0] if geos else "US")
                lang = b.get("lang") or "en"
                minv = int(b.get("min_volume") or 0)
                mixed = gkp.parse_keyword_text(b.get("mixed"))
                header, rows, cut, stats = sop.build(
                    seeds=gkp.parse_keyword_text(b.get("seeds")),
                    competitor_sites=gkp.parse_keyword_text(b.get("sites")),
                    customer_words=_lines(b.get("customer")),
                    mixed_words=mixed,
                    exclude_words=gkp.parse_keyword_text(b.get("exclude")),
                    market=market, lang=lang, min_volume=minv,
                    usd_rate=b.get("usd_rate") or None, job=j)
                if not rows:
                    return {"count": 0, "preview": [], "csv": None}
                csv_path = sop.save_csv(header, rows)
                xlsx_path = sop.save_workbook(
                    header, rows, cut, stats,
                    {"market": market, "lang": lang, "min_volume": minv, "mixed": mixed})
                j.log("已导出 -> %s(总表 CSV)" % csv_path.name)
                j.log("已导出 -> %s(4 张表的工作簿,可直接导进飞书)" % xlsx_path.name)
                # 预览用中文表头,把 {市场} 占位换成实际市场名
                keymap = dict(zip(sop.COLUMNS, header))
                preview = [{keymap[k]: v for k, v in r.items() if k in keymap}
                           for r in rows[:200]]
                clean = {k: v for k, v in stats.items() if not k.startswith("_")}
                return {"count": len(rows), "columns": header, "preview": preview,
                        "csv": csv_path.name, "xlsx": xlsx_path.name,
                        "truncated": len(rows) > 200, "stats": clean}

            return self._json({"job": jobs.start("生成 SOP 总表", run).id})

        if p == "/api/keywords/volume":
            geos = [g for g in (b.get("geos") or []) if g]
            words = gkp.parse_keyword_text(b.get("keywords"))
            lang = b.get("lang") or "en"

            def run(j):
                rows = gkp.volume(words, geos=geos, lang=lang, job=j)
                return _finish(j, rows, "volume", bool(b.get("usd")), b.get("usd_rate"))

            return self._json({"job": jobs.start("补搜索量", run).id})

        return self._json({"error": "没有这个接口"}, 404)


# 表头行的第一格长这样就当成表头扔掉。比较前会去掉所有空白,
# 所以「词 / 模式」和「词/模式」都能命中。
# **本程序自己发的模板表头必须全部在这里** —— 否则用户下载模板填完传回来,
# 表头会被当成一个关键词导进去。
HEADER_WORDS = {
    "keyword", "keywords", "关键词", "词", "term", "terms", "query", "queries",
    "searchterm", "searchterms",
    # 模板表头
    "种子词", "种子", "seed", "seeds",
    "竞品网址", "网址", "域名", "url", "urls", "site", "sites", "domain", "domains",
    "词/模式", "模式", "pattern", "patterns",
    "剔除词", "排除词",
    # 客户给的词表常见首列
    "序号", "编号", "no", "no.", "#", "index", "id",
}


def parse_table_bytes(raw, filename=""):
    """把上传的 txt / csv / tsv / xlsx 字节流解析成 [[单元格,...], ...]。

    返回 (行数组, 说明文字)。说明会告诉用户识别出了什么编码/格式,
    出问题时一眼能看出是不是认错了。
    """
    low = filename.lower()

    # ---- xlsx ----
    if low.endswith((".xlsx", ".xlsm")):
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = []
        for r in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in r]
            while cells and not cells[-1]:
                cells.pop()
            if cells and any(cells):
                rows.append(cells)
        wb.close()
        return _strip_header(rows), "xlsx · 工作表「%s」" % ws.title

    # ---- 文本:逐个编码试,UTF-16 放前面(GKP 导出就是它)----
    text, enc = None, None
    for candidate in ("utf-8-sig", "utf-16", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            t = raw.decode(candidate)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # UTF-16 解错时常见表现是夹杂大量 NUL
        if "\x00" in t:
            continue
        text, enc = t, candidate
        break
    if text is None:
        return [], "认不出编码"

    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    # 分隔符:Tab 优先(GKP 导出是 Tab),否则逗号
    sample = "\n".join(lines[:40])
    sep = "\t" if sample.count("\t") >= sample.count(",") and "\t" in sample else ","
    rows = []
    for ln in lines:
        if not ln.strip():
            continue
        cells = [c.strip().strip('"') for c in ln.split(sep)]
        while cells and not cells[-1]:
            cells.pop()
        if cells and any(cells):
            rows.append(cells)
    if sep not in sample:
        note = "%s · 单列" % enc
    else:
        note = "%s · %s 分隔" % (enc, "Tab" if sep == "\t" else "逗号")
    return _strip_header(rows), note


def _strip_header(rows):
    """剥掉文件开头的前言和表头。

    GKP 网页版导出的 CSV 长这样 —— 前两行是标题和日期,第三行才是真表头:
        关键字统计信息
        2026-09-20
        Keyword <Tab> Avg. monthly searches <Tab> Competition
        conveyor roller <Tab> 4400 <Tab> High
    所以先按「数据区是几列」判断,把开头那些列数明显偏少的前言行逐行扔掉,
    再扔掉一行表头。单列词表(每行就一个词)不会被误伤。
    """
    out = list(rows)
    if not out:
        return out

    # 数据区的典型列数:取后半部分的众数,避开开头的前言行
    tail = out[max(0, len(out) // 2):]
    widths = {}
    for r in tail:
        widths[len(r)] = widths.get(len(r), 0) + 1
    modal = max(widths, key=widths.get) if widths else 1

    # 前言行:列数比数据区少,且还剩得下数据
    while len(out) > 1 and modal > 1 and len(out[0]) < modal:
        out.pop(0)

    # 表头行:比较时去掉全部空白,「词 / 模式」和「词/模式」都能命中
    if out and out[0]:
        first = re.sub(r"\s+", "", str(out[0][0])).lower()
        if first in HEADER_WORDS:
            out.pop(0)
    return out


def _lines(text):
    """整行返回,不像 parse_keyword_text 那样只取第一列 ——
    客户原始词那一栏允许写「词<Tab>中文<Tab>级别」,后两段不能被丢掉。"""
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]


def _finish(job, rows, prefix, to_usd=False, rate=None):
    """统一收尾:按需换算币种、落 CSV,回传前 200 行给界面预览。"""
    if not rows:
        job.log("没有拿到任何数据")
        return {"count": 0, "preview": [], "csv": None}
    rate = float(rate or config.get("defaults.usd_rate", 7.0))
    raw_cur = rows[0].get("货币")
    rows, cur = gkp.apply_currency(rows, to_usd, rate)
    if to_usd and raw_cur != cur:
        job.log("出价已按 1 USD = %.2f %s 换算,高价值阈值同步切到 USD 档" % (rate, raw_cur))
    elif raw_cur != "USD":
        job.log("出价单位是账号币种 %s(Google Ads 的出价跟查哪个市场无关)" % raw_cur)
    path = gkp.save_csv(rows, prefix, cur)
    job.log("已导出 %d 行 -> %s" % (len(rows), path.name))
    cols = gkp.display_columns(cur)
    keymap = dict(zip(gkp.DISPLAY_KEYS, cols))
    preview = [{keymap[k]: r.get(k, "") for k in gkp.DISPLAY_KEYS} for r in rows[:200]]
    return {"count": len(rows), "columns": cols, "currency": cur,
            "preview": preview, "csv": path.name,
            "truncated": len(rows) > 200}


def serve():
    host = config.get("server.host", "127.0.0.1")
    port = int(config.get("server.port", 8790))
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = "http://%s:%d" % (host, port)
    print("互旦 SEO 工作台 v%s" % VERSION)
    print("控制台:%s" % url)
    if not config.LOCAL.exists():
        print("\n[提示] 还没有 config.local.yaml —— 复制 config.example.yaml "
              "改名后填凭据,界面上的「设置」页有说明。")
    print("按 Ctrl+C 退出\n")
    if config.get("server.open_browser", True):
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
