# -*- coding: utf-8 -*-
"""一次性 OAuth 授权,把 refresh_token 写回 config.local.yaml。

前置:config.local.yaml 里已填好 google_ads.client_id / client_secret
(Cloud 控制台 -> OAuth 客户端 -> 应用类型「桌面应用」)。

    python -m app.auth              浏览器自动打开
    python -m app.auth --no-browser 只打印链接,自己去开(适合远程/双账号场景)
"""
import re
import sys

from app import config

SCOPES = ["https://www.googleapis.com/auth/adwords"]
PORT = 8765


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    open_browser = "--no-browser" not in argv

    cid = str(config.get("google_ads.client_id", "")).strip()
    csec = str(config.get("google_ads.client_secret", "")).strip()
    if not cid or not csec:
        print("[中断] config.local.yaml 里还没填 google_ads.client_id / client_secret。",
              file=sys.stderr)
        print("  Cloud 控制台 -> API 和服务 -> 凭据 -> 创建 OAuth 客户端 ID -> 桌面应用",
              file=sys.stderr)
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config({
        "installed": {
            "client_id": cid,
            "client_secret": csec,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }, scopes=SCOPES)

    creds = flow.run_local_server(
        port=PORT,
        open_browser=open_browser,
        access_type="offline",   # 没有这个拿不到 refresh_token
        prompt="consent",        # 强制重新签发
        authorization_prompt_message="请在浏览器里授权(选能登录 ads.google.com 的账号):\n{url}",
        success_message="授权成功,可以关掉这个页面回终端了。",
    )
    if not creds.refresh_token:
        print("\n[失败] 没拿到 refresh_token。到 myaccount.google.com/permissions "
              "撤销这个应用的授权后重跑。", file=sys.stderr)
        return 1

    if not _write_back("refresh_token", creds.refresh_token):
        print("\n[提示] 没能自动写回配置,请手动把下面这行填进 config.local.yaml "
              "的 google_ads 段:")
        print('  refresh_token: "%s"' % creds.refresh_token)
        return 0

    print("\n[完成] refresh_token 已写入 %s" % config.LOCAL)
    print("下一步:启动程序,在界面上点「自检」")
    return 0


def _write_back(key, value):
    """只改目标那一行,保住文件里的注释。"""
    p = config.LOCAL
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    pat = re.compile(r'^(\s*%s\s*:\s*)(.*)$' % re.escape(key), re.M)
    if not pat.search(text):
        return False
    new = pat.sub(lambda m: '%s"%s"' % (m.group(1), value), text, count=1)
    p.write_text(new, encoding="utf-8")
    config.load(refresh=True)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
