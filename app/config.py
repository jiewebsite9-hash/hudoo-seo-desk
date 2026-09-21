# -*- coding: utf-8 -*-
"""配置加载。

优先级:环境变量 > config.local.yaml > config.example.yaml 里的默认值。
凭据只从本机读,永远不写回仓库目录以外的地方,也永远不打印原值。
"""
import os
from pathlib import Path

# 打包成 exe 后 __file__ 在临时解包目录,配置要按 exe 所在目录找
import sys
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent

LOCAL = ROOT / "config.local.yaml"
EXAMPLE = ROOT / "config.example.yaml"

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


def skills_dir():
    p = get("paths.skills_dir")
    if p:
        return Path(p)
    local = ROOT / "skills"
    if local.exists() and any(f.is_dir() for f in local.iterdir()):
        return local
    return Path.home() / ".claude" / "skills"


def out_dir():
    d = ROOT / str(get("paths.out_dir", "out"))
    d.mkdir(parents=True, exist_ok=True)
    return d


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
