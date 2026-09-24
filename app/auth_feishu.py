# -*- coding: utf-8 -*-
"""飞书网页登录(OAuth 2.0 授权码)。复用「程序开发助手」这个自建应用。

只有 config 里 auth.mode = "feishu" 时启用(服务器部署);本机版不走这里。

流程:/auth/login -> 飞书授权页 -> /auth/feishu/callback?code&state
      -> code 换 user_access_token -> user_info 拿 open_id / 姓名 -> 建会话(cookie)

**为什么服务器上不能沿用「本机免登」**:请求都经 nginx 转发,在程序看来全部来自
127.0.0.1。飞书模式下每个请求都必须有有效会话,不认来源地址。

自建应用只有本企业成员能授权,所以「能登进来」= 是互旦员工。
要再收窄范围就在 auth.allow_open_ids 里列白名单。
"""
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from app import config

AUTHORIZE = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
USER_INFO = "https://open.feishu.cn/open-apis/authen/v1/user_info"
COOKIE = "hsd_sid"
STATE_COOKIE = "hsd_state"
TTL = 30 * 24 * 3600

_LOCK = threading.Lock()


class AuthError(RuntimeError):
    pass


def enabled():
    return str(config.get("auth.mode", "") or "").lower() == "feishu"


def base_url():
    return str(config.get("auth.base_url", "") or "").rstrip("/")


def redirect_uri():
    return base_url() + "/auth/feishu/callback"


def admin_ids():
    ids = config.get("auth.admin_open_ids") or []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.split(",")]
    owner = config.get("social.owner_open_id")
    return {i for i in list(ids) + [owner] if i}


# ---------------------------------------------------------------- 会话存储

def _path():
    return config.data_dir() / "sessions.json"


def _load():
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _dump(d):
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    try:
        p.chmod(0o600)
    except OSError:
        pass


def session_user(sid):
    if not sid:
        return None
    with _LOCK:
        d = _load()
        s = d.get(sid)
        if not s or s.get("exp", 0) < time.time():
            return None
    s = dict(s)
    s["admin"] = s.get("open_id") in admin_ids()
    return s


def create_session(user):
    sid = secrets.token_urlsafe(32)
    with _LOCK:
        d = _load()
        now = time.time()
        d = {k: v for k, v in d.items() if v.get("exp", 0) > now}        # 顺手清过期
        d[sid] = dict(user, exp=now + TTL, created=now)
        _dump(d)
    return sid


def drop_session(sid):
    with _LOCK:
        d = _load()
        if d.pop(sid, None) is not None:
            _dump(d)


# ---------------------------------------------------------------- OAuth

def login_url(state):
    q = {"client_id": config.get("feishu.app_id"), "response_type": "code",
         "redirect_uri": redirect_uri(), "state": state}
    return AUTHORIZE + "?" + urllib.parse.urlencode(q)


def new_state():
    return secrets.token_urlsafe(16)


def state_ok(got, want):
    return bool(got and want) and hmac.compare_digest(str(got), str(want))


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers=dict({"Content-Type": "application/json; charset=utf-8"}, **(headers or {})))
    try:
        return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except ValueError:
            return {"code": e.code, "msg": "HTTP %d" % e.code}


def exchange(code):
    """code -> 用户信息 {open_id, name, avatar}。"""
    t = _post(TOKEN, {"grant_type": "authorization_code", "client_id": config.get("feishu.app_id"),
                      "client_secret": config.get("feishu.app_secret"), "code": code,
                      "redirect_uri": redirect_uri()})
    at = t.get("access_token") or (t.get("data") or {}).get("access_token")
    if not at:
        raise AuthError("飞书换取令牌失败(%s):%s" % (t.get("code"), t.get("error_description") or t.get("msg")))
    req = urllib.request.Request(USER_INFO, headers={"Authorization": "Bearer " + at})
    try:
        u = json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e:
        u = json.loads(e.read().decode("utf-8", "replace"))
    if u.get("code") != 0:
        raise AuthError("读取飞书用户信息失败(%s):%s" % (u.get("code"), u.get("msg")))
    d = u.get("data") or {}
    oid = d.get("open_id")
    if not oid:
        raise AuthError("飞书没有返回 open_id")
    allow = config.get("auth.allow_open_ids") or []
    if allow and oid not in allow and oid not in admin_ids():
        raise AuthError("你的飞书账号(%s)不在工作台白名单里,请联系管理员。" % (d.get("name") or oid))
    return {"open_id": oid, "name": d.get("name") or d.get("en_name") or oid[-6:],
            "avatar": d.get("avatar_thumb") or d.get("avatar_url") or ""}
