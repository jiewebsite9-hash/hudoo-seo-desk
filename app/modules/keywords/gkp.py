# -*- coding: utf-8 -*-
"""Google 关键词规划师取数(Google Ads API v25)。

两个入口对应 UI 上的两个动作:
  ideas()  = 「发现新关键字」,种子上限 20/请求,自动分批去重
  volume() = 「获取搜索量和预测数据」,上限 1 万词/请求

地区必须显式传 —— API 不传不会默认中国,但会给全球汇总值,对外贸站同样没意义。
"""
import re
import csv
from datetime import datetime

from app import config
from .locations import resolve_geo, resolve_lang

API_VERSION = "v25"
VOLUME_BATCH = 10000
IDEAS_SEED_BATCH = 20

MONTHS = {"JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5,
          "JUNE": 6, "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10,
          "NOVEMBER": 11, "DECEMBER": 12}

# SSOT:可行性评分 低->5 / 中->4 / 高->3
FEASIBILITY = {"LOW": 5, "MEDIUM": 4, "HIGH": 3}
COMP_CN = {"LOW": "低", "MEDIUM": "中", "HIGH": "高"}

# SSOT 高价值线:页首出价(高位)。注意这条线是按通用外贸品类定的,
# 工业/机械类词出价普遍高得多,会把整列打成「极高」失去区分度 —— 见 README。
VALUE_LINE = {"CNY": (20.0, 35.0), "USD": (2.8, 5.0), "EUR": (2.6, 4.6)}

# 内部字段名(sop.py 也依赖这套键,别改)
COLUMNS = ["关键词", "月均搜索量", "搜索量档位", "竞争程度", "竞争指数",
           "页首出价低", "页首出价高", "平均CPC", "货币", "可行性评分",
           "高价值", "近12月"]

# 导出/界面用的表头:把币种写进列名,并去掉单独的「货币」列。
# 原先出价列不带单位、币种藏在最后一列,查美国市场看到人民币金额很容易误读。
DISPLAY = ["关键词", "月均搜索量", "搜索量档位", "竞争程度", "竞争指数",
           "页首出价低({cur})", "页首出价高({cur})", "平均CPC({cur})",
           "可行性评分", "高价值", "近12月"]
DISPLAY_KEYS = ["关键词", "月均搜索量", "搜索量档位", "竞争程度", "竞争指数",
                "页首出价低", "页首出价高", "平均CPC",
                "可行性评分", "高价值", "近12月"]


def display_columns(currency):
    return [c.replace("{cur}", currency or "?") for c in DISPLAY]


def apply_currency(rows, to_usd=False, rate=7.0):
    """按需把出价列换算成 USD,并重算「高价值」。

    **Google Ads 返回的出价永远是账号币种,跟查哪个市场无关** —— 人民币账号查
    美国市场拿到的也是 CNY。做外贸时通常想看 USD,所以给一个换算开关。
    换算后高价值阈值要跟着切到 USD 档(≥$2.8 高 / ≥$5 极高),否则整列会失真。
    """
    cur = (rows[0].get("货币") if rows else None) or "USD"
    if not rows or not to_usd or cur == "USD":
        return rows, cur
    rate = float(rate or 7.0)
    for r in rows:
        for k in ("页首出价低", "页首出价高", "平均CPC"):
            r[k] = round((r[k] or 0) / rate, 2)
        r["货币"] = "USD"
        r["高价值"] = value_tag(r["页首出价高"], "USD")
    return rows, "USD"

ERROR_HINTS = [
    ("DEVELOPER_TOKEN_NOT_APPROVED",
     "访问级别还停在 测试/探索者 —— 关键词规划师(KeywordPlanIdeaService)被屏蔽。\n"
     "  2026-09-10 起权限挂在 Google Cloud 项目上,不再看开发者令牌:\n"
     "  去 Cloud 控制台的 Google Ads API 概览页申请「基本(Basic)」,前置是品牌验证。"),
    ("USER_PERMISSION_DENIED",
     "login_customer_id 不对,或授权的 Google 账号对这个广告账号没权限。要纯数字不带横杠。"),
    ("CUSTOMER_NOT_ENABLED", "广告账号没激活(通常是没填结算信息)。"),
    ("AUTHENTICATION_ERROR", "refresh_token / client_id / client_secret 对不上,重跑一次授权。"),
    ("QUOTA_ERROR", "撞到当日配额(基本访问权限 15000 次操作/天)。"),
    ("RESOURCE_EXHAUSTED", "短时请求太密被限流,等几分钟再试。"),
]


class GkpError(RuntimeError):
    pass


def _explain(exc):
    parts = []
    for err in exc.failure.errors:
        parts.append("%s: %s" % (str(err.error_code).strip(), err.message))
    blob = "\n".join(parts)
    for token, hint in ERROR_HINTS:
        if token in blob.upper():
            return blob + "\n\n>> " + hint
    return blob


def make_client():
    cfg = config.google_ads_dict()
    missing = [k for k in ("developer_token", "client_id", "client_secret",
                           "refresh_token", "login_customer_id") if not cfg.get(k)]
    if missing:
        raise GkpError("配置缺这几项:%s —— 去「设置」里补齐 config.local.yaml"
                       % "、".join(missing))
    from google.ads.googleads.client import GoogleAdsClient
    return GoogleAdsClient.load_from_dict(cfg, version=API_VERSION)


def customer_id():
    return str(config.get("google_ads.login_customer_id", "")).replace("-", "").strip()


def customer_info(client, cid):
    from google.ads.googleads.errors import GoogleAdsException
    svc = client.get_service("GoogleAdsService")
    q = ("SELECT customer.id, customer.descriptive_name, customer.currency_code, "
         "customer.time_zone, customer.manager FROM customer LIMIT 1")
    try:
        for row in svc.search(customer_id=cid, query=q):
            c = row.customer
            return {"id": c.id, "name": c.descriptive_name,
                    "currency": c.currency_code, "tz": c.time_zone,
                    "manager": c.manager}
    except GoogleAdsException as e:
        raise GkpError(_explain(e))
    return {}


# ---------------------------------------------------------------- 工具

def _micros(v):
    return round(v / 1_000_000, 2) if v else 0.0


def bucket(v):
    if not v:
        return "无数据"
    if v < 10:
        return "0-10"
    if v < 100:
        return "10-100"
    if v < 1000:
        return "100-1K"
    if v < 10000:
        return "1K-10K"
    return "10K+"


def value_tag(high_bid, currency):
    lo, hi = VALUE_LINE.get(currency, VALUE_LINE["USD"])
    if high_bid >= hi:
        return "极高"
    if high_bid >= lo:
        return "高"
    return ""


def metrics_row(text, m, currency, close_variants=None):
    monthly = ["%d-%02d:%d" % (v.year, MONTHS.get(v.month.name, 0), v.monthly_searches)
               for v in m.monthly_search_volumes]
    comp = m.competition.name if m.competition else ""
    high = _micros(m.high_top_of_page_bid_micros)
    return {
        "关键词": text,
        # GKP 把写法变体(单复数 / 连字符)合并成一组,只回代表词,其余列在这里。
        # 不接这个字段,被合并的词就会被记成「无数据」。
        "变体": list(close_variants or []),
        "月均搜索量": m.avg_monthly_searches,
        "搜索量档位": bucket(m.avg_monthly_searches),
        "竞争程度": COMP_CN.get(comp, comp),
        "竞争指数": m.competition_index,
        "页首出价低": _micros(m.low_top_of_page_bid_micros),
        "页首出价高": high,
        "平均CPC": _micros(getattr(m, "average_cpc_micros", 0)),
        "货币": currency,
        "可行性评分": FEASIBILITY.get(comp, ""),
        "高价值": value_tag(high, currency),
        "近12月": "|".join(monthly),
    }


def _apply_targeting(client, req, geos, lang, partners=False, adult=False, avg_cpc=False):
    req.customer_id = customer_id()
    req.language = resolve_lang(lang)
    for g in geos:
        req.geo_target_constants.append(resolve_geo(g))
    net = client.enums.KeywordPlanNetworkEnum
    req.keyword_plan_network = (net.GOOGLE_SEARCH_AND_PARTNERS if partners
                                else net.GOOGLE_SEARCH)
    req.include_adult_keywords = adult
    if avg_cpc:
        req.historical_metrics_options.include_average_cpc = True


def _check_geos(geos):
    if not geos:
        raise GkpError("必须指定地区。不指定的话 API 返回全球汇总值,对外贸站没意义 —— "
                       "这是 UI 上「默认定位中国」那个坑的 API 版本。")


# ---------------------------------------------------------------- 入口

def check(job=None):
    log = job.log if job else (lambda m: None)
    log("连接 Google Ads API (%s)..." % API_VERSION)
    client = make_client()
    cid = customer_id()
    info = customer_info(client, cid)
    log("账号:%s | 币种 %s | 时区 %s | 经理账号=%s"
        % (info.get("name"), info.get("currency"), info.get("tz"), info.get("manager")))
    log("[1/2] 授权通过")

    from google.ads.googleads.errors import GoogleAdsException
    svc = client.get_service("KeywordPlanIdeaService")
    req = client.get_type("GenerateKeywordHistoricalMetricsRequest")
    _apply_targeting(client, req, ["US"], "en")
    req.keywords.append("stainless steel")
    try:
        resp = svc.generate_keyword_historical_metrics(request=req)
    except GoogleAdsException as e:
        raise GkpError(_explain(e))
    for r in resp.results:
        m = r.keyword_metrics
        log("[2/2] 取数通过:'%s' 月均 %s / 竞争 %s"
            % (r.text, m.avg_monthly_searches, m.competition.name))
        return {"ok": True, "account": info,
                "sample": {"kw": r.text, "volume": m.avg_monthly_searches}}
    log("[2/2] 接口通了但没返回数据 —— 访问级别多半还没到「基本」")
    return {"ok": False, "account": info,
            "message": "接口可达但无数据,检查 Cloud 项目的 Google Ads API 访问级别"}


def volume(keywords, geos, lang="en", partners=False, avg_cpc=True, job=None):
    log = job.log if job else (lambda m: None)
    _check_geos(geos)
    from google.ads.googleads.errors import GoogleAdsException
    client = make_client()
    cid = customer_id()
    currency = customer_info(client, cid).get("currency", "USD")
    svc = client.get_service("KeywordPlanIdeaService")
    words = _dedupe(keywords)
    log("共 %d 个词,地区 %s / 语言 %s" % (len(words), ",".join(geos), lang))
    rows = []
    for i in range(0, len(words), VOLUME_BATCH):
        chunk = words[i:i + VOLUME_BATCH]
        req = client.get_type("GenerateKeywordHistoricalMetricsRequest")
        _apply_targeting(client, req, geos, lang, partners, False, avg_cpc)
        req.keywords.extend(chunk)
        try:
            resp = svc.generate_keyword_historical_metrics(request=req)
        except GoogleAdsException as e:
            raise GkpError(_explain(e))
        got = 0
        for r in resp.results:
            rows.append(metrics_row(r.text, r.keyword_metrics, currency, getattr(r, "close_variants", None)))
            got += 1
        log("  批次 %d:送 %d 回 %d(GKP 自己会去重/丢弃无数据词)"
            % (i // VOLUME_BATCH + 1, len(chunk), got))
    return rows


def ideas(seeds=None, url=None, site=False, geos=None, lang="en",
          min_volume=0, partners=False, avg_cpc=True, job=None):
    log = job.log if job else (lambda m: None)
    _check_geos(geos)
    seeds = _dedupe(seeds or [])
    if not seeds and not url:
        raise GkpError("拓词要么给种子词,要么给页面地址。")
    from google.ads.googleads.errors import GoogleAdsException
    client = make_client()
    cid = customer_id()
    currency = customer_info(client, cid).get("currency", "USD")
    svc = client.get_service("KeywordPlanIdeaService")

    batches = []
    if url:
        batches.append(("url", None))
    for i in range(0, len(seeds), IDEAS_SEED_BATCH):
        batches.append(("kw", seeds[i:i + IDEAS_SEED_BATCH]))
    log("%d 个种子 -> %d 批,地区 %s / 语言 %s"
        % (len(seeds), len(batches), ",".join(geos), lang))

    seen, rows = set(), []
    for kind, chunk in batches:
        req = client.get_type("GenerateKeywordIdeasRequest")
        _apply_targeting(client, req, geos, lang, partners, False, avg_cpc)
        if kind == "url":
            if site:
                req.site_seed.site = url
            else:
                req.url_seed.url = url
            label = url
        else:
            req.keyword_seed.keywords.extend(chunk)
            label = "%d 个种子" % len(chunk)
        try:
            resp = svc.generate_keyword_ideas(request=req)
        except GoogleAdsException as e:
            raise GkpError(_explain(e))
        got = 0
        for r in resp:
            k = r.text.lower()
            if k in seen:
                continue
            seen.add(k)
            rows.append(metrics_row(r.text, r.keyword_idea_metrics, currency, getattr(r, "close_variants", None)))
            got += 1
        log("  %s -> 新增 %d(累计 %d)" % (label, got, len(rows)))

    if min_volume:
        before = len(rows)
        rows = [r for r in rows if (r["月均搜索量"] or 0) >= min_volume]
        log("  按月均搜索量 >= %d 过滤:%d -> %d" % (min_volume, before, len(rows)))
    rows.sort(key=lambda r: r["月均搜索量"] or 0, reverse=True)
    return rows


# ---------------------------------------------------------------- 辅助

def _dedupe(words):
    seen, out = set(), []
    for w in words:
        w = str(w).strip()
        if not w:
            continue
        k = w.lower()
        if k not in seen:
            seen.add(k)
            out.append(w)
    return out


# Word / 飞书 会把连字符自动替换成 ‐ ‑ – — 这些非 ASCII 字符,肉眼看不出来,
# GKP 一律查不到(实测客户文件里 4 个词因此「无数据」)。
_DASHES = re.compile("[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")


def norm_kw(w):
    return _DASHES.sub("-", str(w)).replace("\u00a0", " ").strip()


def parse_keyword_text(text):
    """界面文本框里一行一个词;也兼容直接粘 CSV(只取第一列)。"""
    out = []
    for line in str(text or "").splitlines():
        w = norm_kw(line.split("\t")[0].split(",")[0].strip().strip('"'))
        if w:
            out.append(w)
    if out and out[0].lower() in ("keyword", "keywords", "关键词"):
        out.pop(0)
    return out


def save_csv(rows, prefix, currency=None):
    """utf-8-sig,Excel 双击直接认中文。表头把币种写进出价列名。"""
    cur = currency or (rows[0].get("货币") if rows else "USD")
    path = config.out_dir() / ("%s_%s.csv" % (prefix, datetime.now().strftime("%Y%m%d_%H%M%S")))
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(display_columns(cur))
        for r in rows:
            w.writerow([r.get(k, "") for k in DISPLAY_KEYS])
    return path
