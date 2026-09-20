# -*- coding: utf-8 -*-
"""地区 / 语言常量,以及「选了地区自动带出语言」的对应表。

国家级地理定位 ID 有个规律:**2000 + ISO 3166-1 数字代码**(美国 840 -> 2840)。
省/市一级没有这个规律,要用 Google Ads 的地点建议接口现查。
语言常量 ID 没有规律,下表是常用的。

`default_lang` 是「在这个市场做外贸 SEO 通常该查哪种语言」,不是该国官方语言的
学术答案 —— 例如印度、新加坡、菲律宾、马来西亚的 B2B 搜索以英语为主,所以给 en。
选完地区仍可手动改语言。
"""

# 代码: (中文名, geo_target ID, 默认语言, 分组)
COUNTRIES = {
    "US": ("美国", 2840, "en", "北美"),
    "CA": ("加拿大", 2124, "en", "北美"),
    "MX": ("墨西哥", 2484, "es", "北美"),

    "GB": ("英国", 2826, "en", "欧洲"),
    "DE": ("德国", 2276, "de", "欧洲"),
    "FR": ("法国", 2250, "fr", "欧洲"),
    "IT": ("意大利", 2380, "it", "欧洲"),
    "ES": ("西班牙", 2724, "es", "欧洲"),
    "NL": ("荷兰", 2528, "nl", "欧洲"),
    "BE": ("比利时", 2056, "fr", "欧洲"),
    "AT": ("奥地利", 2040, "de", "欧洲"),
    "CH": ("瑞士", 2756, "de", "欧洲"),
    "SE": ("瑞典", 2752, "sv", "欧洲"),
    "NO": ("挪威", 2578, "no", "欧洲"),
    "DK": ("丹麦", 2208, "da", "欧洲"),
    "FI": ("芬兰", 2246, "fi", "欧洲"),
    "IE": ("爱尔兰", 2372, "en", "欧洲"),
    "PT": ("葡萄牙", 2620, "pt", "欧洲"),
    "PL": ("波兰", 2616, "pl", "欧洲"),
    "CZ": ("捷克", 2203, "cs", "欧洲"),
    "GR": ("希腊", 2300, "el", "欧洲"),
    "RO": ("罗马尼亚", 2642, "ro", "欧洲"),
    "HU": ("匈牙利", 2348, "hu", "欧洲"),
    "UA": ("乌克兰", 2804, "uk", "欧洲"),
    "RU": ("俄罗斯", 2643, "ru", "欧洲"),
    "TR": ("土耳其", 2792, "tr", "欧洲"),

    "JP": ("日本", 2392, "ja", "亚太"),
    "KR": ("韩国", 2410, "ko", "亚太"),
    "CN": ("中国大陆", 2156, "zh_CN", "亚太"),
    "HK": ("中国香港", 2344, "zh_TW", "亚太"),
    "TW": ("中国台湾", 2158, "zh_TW", "亚太"),
    "SG": ("新加坡", 2702, "en", "亚太"),
    "MY": ("马来西亚", 2458, "en", "亚太"),
    "TH": ("泰国", 2764, "th", "亚太"),
    "VN": ("越南", 2704, "vi", "亚太"),
    "ID": ("印度尼西亚", 2360, "id", "亚太"),
    "PH": ("菲律宾", 2608, "en", "亚太"),
    "IN": ("印度", 2356, "en", "亚太"),
    "AU": ("澳大利亚", 2036, "en", "亚太"),
    "NZ": ("新西兰", 2554, "en", "亚太"),

    "AE": ("阿联酋", 2784, "ar", "中东非洲"),
    "SA": ("沙特阿拉伯", 2682, "ar", "中东非洲"),
    "IL": ("以色列", 2376, "he", "中东非洲"),
    "EG": ("埃及", 2818, "ar", "中东非洲"),
    "ZA": ("南非", 2710, "en", "中东非洲"),
    "NG": ("尼日利亚", 2566, "en", "中东非洲"),

    "BR": ("巴西", 2076, "pt", "拉美"),
    "AR": ("阿根廷", 2032, "es", "拉美"),
    "CL": ("智利", 2152, "es", "拉美"),
    "CO": ("哥伦比亚", 2170, "es", "拉美"),
    "PE": ("秘鲁", 2604, "es", "拉美"),
}

GROUP_ORDER = ["北美", "欧洲", "亚太", "中东非洲", "拉美"]

# 代码: (中文名, language_constant ID)
LANGUAGES = {
    "en": ("英语", 1000), "de": ("德语", 1001), "fr": ("法语", 1002),
    "es": ("西班牙语", 1003), "it": ("意大利语", 1004), "ja": ("日语", 1005),
    "da": ("丹麦语", 1009), "nl": ("荷兰语", 1010), "fi": ("芬兰语", 1011),
    "ko": ("韩语", 1012), "no": ("挪威语", 1013), "pt": ("葡萄牙语", 1014),
    "sv": ("瑞典语", 1015), "zh_CN": ("中文(简体)", 1017), "zh_TW": ("中文(繁体)", 1018),
    "ar": ("阿拉伯语", 1019), "bg": ("保加利亚语", 1020), "cs": ("捷克语", 1021),
    "el": ("希腊语", 1022), "hi": ("印地语", 1023), "hu": ("匈牙利语", 1024),
    "id": ("印尼语", 1025), "is": ("冰岛语", 1026), "he": ("希伯来语", 1027),
    "lv": ("拉脱维亚语", 1028), "lt": ("立陶宛语", 1029), "pl": ("波兰语", 1030),
    "ru": ("俄语", 1031), "ro": ("罗马尼亚语", 1032), "sk": ("斯洛伐克语", 1033),
    "sl": ("斯洛文尼亚语", 1034), "sr": ("塞尔维亚语", 1035), "uk": ("乌克兰语", 1036),
    "tr": ("土耳其语", 1037), "ca": ("加泰罗尼亚语", 1038), "hr": ("克罗地亚语", 1039),
    "vi": ("越南语", 1040), "ur": ("乌尔都语", 1041), "tl": ("菲律宾语", 1042),
    "et": ("爱沙尼亚语", 1043), "th": ("泰语", 1044),
}

# 常见写法兜底 —— 手填或从别处粘来的代码不一定跟上表一致。
# 「zh」就是典型:表里只有 zh_CN / zh_TW,不兜底会直接报错。
LANG_ALIASES = {
    "zh": "zh_CN", "zh_cn": "zh_CN", "zh_hans": "zh_CN",
    "cn": "zh_CN", "chinese": "zh_CN",
    "zh_tw": "zh_TW", "zh_hant": "zh_TW", "zh_hk": "zh_TW",
    "en_us": "en", "en_gb": "en", "english": "en",
    "pt_br": "pt", "pt_pt": "pt",
    "nb": "no", "nn": "no",
    "iw": "he", "in": "id", "fil": "tl", "jp": "ja", "kr": "ko",
}

# 旧接口保留:代码 -> ID
GEO = {c: v[1] for c, v in COUNTRIES.items()}
GEO["UK"] = GEO["GB"]
LANG = {c: v[1] for c, v in LANGUAGES.items()}


class TargetingError(ValueError):
    """地区/语言认不出来。

    **必须是 Exception 的子类** —— 这里原先抛的是 SystemExit,而 SystemExit
    属于 BaseException,在作业线程里会绕过 `except Exception`,
    结果是作业永远卡在 running、界面一直转圈且不报错。
    """


def resolve_geo(value):
    """接受 'US' / 'us' / 裸数字 ID / 'geoTargetConstants/2840',统一返回资源名。"""
    v = str(value).strip()
    if v.startswith("geoTargetConstants/"):
        return v
    if v.isdigit():
        return "geoTargetConstants/%s" % v
    key = v.upper()
    if key in GEO:
        return "geoTargetConstants/%d" % GEO[key]
    raise TargetingError(
        "认不出地区 %r。用两位国家代码(US / DE / FR…)或数字 ID;"
        "省市一级要先用 Google Ads 的地点建议接口查 ID。" % value)


def resolve_lang(value):
    """接受 'en' / 'zh_CN' / 'zh' / 裸数字 ID / 'languageConstants/1000'。"""
    v = str(value).strip()
    if v.startswith("languageConstants/"):
        return v
    if v.isdigit():
        return "languageConstants/%s" % v
    if v in LANG:
        return "languageConstants/%d" % LANG[v]
    low = v.lower().replace("-", "_")
    if low in LANG_ALIASES:
        return "languageConstants/%d" % LANG[LANG_ALIASES[low]]
    for k in LANG:
        if k.lower() == low:
            return "languageConstants/%d" % LANG[k]
    raise TargetingError(
        "认不出语言 %r。常用代码:en / de / fr / es / zh_CN / ja …"
        "(界面的语言下拉里列了全部可选项)" % value)


def default_lang_for(country_code):
    """选了地区之后默认带出哪种语言。"""
    row = COUNTRIES.get(str(country_code).strip().upper())
    return row[2] if row else "en"


def options():
    """喂给界面下拉框的数据 —— 后端是唯一事实来源,前端不再复制一份。"""
    groups = []
    for g in GROUP_ORDER:
        items = [{"code": c, "name": v[0], "lang": v[2]}
                 for c, v in COUNTRIES.items() if v[3] == g]
        items.sort(key=lambda x: x["name"])
        groups.append({"group": g, "items": items})
    langs = [{"code": c, "name": "%s (%s)" % (v[0], c)}
             for c, v in sorted(LANGUAGES.items(), key=lambda kv: kv[1][1])]
    return {"countries": groups, "languages": langs}
