# -*- coding: utf-8 -*-
"""从做过的项目里学剔除清单。

输入两份词:**保留词**(上过总表的)和**剔除词**(被剔掉的)。
输出一份可直接用的剔除模式清单,零 AI、零成本、结果可复现。

算法就三步,但第二步是全部要害:

  1. 从剔除词里抽 1/2/3-gram 当候选
  2. **把每个候选当成模式 `*gram*` 真跑一遍保留词,开火的一律丢弃**
  3. 去冗余:能被更短的模式覆盖的长模式丢掉

第 2 步不能省。n-gram 统计和通配符匹配不是一回事 —— `system` 按词统计可能只在
两个保留词里出现、看着很安全,但 `*system*` 按子串会命中一大片。
实测跳过这一步误杀率 54%,做了之后 0%。
"""
import re
from collections import Counter

TOKEN = re.compile(r"[a-z0-9]+")


def _grams(words, max_n=3):
    c = Counter()
    for w in words:
        t = TOKEN.findall(str(w).lower())
        for n in range(1, max_n + 1):
            for i in range(len(t) - n + 1):
                c[" ".join(t[i:i + n])] += 1
    return c


def _norm(w):
    return re.sub(r"\s+", " ", str(w).strip().lower())


def _fires_on(gram, words):
    """这个 gram 当成 *gram* 模式,会命中多少个词。"""
    pat = re.compile(r"\A.*%s.*\Z" % re.escape(gram), re.I)
    return sum(1 for w in words if pat.match(_norm(w)))


def learn(cut_words, keep_words, min_support=8, max_n=3, allow_false_kill=0):
    """返回 (清单, 指标, 建议的产品核心词)。

    `allow_false_kill` = 允许一个模式误杀几个保留词。默认 0(最严格)。
    历史数据本身有噪音时可以放到 1-2 换更高召回。
    """
    cut = [_norm(w) for w in cut_words if str(w).strip()]
    keep = [_norm(w) for w in keep_words if str(w).strip()]
    if not cut:
        raise ValueError("剔除词一个都没有,学不出东西。")

    cands = [(n, g) for g, n in _grams(cut, max_n).items() if n >= min_support]
    cands.sort(key=lambda x: -x[0])

    kept, rejected = [], []
    for n, g in cands:
        fk = _fires_on(g, keep) if keep else 0
        if fk > allow_false_kill:
            if n >= min_support * 3:          # 只记高频的,给人看「为什么没收这条」
                rejected.append({"pattern": g, "cut_hits": n, "would_kill": fk})
            continue
        kept.append({"gram": g, "cut_hits": _fires_on(g, cut), "false_kill": fk})

    # 去冗余:*conveyor system* 已经能命中 conveyor systems,后者就不必再留
    kept.sort(key=lambda x: (len(x["gram"]), -x["cut_hits"]))
    final = []
    for item in kept:
        if any(other["gram"] in item["gram"] for other in final):
            continue
        final.append(item)
    final.sort(key=lambda x: -x["cut_hits"])

    # 自检:把学出来的清单整体跑一遍
    patterns = [x["gram"] for x in final]
    big = re.compile("|".join(r"\A.*%s.*\Z" % re.escape(p) for p in patterns), re.I) \
        if patterns else None
    hit = sum(1 for w in cut if big and big.match(w))
    fk = sum(1 for w in keep if big and big.match(w))

    rows = [{"模式": "*%s*" % x["gram"], "剔除中命中": x["cut_hits"],
             "误杀保留词": x["false_kill"],
             "样例": " / ".join([w for w in cut if x["gram"] in w][:3])}
            for x in final]

    metrics = {
        "剔除词总数": len(cut),
        "保留词总数": len(keep),
        "学出模式数": len(final),
        "候选数": len(cands),
        "召回": round(100 * hit / max(len(cut), 1), 1),
        "误杀": round(100 * fk / max(len(keep), 1), 1),
        "命中剔除词": hit,
        "误杀数": fk,
    }
    return rows, metrics, suggest_core(keep, patterns), rejected[:12]


def suggest_core(keep_words, patterns, top=12):
    """从保留词里挑「产品核心词」,给剔除时的豁免层用。

    命中剔除清单但含核心词的词应当放行 —— 比如 `conveyor belt rollers`
    含 conveyor belt,但它是自家的滚筒产品。实测这一层把误杀从 13.8% 压到 1.6%。
    """
    if not keep_words:
        return []
    c = _grams([_norm(w) for w in keep_words], max_n=1)
    out = []
    for g, n in c.most_common(80):
        if len(g) < 4 or g.isdigit():
            continue
        if any(p in g or g in p for p in patterns):   # 已经是剔除模式的别当核心词
            continue
        out.append({"词": g, "保留词中出现": n})
        if len(out) >= top:
            break
    return out


# ---------------------------------------------------------------- 清单库

def _lib_dir():
    from app import config
    d = config.ROOT / "data" / "lists"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_list(name, rows, metrics, core, note=""):
    """存进行业清单库。同名覆盖 —— 学第二遍通常就是想更新它。"""
    import json
    import re as _re
    from datetime import datetime
    safe = _re.sub(r"[^\w一-龥.-]+", "_", str(name).strip()) or "未命名"
    payload = {
        "name": name, "note": note,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": metrics,
        "patterns": [r["模式"] for r in rows],
        "core_words": [c["词"] for c in core],
        "detail": rows,
    }
    path = _lib_dir() / (safe + ".json")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path.name


def list_saved():
    import json
    out = []
    for f in sorted(_lib_dir().glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"file": f.name, "name": d.get("name", f.stem),
                    "saved_at": d.get("saved_at", ""), "note": d.get("note", ""),
                    "patterns": len(d.get("patterns") or []),
                    "recall": (d.get("metrics") or {}).get("召回"),
                    "false_kill": (d.get("metrics") or {}).get("误杀")})
    return out


def load_list(fname):
    import json
    f = _lib_dir() / fname
    if not f.exists() or f.parent != _lib_dir():
        raise KeyError("清单不存在:%s" % fname)
    return json.loads(f.read_text(encoding="utf-8"))


def save_xlsx(rows, metrics, core, rejected, name="学出的剔除清单"):
    """导出成 xlsx:清单 / 建议核心词 / 被否掉的候选 / 指标。"""
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from app import config

    wb = Workbook()
    hf, hp = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="4A5568")

    def sheet(title, cols, data, widths):
        ws = wb.create_sheet(title)
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill = hf, hp
        for r in data:
            ws.append(r)
        ws.freeze_panes = "A2"
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
        return ws

    sheet("剔除清单", ["模式", "剔除中命中", "误杀保留词", "样例"],
          [[r["模式"], r["剔除中命中"], r["误杀保留词"], r["样例"]] for r in rows],
          [30, 12, 12, 70])
    sheet("建议产品核心词", ["词", "保留词中出现"],
          [[c["词"], c["保留词中出现"]] for c in core], [22, 14])
    if rejected:
        sheet("被否掉的候选", ["模式", "剔除中命中", "会误杀保留词"],
              [[r["pattern"], r["cut_hits"], r["would_kill"]] for r in rejected],
              [30, 14, 16])
    ws = sheet("指标", ["项目", "值"], [[k, v] for k, v in metrics.items()], [18, 60])
    ws.append([])
    ws.append(["怎么用", "把「剔除清单」那一列粘进 SOP 总表页的剔除清单框即可。"])
    ws.append(["", "「建议产品核心词」是豁免层:命中剔除清单但含这些词的词应当放行"])
    ws.append(["", "(例如 conveyor belt rollers 含 conveyor belt,但它是自家滚筒产品)。"])
    for row in ws.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    wb.remove(wb["Sheet"])
    path = config.out_dir() / ("剔除清单_%s.xlsx" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    wb.save(path)
    return path
