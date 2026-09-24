# -*- coding: utf-8 -*-
"""Google Search Console 取数。走 REST,不依赖 googleapiclient(没装,也用不着)。

授权:复用 google_ads 的 client_id / client_secret(同一个 Cloud 项目,要先在项目里
启用「Google Search Console API」),单独授权一次 webmasters.readonly,refresh_token
存 gsc.refresh_token —— 和 Ads 的分开存,两边 scope 不同,混用会互相作废。
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from app import config

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
API = "https://searchconsole.googleapis.com/webmasters/v3"
INSPECT = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
TOKEN_URI = "https://oauth2.googleapis.com/token"
PORT = 8766          # 别和 Ads 授权(8765)撞


class GscError(RuntimeError):
    """普通失败。不用 SystemExit —— 在作业线程里会绕过 except Exception 让作业静默卡死。"""


def configured():
    return bool(config.get("gsc.refresh_token") and config.get("google_ads.client_id")
                and config.get("google_ads.client_secret"))


def authorize(open_browser=True, log=None):
    """跑一次 OAuth,把 refresh_token 写进 config.local.yaml 的 gsc 段。本机弹浏览器登录。"""
    log = log or (lambda m: None)
    cid = str(config.get("google_ads.client_id", "")).strip()
    csec = str(config.get("google_ads.client_secret", "")).strip()
    if not cid or not csec:
        raise GscError("config.local.yaml 里还没填 google_ads.client_id / client_secret(和 Ads 共用一套)。")
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config({
        "installed": {"client_id": cid, "client_secret": csec,
                      "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                      "token_uri": TOKEN_URI, "redirect_uris": ["http://localhost"]}
    }, scopes=SCOPES)
    log("浏览器会打开 Google 登录页 —— 选**能在 Search Console 里看到客户资源**的账号;没弹出来就复制日志里的链接手动开。")
    creds = flow.run_local_server(
        port=PORT, open_browser=open_browser, access_type="offline", prompt="consent",
        authorization_prompt_message="请在浏览器里授权:\n{url}",
        success_message="授权成功,可以关掉这个页面回工作台了。")
    if not creds.refresh_token:
        raise GscError("没拿到 refresh_token。到 myaccount.google.com/permissions 把这个应用的授权删掉再来一次。")
    write_token(creds.refresh_token)
    log("refresh_token 已写入 %s 的 gsc 段" % config.LOCAL)
    return True


def write_token(value, path=None):
    """只改 gsc 段里的 refresh_token 那一行,保住文件里的注释和别的段。

    **不能照搬 auth._write_back**:它按「第一行 refresh_token:」替换,第一行是
    google_ads 的 —— 会把 Ads 授权覆盖掉。也不能 yaml.safe_dump 整份重写,注释全没了。
    """
    import re
    p = path or config.LOCAL
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(r"^gsc\s*:\s*(#.*)?$", l)), None)
    line = '  refresh_token: "%s"' % value
    if start is None:
        lines += ["", "# ---------- Google Search Console(GSC 周报)----------", "gsc:", line]
    else:
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i] and not lines[i].startswith((" ", "\t", "#"))), len(lines))
        hit = next((i for i in range(start + 1, end)
                    if re.match(r"^\s+refresh_token\s*:", lines[i])), None)
        if hit is None:
            lines.insert(start + 1, line)
        else:
            lines[hit] = line
    p.write_text("\n".join(lines), encoding="utf-8")
    if path is None:
        config.load(refresh=True)


def access_token():
    rt = config.get("gsc.refresh_token")
    if not rt:
        raise GscError("还没授权 Search Console —— 去「设置」页点「授权 GSC」,用能看 GSC 的 Google 账号登录。")
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    c = Credentials(None, refresh_token=rt, token_uri=TOKEN_URI,
                    client_id=config.get("google_ads.client_id"),
                    client_secret=config.get("google_ads.client_secret"), scopes=SCOPES)
    try:
        c.refresh(Request())
    except Exception as e:
        raise GscError("刷新 GSC 令牌失败:%s —— 多半是授权被撤销或过期,去设置页重新授权一次。" % str(e)[:160])
    return c.token


def _call(method, url, tok, body=None, timeout=90):
    req = urllib.request.Request(
        url, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            msg = (json.loads(raw).get("error") or {}).get("message") or raw
        except ValueError:
            msg = raw
        if e.code == 403 and ("has not been used" in msg or "is disabled" in msg or "accessNotConfigured" in raw):
            raise GscError("Google Cloud 项目里没启用 Search Console API:到 console.cloud.google.com → "
                           "API 和服务 → 库 → 搜「Google Search Console API」→ 启用,几分钟后再试。")
        if e.code == 403:
            raise GscError("没有这个资源的权限(403):%s —— 授权的 Google 账号在 GSC 里看不到它。" % msg[:160])
        if e.code == 429:
            raise GscError("GSC 接口限流(429),几分钟后再试。")
        raise GscError("GSC 请求失败 HTTP %d:%s" % (e.code, msg[:200]))


def sites():
    """授权账号能看到的全部资源。sc-domain:xxx 是域名资源,https://xxx/ 是网址前缀资源。"""
    tok = access_token()
    d = _call("GET", API + "/sites", tok)
    out = [{"site": s.get("siteUrl"), "permission": s.get("permissionLevel")}
           for s in d.get("siteEntry") or []]
    out.sort(key=lambda s: s["site"])
    return out


def query(site, start, end, dimensions, row_limit=25000, filters=None):
    """searchAnalytics.query,自动翻页。返回 [{keys, clicks, impressions, ctr, position}]。"""
    tok = access_token()
    url = "%s/sites/%s/searchAnalytics/query" % (API, urllib.parse.quote(site, safe=""))
    rows, start_row = [], 0
    while True:
        body = {"startDate": start, "endDate": end, "dimensions": dimensions,
                "rowLimit": min(row_limit, 25000), "startRow": start_row}
        if filters:
            body["dimensionFilterGroups"] = [{"filters": filters}]
        d = _call("POST", url, tok, body)
        got = d.get("rows") or []
        rows.extend(got)
        if len(got) < body["rowLimit"] or len(rows) >= row_limit:
            break
        start_row += len(got)
    return rows


def sitemaps(site):
    """已提交的 sitemap 及其条数。注意 GSC 早就不再回 indexed(总是 0),已收录数要看页面索引报告。"""
    tok = access_token()
    d = _call("GET", "%s/sites/%s/sitemaps" % (API, urllib.parse.quote(site, safe="")), tok)
    out = []
    for s in d.get("sitemap") or []:
        cont = s.get("contents") or []
        out.append({"path": s.get("path"),
                    "submitted": sum(int(c.get("submitted") or 0) for c in cont),
                    "indexed": sum(int(c.get("indexed") or 0) for c in cont),
                    "last_submitted": (s.get("lastSubmitted") or "")[:10],
                    "errors": int(s.get("errors") or 0), "warnings": int(s.get("warnings") or 0)})
    return out


def inspect(site, url):
    """网址检查(每天 2000 次配额,只给新发页面用)。"""
    tok = access_token()
    d = _call("POST", INSPECT, tok, {"inspectionUrl": url, "siteUrl": site})
    r = (d.get("inspectionResult") or {}).get("indexStatusResult") or {}
    return {"verdict": r.get("verdict"), "coverage": r.get("coverageState"),
            "last_crawl": (r.get("lastCrawlTime") or "")[:10]}
