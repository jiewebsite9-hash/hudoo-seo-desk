# -*- coding: utf-8 -*-
"""地区 / 语言常量。

Google 的「国家」地理定位 ID 有个规律:**2000 + ISO 3166-1 数字代码**
(美国 840 -> 2840, 英国 826 -> 2826)。省/市一级没有这个规律,要用
`gkp.py geo <地名>` 现场问 API。

语言常量 ID 没有规律,下表是常用的;**不确定就跑 `gkp.py lang` 从 API 拉全表核对**,
别照抄别人博客里的数字。
"""

# 常用市场(外贸站命中率最高的那批)
GEO = {
    "US": 2840, "GB": 2826, "UK": 2826, "DE": 2276, "FR": 2250, "IT": 2380,
    "ES": 2724, "NL": 2528, "BE": 2056, "AT": 2040, "CH": 2756, "SE": 2752,
    "NO": 2578, "DK": 2208, "FI": 2246, "IE": 2372, "PT": 2620, "PL": 2616,
    "CZ": 2203, "GR": 2300, "RO": 2642, "HU": 2348, "UA": 2804, "RU": 2643,
    "TR": 2792, "CA": 2124, "MX": 2484, "BR": 2076, "AR": 2032, "CL": 2152,
    "CO": 2170, "PE": 2604, "AU": 2036, "NZ": 2554, "JP": 2392, "KR": 2410,
    "CN": 2156, "HK": 2344, "TW": 2158, "SG": 2702, "MY": 2458, "TH": 2764,
    "VN": 2704, "ID": 2360, "PH": 2608, "IN": 2356, "AE": 2784, "SA": 2682,
    "IL": 2376, "EG": 2818, "ZA": 2710, "NG": 2566,
}

# 常用语言
LANG = {
    "en": 1000, "de": 1001, "fr": 1002, "es": 1003, "it": 1004, "ja": 1005,
    "da": 1009, "nl": 1010, "fi": 1011, "ko": 1012, "no": 1013, "pt": 1014,
    "sv": 1015, "zh_CN": 1017, "zh_TW": 1018, "ar": 1019, "bg": 1020,
    "cs": 1021, "el": 1022, "hi": 1023, "hu": 1024, "id": 1025, "is": 1026,
    "he": 1027, "lv": 1028, "lt": 1029, "pl": 1030, "ru": 1031, "ro": 1032,
    "sk": 1033, "sl": 1034, "sr": 1035, "uk": 1036, "tr": 1037, "ca": 1038,
    "hr": 1039, "vi": 1040, "ur": 1041, "tl": 1042, "et": 1043, "th": 1044,
}


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
    raise SystemExit(
        "认不出地区 %r。用两位国家代码(US/DE/FR...)、数字 ID,"
        "或先跑 `gkp.py geo \"%s\"` 查它的 ID。" % (value, value)
    )


def resolve_lang(value):
    """接受 'en' / 'zh_CN' / 裸数字 ID / 'languageConstants/1000'。"""
    v = str(value).strip()
    if v.startswith("languageConstants/"):
        return v
    if v.isdigit():
        return "languageConstants/%s" % v
    for key in (v, v.replace("-", "_")):
        if key in LANG:
            return "languageConstants/%d" % LANG[key]
        low = key.lower()
        for k in LANG:
            if k.lower() == low:
                return "languageConstants/%d" % LANG[k]
    raise SystemExit(
        "认不出语言 %r。用 en/de/fr/zh_CN 这类代码或数字 ID,"
        "或跑 `gkp.py lang` 从 API 拉全表。" % value
    )
