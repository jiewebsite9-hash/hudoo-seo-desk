# -*- coding: utf-8 -*-
"""排名监控的飞书闭环:词库同步 -> 查排名 -> 写回排名列 -> 推送。

凭据和表链接全部来自 config.local.yaml,**公开仓库不带任何租户信息**。

这里有三处是拿真实事故换来的,改之前先读注释:
  1. 列存在 ≠ 列有值(飞书空单元格也返回 key=None)
  2. 带 view_id 拉记录会慢 10 倍
  3. 各家表的「N月排名」列类型不一致,写错类型直接报 1254061
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from app import config

API = "https://open.feishu.cn/open-apis"


class FeishuError(RuntimeError):
    """普通失败。**不要用 SystemExit** —— 那是 BaseException,
    在作业线程里会绕过异常处理让作业静默卡死。"""


def _creds():
    app_id = config.get("feishu.app_id")
    secret = config.get("feishu.app_secret")
    if not app_id or not secret:
        raise FeishuError("要先在 config.local.yaml 里配 feishu.app_id / app_secret。")
    return app_id, secret


def token():
    app_id, secret = _creds()
    req = urllib.request.Request(
        API + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": secret}).encode(),
        headers={"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    if d.get("code") != 0:
        raise FeishuError("拿飞书 token 失败(code %s): %s" % (d.get("code"), d.get("msg")))
    return d["tenant_access_token"]


# 这些 code 都是「机器人没被授权看这张表」,不是链接写错。
# 分开说,因为两者的解法完全不同:前者要去飞书分享,后者要改链接。
NO_PERM_CODES = {91403, 1770032, 99991672, 99991673}


def _explain(code, body, status=None):
    if code in NO_PERM_CODES:
        return FeishuError(
            "飞书拒绝访问(code %s)——这张表没有授权给机器人。"
            "去飞书把这个文档或它的父文件夹分享给机器人应用,给「可编辑」。" % code)
    if code in (1254004, 1254005, 1254043):
        return FeishuError("飞书说找不到这张表或这个视图(code %s)——"
                           "多半是链接里的 table/view 参数不对。" % code)
    # token 不对时飞书把 msg 直接写成 NOTEXIST —— 链接贴错时最常见的一种。
    # 注意这条走的是合法 JSON,不是非 JSON 响应,所以必须在这里判。
    if "NOTEXIST" in str(body).upper():
        return FeishuError(
            "飞书找不到这个多维表格 —— 链接里的 base token 不对,或者这张表已经被删了。"
            "确认链接形如 https://xxx.feishu.cn/base/<token>?table=<id>")
    return FeishuError("飞书请求失败%s:%s"
                       % (" HTTP %d" % status if status else "", body[:300]))


def _call(req, timeout):
    try:
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except ValueError:
            # 多维表格 token 不对时飞书直接回一句裸文本 NOTEXIST,不是 JSON。
            # 这是链接贴错时最常见的一种,单独说清楚。
            if "NOTEXIST" in raw.upper():
                raise FeishuError(
                    "飞书找不到这个多维表格 —— 链接里的 base token 不对,"
                    "或者这张表已经被删了。确认链接形如 "
                    "https://xxx.feishu.cn/base/<token>?table=<id>")
            raise FeishuError("飞书请求失败 HTTP %d:%s" % (e.code, raw[:300]))
        raise _explain(body.get("code"), body.get("msg") or raw, e.code)
    # HTTP 200 但 code 非 0 也是失败 —— 飞书很多错误是这么返回的
    if isinstance(d, dict) and d.get("code") not in (0, None):
        raise _explain(d.get("code"), d.get("msg") or str(d))
    return d


def _get(url, tok, timeout=60):
    return _call(urllib.request.Request(
        url, headers={"Authorization": "Bearer " + tok}), timeout)


def _post(url, tok, body, timeout=60):
    return _call(urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + tok,
                 "Content-Type": "application/json"}), timeout)


# ---------------------------------------------------------------- 链接

def parse_url(url):
    """从多维表格分享链接解析 (host, base_token, table_id, view_id)。"""
    m = re.search(r"/base/([A-Za-z0-9]+)", url or "")
    if not m:
        raise FeishuError(
            "链接里找不到 /base/<token>。请用多维表格的分享链接(形如 .../base/xxx?table=yyy)。"
            "若是 /wiki/ 开头的知识库链接,请在表格右上角「… → 复制链接」取 base 链接。")
    q = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(q.query)
    return (q.netloc, m.group(1),
            (qs.get("table") or [""])[0], (qs.get("view") or [""])[0])


def resolve_table(base_token, table, tok):
    """table 缺省时智能补全:优先名字含「关键词/keyword」的表;只有一张表就用它。"""
    if table:
        return table
    d = _get("%s/bitable/v1/apps/%s/tables?page_size=100" % (API, base_token), tok)
    if d.get("code") != 0:
        raise FeishuError("读取多维表格失败(code %s): %s。"
                          "多半是这张表还没分享给机器人。" % (d.get("code"), d.get("msg")))
    tables = (d.get("data") or {}).get("items") or []
    if not tables:
        raise FeishuError("这个多维表格里没有任何数据表。")
    kw = [t for t in tables if "关键词" in (t.get("name") or "")
          or "keyword" in (t.get("name") or "").lower()]
    if len(kw) == 1:
        return kw[0]["table_id"]
    if len(tables) == 1:
        return tables[0]["table_id"]
    names = "；".join("%s=%s" % (t.get("name"), t.get("table_id")) for t in tables)
    raise FeishuError("链接只到 base、没指定具体表,而里面有多张表无法自动判断。"
                      "请打开关键词库那张表、复制地址栏完整链接(带 ?table=...)。"
                      "可选表:" + names)


def _view_has_filter(base_token, table, view, tok):
    """视图有没有设筛选。

    **带 view_id 拉记录,飞书侧要现场渲染视图 —— 实测 310 行要 22 秒,不带只要 1.3 秒。**
    视图没筛选时两者结果集完全一样,可以绕开。查不到就当「有筛选」(保守,宁可慢不可少词)。
    """
    try:
        d = _get("%s/bitable/v1/apps/%s/tables/%s/views/%s" % (API, base_token, table, view),
                 tok, timeout=15)
        if d.get("code") != 0:
            return True
        prop = ((d.get("data") or {}).get("view") or {}).get("property") or {}
        return bool(prop.get("filter_info") or prop.get("filter_conditions"))
    except Exception:
        return True


def records(base_token, table, view, tok, log=None):
    log = log or (lambda m: None)
    if view and not _view_has_filter(base_token, table, view, tok):
        log("视图没设筛选,丢掉 view_id 走快路径(带上会慢 10 倍)")
        view = ""
    out, page = [], ""
    while True:
        q = {"page_size": "500"}
        if view:
            q["view_id"] = view
        if page:
            q["page_token"] = page
        d = _get("%s/bitable/v1/apps/%s/tables/%s/records?%s"
                 % (API, base_token, table, urllib.parse.urlencode(q)), tok)
        if d.get("code") != 0:
            raise FeishuError("飞书读取失败(code %s): %s。"
                              "多半是这张表还没分享给机器人。" % (d.get("code"), d.get("msg")))
        data = d["data"]
        out.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page = data.get("page_token", "")
    return out


# ---------------------------------------------------------------- 字段

def has_value(v):
    """这个单元格算不算「填了值」。

    **列存在 ≠ 列有值。** 飞书 records 接口会把空单元格也返回成 `key: None`,
    所以用 `"列名" in fields` 判断「有没有这一列」**永远为真**。
    踩过的事故:某张表 310 行的「是否需要排名监控」整列没人填,老代码把它当成
    有效筛选条件,于是全被「值不等于 Y」筛掉,前端显示「飞书共 0 词」,
    看着像没权限,其实接口一切正常。
    **排查这类问题先看列值分布,别先怀疑权限。**
    """
    if v is None:
        return False
    if isinstance(v, str):
        return bool(v.strip())
    if isinstance(v, (list, tuple, dict)):
        return bool(v)
    return True


def cell_text(v):
    """飞书文本字段可能是 str,也可能是 [{'text': '...'}] 这种富文本数组。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, list):
        return " ".join(
            (i.get("text") or "") if isinstance(i, dict) else str(i) for i in v).strip()
    if isinstance(v, dict):
        return str(v.get("text") or "").strip()
    return str(v).strip()


def _norm_kw(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


# ---------------------------------------------------------------- 同步

def sync(url, log=None):
    """按飞书链接拉词库。返回 {keywords, rows, skipped, filtered_by_monitor, resolved_url}。

    字段容错:有「是否需要排名监控」列则只取 Y;**整列空白视为没这个筛选,全量导入**。
    """
    log = log or (lambda m: None)
    host, base_token, table, view = parse_url(url)
    tok = token()
    table = resolve_table(base_token, table, tok)
    recs = records(base_token, table, view, tok, log=log)
    log("飞书返回 %d 行" % len(recs))

    monitor_col = any(has_value((r.get("fields") or {}).get("是否需要排名监控")) for r in recs)
    if monitor_col:
        log("「是否需要排名监控」列有值,只取标 Y 的行")
    else:
        log("「是否需要排名监控」列整列空白(或没这列),全量导入")

    seen, words, skipped = set(), [], 0
    for r in recs:
        f = r.get("fields") or {}
        if monitor_col and cell_text(f.get("是否需要排名监控")).upper() != "Y":
            skipped += 1
            continue
        kw = cell_text(f.get("关键词"))
        if not kw or kw.lower() in seen:
            continue
        seen.add(kw.lower())
        words.append(kw)

    full = "https://%s/base/%s?table=%s" % (host, base_token, table)
    if view:
        full += "&view=" + view
    log("拉到 %d 个词%s" % (len(words),
                          ",其中 %d 行未标 Y 被过滤" % skipped if skipped else ""))
    return {"keywords": words, "rows": len(recs), "skipped": skipped,
            "filtered_by_monitor": monitor_col, "resolved_url": full}


# ---------------------------------------------------------------- 写回

def writeback(url, positions, field_name=None, log=None):
    """把排名写回飞书的排名列。`positions` = {关键词: 名次或 None}。

    **列类型必须自适应。** 各家表的「N月排名」列建得不一样:有的是文本列、
    有的是数字列。往数字列塞字符串会报 `1254061 NumberFieldConvFail`。
    规则:数字列写 int,没排名写 None(清空);文本列写 "8" / "-"。
    """
    log = log or (lambda m: None)
    host, base_token, table, view = parse_url(url)
    tok = token()
    table = resolve_table(base_token, table, tok)
    field_name = field_name or ("%d月排名" % datetime.now().month)

    d = _get("%s/bitable/v1/apps/%s/tables/%s/fields?page_size=200"
             % (API, base_token, table), tok)
    if d.get("code") != 0:
        raise FeishuError("读取字段失败(code %s): %s" % (d.get("code"), d.get("msg")))
    items = (d.get("data") or {}).get("items") or []
    types = {f["field_name"]: f.get("type") for f in items}
    if field_name not in types:
        cands = [f for f in types if "排名" in f]
        raise FeishuError("表里没有字段「%s」。可用的排名字段:%s。"
                          "可以在项目配置里用 feishu_rank_field 指定。"
                          % (field_name, cands or "(一个都没有)"))
    ftype = types[field_name]
    if ftype not in (1, 2):
        raise FeishuError("字段「%s」类型不支持(type=%s),排名列请建成「文本」或「数字」。"
                          % (field_name, ftype))
    is_number = ftype == 2
    log("写回字段「%s」(%s列)" % (field_name, "数字" if is_number else "文本"))

    want = {}
    for kw, pos in (positions or {}).items():
        if is_number:
            want[_norm_kw(kw)] = int(pos) if pos else None
        else:
            want[_norm_kw(kw)] = str(pos) if pos else "-"

    recs = records(base_token, table, view, tok, log=log)
    updates, no_data = [], 0
    for rec in recs:
        kw = _norm_kw(cell_text((rec.get("fields") or {}).get("关键词")))
        if not kw or kw not in want:
            no_data += 1
            continue
        updates.append({"record_id": rec["record_id"],
                        "fields": {field_name: want[kw]}})

    written = 0
    for i in range(0, len(updates), 500):
        batch = updates[i:i + 500]
        res = _post("%s/bitable/v1/apps/%s/tables/%s/records/batch_update"
                    % (API, base_token, table), tok, {"records": batch})
        if res.get("code") != 0:
            raise FeishuError("批量写入失败(code %s): %s" % (res.get("code"), res.get("msg")))
        written += len(batch)

    log("写回完成:%d 行(表里另有 %d 行本轮没数据,跳过)" % (written, no_data))
    return {"field": field_name, "written": written, "no_data": no_data,
            "field_type": "数字" if is_number else "文本"}


# ---------------------------------------------------------------- 推送

def push(text, receive_id=None, log=None):
    """把结果推给配置里的接收人(open_id)。没配就静默跳过。"""
    log = log or (lambda m: None)
    rid = receive_id or config.get("feishu.receive_id")
    if not rid:
        log("没配 feishu.receive_id,跳过推送")
        return None
    tok = token()
    res = _post("%s/im/v1/messages?receive_id_type=open_id" % API, tok,
                {"receive_id": rid, "msg_type": "text",
                 "content": json.dumps({"text": text}, ensure_ascii=False)})
    if res.get("code") != 0:
        raise FeishuError("推送失败(code %s): %s" % (res.get("code"), res.get("msg")))
    log("已推送给 %s" % rid)
    return res.get("data", {}).get("message_id")
