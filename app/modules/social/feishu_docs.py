# -*- coding: utf-8 -*-
"""把周报 markdown 建成飞书云文档。

走 convert + descendant 路线,**不走 import_tasks** —— 自建应用没有 drive 上传权限,
import_tasks 会直接 403(1061004)。三步:
    POST docx/v1/documents                      建空文档
    POST docx/v1/documents/blocks/convert       markdown -> 块
    POST docx/v1/documents/{doc}/blocks/{doc}/descendant   分批插回

建完可选:给指定人加编辑权 + 挪进指定文件夹。
"""
import json
import time
import urllib.error
import urllib.request

from app import config

API = "https://open.feishu.cn/open-apis"
BATCH = 40           # 一次插多少个一级块;太多会超时
MAX_DESC = 400       # 一次最多插多少个块(含子孙);超了飞书报 99992402


class FeishuError(RuntimeError):
    """普通失败。**不要用 SystemExit** —— 那是 BaseException，
    在作业线程里会绕过 except Exception 让作业静默卡死。"""


def configured():
    return bool(config.get("feishu.app_id") and config.get("feishu.app_secret"))


def token():
    app_id = config.get("feishu.app_id")
    secret = config.get("feishu.app_secret")
    if not app_id or not secret:
        raise FeishuError("要先在 config.local.yaml 里配 feishu.app_id / app_secret。")
    req = urllib.request.Request(
        API + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": secret}).encode(),
        headers={"Content-Type": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    if d.get("code") != 0:
        raise FeishuError("拿飞书 token 失败(code %s): %s" % (d.get("code"), d.get("msg")))
    return d["tenant_access_token"]


def _call(method, path, tok, body=None, timeout=120):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": "Bearer " + tok,
                 "Content-Type": "application/json; charset=utf-8"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        # 飞书把真正的错误码放在 body 里,HTTP 状态只是个壳,必须读出来
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except ValueError:
            raise FeishuError("飞书请求失败 HTTP %d" % e.code)


def _ok(r, what):
    if r.get("code") != 0:
        raise FeishuError("%s失败(code %s): %s" % (what, r.get("code"), r.get("msg")))
    return r.get("data") or {}


def _clean(block):
    """convert 出来的块不能原样喂给 descendant。

    表格块带 `table.cells` 和 `merge_info`,创建接口不认,会报 1770001 invalid param;
    table 字段只能留 property。无合并单元格时 cells/merge_info 可以无损丢弃。
    """
    b = dict(block)
    if b.get("block_type") == 31 and isinstance(b.get("table"), dict):
        prop = (b["table"] or {}).get("property") or {}
        b["table"] = {"property": {k: prop[k] for k in
                                   ("row_size", "column_size", "column_width")
                                   if k in prop}}
    return b


def doc_url(doc_id):
    """拼文档链接。租户域名各公司不同,接口又不返回链接,只能配。

    配了就用配的,没配退回通用域名(能打开,会跳到自己的租户)。
    """
    base = str(config.get("feishu.tenant_url", "") or "https://feishu.cn").rstrip("/")
    return "%s/docx/%s" % (base, doc_id)


def create(markdown, title, folder_token=None, owner_open_id=None, log=None):
    """建文档并写入内容。返回 {doc_token, url}。"""
    log = log or (lambda *a: None)
    tok = token()

    body = {"title": title}
    if folder_token:
        body["folder_token"] = folder_token
    doc = _ok(_call("POST", "/docx/v1/documents", tok, body), "建文档")
    doc_id = doc["document"]["document_id"]
    url = doc_url(doc_id)
    log("已建文档 %s" % url)

    conv = _ok(_call("POST", "/docx/v1/documents/blocks/convert", tok,
                     {"content_type": "markdown", "content": markdown}),
               "markdown 转块")
    blocks = {b["block_id"]: _clean(b) for b in conv["blocks"]}
    top = conv["first_level_block_ids"]
    log("转换出 %d 个块（一级 %d）" % (len(blocks), len(top)))

    def subtree(bid, seen):
        """一级块连同它的全部后代 —— descendant 接口要求整棵子树一起传。"""
        out = []
        stack = [bid]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in blocks:
                continue
            seen.add(cur)
            out.append(blocks[cur])
            stack.extend(blocks[cur].get("children") or [])
        return out

    # 按块数分批,不按一级块数:一张 268 行的表一个子树就 1000 多个块,和别的块
    # 一起一次写入会被拒(99992402)。子树必须整棵一起传,所以大表要在 markdown 侧先分段。
    state = {"index": 0, "n": 0, "ids": [], "desc": []}

    def flush():
        if not state["ids"]:
            return
        state["n"] += 1
        _ok(_call("POST", "/docx/v1/documents/%s/blocks/%s/descendant" % (doc_id, doc_id),
                  tok, {"children_id": state["ids"], "index": state["index"],
                        "descendants": state["desc"]}),
            "写入第 %d 批" % state["n"])
        state["index"] += len(state["ids"])
        log("  写入 %d 个一级块（%d 个块）" % (len(state["ids"]), len(state["desc"])))
        state["ids"], state["desc"] = [], []

    try:
        for bid in top:
            sub = subtree(bid, set())
            if state["ids"] and (len(state["desc"]) + len(sub) > MAX_DESC or len(state["ids"]) >= BATCH):
                flush()
            state["ids"].append(bid)
            state["desc"].extend(sub)
        flush()
    except FeishuError:
        # 写一半失败别留空壳:删掉再抛,调用方拿到的是失败不是一个空文档链接
        _call("DELETE", "/drive/v1/files/%s?type=docx" % doc_id, tok)
        log("写入失败,已删除空文档")
        raise

    if owner_open_id:
        r = _call("POST", "/drive/v1/permissions/%s/members?type=docx&need_notification=false"
                  % doc_id, tok,
                  {"member_type": "openid", "member_id": owner_open_id, "perm": "edit"})
        log("授权编辑权：%s" % ("成功" if r.get("code") == 0 else r.get("msg")))
    share_tenant(tok, doc_id, "docx", log)
    grant_managers(tok, doc_id, "docx", log)

    return {"doc_token": doc_id, "url": url}


LINK_SHARE = {"tenant_editable": "组织内获得链接的人可编辑", "tenant_readable": "组织内获得链接的人可阅读"}


_MGR = {"t": 0.0, "key": None, "ids": []}


def manager_ids(tok, log=None):
    """config feishu.manage_departments 里各部门(含子部门)的在职成员 open_id。缓存 1 小时。

    飞书不允许应用把整个部门加为协作者(1063001),只能按人加;所以部门新来的人
    只对之后生成的文档有管理权,老文档要补跑一次。
    """
    names = config.get("feishu.manage_departments") or []
    if isinstance(names, str):
        names = [x.strip() for x in names.split(",") if x.strip()]
    key = tuple(names)
    if not names:
        return []
    if _MGR["key"] == key and time.time() - _MGR["t"] < 3600:
        return list(_MGR["ids"])
    r = _call("GET", "/contact/v3/departments/0/children?department_id_type=open_department_id"
                     "&fetch_child=true&page_size=50", tok)
    deps = [d for d in (r.get("data") or {}).get("items", []) if d.get("name") in names]
    ods = [d["open_department_id"] for d in deps]
    # 子部门:parent 在已选部门里的也算
    allds = (r.get("data") or {}).get("items", [])
    grew = True
    while grew:
        grew = False
        for d in allds:
            if d.get("parent_department_id") in ods and d["open_department_id"] not in ods:
                ods.append(d["open_department_id"])
                grew = True
    missing = set(names) - {d.get("name") for d in deps}
    if missing and log:
        log("没找到部门:%s(看 feishu.manage_departments 的写法)" % "、".join(missing))
    ids = []
    for od in ods:
        pt = ""
        while True:
            m = _call("GET", "/contact/v3/users/find_by_department?department_id=%s&department_id_type="
                             "open_department_id&page_size=50%s" % (od, "&page_token=" + pt if pt else ""), tok)
            d = m.get("data") or {}
            for u in d.get("items", []):
                if not (u.get("status") or {}).get("is_resigned") and u["open_id"] not in ids:
                    ids.append(u["open_id"])
            if not d.get("has_more"):
                break
            pt = d.get("page_token")
    _MGR.update({"t": time.time(), "key": key, "ids": ids})
    return list(ids)


def grant_managers(tok, token, typ, log=None):
    """给 manage_departments 的成员开「可管理」。已是协作者的改成可管理。失败只写日志。"""
    log = log or (lambda m: None)
    try:
        ids = manager_ids(tok, log)
    except Exception as e:
        log("读部门成员失败,管理权限没开:%s" % e)
        return 0
    ok = 0
    for oid in ids:
        r = _call("POST", "/drive/v1/permissions/%s/members?type=%s&need_notification=false" % (token, typ), tok,
                  {"member_type": "openid", "member_id": oid, "perm": "full_access"})
        if r.get("code") != 0:     # 已是协作者(比如操作人先拿了编辑权):改成可管理
            r = _call("PUT", "/drive/v1/permissions/%s/members/%s?type=%s&member_type=openid&need_notification=false"
                      % (token, oid, typ), tok, {"member_type": "openid", "perm": "full_access"})
        ok += r.get("code") == 0
    if ids:
        log("管理权限:%d/%d 人(%s)" % (ok, len(ids), "、".join(config.get("feishu.manage_departments") or [])
                                        if not isinstance(config.get("feishu.manage_departments"), str)
                                        else config.get("feishu.manage_departments")))
    return ok


def share_tenant(tok, token, typ, log=None):
    """打开链接分享:组织内获得链接的人可编辑(默认)。只在组织内,不碰对外分享。

    config feishu.link_share 可改成 tenant_readable,或 off 不开。失败只写日志。
    """
    log = log or (lambda m: None)
    mode = str(config.get("feishu.link_share", "tenant_editable") or "off")
    if mode not in LINK_SHARE:
        return False
    body = {"link_share_entity": mode, "share_entity": "same_tenant"}
    if mode == "tenant_editable":
        body.update({"security_entity": "anyone_can_edit", "comment_entity": "anyone_can_edit"})
    r = _call("PATCH", "/drive/v1/permissions/%s/public?type=%s" % (token, typ), tok, body)
    ok = r.get("code") == 0
    log("链接分享:%s" % (LINK_SHARE[mode] if ok else "开启失败(%s)" % r.get("msg")))
    return ok


def move(doc_token, folder_token, log=None):
    log = log or (lambda *a: None)
    r = _call("POST", "/drive/v1/files/%s/move" % doc_token, token(),
              {"type": "docx", "folder_token": folder_token})
    log("移动到目标文件夹：%s" % ("成功" if r.get("code") == 0 else r.get("msg")))
    return r.get("code") == 0
