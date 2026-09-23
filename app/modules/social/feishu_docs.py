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
import urllib.error
import urllib.request

from app import config

API = "https://open.feishu.cn/open-apis"
BATCH = 40           # 一次插多少个一级块;太多会超时


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

    index = 0
    for i in range(0, len(top), BATCH):
        chunk = top[i:i + BATCH]
        seen = set()
        desc = []
        for bid in chunk:
            desc.extend(subtree(bid, seen))
        _ok(_call("POST", "/docx/v1/documents/%s/blocks/%s/descendant" % (doc_id, doc_id),
                  tok, {"children_id": chunk, "index": index, "descendants": desc}),
            "写入第 %d 批" % (i // BATCH + 1))
        index += len(chunk)
        log("  写入 %d 个一级块（%d 个块）" % (len(chunk), len(desc)))

    if owner_open_id:
        r = _call("POST", "/drive/v1/permissions/%s/members?type=docx&need_notification=false"
                  % doc_id, tok,
                  {"member_type": "openid", "member_id": owner_open_id, "perm": "edit"})
        log("授权编辑权：%s" % ("成功" if r.get("code") == 0 else r.get("msg")))

    return {"doc_token": doc_id, "url": url}


def move(doc_token, folder_token, log=None):
    log = log or (lambda *a: None)
    r = _call("POST", "/drive/v1/files/%s/move" % doc_token, token(),
              {"type": "docx", "folder_token": folder_token})
    log("移动到目标文件夹：%s" % ("成功" if r.get("code") == 0 else r.get("msg")))
    return r.get("code") == 0
