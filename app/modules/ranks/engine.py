# -*- coding: utf-8 -*-
"""DataForSEO 排名取数引擎。

两种计费模式,拿的是**同一个 advanced 端点的数据**,位次口径完全一致:

  standard(队列,主力)  depth=3 时约 $0.0015/词。批量下单 100 词/次,
                       队列延迟是常数,1000 词也是十分钟量级。
  live(实时)           约 $0.005/词,且**逐词同步、实测 11 秒/词**。
                       只在「就查几个词、要马上看结果」时才值得用。

计价公式 base × (1 + 0.75×(页数-1)):第 1 页原价,后续页 75 折。
base 为 live $0.002 / standard $0.0006。

**错误分级是这里最容易写错的地方**:只有认证失败(401)和余额不足(402/40200)
才是终止级、要整轮停。`40101 Internal SE Server Error` 和 `40106 Task completed
with partial results` 都是**临时抖动,重试即恢复** —— 早期版本把 40101 当成终止级,
一遇到就整轮停还误报「余额不足」,其实余额充足。
"""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = "https://api.dataforseo.com/v3/serp/google/organic"
URL_POST = ROOT + "/task_post"
URL_READY = ROOT + "/tasks_ready"
URL_GET = ROOT + "/task_get/advanced/"
URL_LIVE = ROOT + "/live/advanced"

GL_TO_LOCATION = {
    "us": "United States", "gb": "United Kingdom", "uk": "United Kingdom",
    "de": "Germany", "fr": "France", "it": "Italy", "es": "Spain",
    "nl": "Netherlands", "ca": "Canada", "au": "Australia", "jp": "Japan",
    "kr": "South Korea", "in": "India", "br": "Brazil", "mx": "Mexico",
    "ru": "Russia", "sg": "Singapore", "ae": "United Arab Emirates",
    "th": "Thailand", "vn": "Vietnam", "id": "Indonesia", "pl": "Poland",
    "se": "Sweden", "tr": "Turkey", "za": "South Africa", "cn": "China",
}


class RankError(RuntimeError):
    """普通失败。**必须是 Exception 的子类** —— 原版这里抛 SystemExit,
    那属于 BaseException,在作业线程里会绕过异常处理让作业静默卡死。"""


class Blocked(RankError):
    """终止级:认证失败或余额不足,整轮停,别再烧钱。"""


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class DataForSeo:
    TERMINAL_HTTP = {401, 402}
    TERMINAL_CODES = {40200}
    QUEUE_CODES = {40601, 40602}      # 任务还在队列里,不是错误

    def __init__(self, login, password, *, gl="us", hl="en", device="desktop",
                 depth=3, location=None, location_code=None, mode="standard",
                 batch=100, poll_sec=15, timeout_sec=1800, journal=None, log=None):
        if not login or not password:
            raise RankError("排名监控需要 DataForSEO 账号密码 —— 去「设置」里补 config.local.yaml")
        self.auth = "Basic " + base64.b64encode(
            ("%s:%s" % (login, password)).encode()).decode()
        self.location = location or GL_TO_LOCATION.get((gl or "us").lower(), "United States")
        self.location_code = location_code
        self.language = hl or "en"
        self.device = device or "desktop"
        self.pages = max(1, int(depth or 3))
        self.depth = self.pages * 10
        self.mode = mode
        self.batch = max(1, min(100, int(batch)))
        self.poll = max(5, int(poll_sec))
        self.timeout = max(60, int(timeout_sec))
        self.journal = Path(journal) if journal else None
        self.log = log or (lambda m: None)
        self.total_cost = 0.0
        self.posted = 0
        self.reused = 0
        self.cost_by_kw = {}
        self._opener = urllib.request.build_opener()

    # ---------------------------------------------------------- 价格
    @staticmethod
    def unit_price(pages, mode="standard"):
        base = 0.0006 if mode == "standard" else 0.002
        return round(base * (1 + 0.75 * (max(1, pages) - 1)), 6)

    def estimate(self, n):
        return round(self.unit_price(self.pages, self.mode) * max(0, n), 4)

    # ---------------------------------------------------------- journal
    def _sig(self):
        return "|".join(str(x) for x in (
            self.location_code or self.location, self.language, self.device, self.depth))

    def _load_journal(self):
        if not self.journal:
            return {}
        try:
            data = json.loads(self.journal.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if data.get("sig") != self._sig():
            if data.get("tasks"):
                self.log("上次遗留 %d 个任务的采样参数跟这次不一致,已丢弃"
                         % len(data["tasks"]))
            return {}
        return dict(data.get("tasks") or {})

    def _save_journal(self, tasks):
        if not self.journal:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_text(json.dumps(
            {"sig": self._sig(), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "tasks": tasks}, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------------------------------------------------------- HTTP
    def _request(self, url, payload=None, tries=3):
        data = json.dumps(payload).encode() if payload is not None else None
        last = None
        for attempt in range(tries):
            req = urllib.request.Request(
                url, data=data,
                headers={"Authorization": self.auth, "Content-Type": "application/json"})
            try:
                return json.loads(self._opener.open(req, timeout=90).read().decode())
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:200]
                if e.code in self.TERMINAL_HTTP:
                    raise Blocked("DataForSEO 认证失败或余额不足 (HTTP %d): %s" % (e.code, detail))
                last = "HTTP %d: %s" % (e.code, detail)
            except Exception as e:
                last = repr(e)
            time.sleep(1.0 * (attempt + 1))
        raise RankError("DataForSEO 请求多次失败(临时): %s" % last)

    def _check_top(self, body):
        code = body.get("status_code")
        if code != 20000:
            if code in self.TERMINAL_CODES:
                raise Blocked("DataForSEO 认证/余额错误 %s: %s"
                              % (code, body.get("status_message")))
            raise RankError("DataForSEO 临时错误 %s: %s"
                            % (code, body.get("status_message")))
        return body

    def _task_body(self, keyword):
        t = {"keyword": keyword, "language_code": self.language,
             "device": self.device, "depth": self.depth, "tag": "hudoo-seo-desk"}
        if self.location_code:
            t["location_code"] = int(self.location_code)
        else:
            t["location_name"] = self.location
        return t

    @staticmethod
    def _organic(task):
        """抽出 organic 结果,按 rank_absolute 排序 —— 只认自然结果,广告位不算。"""
        results = task.get("result") or []
        items = (results[0].get("items") if results else None) or []
        organic = [it for it in items if it.get("type") == "organic"]
        organic.sort(key=lambda it: it.get("rank_absolute", 1e9))
        return [{"url": it["url"], "title": it.get("title", "")}
                for it in organic if it.get("url")]

    # ---------------------------------------------------------- 主流程
    def fetch_many(self, keywords, progress=None):
        if self.mode == "live":
            return self._fetch_live(keywords, progress)
        return self._fetch_standard(keywords, progress)

    def _fetch_live(self, keywords, progress):
        out = {}
        want = list(dict.fromkeys(keywords))
        for i, kw in enumerate(want, 1):
            try:
                body = self._check_top(self._request(URL_LIVE, [self._task_body(kw)]))
                self.total_cost += float(body.get("cost") or 0)
                task = (body.get("tasks") or [{}])[0]
                out[kw] = {"status": "ok", "results": self._organic(task), "msg": ""}
            except Blocked:
                raise
            except Exception as e:
                out[kw] = {"status": "error", "results": [], "msg": str(e)[:120]}
            self.cost_by_kw[kw] = self.unit_price(self.pages, "live")
            if progress:
                progress(i, len(want), kw, out[kw]["status"])
        return out

    def _fetch_standard(self, keywords, progress):
        out = {}
        want = list(dict.fromkeys(keywords))
        total = len(want)

        # 1) 先捡回上次没取完的任务 —— 已经付过费,绝不重复下单
        pending = {tid: kw for tid, kw in self._load_journal().items() if kw in want}
        if pending:
            self.reused = len(pending)
            for kw in pending.values():
                self.cost_by_kw[kw] = 0.0
            self.log("复用上次未取回的 %d 个任务(已付过费,不重复扣费)" % len(pending))

        # 2) 其余的批量下单
        todo = [kw for kw in want if kw not in set(pending.values())]
        for chunk in _chunks(todo, self.batch):
            body = self._check_top(self._request(
                URL_POST, [self._task_body(k) for k in chunk]))
            self.total_cost += float(body.get("cost") or 0)
            for t in (body.get("tasks") or []):
                tid = t.get("id")
                kw = ((t.get("data") or {}).get("keyword"))
                if tid and kw:
                    pending[tid] = kw
                    self.posted += 1
                    self.cost_by_kw[kw] = self.unit_price(self.pages, "standard")
            # 下单成功立刻落盘 —— 断网/崩溃/Ctrl-C 之后能原样取回,不重复付钱
            self._save_journal(pending)
            self.log("已下单 %d 个词(累计 %d)" % (len(chunk), self.posted))

        # 3) 轮询取回
        done = 0
        deadline = time.time() + self.timeout
        while pending and time.time() < deadline:
            ready = set()
            try:
                body = self._request(URL_READY)
                for t in (body.get("tasks") or []):
                    for r in (t.get("result") or []):
                        if r.get("id") in pending:
                            ready.add(r["id"])
                    if t.get("id") in pending:
                        ready.add(t["id"])
            except Exception as e:
                self.log("轮询出错(临时,继续等): %s" % str(e)[:80])

            for tid in list(ready or pending.keys())[:self.batch]:
                kw = pending.get(tid)
                if not kw:
                    continue
                try:
                    body = self._request(URL_GET + tid, tries=2)
                    task = (body.get("tasks") or [{}])[0]
                    code = task.get("status_code")
                    if code in self.QUEUE_CODES:
                        continue                      # 还没好,下一轮再来
                    if code != 20000:
                        # 40101 / 40106 属于临时抖动,原样重试即可
                        out[kw] = {"status": "error", "results": [],
                                   "msg": "%s %s" % (code, task.get("status_message"))}
                    else:
                        out[kw] = {"status": "ok", "results": self._organic(task), "msg": ""}
                except Blocked:
                    raise
                except Exception as e:
                    out[kw] = {"status": "error", "results": [], "msg": str(e)[:120]}
                pending.pop(tid, None)
                done += 1
                if progress:
                    progress(done, total, kw, out[kw]["status"])
            self._save_journal(pending)
            if pending:
                time.sleep(self.poll)

        for kw in pending.values():
            out[kw] = {"status": "error", "results": [], "msg": "超时未取回(下轮自动取回,不重复扣费)"}
        return out
