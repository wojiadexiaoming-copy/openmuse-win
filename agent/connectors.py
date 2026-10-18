"""§28 Connectors 第三方连接 [必选]

Vault 存密码，Connector 是"连上某个服务"的完整方案：
OAuth 授权流 + 权限范围 + Token 自动刷新 + API 封装。

实现状态（2026-10-15）：ConnectorManager + Gmail 预置。
"""
from __future__ import annotations

import logging

log = logging.getLogger("connectors")

import time
import urllib.parse
from dataclasses import dataclass, field


@dataclass
class Connector:
    name: str                           # "gmail"
    client_id_ref: str                  # "vault://gmail_client_id"
    client_secret_ref: str              # "vault://gmail_client_secret"
    auth_url: str                       # 授权页
    token_url: str                      # 换 token
    scopes: list[str] = field(default_factory=list)
    access_token_ref: str = ""          # "vault://gmail_access"
    refresh_token_ref: str = ""         # "vault://gmail_refresh"
    expires_at: float = 0.0             # access_token 过期时间戳


# 预置连接器（按需扩展）
PRESET_CONNECTORS = {
    "gmail": Connector(
        name="gmail",
        client_id_ref="vault://gmail_client_id",
        client_secret_ref="vault://gmail_client_secret",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send"],
        access_token_ref="vault://gmail_access",
        refresh_token_ref="vault://gmail_refresh",
    ),
}


class ConnectorManager:
    """第三方服务连接管理：OAuth 授权 + token 自动刷新 + API 调用。"""

    def __init__(self, vault=None):
        self._vault = vault
        self._connectors: dict[str, Connector] = dict(PRESET_CONNECTORS)

    def _resolve(self, ref: str) -> str:
        """vault://xxx → 明文（走 vault.resolve）。"""
        if self._vault is None:
            raise RuntimeError("Vault 未注入，无法解析 " + ref)
        # vault.resolve(ref, caller=...) 签名按 vault/manager.py
        try:
            return self._vault.resolve(ref, caller="agent.connectors")
        except TypeError:
            return self._vault.resolve(ref)

    def _store(self, ref: str, value: str):
        if self._vault is None:
            raise RuntimeError("Vault 未注入，无法存储 " + ref)
        name = ref.replace("vault://", "")
        try:
            self._vault.store(name, value, caller="agent.connectors")
        except TypeError:
            self._vault.store(name, value)

    def get(self, name: str) -> Connector | None:
        return self._connectors.get(name)

    def register(self, connector: Connector):
        self._connectors[connector.name] = connector

    def get_auth_link(self, name: str,
                      redirect_uri: str = "http://localhost:8080/callback") -> str:
        """生成授权链接，发给用户点。"""
        c = self.get(name)
        if c is None:
            raise ValueError(f"未知连接器: {name}")
        params = {
            "client_id": self._resolve(c.client_id_ref),
            "scope": " ".join(c.scopes),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "access_type": "offline",  # 要 refresh_token
            "prompt": "consent",
        }
        return f"{c.auth_url}?{urllib.parse.urlencode(params)}"

    def handle_callback(self, name: str, code: str,
                        redirect_uri: str = "http://localhost:8080/callback") -> bool:
        """用户授权后，用 code 换 token 存 vault。"""
        import urllib.request
        import json as _json
        c = self.get(name)
        if c is None:
            raise ValueError(f"未知连接器: {name}")
        data = urllib.parse.urlencode({
            "code": code,
            "client_id": self._resolve(c.client_id_ref),
            "client_secret": self._resolve(c.client_secret_ref),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode()
        req = urllib.request.Request(c.token_url, data=data,
                                      headers={"Content-Type":
                                               "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                tokens = _json.loads(resp.read().decode())
        except Exception as e:
            raise RuntimeError(f"换 token 失败: {e}")
        self._store(c.access_token_ref, tokens["access_token"])
        if tokens.get("refresh_token"):
            self._store(c.refresh_token_ref, tokens["refresh_token"])
        c.expires_at = time.time() + tokens.get("expires_in", 3600) - 60
        log.info(f"[connector] {name} OAuth 连接成功，scopes={c.scopes}")
        return True

    def _refresh(self, name: str):
        """access_token 过期 → 用 refresh_token 换新的（用户无感）。"""
        import urllib.request
        import json as _json
        c = self.get(name)
        data = urllib.parse.urlencode({
            "refresh_token": self._resolve(c.refresh_token_ref),
            "client_id": self._resolve(c.client_id_ref),
            "client_secret": self._resolve(c.client_secret_ref),
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(c.token_url, data=data,
                                      headers={"Content-Type":
                                               "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            tokens = _json.loads(resp.read().decode())
        self._store(c.access_token_ref, tokens["access_token"])
        c.expires_at = time.time() + tokens.get("expires_in", 3600) - 60
        log.info(f"[connector] {name} token 已刷新")

    def api_call(self, name: str, method: str, url: str,
                 json_body: dict | None = None,
                 params: dict | None = None) -> dict:
        """带自动刷新的 API 调用。返回解析后的 JSON。"""
        import urllib.request
        import json as _json
        c = self.get(name)
        if c is None:
            raise ValueError(f"未知连接器: {name}")
        # 过期先刷新，用户无感
        if time.time() >= c.expires_at:
            self._refresh(name)
        token = self._resolve(c.access_token_ref)
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        full_url = url
        if params:
            full_url += "?" + urllib.parse.urlencode(params)
        data = _json.dumps(json_body).encode() if json_body else None
        req = urllib.request.Request(full_url, data=data, headers=headers,
                                      method=method.upper())
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return _json.loads(body) if body else {}

    # ---- Gmail 快捷方法 ----
    def gmail_list(self, query: str = "", max_results: int = 10) -> dict:
        """列邮件（Gmail API）。"""
        return self.api_call(
            "gmail", "GET",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            params={"q": query, "maxResults": max_results})

    def gmail_get(self, msg_id: str, format: str = "full") -> dict:
        """读一封邮件全文。"""
        return self.api_call(
            "gmail", "GET",
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}",
            params={"format": format})

    def gmail_send(self, to: str, subject: str, body: str) -> dict:
        """发邮件（plain text）。"""
        import base64
        raw = (f"To: {to}\r\nSubject: {subject}\r\n"
               f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}")
        encoded = base64.urlsafe_b64encode(raw.encode()).decode()
        return self.api_call(
            "gmail", "POST",
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            json_body={"raw": encoded})


def make_connector_tools(manager: "ConnectorManager"):
    """构建连接器工具供主 Agent 调用。"""
    from agent.tools_registry import ToolDefinition

    def _auth_link(args: dict) -> str:
        name = args.get("name", "gmail")
        try:
            link = manager.get_auth_link(name)
            return f"[授权链接] 请在浏览器打开并点允许：\n{link}"
        except Exception as e:
            return f"[工具执行失败] {e}"

    def _gmail_list(args: dict) -> str:
        try:
            r = manager.gmail_list(args.get("query", ""),
                                    int(args.get("max_results", 10)))
            msgs = r.get("messages", [])
            lines = [f"- {m['id']}" for m in msgs[:10]]
            return f"[Gmail] 找到 {r.get('resultSizeEstimate', 0)} 封：\n" + "\n".join(lines)
        except Exception as e:
            return f"[工具执行失败] {e}"

    def _gmail_send(args: dict) -> str:
        try:
            r = manager.gmail_send(args["to"], args["subject"], args["body"])
            return f"[Gmail] 已发送，id={r.get('id', '')}"
        except Exception as e:
            return f"[工具执行失败] {e}"

    return [
        ToolDefinition(
            name="connector_auth_link",
            description="生成第三方服务 OAuth 授权链接（如 Gmail），发给用户点授权。",
            parameters={"type": "object",
                         "properties": {"name": {"type": "string",
                                                  "description": "连接器名，如 gmail"}},
                         "required": []},
            func=_auth_link,
        ),
        ToolDefinition(
            name="gmail_list",
            description="列 Gmail 邮件（需先完成 OAuth 授权）。",
            parameters={"type": "object",
                         "properties": {
                             "query": {"type": "string", "description": "搜索，如 from:x"},
                             "max_results": {"type": "integer", "description": "最多几封"}},
                         "required": []},
            func=_gmail_list,
        ),
        ToolDefinition(
            name="gmail_send",
            description="发 Gmail 邮件（需先完成 OAuth 授权）。",
            parameters={"type": "object",
                         "properties": {
                             "to": {"type": "string"},
                             "subject": {"type": "string"},
                             "body": {"type": "string"}},
                         "required": ["to", "subject", "body"]},
            func=_gmail_send,
        ),
    ]
