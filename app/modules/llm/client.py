# -*- coding: utf-8 -*-
"""LLM 调用层:一个接口,底下可换 provider。

**刻意不绑死任何一家**,原因有三:
  1. 模型名变动很快 —— DeepSeek 半年改了三次(deepseek-chat / deepseek-reasoner
     2026-07-24 退役,deepseek-v4-flash 2026-09-10 退役),写死在代码里迟早要疼。
  2. 不同 skill 该配不同模型 —— 审计类是规则性检查,便宜模型够用;
     内容创作可能想要更强的。所以支持按 skill 覆盖。
  3. 两边本来就是同一个接口形状,抽象成本几乎为零。

价格表也放配置里,别写死 —— 涨价降价都不用改代码。
"""
import time

from app import config

# 缺省价格($/1M token),可被 config 的 llm.pricing 覆盖。
# 只是用来估算花费,报不准不影响功能。
DEFAULT_PRICING = {
    "deepseek-flash": {"in": 0.30, "out": 1.20, "cache_in": 0.007},
    "deepseek-v4-pro": {"in": 0.66, "out": 1.98, "cache_in": 0.014},
    "claude-opus-5": {"in": 5.00, "out": 25.00},
    "claude-sonnet-5": {"in": 2.00, "out": 10.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
}

PROVIDER_DEFAULTS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    "openai_compatible": {"base_url": "", "model": ""},
    "anthropic": {"base_url": "", "model": "claude-opus-5"},
}


class LlmError(RuntimeError):
    """普通失败。**不要用 SystemExit** —— 那是 BaseException,
    在作业线程里会绕过异常处理让作业静默卡死。"""


def settings(skill=None):
    """当前生效的 LLM 设置。skill 级配置可覆盖全局。"""
    prov = str(config.get("llm.provider", "deepseek")).lower()
    d = PROVIDER_DEFAULTS.get(prov, PROVIDER_DEFAULTS["openai_compatible"])
    s = {
        "provider": prov,
        "model": config.get("llm.model") or d["model"],
        "api_key": config.get("llm.api_key") or "",
        "base_url": config.get("llm.base_url") or d["base_url"],
        "effort": config.get("llm.effort") or "",
        "max_tokens": int(config.get("llm.max_tokens", 16000)),
        "timeout": int(config.get("llm.timeout", 600)),
    }
    if skill:
        over = (config.get("llm.per_skill") or {}).get(skill) or {}
        for k in ("provider", "model", "api_key", "base_url", "effort", "max_tokens"):
            if over.get(k):
                s[k] = over[k]
        if over.get("provider") and not over.get("base_url"):
            s["base_url"] = PROVIDER_DEFAULTS.get(
                str(over["provider"]).lower(), {}).get("base_url", "")
    if not s["api_key"]:
        raise LlmError("还没配 LLM 的 api_key —— 去 config.local.yaml 的 llm 段填。")
    if not s["model"]:
        raise LlmError("还没指定模型名(llm.model)。")
    return s


def pricing(model):
    table = dict(DEFAULT_PRICING)
    table.update(config.get("llm.pricing") or {})
    for name, p in table.items():
        if model == name or model.startswith(name):
            return p
    return None


def cost_of(model, usage):
    p = pricing(model)
    if not p:
        return None
    cached = usage.get("cache_in") or 0
    fresh = max(0, (usage.get("in") or 0) - cached)
    c = fresh * p.get("in", 0) / 1e6
    c += cached * p.get("cache_in", p.get("in", 0)) / 1e6
    c += (usage.get("out") or 0) * p.get("out", 0) / 1e6
    return round(c, 6)


# ---------------------------------------------------------------- 调用

def complete(system, user, *, skill=None, stream=True, on_text=None, log=None,
             max_tokens=None, effort=None, thinking=None):
    """跑一次补全。返回 {text, usage, cost, model, provider, seconds}。

    `on_text(chunk)` 在流式时逐段回调,用来把生成过程写进作业日志。
    """
    log = log or (lambda m: None)
    s = settings(skill)
    if max_tokens:
        s["max_tokens"] = int(max_tokens)
    if effort is not None:
        s["effort"] = effort
    s["thinking"] = thinking
    t0 = time.time()
    if s["provider"] == "anthropic":
        text, usage = _anthropic(s, system, user, stream, on_text)
    else:
        text, usage = _openai_compatible(s, system, user, stream, on_text)
    out = {
        "text": text, "usage": usage, "model": s["model"],
        "provider": s["provider"], "seconds": round(time.time() - t0, 1),
        "cost": cost_of(s["model"], usage),
    }
    log("%s/%s:输入 %s(缓存命中 %s)输出 %s,%.1fs%s"
        % (s["provider"], s["model"], usage.get("in"), usage.get("cache_in") or 0,
           usage.get("out"), out["seconds"],
           ",约 $%.4f" % out["cost"] if out["cost"] is not None else ""))
    return out


def _openai_compatible(s, system, user, stream, on_text):
    """DeepSeek 及任何 OpenAI 兼容端点。

    DeepSeek 的上下文缓存是**自动的**,不用像 Anthropic 那样显式打断点 ——
    把固定不变的内容(skill 正文、口径表)放 system 里,变动的放 user 里,
    命中率自然就高。命中价约为未命中的 1/31。
    """
    from openai import OpenAI
    try:
        cli = OpenAI(api_key=s["api_key"], base_url=s["base_url"] or None,
                     timeout=s["timeout"])
    except Exception as e:
        raise LlmError("建客户端失败:%s" % e)

    kw = {"model": s["model"],
          "messages": [{"role": "system", "content": system},
                       {"role": "user", "content": user}],
          "max_tokens": s["max_tokens"], "stream": stream}
    extra = {}
    if s["effort"]:
        # reasoning_effort 不是所有兼容端点都认,走 extra_body 更稳
        extra["reasoning_effort"] = s["effort"]
    if s.get("thinking") is False:
        # **结构化抽取任务要关掉深思。** 否则模型可能把 max_tokens 全花在推理上,
        # content 返回空字符串 —— 表现是「解析不到 JSON」,很难猜到真因。
        extra["thinking"] = {"type": "disabled"}
    if extra:
        kw["extra_body"] = extra

    try:
        if not stream:
            r = cli.chat.completions.create(**kw)
            ch = r.choices[0]
            text = ch.message.content or ""
            if not text and getattr(ch, "finish_reason", "") == "length":
                raise LlmError(
                    "输出撞到 max_tokens(%d)上限,内容被截断成空。"
                    "这类任务多半是把额度全花在推理上了 —— "
                    "把 max_tokens 调大,或调用时传 thinking=False 关掉深思。"
                    % s["max_tokens"])
            return text, _usage_openai(r.usage)
        kw["stream_options"] = {"include_usage": True}
        parts, usage = [], {}
        for ev in cli.chat.completions.create(**kw):
            if getattr(ev, "usage", None):
                usage = _usage_openai(ev.usage)
            for ch in (ev.choices or []):
                piece = getattr(ch.delta, "content", None)
                if piece:
                    parts.append(piece)
                    if on_text:
                        on_text(piece)
        return "".join(parts), usage
    except Exception as e:
        raise LlmError(_hint(e, s))


def _usage_openai(u):
    if not u:
        return {}
    # DeepSeek 会多给 prompt_cache_hit_tokens / prompt_cache_miss_tokens
    cache = getattr(u, "prompt_cache_hit_tokens", None)
    if cache is None:
        details = getattr(u, "prompt_tokens_details", None)
        cache = getattr(details, "cached_tokens", None) if details else None
    return {"in": getattr(u, "prompt_tokens", 0) or 0,
            "out": getattr(u, "completion_tokens", 0) or 0,
            "cache_in": cache or 0}


def _anthropic(s, system, user, stream, on_text):
    try:
        from anthropic import Anthropic
    except ImportError:
        raise LlmError("要用 Anthropic 得先装 SDK:pip install anthropic")
    cli = Anthropic(api_key=s["api_key"], base_url=s["base_url"] or None,
                    timeout=s["timeout"])
    kw = {
        "model": s["model"],
        "max_tokens": s["max_tokens"],
        # system 放 cache_control 断点 —— skill 正文是固定前缀,缓存它最划算
        "system": [{"type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
        "thinking": {"type": "adaptive"},
    }
    if s["effort"]:
        kw["output_config"] = {"effort": s["effort"]}
    try:
        if not stream:
            r = cli.messages.create(**kw)
            return _anthropic_text(r), _usage_anthropic(r.usage)
        with cli.messages.stream(**kw) as st:
            for piece in st.text_stream:
                if on_text:
                    on_text(piece)
            msg = st.get_final_message()
        return _anthropic_text(msg), _usage_anthropic(msg.usage)
    except Exception as e:
        raise LlmError(_hint(e, s))


def _anthropic_text(msg):
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def _usage_anthropic(u):
    if not u:
        return {}
    read = getattr(u, "cache_read_input_tokens", 0) or 0
    write = getattr(u, "cache_creation_input_tokens", 0) or 0
    return {"in": (getattr(u, "input_tokens", 0) or 0) + read + write,
            "out": getattr(u, "output_tokens", 0) or 0,
            "cache_in": read}


HINTS = [
    ("model_not_found", "模型名不对。DeepSeek 的名字换过几次:"
                        "deepseek-chat / deepseek-reasoner 已于 2026-07-24 退役,"
                        "deepseek-v4-flash 已于 2026-09-10 退役,"
                        "现在用 deepseek-flash 或 deepseek-v4-pro。"),
    ("does not exist", "模型名不对,见上一条关于 DeepSeek 模型改名的说明。"),
    ("401", "api_key 不对或没权限。"),
    ("authentication", "api_key 不对或没权限。"),
    ("insufficient", "余额不足。"),
    ("rate limit", "被限流了,等一会儿再试。"),
    ("429", "被限流了,等一会儿再试。"),
    ("timeout", "超时。长输出建议开流式(stream=True),或把 llm.timeout 调大。"),
    ("context", "超出上下文长度。skill 正文太长时把 references 拆开按需加载。"),
]


def _hint(exc, s):
    msg = "%s: %s" % (type(exc).__name__, exc)
    low = msg.lower()
    for key, tip in HINTS:
        if key in low:
            return msg[:300] + "\n\n>> " + tip
    return msg[:300] + "\n\n(provider=%s model=%s base_url=%s)" % (
        s["provider"], s["model"], s["base_url"] or "默认")


def check(log=None):
    """连通性自检:发一句最短的话,报回模型、耗时、用量、花费。"""
    log = log or (lambda m: None)
    s = settings()
    log("provider=%s model=%s base_url=%s"
        % (s["provider"], s["model"], s["base_url"] or "(默认)"))
    r = complete("你是一个测试助手,只回复收到的内容。",
                 "请只回复两个字:通了", stream=False, log=log)
    return {"ok": True, "reply": (r["text"] or "").strip()[:40],
            "model": r["model"], "provider": r["provider"],
            "seconds": r["seconds"], "usage": r["usage"], "cost": r["cost"]}
