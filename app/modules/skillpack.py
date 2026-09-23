# -*- coding: utf-8 -*-
"""skill 内容包的导出与导入。

为什么需要这个:skill 内容包是使用方的内部资产,**不进公开仓库**,
所以程序发出去之后对方机器上是空的。开发机上它恰好落在 ~/.claude/skills
(装了 Claude Code 才有),这是个假象 —— 团队成员机器上根本没这个目录。

做法:开发机导出成 zip,连同程序一起发给对方,对方在设置页点一下导入,
解压进自己的数据目录。
"""
import io
import re
import zipfile
from pathlib import Path

from app import config


class SkillPackError(RuntimeError):
    pass


# 不打进包里的东西
SKIP_DIRS = {"__pycache__", ".git", ".svn", "node_modules", ".idea", ".vscode"}
SKIP_SUFFIX = {".pyc", ".pyo", ".tmp", ".log"}


def list_skills(root=None):
    root = Path(root) if root else config.skills_dir()
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name in SKIP_DIRS:
            continue
        if not (d / "SKILL.md").exists():
            continue
        files = [f for f in d.rglob("*") if f.is_file()]
        out.append({"name": d.name, "files": len(files),
                    "kb": round(sum(f.stat().st_size for f in files) / 1024, 1)})
    return out


def export_zip(names=None, root=None):
    """把 skill 打成 zip 字节流。names 为空则全打。"""
    root = Path(root) if root else config.skills_dir()
    if not root.exists():
        raise SkillPackError("skills 目录不存在:%s" % root)
    want = set(names or [])
    buf = io.BytesIO()
    n_skill = n_file = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in SKIP_DIRS:
                continue
            if not (d / "SKILL.md").exists():
                continue
            if want and d.name not in want:
                continue
            n_skill += 1
            for f in sorted(d.rglob("*")):
                if not f.is_file() or f.suffix.lower() in SKIP_SUFFIX:
                    continue
                if any(p in SKIP_DIRS for p in f.relative_to(root).parts):
                    continue
                z.write(f, f.relative_to(root).as_posix())
                n_file += 1
    if not n_skill:
        raise SkillPackError("没有可导出的 skill(目录下每个 skill 要有 SKILL.md)。")
    return buf.getvalue(), n_skill, n_file


def _safe_target(base, member_name):
    """挡 zip slip:压缩包里写 ../../ 就能往目录外面扔文件。"""
    name = member_name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise SkillPackError("压缩包里有绝对路径,拒绝解压:%s" % member_name)
    target = (base / name).resolve()
    if not str(target).startswith(str(base.resolve())):
        raise SkillPackError("压缩包里有跳出目录的路径,拒绝解压:%s" % member_name)
    return target


def import_zip(raw, dest=None, overwrite=True, log=None):
    """解压 skill 包到数据目录。返回统计。"""
    log = log or (lambda m: None)
    dest = Path(dest) if dest else (config.HOME / "skills")
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:
        raise SkillPackError("这不是个有效的 zip:%s" % e)

    members = [m for m in z.infolist() if not m.is_dir()]
    if not members:
        raise SkillPackError("压缩包是空的。")

    # 有些压缩包最外层多套了一层目录(例如 skills/xxx/SKILL.md),自动剥掉
    tops = {m.filename.replace("\\", "/").split("/")[0] for m in members}
    strip = ""
    if len(tops) == 1:
        only = tops.pop()
        has_inside = any(
            m.filename.replace("\\", "/").endswith("/SKILL.md")
            and m.filename.replace("\\", "/").count("/") >= 2 for m in members)
        if has_inside:
            strip = only + "/"
            log("压缩包最外层是「%s」,解压时自动剥掉这一层" % only)

    dest.mkdir(parents=True, exist_ok=True)
    skills, files, skipped = set(), 0, 0
    for m in members:
        name = m.filename.replace("\\", "/")
        if strip and name.startswith(strip):
            name = name[len(strip):]
        if not name or any(p in SKIP_DIRS for p in name.split("/")):
            continue
        if Path(name).suffix.lower() in SKIP_SUFFIX:
            continue
        target = _safe_target(dest, name)
        if target.exists() and not overwrite:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(z.read(m))
        files += 1
        parts = name.split("/")
        if len(parts) > 1:
            skills.add(parts[0])

    ok = [s for s in skills if (dest / s / "SKILL.md").exists()]
    log("解压 %d 个文件,%d 个 skill 目录,其中 %d 个带 SKILL.md"
        % (files, len(skills), len(ok)))
    if not ok:
        raise SkillPackError(
            "解压完了但一个带 SKILL.md 的 skill 都没有 —— 包的目录结构可能不对。"
            "正确结构是:<skill名>/SKILL.md,可以多层但 SKILL.md 要在 skill 目录的根上。")
    return {"dest": str(dest), "files": files, "skills": sorted(ok),
            "skipped": skipped}
