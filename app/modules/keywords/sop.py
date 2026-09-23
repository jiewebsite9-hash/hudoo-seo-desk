# -*- coding: utf-8 -*-
"""按「互旦关键词 SOP V2」总表格式导出。

20 列里分两类:
  · 能算的(8 列) —— 月搜、搜索量区间、全球月搜、竞争程度、页首出价、意图、优先级、金矿、来源渠道
  · 要人判断的(7 列) —— 客户级别、布局角色、目标URL、页面类型、页面状态、该页主词、备注
后者留空但保留列位,拿到表里接着填即可。

判定规则全部来自总表「口径说明」页,改规则请连注释一起改。
"""
import csv
import re
from datetime import datetime

from app import config
from . import gkp
from .locations import COUNTRIES, resolve_lang

COLUMNS = ["序号", "关键词", "词源", "客户级别", "来源渠道", "{市场}月搜", "搜索量区间",
           "全球月搜", "竞争程度", "页首出价低位(USD)", "页首出价高位(USD)", "意图",
           "优先级", "金矿", "布局角色", "目标URL", "页面类型", "页面状态", "该页主词", "备注"]

# 人工判断列 —— 导出时留空。
# 「客户级别」**不在这里**:它是客户提供的数据,不是人工判断。客户词表里填了就该带进总表。
# (实测它不预测优先级 —— 客户按「产品重要性」打分,SOP 按「搜索价值」定级,两把尺子;
#  但定布局角色和目标URL 时要看它,所以必须带过去。)
BLANK = {"布局角色", "目标URL", "页面类型", "页面状态", "该页主词", "备注"}

# ---- 意图判定(口径说明 第 7 条;触发词按真实项目 535 个人工验收词回测对齐)----
# 交易 = 采购意图词;信息 = 问句 / 科普 / 图纸资料;其余归商业(含 vs / types of / 规格 ——
# 这些是买家在比选,广告主也按商业词出价,回测里 type of roller conveyor 出价 $5)
TRANSACTIONAL = re.compile(
    r"\b(manufactur\w*|supplier\w*|factory|factories|distributor\w*|exporter\w*|"
    r"wholesale|bulk|order|for\s+sale|buy|price|prices|pricing|cost|quote|"
    r"custom|customized|oem|odm|vendor|company|companies|near\s+me|hs\s+code)\b", re.I)
INFORMATIONAL = re.compile(
    r"(^(what|how|why|which|when|where|who|is|are|does|do|can)\b|"
    r"\b(guide|tutorial|meaning|definition|diagram|drawing|design|pdf|wikipedia|"
    r"difference\s+between|calculat\w*|standard[s]?)\b)", re.I)


def classify_intent(kw):
    if INFORMATIONAL.search(kw):
        return "信息"
    if TRANSACTIONAL.search(kw):
        return "交易"
    return "商业"


def priority(intent, high_bid_usd, local_volume, competition, mixed=False):
    """口径说明 第 8 条。

    `mixed` = 命中「混杂/泛词清单」——口径原文:「混杂意图词(如 rubber roller、
    steel roller)最高只给 P1」。这是词义判断,没法从数字推出来,靠调用方给清单。

    注意:原文 P0 还有「长尾降为 P1」的补充,但「布局角色」是人工判断列,
    导出时还不知道哪个词会被定成长尾 —— 那一步降级请在表里人工完成。
    """
    if not mixed and intent in ("交易", "商业") and high_bid_usd >= 3 and local_volume >= 20:
        return "P0"
    # SSOT:P1 = 「10–100 长尾且竞争低/中」或「C/T 意图且出价 ¥7–20(≈$1–3)」。
    # 上限 100 不能丢 —— 回测里 6 个 vol=500、出价 $0 的词就是因为没上限被抬成 P1。
    if 10 <= local_volume <= 100 and competition in ("低", "中"):
        return "P1"
    if intent in ("交易", "商业") and high_bid_usd >= 1 and local_volume >= 10:
        return "P1"
    return "P2"


def make_mixed_matcher(words):
    """把「混杂/泛词清单」编译成判定函数。空清单永远返回 False。

    **默认整词匹配,不是子串匹配。** 表里降级的是「整个词组就很泛」的情况:
    `roller manufacturer` 要降级,而 `conveyor roller manufacturer` 是带行业限定的
    合格词、在表里是 P0。用子串匹配会把后者一起误伤。

    要覆盖同族词就用通配符:`rubber roller*` 命中 rubber roller / rubber roller price;
    `*idler*` 命中任何含 idler 的词。
    """
    terms = [re.sub(r"\s+", " ", w.strip().lower()) for w in (words or []) if w.strip()]
    if not terms:
        return lambda kw: False
    parts = []
    for t in terms:
        if "*" in t:
            parts.append(".*".join(re.escape(seg) for seg in t.split("*")))
        else:
            parts.append(re.escape(t))
    pat = re.compile(r"\A(?:%s)\Z" % "|".join(parts), re.I)
    return lambda kw: bool(pat.match(re.sub(r"\s+", " ", kw.strip())))


def gold(intent, high_bid_usd, local_volume, competition):
    """口径说明 第 9 条:有人出高价买这个词,但自然结果竞争不激烈。"""
    if intent == "信息":
        return ""
    if competition in ("低", "中") and high_bid_usd >= 5 and local_volume >= 20:
        return "★"
    if local_volume >= 100 and competition == "低":
        return "★"
    return ""


def build(seeds=None, competitor_sites=None, customer_words=None, mixed_words=None,
          exclude_words=None, market="US", lang="en", min_volume=10,
          usd_rate=None, expand_customer=True, job=None):
    """跑完整条链路并返回 (表头, 总表行, 剔除词行, 统计)。

    `customer_words` 每项可以是纯关键词,也可以是 `词<Tab>中文<Tab>级别`
    (逗号分隔也行) —— 对应「原始词核对」页的客户中文与客户级别两列。

    按 SOP 的实际流程,种子词就是客户给的那批词,没有第二个来源。所以
    `expand_customer=True`(默认)时客户原始词**同时**作为种子做「以关键字拓展」;
    关掉 = 只给这批词补数据不拓(原「补搜索量」)。`seeds` 参数保留给
    程序化调用方另加种子,界面上不再单独有这个框。
    """
    log = job.log if job else (lambda m: None)
    seeds = [s for s in (seeds or []) if s]
    sites = [s.strip() for s in (competitor_sites or []) if s.strip()]
    customer, cust_meta = {}, {}
    for raw in (customer_words or []):
        if not str(raw).strip():
            continue
        # **不能过滤掉中间的空格子**:客户表里常有合并单元格(两行共用一个中文),
        # 传过来就是空串。一旦把空的丢掉,后面的「级别」会顶到「中文」的位置上。
        raw = str(raw)
        # 上传 / 粘贴的表格行一定带 Tab —— 有 Tab 就只按 Tab 切。按逗号也切的话,
        # 中文列里的「槽滚，槽型滚筒」会被劈开,级别顶到中文的位置上(真实客户文件测出来的)。
        if "\t" in raw:
            parts = [x.strip() for x in raw.split("\t")]
        else:
            parts = [x.strip() for x in re.split(r",|，|\|", raw)]
        while parts and not parts[-1]:
            parts.pop()
        # 第一列常常是「序号」—— 纯数字且后面还有内容就丢掉,否则 1/2/3 会变成关键词
        if len(parts) > 1 and re.fullmatch(r"\d{1,5}[.、]?", parts[0]):
            parts = parts[1:]
        if not parts or not parts[0]:
            continue
        w = gkp.norm_kw(parts[0])
        customer[w.lower()] = w
        cust_meta[w.lower()] = {"中文": parts[1] if len(parts) > 1 else "",
                                "级别": parts[2] if len(parts) > 2 else ""}
    market = (market or "US").upper()
    market_cn = COUNTRIES.get(market, ("该市场",))[0]

    if expand_customer and customer:
        # 客户词兼作种子。用原始写法(不是小写键),GKP 对大小写不敏感但日志要好读
        have = {x.lower() for x in seeds}
        extra = [w for w in customer.values() if w.lower() not in have]
        seeds = seeds + extra
        log("客户原始词 %d 个兼作种子拓展" % len(extra))
    if not seeds and not sites and not customer:
        raise gkp.GkpError("至少要给一样:客户原始词或竞品网址。")

    # provenance: 关键词 -> {来源渠道}
    src = {}
    pool = {}      # 小写词 -> 原始写法

    def absorb(rows, channel):
        new = 0
        for r in rows:
            k = r["关键词"].lower()
            if k not in pool:
                pool[k] = r["关键词"]
                new += 1
            src.setdefault(k, set()).add(channel)
        return new

    # ---- 1. 客户原始词 ----
    for k, w in customer.items():
        pool.setdefault(k, w)
        src.setdefault(k, set()).add("客户提供")
    if customer:
        log("客户原始词 %d 个" % len(customer))

    # ---- 2. 关键字拓展 ----
    if seeds:
        rows = gkp.ideas(seeds=seeds, geos=[market], lang=lang, min_volume=0, job=job)
        log("GKP以关键字拓展 -> 新增 %d" % absorb(rows, "GKP以关键字拓展"))

    # ---- 3. 竞品站拓展 ----
    for site in sites:
        path = re.sub(r"^https?://", "", site).strip("/")
        host = path.split("/")[0]
        # 填首页 = 扒整站(site_seed);填到具体页面 = 只看那一页(url_seed)。
        # 靠有没有路径自动判,不用再让人选「整站 / 单页」。
        whole = "/" not in path
        label = "GKP以网站拓展(%s%s)" % (host, "" if whole else " 单页")
        try:
            rows = gkp.ideas(url=site, site=whole, geos=[market], lang=lang,
                             min_volume=0, job=job)
        except Exception as e:
            log("  %s 失败,跳过:%s" % (host, str(e)[:80]))
            continue
        log("%s -> 新增 %d" % (label, absorb(rows, label)))

    words = [pool[k] for k in pool]
    log("合并去重后共 %d 个词,开始补两轮搜索量" % len(words))

    # GKP 返回的文本把连字符换成了空格:送 rubber-coated roller 回 rubber coated roller。
    # 按原键查会落空、客户词被记成「无数据」(真实客户文件里 4 个词就是这么丢的)。
    # 查数一律用「去连字符 + 压空格」的归一键;两种写法会拿到同一行数据,这是对的。
    def gkey(k):
        return re.sub(r"\s+", " ", str(k).lower().replace("-", " ")).strip()

    # ---- 4. 主市场月搜 ----
    local = {gkey(r["关键词"]): r for r in
             gkp.volume(words, geos=[market], lang=lang, job=job)}
    log("%s月搜:拿到 %d 行" % (market_cn, len(local)))

    # ---- 5. 全球月搜(不限地区)----
    world = {gkey(r["关键词"]): r for r in
             _volume_worldwide(words, lang=lang, job=job)}
    log("全球月搜:拿到 %d 行" % len(world))

    # ---- 6. 组表 ----
    rate = float(usd_rate or config.get("defaults.usd_rate", 7.0))
    currency = (list(local.values())[0]["货币"] if local else "USD")
    to_usd = (lambda v: round(v / rate, 2)) if currency != "USD" else (lambda v: v)
    if currency != "USD":
        log("账号币种是 %s,页首出价按 1 USD = %.2f %s 折算成 USD(口径表用 USD)"
            % (currency, rate, currency))

    is_mixed = make_mixed_matcher(mixed_words)
    is_excluded = make_mixed_matcher(exclude_words)
    if mixed_words:
        log("混杂/泛词清单 %d 条,命中的词最高只给 P1" % len([w for w in mixed_words if w.strip()]))
    if exclude_words:
        log("剔除清单 %d 条" % len([w for w in exclude_words if w.strip()]))

    out, cut = [], []

    def drop(kw, lr, reason):
        cut.append({"关键词": kw,
                    "词源": "原始词" if kw.lower() in customer else "拓展词",
                    "{市场}月搜": (lr or {}).get("月均搜索量", ""),
                    "竞争程度": (lr or {}).get("竞争程度", ""),
                    "页首出价高位(USD)": to_usd((lr or {}).get("页首出价高", 0) or 0) or "",
                    "剔除原因": reason})

    for k, kw in pool.items():
        lr = local.get(gkey(k))
        if not lr:
            drop(kw, None, "GKP 无数据")
            continue
        lv = lr["月均搜索量"] or 0
        if is_excluded(kw):
            drop(kw, lr, "命中剔除清单")
            continue
        # 规则⑦只剔拓展词。客户给的词不剔:策略词的定义就是「客户确实做、搜索量低」,
        # 剔了等于把客户的话当没说。它们留在表上按数据给 P2,客户级别列带着,人工提权。
        if min_volume and lv < min_volume and k not in customer:
            drop(kw, lr, "%s月搜 < %d(剔除规则⑦)" % (market_cn, min_volume))
            continue
        wr = world.get(gkey(k)) or {}
        low = to_usd(lr["页首出价低"] or 0)
        high = to_usd(lr["页首出价高"] or 0)
        comp = lr["竞争程度"] or ""
        intent = classify_intent(kw)
        out.append({
            "关键词": kw,
            "词源": "原始词" if k in customer else "拓展词",
            "客户级别": (cust_meta.get(k) or {}).get("级别", ""),
            "来源渠道": "；".join(sorted(src.get(k, {"—"}))),
            "{市场}月搜": lv,
            "搜索量区间": lr["搜索量档位"],
            "全球月搜": wr.get("月均搜索量", ""),
            "竞争程度": comp,
            "页首出价低位(USD)": low or "",
            "页首出价高位(USD)": high or "",
            "意图": intent,
            "优先级": priority(intent, high, lv, comp, is_mixed(kw)),
            "金矿": gold(intent, high, lv, comp),
        })

    # 排序:优先级 -> 金矿 -> 月搜
    rank = {"P0": 0, "P1": 1, "P2": 2}
    out.sort(key=lambda r: (rank.get(r["优先级"], 9), 0 if r["金矿"] else 1,
                            -(r["{市场}月搜"] or 0)))
    for i, r in enumerate(out, 1):
        r["序号"] = i
        for c in BLANK:
            r.setdefault(c, "")

    header = [c.replace("{市场}", market_cn) for c in COLUMNS]
    stats = {
        "总词数": len(out),
        "剔除": len(cut),
        "P0": sum(1 for r in out if r["优先级"] == "P0"),
        "P1": sum(1 for r in out if r["优先级"] == "P1"),
        "P2": sum(1 for r in out if r["优先级"] == "P2"),
        "金矿": sum(1 for r in out if r["金矿"]),
        "原始词": sum(1 for r in out if r["词源"] == "原始词"),
        "币种": currency,
        "汇率": rate if currency != "USD" else None,
        "市场": market_cn,
    }
    log("成表 %d 行(剔除 %d):P0 %d / P1 %d / P2 %d,金矿 %d"
        % (stats["总词数"], len(cut), stats["P0"], stats["P1"], stats["P2"], stats["金矿"]))
    cut.sort(key=lambda r: -(r["{市场}月搜"] or 0))
    stats["_customer_meta"] = cust_meta
    stats["_local"] = {k: local[gkey(k)] for k in customer if gkey(k) in local}
    stats["_world"] = {k: world.get(gkey(k), {}) for k in customer}
    stats["_to_usd_rate"] = rate if currency != "USD" else 1
    return header, out, cut, stats


def _volume_worldwide(words, lang, job):
    """全球月搜 = 不传 geo_target_constants。

    注意这绕开了 gkp.volume 的「必须指定地区」拦截 —— 那个拦截是防用户误操作
    (不指定地区拿到的是全球汇总值,对单一市场没意义),而这里**就是要全球值**。
    """
    from google.ads.googleads.errors import GoogleAdsException
    client = gkp.make_client()
    cid = gkp.customer_id()
    currency = gkp.customer_info(client, cid).get("currency", "USD")
    svc = client.get_service("KeywordPlanIdeaService")
    rows = []
    for i in range(0, len(words), gkp.VOLUME_BATCH):
        chunk = words[i:i + gkp.VOLUME_BATCH]
        req = client.get_type("GenerateKeywordHistoricalMetricsRequest")
        req.customer_id = cid
        req.language = resolve_lang(lang)
        req.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
        req.keywords.extend(chunk)
        try:
            resp = svc.generate_keyword_historical_metrics(request=req)
        except GoogleAdsException as e:
            raise gkp.GkpError(gkp._explain(e))
        for r in resp.results:
            rows.append(gkp.metrics_row(r.text, r.keyword_metrics, currency))
    return rows


def save_csv(header, rows):
    path = config.out_dir() / ("SOP总表_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    keys = COLUMNS
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(k, "") for k in keys])
    return path


CUT_COLUMNS = ["关键词", "词源", "{市场}月搜", "竞争程度", "页首出价高位(USD)", "剔除原因"]
CHECK_COLUMNS = ["序号", "关键词", "客户中文", "客户级别", "{市场}月搜", "全球月搜",
                 "竞争程度", "页首出价高位(USD)"]


def save_workbook(header, rows, cut, stats, params):
    """一个 xlsx 四张表,对齐飞书那本工作簿的结构。

    第 3 页「URL布词视图」不生成 —— 它是人工布词结果(目标URL / 布局角色 / 该页主词)
    的透视,那几列填完之前没法算。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    mk = stats.get("市场", "该市场")
    sub = lambda cols: [c.replace("{市场}", mk) for c in cols]
    wb = Workbook()
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="4A5568")

    def sheet(title, cols, data_rows, widths=None):
        ws = wb.create_sheet(title)
        ws.append(cols)
        for c in ws[1]:
            c.font = head_font
            c.fill = head_fill
            c.alignment = Alignment(vertical="center")
        for r in data_rows:
            ws.append(r)
        ws.freeze_panes = "A2"
        for i, w in enumerate(widths or [], 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
        return ws

    # 1. 总表
    sheet("1.关键词-URL总表", header,
          [[r.get(k, "") for k in COLUMNS] for r in rows],
          [6, 34, 8, 9, 30, 10, 11, 10, 9, 15, 15, 7, 8, 6, 9, 24, 10, 10, 26, 30])

    # 2. 原始词核对
    meta = stats.get("_customer_meta") or {}
    loc = stats.get("_local") or {}
    wor = stats.get("_world") or {}
    rate = stats.get("_to_usd_rate") or 1
    chk = []
    for i, (k, m) in enumerate(sorted(meta.items(),
                                      key=lambda kv: -((loc.get(kv[0]) or {}).get("月均搜索量") or 0)), 1):
        lr = loc.get(k) or {}
        chk.append([i, (lr.get("关键词") or k), m.get("中文", ""), m.get("级别", ""),
                    lr.get("月均搜索量", ""), (wor.get(k) or {}).get("月均搜索量", ""),
                    lr.get("竞争程度", ""),
                    round((lr.get("页首出价高") or 0) / rate, 2) or ""])
    if chk:
        sheet("2.原始词核对", sub(CHECK_COLUMNS), chk, [6, 34, 18, 9, 10, 10, 9, 15])

    # 4. 剔除词
    sheet("4.剔除词", sub(CUT_COLUMNS),
          [[r.get(k, "") for k in CUT_COLUMNS] for r in cut], [34, 8, 10, 9, 15, 26])

    # 5. 口径说明 —— 记录本次实际生效的参数,别人拿到表能复现
    notes = [
        ["数据来源", "Google Ads API KeywordPlanIdeaService(= 关键词规划师 GKP 口径)。"
                     "拓词走 generate_keyword_ideas,补量走 generate_keyword_historical_metrics。"
                     "拉取日期 %s。" % datetime.now().strftime("%Y-%m-%d")],
        ["地区 / 语言", "%s月搜 = %s · %s;全球月搜 = 不限地区 · %s。"
                    % (mk, params.get("market"), params.get("lang"), params.get("lang"))],
        ["词源", "原始词 = 客户提供的词表;拓展词 = GKP 关键字拓展 / 网站拓展。"
                 "来源渠道列记录每个词由哪一路拿到,多路命中用「；」连接。"],
        ["竞争程度", "GKP 高 / 中 / 低(互旦关键词 SOP V2 唯一口径;KD 不进最终表)。"],
        ["页首出价", "GKP 页首出价低位 / 高位。账号币种 %s%s"
                 % (stats.get("币种"),
                    ",已按 1 USD = %s %s 折算成 USD。" % (stats.get("汇率"), stats.get("币种"))
                    if stats.get("汇率") else ",直接为 USD。")],
        ["意图", "交易 = 含 manufacturer / supplier / for sale / custom / price 等采购词;"
                 "信息 = 问句(how/what/which…)/ guide / design / drawing / pdf / 标准等;"
                 "其余归商业(含 vs / types of / 规格 —— 买家在比选,广告主按商业词出价)。"],
        ["优先级", "P0 = 交易/商业意图 且 页首出价高位 ≥ $3 且 %s月搜 ≥ 20;"
                   "P1 = %s月搜 10–100 且竞争低/中,或 C/T 意图且出价 ≥ $1;P2 = 其余。"
                   "最低月搜(规则⑦)只剔拓展词,客户原始词豁免、留在表上按数据定级。"
                   "命中混杂/泛词清单的词最高只给 P1。"
                   "原文「长尾降为 P1」依赖人工的布局角色列,导出时未执行,请人工补。" % (mk, mk)],
        ["金矿 ★", "竞争低/中 且 页首出价高位 ≥ $5 且 %s月搜 ≥ 20,"
                   "或 %s月搜 ≥ 100 且竞争低;信息型词不给。" % (mk, mk)],
        ["剔除", "本次自动剔除 %d 个词,原因见第 4 页。自动执行的只有「%s月搜 < %d」"
                 "和「命中剔除清单」两条;口径里其余剔除规则(泛词/跨行业/系统级/"
                 "不生产/品牌平台词)属词义判断,请把要剔的词加进剔除清单再跑。"
                 % (len(cut), mk, params.get("min_volume"))],
        ["混杂/泛词清单", "整词匹配,支持 * 通配。本次 %d 条。" % len(params.get("mixed") or [])],
        ["人工列", "客户级别 / 布局角色 / 目标URL / 页面类型 / 页面状态 / 该页主词 / 备注 "
                  "这 7 列由人工填写,导出时留空。"],
        ["未生成的页", "「URL布词视图」是人工布词结果的透视,需先在总表里填完 "
                     "目标URL / 布局角色 / 该页主词 才能生成。"],
        ["产出规模", "总表 %d 词(P0 %d / P1 %d / P2 %d,金矿 %d),原始词 %d,剔除 %d。"
                   % (stats["总词数"], stats["P0"], stats["P1"], stats["P2"],
                      stats["金矿"], stats["原始词"], len(cut))],
    ]
    ws = sheet("5.口径说明", ["项目", "说明"], notes, [18, 120])
    for row in ws.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    wb.remove(wb["Sheet"])
    path = config.out_dir() / ("SOP工作簿_%s.xlsx" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    wb.save(path)
    return path
