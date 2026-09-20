# -*- coding: utf-8 -*-
"""本地 HTTP 服务。只监听 127.0.0.1,不对外。

前端是普通网页,后端是标准库 http.server —— 零额外依赖,打包体积小,
和 rank-tracker 一脉相承。
"""
import json
import mimetypes
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

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

    # ---------------------------------------------------------- POST
    def do_POST(self):
        p = urlparse(self.path).path
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
                return _finish(j, rows, "ideas")

            return self._json({"job": jobs.start("拓词", run).id})

        if p == "/api/keywords/volume":
            geos = [g for g in (b.get("geos") or []) if g]
            words = gkp.parse_keyword_text(b.get("keywords"))
            lang = b.get("lang") or "en"

            def run(j):
                rows = gkp.volume(words, geos=geos, lang=lang, job=j)
                return _finish(j, rows, "volume")

            return self._json({"job": jobs.start("补搜索量", run).id})

        return self._json({"error": "没有这个接口"}, 404)


def _finish(job, rows, prefix):
    """统一收尾:落 CSV,回传前 200 行给界面预览,其余走下载。"""
    if not rows:
        job.log("没有拿到任何数据")
        return {"count": 0, "preview": [], "csv": None}
    path = gkp.save_csv(rows, prefix)
    job.log("已导出 %d 行 -> %s" % (len(rows), path.name))
    return {"count": len(rows), "columns": gkp.COLUMNS,
            "preview": rows[:200], "csv": path.name,
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
