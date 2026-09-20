# -*- coding: utf-8 -*-
"""入口。

    python -m app.main            启动控制台(默认)
    python -m app.main auth       跑一次 OAuth 授权
    python -m app.main check      命令行自检,不开界面
"""
import sys


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "serve"

    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    if cmd == "auth":
        from app.auth import main as auth_main
        return auth_main(argv[1:])

    if cmd == "check":
        from app.modules.keywords import gkp

        class _Log:
            def log(self, m):
                print(" ", m)

        try:
            r = gkp.check(_Log())
        except Exception as e:
            print("[失败] %s" % e, file=sys.stderr)
            return 1
        print("自检结果:", "通过" if r.get("ok") else "接口可达但无数据")
        return 0 if r.get("ok") else 1

    from app.server import serve
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
