"""Safely test Gmail OAuth/API connectivity without listing or reading messages."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
DEFAULT_CLIENT_FILE = Path("credentials.json")
DEFAULT_TOKEN_FILE = Path(".gmail_token.json")
CONFIRMATION = "我已了解 Gmail 只读授权测试"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Gmail OAuth access without listing or reading email messages."
    )
    parser.add_argument("--client-secrets", type=Path, default=DEFAULT_CLIENT_FILE)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    args = parser.parse_args()

    print("Gmail 连接测试提示")
    print("- 将通过 Google OAuth 请求 Gmail 元数据只读权限（gmail.metadata）。")
    print("- 测试调用 users.getProfile；不会列出邮件、读取正文或下载附件。")
    print("- Google 会处理授权请求；测试成功后会在本机保存 OAuth token 文件。")
    print("- 此步骤不会把邮件或附件发送给 AI。")
    print(f"- OAuth 客户端文件：{args.client_secrets.resolve()}")
    print(f"- 本地 token 文件：{args.token_file.resolve()}")
    print("请确认这是获公司允许的邮箱和设备。")

    try:
        answer = input(f'继续请输入“{CONFIRMATION}”：').strip()
    except (EOFError, KeyboardInterrupt):
        print("已取消；未请求授权，也未访问 Gmail。")
        return 1
    if answer != CONFIRMATION:
        print("确认文字不匹配，已取消；未请求授权，也未访问 Gmail。")
        return 1

    if not args.client_secrets.is_file():
        print(
            "找不到 OAuth 客户端 JSON。Gmail API key 不能代替 OAuth 用户授权；"
            "请先从 Google Cloud Console 下载 Desktop app OAuth client JSON，"
            "并将其安全放在此文件路径。"
        )
        return 2

    try:
        creds = None
        if args.token_file.is_file():
            creds = Credentials.from_authorized_user_file(str(args.token_file), [SCOPE])

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(args.client_secrets), scopes=[SCOPE]
            )
            creds = flow.run_local_server(port=0, open_browser=True)

        # Persist credentials only after the user confirmed. The token file is git-ignored.
        args.token_file.write_text(creds.to_json(), encoding="utf-8")

        # This endpoint returns profile metadata (including mailbox counts); only display
        # the account address and never print/store the response object or counts.
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = gmail.users().getProfile(userId="me").execute()
        email_address = profile.get("emailAddress")
        if not email_address:
            print("Google OAuth 已通过，但 Gmail 未返回邮箱地址；连接测试未通过。")
            return 3
        print(f"连接测试成功。已授权账户：{email_address}")
        print("未查询邮件列表、邮件正文或附件，也未向 AI 上传数据。")
        print(f"OAuth token 已保存在本机：{args.token_file.resolve()}")
        print("请勿分享或提交该 token 文件；公司需要撤销时，可在 Google 账户安全设置中移除授权。")
        return 0
    except (GoogleAuthError, HttpError, OSError, ValueError) as exc:
        # Avoid dumping exception payloads, which may contain request details.
        status = getattr(getattr(exc, "resp", None), "status", None)
        suffix = f"（HTTP {status}）" if status else ""
        print(f"连接测试失败：{type(exc).__name__}{suffix}。未读取邮件内容或附件。")
        return 4


if __name__ == "__main__":
    sys.exit(main())
