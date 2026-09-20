# -*- coding: utf-8 -*-
"""后台作业 + 实时日志。

界面点一下按钮就起一个 Job,前端按 since 游标轮询增量日志。
脱胎自 rank-tracker 的 _run_job/_append,但改成线程内直接跑函数,
不再起子进程 —— 取数逻辑本来就是库函数,没必要多一层。
"""
import threading
import time
import traceback
import uuid

_LOCK = threading.Lock()
_JOBS = {}
MAX_KEEP = 40


class Job:
    def __init__(self, name):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.status = "running"      # running / done / error
        self.lines = []
        self.result = None
        self.error = None
        self.started = time.time()
        self.finished = None

    def log(self, msg):
        with _LOCK:
            self.lines.append("%s  %s" % (time.strftime("%H:%M:%S"), msg))

    def snapshot(self, since=0):
        with _LOCK:
            return {
                "id": self.id,
                "name": self.name,
                "status": self.status,
                "lines": self.lines[since:],
                "total_lines": len(self.lines),
                "result": self.result,
                "error": self.error,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
            }


def _prune():
    if len(_JOBS) <= MAX_KEEP:
        return
    done = sorted([j for j in _JOBS.values() if j.status != "running"],
                  key=lambda j: j.finished or 0)
    for j in done[:len(_JOBS) - MAX_KEEP]:
        _JOBS.pop(j.id, None)


def start(name, fn):
    """fn(job) -> result(可 JSON 序列化)。异常会被抓住并记进 job.error。"""
    job = Job(name)
    with _LOCK:
        _JOBS[job.id] = job
        _prune()

    def runner():
        try:
            job.result = fn(job)
            job.status = "done"
        except Exception as e:
            job.error = "%s: %s" % (type(e).__name__, e)
            job.status = "error"
            job.log("[出错] " + job.error)
            for ln in traceback.format_exc().splitlines()[-6:]:
                job.log("  " + ln)
        finally:
            job.finished = time.time()

    threading.Thread(target=runner, daemon=True, name="job-" + job.id).start()
    return job


def get(job_id):
    return _JOBS.get(job_id)


def listing():
    with _LOCK:
        return [{"id": j.id, "name": j.name, "status": j.status,
                 "started": j.started,
                 "elapsed": round((j.finished or time.time()) - j.started, 1)}
                for j in sorted(_JOBS.values(), key=lambda x: -x.started)]
