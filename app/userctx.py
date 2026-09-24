# -*- coding: utf-8 -*-
"""当前用户(线程本地)。

请求线程里由 server 设置;jobs.start 把发起人带进作业线程 —— 作业里的归档、导出目录、
文档授权都靠它知道「是谁跑的」。本机模式下永远是 None,各处按单用户处理。
"""
import threading

_local = threading.local()


def set_user(user):
    _local.user = user


def get_user():
    return getattr(_local, "user", None)


def open_id():
    u = get_user()
    return (u or {}).get("open_id")


def name():
    u = get_user()
    return (u or {}).get("name")


def is_admin():
    u = get_user()
    return bool(u and u.get("admin"))
