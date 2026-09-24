# -*- coding: utf-8 -*-
"""配置加载。

优先级:环境变量 > config.local.yaml > config.example.yaml 里的默认值。
凭据只从本机读,永远不写回仓库目录以外的地方,也永远不打印原值。
"""
import os
from pathlib import Path

# 打包成 exe 后 __file__ 在临时解包目录,程序目录要按 exe 所在目录找
import sys
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent


def _home():
    """**可写数据放哪。** 程序目录只读,配置/数据/导出全走这里。

    这个工具是发给每个人装在自己机器上跑的,所以:
      · 配置不能躺在程序目录 —— 更新程序时替换文件夹会把人家的凭据冲掉
      · 一台机器多个用户要各用各的
      · 导出和数据库同理

    解析顺序:
      1. 环境变量 HUDOO_SEO_DESK_HOME —— 想放哪放哪,也方便做绿色版
      2. 程序目录下已经有 config.local.yaml -> **便携模式**,就地用
         (从仓库直接跑、或者有意做成便携版的情形)
      3. %APPDATA%\\HudooSeoDesk (Windows) / ~/.config/hudoo-seo-desk (其他)
    """
    env = os.environ.get("HUDOO_SEO_DESK_HOME")
    if env:
        return Path(env).expanduser()
    if (ROOT / "config.local.yaml").exists():
        return ROOT
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "HudooSeoDesk"
    return Path.home() / ".config" / "hudoo-seo-desk"


HOME = _home()
LOCAL = HOME / "config.local.yaml"
EXAMPLE = ROOT / "config.example.yaml"     # 模板跟程序走,只读
PORTABLE = HOME == ROOT

# 配置路径 -> 环境变量名
ENV_MAP = {
    "google_ads.developer_token": "GKP_DEVELOPER_TOKEN",
    "google_ads.client_id": "GKP_CLIENT_ID",
    "google_ads.client_secret": "GKP_CLIENT_SECRET",
    "google_ads.refresh_token": "GKP_REFRESH_TOKEN",
    "google_ads.login_customer_id": "GKP_LOGIN_CUSTOMER_ID",
    "anthropic.api_key": "ANTHROPIC_API_KEY",
    "llm.api_key": "LLM_API_KEY",
    "dataforseo.login": "DATAFORSEO_LOGIN",
    "dataforseo.password": "DATAFORSEO_PASSWORD",
    "feishu.app_id": "FEISHU_APP_ID",
    "feishu.app_secret": "FEISHU_APP_SECRET",
    "gsc.refresh_token": "GSC_REFRESH_TOKEN",
}

# 哪些字段算凭据 —— 状态接口只报「有没有」,绝不回传原值
SECRET_PATHS = set(ENV_MAP.keys())

_cache = None


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v not in (None, ""):
            out[k] = v
        elif k not in out:
            out[k] = v
    return out


def _read_yaml(p):
    if not p.exists():
        return {}
    import yaml
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def load(refresh=False):
    global _cache
    if _cache is not None and not refresh:
        return _cache
    cfg = _deep_merge(_read_yaml(EXAMPLE), _read_yaml(LOCAL))
    # 环境变量覆盖
    for path, env in ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            set_path(cfg, path, val)
    _cache = cfg
    return cfg


def get(path, default=None):
    node = load()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node in (None, "") else node


def set_path(cfg, path, value):
    parts = path.split(".")
    node = cfg
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def google_ads_dict():
    """喂给 GoogleAdsClient.load_from_dict 的配置。"""
    return {
        "developer_token": str(get("google_ads.developer_token", "")),
        "client_id": str(get("google_ads.client_id", "")),
        "client_secret": str(get("google_ads.client_secret", "")),
        "refresh_token": str(get("google_ads.refresh_token", "")),
        "login_customer_id": str(get("google_ads.login_customer_id", "")).replace("-", ""),
        "use_proto_plus": True,
    }


def _resolve(p, base):
    """配置里的路径:绝对路径照用,相对路径相对 base。"""
    q = Path(str(p)).expanduser()
    return q if q.is_absolute() else (base / q)


def data_dir(*parts):
    """可写数据(SQLite、清单库、任务 journal)。跟着用户走,不在程序目录。"""
    d = HOME.joinpath("data", *parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def skills_dir():
    """skill 内容包在哪。

    **默认值不能指向 ~/.claude/skills** —— 那是装了 Claude Code 才有的目录,
    团队成员机器上没有。优先找用户自己的数据目录,再找程序目录,
    最后才回落到 .claude(装了 Claude Code 的人受益,没装的人也不会报怪错)。
    """
    p = get("paths.skills_dir")
    if p:
        return _resolve(p, HOME)
    for cand in (HOME / "skills", ROOT / "skills"):
        if cand.exists() and any(f.is_dir() for f in cand.iterdir()):
            return cand
    fallback = Path.home() / ".claude" / "skills"
    if fallback.exists():
        return fallback
    return HOME / "skills"


def out_dir():
    """导出目录。飞书登录模式下按人分子目录 —— 下载接口只认当前用户自己的目录,
    成员拿不到别人的文件(文件名是可猜的)。"""
    d = _resolve(get("paths.out_dir", "out"), HOME)
    from app import userctx
    oid = userctx.open_id()
    if oid:
        d = d / oid
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_local_config():
    """首次运行:把模板复制成用户自己的 config.local.yaml。

    只复制模板,不带任何凭据 —— 每个人填自己的。
    """
    if LOCAL.exists() or not EXAMPLE.exists():
        return False
    LOCAL.parent.mkdir(parents=True, exist_ok=True)
    LOCAL.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def status():
    """给界面看的配置体检:只报「配没配」,不回传任何凭据原值。"""
    cfg = load()
    creds = {}
    for path in sorted(SECRET_PATHS):
        node, ok = cfg, True
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                ok = False
                break
            node = node[part]
        creds[path] = bool(ok and node)
    sd = skills_dir()
    return {
        "config_file": str(LOCAL) if LOCAL.exists() else None,
        "home": str(HOME),
        "portable": PORTABLE,
        "out_dir": str(out_dir()),
        "has_local_config": LOCAL.exists(),
        "credentials": creds,
        "ready": {
            "keywords": all(creds.get("google_ads." + k) for k in
                            ("developer_token", "client_id", "client_secret",
                             "refresh_token", "login_customer_id")),
            "skills": bool(creds.get("llm.api_key") or creds.get("anthropic.api_key")),
            "ranks": creds.get("dataforseo.login") and creds.get("dataforseo.password"),
        },
        "skills_dir": str(sd),
        "skills_found": sorted([d.name for d in sd.iterdir()
                                if d.is_dir() and (d / "SKILL.md").exists()])[:200]
        if sd.exists() else [],
        "defaults": cfg.get("defaults", {}),
    }
