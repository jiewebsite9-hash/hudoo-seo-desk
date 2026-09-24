# -*- coding: utf-8 -*-
"""本地 HTTP 服务。只监听 127.0.0.1,不对外。

前端是普通网页,后端是标准库 http.server —— 零额外依赖,打包体积小,
和 rank-tracker 一脉相承。
"""
import hmac
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


def _disposition(name):
    """拼 Content-Disposition。

    **文件名必须百分号编码。** HTTP 头按 latin-1 编码,直接塞中文会在
    send_header 里抛 UnicodeEncodeError —— 表现不是报错页,而是**连接被直接掐断**
    (客户端看到 RemoteDisconnected / 「下载失败」),很难猜到是文件名的问题。
    本程序的产出大量是中文名(周报_*.md、SOP工作簿_*.xlsx、剔除清单_*.xlsx),
    所以这里必须处理。

    同时给一个纯 ASCII 的 filename= 兜底,老客户端不认 filename* 时还能存下来。
    """
    ascii_name = name.encode("ascii", "ignore").decode("ascii").strip(" .") or "download"
    ascii_name = ascii_name.replace('"', "").replace("\\", "")
    return ('attachment; filename="%s"; filename*=UTF-8\'\'%s'
            % (ascii_name, quote(name, safe="")))


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
            self.send_header("Content-Disposition", _disposition(download_name))
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


    # ---------------------------------------------------------- 访问控制
    def _allowed(self):
        """本机随便访问;**非本机必须带口令**。

        这个程序手上有 Google Ads、DataForSEO、DeepSeek、飞书四套凭据,
        其中排名查询和 LLM 调用是**花钱**的。绑到 0.0.0.0 供全组用的时候,
        没有这道门就等于把公司的账单和客户数据交给整个内网。

        口令从 config 的 server.access_token 读;X-Access-Token 头或 ?t= 都认
        (界面把口令存在 localStorage,之后每个请求自动带头)。
        """
        host = (self.client_address or ["", 0])[0]
        if host in ("127.0.0.1", "::1", "localhost"):
            return True
        want = str(config.get("server.access_token", "") or "")
        if not want:
            return False
        got = (self.headers.get("X-Access-Token")
               or parse_qs(urlparse(self.path).query).get("t", [""])[0] or "")
        # 逐字符等价比较,不要用 == 短路(时序侧信道)
        return hmac.compare_digest(str(got), want)

    def _deny(self):
        return self._json({"error": "需要访问口令。请向管理员要访问链接"
                                    "(形如 http://内网IP:8790/?t=口令)。"}, 403)

    # ---------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path

        if not self._allowed():
            return self._deny()

        if p in ("/", "/index.html"):
            return self._file(web_dir() / "index.html")
        if p == "/app.js":
            return self._file(web_dir() / "app.js")
        if p == "/social.js":
            return self._file(web_dir() / "social.js")

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
                             _disposition(name))
            self.end_headers()
            return self.wfile.write(data)

        if p == "/api/gsc/sites":
            from app.modules.gsc import client as gsc
            if not gsc.configured():
                return self._json({"sites": [], "configured": False})
            try:
                return self._json({"sites": gsc.sites(), "configured": True})
            except gsc.GscError as e:
                return self._json({"error": str(e), "configured": True}, 400)

        if p == "/api/ranks/balance":
            # 查余额本身免费,但别让刷一次页面就打一发 —— 缓存 60 秒。
            # ?force=1 可以跳过缓存(刚充完值想立刻看到)。
            import time as _t
            from app.modules.ranks import engine
            now = _t.time()
            c = getattr(Handler, "_bal_cache", None)
            if c and now - c[0] < 60 and not q.get("force"):
                return self._json(dict(c[1], cached=True))
            try:
                d = engine.balance()
            except Exception as e:
                return self._json({"error": str(e)}, 400)
            Handler._bal_cache = (now, d)
            return self._json(dict(d, cached=False))

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

        if p == "/api/skills/list":
            from app.modules import skillpack
            return self._json({"dir": str(config.skills_dir()),
                               "home_skills": str(config.HOME / "skills"),
                               "skills": skillpack.list_skills()})

        if p == "/api/skills/export":
            from app.modules import skillpack
            names = [n for n in (q.get("name") or []) if n]
            try:
                data, ns, nf = skillpack.export_zip(names or None)
            except skillpack.SkillPackError as e:
                return self._json({"error": str(e)}, 400)
            from datetime import datetime
            fn = "skill包_%d个_%s.zip" % (ns, datetime.now().strftime("%Y%m%d"))
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", _disposition(fn))
            self.end_headers()
            return self.wfile.write(data)

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

    def _social_upload(self):
        """运营专员上传的后台导出包(zip 或单个文件)。

        解析 + 算指标 + 口径体检,**不调 AI**,所以很快,界面可以先看预览再决定出稿。
        解析结果缓存在服务端,出稿时按 upload_id 取,免得把几十张表回传给浏览器再传回来。
        """
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return self._json({"error": "没收到文件内容"}, 400)
        if n > 60 * 1024 * 1024:
            return self._json({"error": "整包超过 60MB"}, 400)
        raw = self.rfile.read(n)
        name = unquote(self.headers.get("X-Filename") or "上传.zip")

        from app.modules.social import report as social
        lines = []
        try:
            analysis = social.analyze(raw, name, log=lines.append)
        except Exception as e:
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 400)

        uid = social_cache.put(analysis)
        clients = []
        for c in social.clients_of(analysis):
            plats = [{"platform": k[1],
                      "cn": social.platforms.PLATFORM_CN[k[1]],
                      "start": social.metrics.period(analysis["bundles"][k])[0],
                      "end": social.metrics.period(analysis["bundles"][k])[1],
                      "posts": analysis["totals"][k].get("posts", 0)}
                     for k in analysis["bundles"] if k[0] == c]
            clients.append({"client": c, "platforms": plats})
        return self._json({"upload_id": uid, "clients": clients,
                           "issues": analysis["issues"], "log": lines,
                           "feishu_ready": social_feishu_ready()})

    # ---------------------------------------------------------- POST
    def do_POST(self):
        p = urlparse(self.path).path

        if not self._allowed():
            return self._deny()

        # 必须在 _body() 之前 —— 文件上传的请求体是二进制,
        # 一旦被 _body() 按 JSON 读掉,这里再读 Content-Length 就会永久阻塞。
        if p == "/api/parse-file":
            return self._parse_file()

        # 同理:zip 是二进制,不能先被 _body() 当 JSON 读掉
        if p == "/api/skills/import":
            from app.modules import skillpack
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return self._json({"error": "没收到文件内容"}, 400)
            if n > 60 * 1024 * 1024:
                return self._json({"error": "文件超过 60MB"}, 400)
            raw = self.rfile.read(n)
            try:
                r = skillpack.import_zip(raw)
            except skillpack.SkillPackError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 400)
            return self._json(r)

        # 同理,社媒数据包也是二进制上传,必须排在 _body() 之前
        if p == "/api/social/upload":
            return self._social_upload()

        b = self._body()

        if p == "/api/keywords/check":
            job = jobs.start("自检 Google Ads 连接", lambda j: gkp.check(j))
            return self._json({"job": job.id})

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

        if p == "/api/social/generate":
            from app.modules.social import report as social
            analysis = social_cache.get(b.get("upload_id"))
            if not analysis:
                return self._json({"error": "上传记录已过期，请重新上传数据包"}, 400)
            client = (b.get("client") or "").strip()
            if client not in social.clients_of(analysis):
                return self._json({"error": "没有这个客户：%s" % client}, 400)
            use_ai = b.get("use_ai", True)
            push = bool(b.get("push_feishu"))

            def run(j):
                return social_generate(j, analysis, client, use_ai, push)

            return self._json({"job": jobs.start("出周报 · " + client, run).id})

        if p == "/api/social/history":
            from app.modules.social import store
            return self._json({"rows": store.history(b.get("client"), b.get("platform"))})

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

        if p == "/api/gsc/auth":
            # 本机弹浏览器走 OAuth,拿 webmasters.readonly 的 refresh_token
            from app.modules.gsc import client as gsc

            def run(j):
                gsc.authorize(open_browser=True, log=j.log)
                ss = gsc.sites()
                j.log("授权完成,这个账号能看到 %d 个资源" % len(ss))
                return {"count": len(ss), "columns": ["资源", "权限"],
                        "preview": [{"资源": x["site"], "权限": x["permission"]} for x in ss],
                        "csv": None, "stats": {"资源数": len(ss)}}

            return self._json({"job": jobs.start("授权 Search Console", run).id})

        if p == "/api/gsc/report":
            site = (b.get("site") or "").strip()
            name = (b.get("client") or "").strip()
            if not site:
                return self._json({"error": "先选一个 GSC 资源。"}, 400)

            def run(j):
                return _gsc_report(j, site, name or _site_name(site), (b.get("end") or "").strip() or None,
                                   [x.strip() for x in str(b.get("brand") or "").split(",") if x.strip()],
                                   bool(b.get("use_ai", True)), bool(b.get("push", True)))

            return self._json({"job": jobs.start("出 GSC 周报", run).id})

        if p == "/api/ranks/report":
            # 拿最近一轮结果出飞书文档 + 私聊链接,不重新检查
            domain = (b.get("domain") or "").strip()
            if not domain:
                return self._json({"error": "域名不能为空。"}, 400)

            def run(j):
                return _rank_report(j, domain, bool(b.get("push", True)))

            return self._json({"job": jobs.start("出飞书排名汇报", run).id})

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
                    only_failed=bool(b.get("only_failed")),
                    kw_source=b.get("kw_source"), job=j)
                if b.get("writeback"):
                    try:
                        wb = tracker.writeback(b.get("domain") or "", job=j)
                        stats["写回"] = "%d 行 -> %s" % (wb["written"], wb["field"])
                    except Exception as e:
                        j.log("[写回失败] %s(排名数据已存好,可以单独重试写回)" % str(e)[:120])
                if b.get("push"):
                    try:
                        rep = _rank_report(j, b.get("domain") or "", push=True)
                        if rep.get("doc_url"):
                            stats["飞书文档"] = rep["doc_url"]
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
            from app.modules.keywords import sop, derive, learn
            geos = [g for g in (b.get("geos") or []) if g]

            def run(j):
                market = (geos[0] if geos else "US")
                lang = b.get("lang") or "en"
                minv = int(b.get("min_volume") or 0)
                mixed = gkp.parse_keyword_text(b.get("mixed"))
                exclude = gkp.parse_keyword_text(b.get("exclude"))
                col = sop.collect(
                    competitor_sites=gkp.parse_keyword_text(b.get("sites")),
                    client_site=(b.get("client_site") or "").strip() or None,
                    customer_words=_lines(b.get("customer")),
                    market=market, lang=lang,
                    expand_customer=bool(b.get("expand_customer", True)), job=j)
                met = sop.fetch(col, job=j)

                # ---- AI 筛词:在取数之后、打分之前 ----
                # 样本按主市场月搜从高到低取前 300:量大的词代表候选池的主体,清单准不准看它们。
                ai, soft, core = None, [], []
                material = (b.get("material") or "").strip()
                if b.get("auto_lists", True) and len(material) >= 30:
                    ranked = sorted(col["pool"].values(), key=lambda w: -float(
                        (met["local"].get(sop.gkey(w)) or {}).get("月均搜索量") or 0))
                    ai = derive.derive(material, sample_words=ranked[:300],
                                       extra=b.get("extra"), job=j)
                    # AI 剔除模式一旦命中客户词,就是它泛化过头的铁证(实测把「不生产皮带轮」
                    # 泛化成 *pulley*,连 conveyor pulley 都剔)。整条停用,界面上注明,人要用再勾回。
                    cust_words = list(col["customer"].values())
                    conflicts = []
                    for e in ai["exclude"]:
                        hits = [w for w in cust_words if sop.make_mixed_matcher([e["pattern"]])(w)]
                        if hits:
                            e["use"] = False
                            e["conflict"] = "命中客户词 " + " / ".join(hits[:3])
                            conflicts.append("%s(%s)" % (e["pattern"], hits[0]))
                    # 混杂模式只压 P1 不删词,不停用,但标出命中了哪些客户词 —— *steel* 一条
                    # 盖住 5 个客户产品词,过宽与否让人一眼看出来
                    for m in ai["mixed"]:
                        hits = [w for w in cust_words if sop.make_mixed_matcher([m["pattern"]])(w)]
                        if hits:
                            m["cust_hits"] = hits
                    if conflicts:
                        msg = ("AI 有 %d 条剔除模式命中了客户自己给的词,说明泛化过头,已停用:%s"
                               % (len(conflicts), "、".join(conflicts[:5])))
                        ai.setdefault("warnings", []).append(msg)
                        j.log("[当心] " + msg)
                    exclude = exclude + [e["pattern"] for e in ai["exclude"] if e.get("hard") and e.get("use", True)]
                    soft = [e["pattern"] for e in ai["exclude"] if not e.get("hard") and e.get("use", True)]
                    mixed = mixed + [m["pattern"] for m in ai["mixed"]]
                    core = list(ai["core"])
                    strat = [x["keyword"] for x in ai["strategy"]]
                    if strat:
                        n = sop.add_words(col, met, strat, "行业速通/策略词(AI)", job=j)
                        j.log("AI 策略词 %d 个,其中 %d 个是池子里没有的,已补数据入池" % (len(strat), n))
                    if (b.get("save_as") or "").strip():
                        rows_, metrics_, core_, note_ = derive.to_list_payload(ai, b["save_as"], "")
                        j.log("已存进清单库:%s" % learn.save_list(
                            b["save_as"].strip(), rows_, metrics_, core_, note=note_ or "一键流水线生成"))
                elif b.get("auto_lists", True):
                    j.log("客户资料不足 30 字,跳过 AI 筛词 —— 只用手动清单")

                reasons = {e["pattern"]: e.get("reason", "") for e in (ai["exclude"] if ai else [])}
                header, rows, cut, stats = sop.assemble(
                    col, met, mixed_words=mixed, exclude_words=exclude,
                    soft_exclude=soft, core=core, reasons=reasons, min_volume=minv,
                    usd_rate=b.get("usd_rate") or None, job=j)
                # ---- AI 布词:填 布局角色 / 目标URL / 页面类型 / 页面状态 / 该页主词,出第 3 页 ----
                lay = None
                client_site = (b.get("client_site") or "").strip()
                if b.get("auto_layout", True) and client_site:
                    from app.modules.keywords import layout
                    try:
                        rows, plan, pv, info = layout.run(client_site, rows, "{市场}月搜", extra=b.get("extra"), log=j.log)
                        assigned = info.pop("assigned")
                        stats["_pivot"] = pv
                        lay = {"plan": plan, "assigned": assigned}
                        info["pivot"] = pv[:80]
                    except Exception as e:
                        # 布词失败不能把前面的活一起废掉:表照出,只是那几列空着
                        j.log("[布词失败] %s —— 总表照常导出,布词列留空" % str(e)[:160])
                        info = None
                elif b.get("auto_layout", True):
                    j.log("没填客户网址,跳过 AI 布词")
                    info = None
                else:
                    info = None
                # 缓存取数结果:改清单只重打分,不重新取数;布词结果一起缓存
                j.cache = {"col": col, "met": met, "layout": lay}
                res = _sop_result(j, sop, header, rows, cut, stats,
                                  {"market": market, "lang": lang, "min_volume": minv, "mixed": mixed})
                if info:
                    res["layout"] = info
                res["lists_used"] = {"mixed": mixed, "exclude": exclude, "soft": soft, "core": core}
                if ai:
                    res["ai"] = {"exclude": ai["exclude"], "mixed": ai["mixed"],
                                 "strategy": ai["strategy"], "core": ai["core"],
                                 "warnings": ai.get("warnings") or [], "cost": ai.get("cost"),
                                 "preview": (ai.get("preview") or {}).get("rows") or []}
                return res

            return self._json({"job": jobs.start("拓词 → AI 筛词 → 总表", run).id})

        if p == "/api/keywords/rescore":
            # 改清单重打分:用上一轮缓存的取数结果,不碰 GKP,秒出
            from app.modules.keywords import sop
            prev = jobs.get(b.get("job") or "")
            cache = getattr(prev, "cache", None) if prev else None
            if not cache:
                return self._json({"error": "上一轮的取数结果已经不在了(程序重启过或作业被清理),请重新生成。"}, 400)

            def run(j):
                minv = int(b.get("min_volume") or 0)
                mixed = gkp.parse_keyword_text(b.get("mixed"))
                exclude = gkp.parse_keyword_text(b.get("exclude"))
                soft = [str(x) for x in (b.get("soft") or []) if str(x).strip()]
                core = [str(x) for x in (b.get("core") or []) if str(x).strip()]
                reasons = {str(k): str(v) for k, v in (b.get("reasons") or {}).items()}
                header, rows, cut, stats = sop.assemble(
                    cache["col"], cache["met"], mixed_words=mixed, exclude_words=exclude,
                    soft_exclude=soft, core=core, reasons=reasons, min_volume=minv,
                    usd_rate=b.get("usd_rate") or None, job=j)
                lay = cache.get("layout")
                if lay:
                    from app.modules.keywords import layout
                    rows = layout.apply(rows, lay["plan"], lay["assigned"], "{市场}月搜", log=j.log)
                    stats["_pivot"] = layout.pivot(rows, lay["plan"], "{市场}月搜")
                j.cache = cache
                res = _sop_result(j, sop, header, rows, cut, stats,
                                  {"market": cache["col"]["market"], "lang": cache["col"]["lang"],
                                   "min_volume": minv, "mixed": mixed})
                if lay:
                    pv = stats["_pivot"]
                    res["layout"] = {"existing": sum(1 for p in lay["plan"] if p["status"] == "已有"),
                                     "new": sum(1 for p in lay["plan"] if p["status"] == "待建"),
                                     "orphans": sum(1 for r in rows if r.get("优先级") != "—" and not r.get("目标URL")),
                                     "cost": 0, "pages_crawled": None, "pivot": pv[:80]}
                res["lists_used"] = {"mixed": mixed, "exclude": exclude, "soft": soft, "core": core}
                return res

            return self._json({"job": jobs.start("重打分(不取数)", run).id})

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

    # ---- xls(老 Excel,OLE2)----
    # 客户发来的词表十有八九是这个格式。按文件头判,不只看后缀 ——
    # 有人会把 xls 改名成 xlsx,改名不改格式,openpyxl 一样打不开。
    if low.endswith(".xls") or raw[:4] == b"\xd0\xcf\x11\xe0":
        import xlrd
        wb = xlrd.open_workbook(file_contents=raw)
        sh = wb.sheet_by_index(0)
        rows = []
        for i in range(sh.nrows):
            cells = []
            for c in sh.row(i):
                v = c.value
                # 数字列 xlrd 一律给 float:序号 1 读成 1.0。下游剥序号只认 \d+,
                # 1.0 会漏过去变成关键词 —— 整数值一律还原成 int。
                if c.ctype == 2 and float(v).is_integer():
                    v = int(v)
                # 单元格内换行(Alt+Enter)必须压平:下游按行切,一个格子两行就变两个词
                cells.append(str(v).replace("\r", "").replace("\n", "；").strip())
            while cells and not cells[-1]:
                cells.pop()
            if cells and any(cells):
                rows.append(cells)
        return _strip_header(rows), "xls · 工作表「%s」" % sh.name

    # ---- xlsx ----
    if low.endswith((".xlsx", ".xlsm")):
        import io
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = []
        for r in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).replace("\r", "").replace("\n", "；").strip() for c in r]
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


def _site_name(site):
    return site.replace("sc-domain:", "").replace("https://", "").replace("http://", "").strip("/")


def _gsc_report(j, site, client_name, end, brand, use_ai, push):
    """GSC 周报:取数 → 判定 → AI 写字 → markdown 落盘 → 飞书文档(业主编辑权)→ 私聊。"""
    from app.modules.gsc import report as gr
    from app.modules.ranks import feishu
    from app.modules.social import feishu_docs
    title, md, facts, text, cost = gr.build(site, client_name, end=end, brand=brand, use_ai=use_ai, log=j.log)
    w = facts["window"]
    path = config.out_dir() / ("GSC周报_%s_%s-%s.md" % (_site_name(site), w["start"].replace("-", ""), w["end"].replace("-", "")))
    path.write_text(md, encoding="utf-8")
    j.log("已导出 %s" % path.name)
    url = None
    if feishu_docs.configured():
        folder = config.get("gsc.folder_token") or config.get("social.folder_token") or None
        owner = config.get("social.owner_open_id") or None
        try:
            url = feishu_docs.create(md, title, folder_token=folder, owner_open_id=owner, log=j.log)["url"]
        except Exception as e:
            j.log("建飞书文档失败(%s: %s);markdown 已导出,可手动上传" % (type(e).__name__, e))
    if push:
        c = facts["cur"]
        snap = {x["指标"]: x for x in facts["snapshot"]}
        msg = ["【GSC 周同步】%s  %s ~ %s" % (client_name, w["start"], w["end"]),
               "整体:%s" % text["status"],
               "曝光 %s(%s)· 点击 %s(%s)· CTR %.1f%%(%s)· 平均排名 %.1f(%s)" % (
                   "{:,}".format(c["impressions"]), snap["曝光"]["环比"], "{:,}".format(c["clicks"]), snap["点击"]["环比"],
                   c["ctr"], snap["CTR"]["环比"], c["position"], snap["平均排名"]["环比"])]
        if facts["diverge"]:
            msg.append("背离:" + "、".join(facts["diverge"]))
        if text["conclusion"]:
            msg.append(text["conclusion"][0])
        if url:
            msg.append("文档:" + url)
        try:
            feishu.push("\n".join(msg), log=j.log)
        except Exception as e:
            j.log("推送失败:%s" % str(e)[:160])
    rows = [dict(r, 维度="查询词↑") for r in facts["queries_up"]] + [dict(r, 维度="查询词↓") for r in facts["queries_down"]] \
         + [dict(r, 维度="页面↑") for r in facts["pages_up"]] + [dict(r, 维度="页面↓") for r in facts["pages_down"]]
    st = {x["指标"]: "%s(%s·%s)" % (x["本周"], x["环比"], x["判定"]) for x in facts["snapshot"]}
    st.update({"整体": text["status"], "背离": "、".join(facts["diverge"]) or "无",
               "AI 花费": ("$%s" % cost) if cost is not None else "未调用"})
    return {"count": len(rows), "columns": ["维度", "对象", "本周点击", "上周点击", "差值", "本周排名", "词性"],
            "preview": rows, "csv": None, "stats": st, "doc_url": url, "md": path.name}


def _rank_report(j, domain, push=True):
    """最近一轮排名 -> markdown 落盘 -> 飞书文档(业主编辑权)-> 私聊链接。"""
    from app.modules.ranks import tracker, feishu
    from app.modules.social import feishu_docs
    title, md, summ = tracker.report_markdown(domain)
    if not summ["total"]:
        raise tracker.RankError("这个域名还没有任何检查记录。")
    path = config.out_dir() / ("排名汇报_%s_%s.md" % (summ["host"], summ["run_date"]))
    path.write_text(md, encoding="utf-8")
    j.log("已导出 %s" % path.name)
    url = None
    if feishu_docs.configured():
        folder = config.get("ranks.folder_token") or config.get("social.folder_token") or None
        owner = config.get("social.owner_open_id") or None
        try:
            doc = feishu_docs.create(md, title, folder_token=folder, owner_open_id=owner, log=j.log)
            url = doc["url"]
        except Exception as e:
            # 建文档失败不能把汇报废掉:markdown 已落盘,简报照发
            j.log("建飞书文档失败(%s: %s);markdown 已导出,可手动上传" % (type(e).__name__, e))
    else:
        j.log("没配飞书凭据,只导出 markdown")
    if push:
        try:
            feishu.push(tracker.report_summary_text(summ, url), log=j.log)
        except Exception as e:
            j.log("推送失败:%s" % str(e)[:160])
    return {"count": len(summ["rows"]), "columns": ["关键词", "排名", "上轮", "变化", "URL", "轮次"],
            "preview": summ["rows"][:200], "csv": None, "truncated": len(summ["rows"]) > 200,
            "stats": {"轮次": summ["run_date"], "前10名": summ["top10"], "前30名": summ["top30"],
                      "未进前30": summ["unranked"], "待补查": summ["failed"],
                      "上升": summ["up"], "下降": summ["down"], "新进": summ["new"], "掉出": summ["out"]},
            "doc_url": url, "md": path.name}


def _sop_result(j, sop, header, rows, cut, stats, params):
    """总表落盘 + 界面预览。一键流水线和重打分共用。"""
    clean = {k: v for k, v in stats.items() if not k.startswith("_")}
    if not rows:
        return {"count": 0, "preview": [], "csv": None, "stats": clean}
    csv_path = sop.save_csv(header, rows)
    xlsx_path = sop.save_workbook(header, rows, cut, stats, params)
    j.log("已导出 -> %s(总表 CSV)" % csv_path.name)
    j.log("已导出 -> %s(4 张表的工作簿,可直接导进飞书)" % xlsx_path.name)
    keymap = dict(zip(sop.COLUMNS, header))
    preview = [{keymap[k]: v for k, v in r.items() if k in keymap} for r in rows[:200]]
    return {"count": len(rows), "columns": header, "preview": preview,
            "csv": csv_path.name, "xlsx": xlsx_path.name,
            "truncated": len(rows) > 200, "stats": clean}


def _lines(text):
    """整行返回,不像 parse_keyword_text 那样只取第一列 ——
    客户原始词那一栏允许写「词<Tab>中文<Tab>级别」,后两段不能被丢掉。"""
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]



# ---------------------------------------------------------------- 社媒周报

class _UploadCache(object):
    """上传解析结果的短期缓存。

    只留最近几次 —— 这些对象里有客户的全部明细数据,没必要长期驻留内存;
    共享部署时也避免几个专员同时传大包把内存吃光。
    """

    LIMIT = 8

    def __init__(self):
        self._d = {}
        self._order = []
        self._lock = threading.Lock()

    def put(self, obj):
        import uuid
        uid = uuid.uuid4().hex[:12]
        with self._lock:
            self._d[uid] = obj
            self._order.append(uid)
            while len(self._order) > self.LIMIT:
                self._d.pop(self._order.pop(0), None)
        return uid

    def get(self, uid):
        with self._lock:
            return self._d.get(uid or "")


social_cache = _UploadCache()


def social_feishu_ready():
    from app.modules.social import feishu_docs
    return feishu_docs.configured()


def social_generate(job, analysis, client, use_ai, push):
    """出一份客户周报,落盘,按需建飞书文档,最后存档。"""
    from app.modules.social import feishu_docs, report as social

    res = social.generate(client, analysis, log=job.log, use_ai=use_ai)
    facts = res["facts"]

    stamp = "%s-%s" % ((facts["period"]["start"] or "").replace("-", ""),
                       (facts["period"]["end"] or "").replace("-", ""))
    fname = "周报_%s_%s.md" % (client, stamp)
    path = config.out_dir() / fname
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(res["markdown"], encoding="utf-8")
    job.log("已导出 %s" % fname)

    doc = None
    if push:
        # 标题里的日期用 2026.09.13 这种写法。直接拿 ISO 日期拼会得到
        # 「2026-09-13-2026-09-20」,一串横杠根本断不开句。
        def dot(d, short=False):
            if not d:
                return "—"
            return d[5:].replace("-", ".") if short else d.replace("-", ".")
        title = str(config.get("social.title",
                               "{client} {label}运营周报 {start}-{end}")).format(
            client=client, label=facts["platform_label"],
            start=dot(facts["period"]["start"]), end=dot(facts["period"]["end"], True))
        folder = config.get("social.folder_token") or None
        owner = config.get("social.owner_open_id") or None
        job.log("飞书目标：文件夹 %s ／ 编辑权给 %s"
                % (folder or "个人空间根目录", owner or "(未配置，不加)"))
        try:
            doc = feishu_docs.create(res["markdown"], title, folder_token=folder,
                                     owner_open_id=owner, log=job.log)
        except Exception as e:
            # 飞书失败不该让整个作业白跑 —— markdown 已经落盘了
            job.log("建飞书文档失败(%s: %s)；markdown 已导出，可手动上传。"
                    % (type(e).__name__, e))

    # **出稿成功后才存档**,避免半截数据污染下期的环比基线
    social.archive(client, analysis)
    job.log("本期汇总已存档，下期起自动带出环比")

    blocks = [i for i in facts["issues"] if i["level"] == "block"]
    return {"client": client, "file": fname, "doc": doc,
            "markdown": res["markdown"][:4000],
            "blockers": blocks, "ai": res["ai"],
            "period": facts["period"]}


def serve():
    # 首次运行:在用户自己的目录里生成一份配置模板(不带任何凭据)
    if config.ensure_local_config():
        print("首次运行,已生成配置文件:%s" % config.LOCAL)
        print("填好里面的凭据再用,界面「设置」页有说明。\n")
    host = config.get("server.host", "127.0.0.1")
    port = int(config.get("server.port", 8790))

    # 绑到非本机地址 = 全组能访问 = 谁都能用你的 Google Ads / DataForSEO / DeepSeek 额度。
    # 没设口令就不让起,免得有人图省事绑了 0.0.0.0 就忘了。
    token_set = str(config.get("server.access_token", "") or "").strip()
    if host not in ("127.0.0.1", "localhost", "::1") and not token_set:
        print("[中断] server.host 设成了 %s(非本机),但 server.access_token 是空的。" % host)
        print("       这个程序持有会花钱的凭据,共享部署必须设访问口令。")
        print("       在 %s 里填 server.access_token,或把 host 改回 127.0.0.1。"
              % config.LOCAL)
        return 2
    # 端口被占时要明确报出来。默认行为是抛 OSError 然后进程静默退出,
    # 而旧实例还在端口上正常应答 —— 表现就是"改了代码重启了却完全没生效",
    # 极难察觉(实测为此白跑了三轮)。
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print("[中断] 端口 %d 起不来:%s" % (port, e))
        print("       多半是上一个实例还在跑。先把它关掉(或改 server.port),再启动。")
        print("       Windows 查占用:netstat -ano | findstr :%d" % port)
        return 2
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
