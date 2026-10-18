"""AlterEgo 微信通道模块（对接 Tencent/openclaw-weixin 公开协议）。

架构与协议说明（基于腾讯 iLink Bot 协议）：
1. 登录协议：
   - 获取二维码：POST https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3
   - 轮询状态：GET https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode={qrcode}
   - 确认登录后返回：bot_token, ilink_bot_id, baseurl, ilink_user_id
2. 消息长轮询协议：
   - 同步消息：POST {baseurl}/ilink/bot/getupdates
3. 发送消息协议：
   - 发送消息：POST {baseurl}/ilink/bot/sendmessage
4. 协同价值：
   - 用户在手机微信中对话与审批，电脑端无需触碰键鼠，彻底杜绝坐标冲突与误触。
"""
from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger("alterego.channels.weixin")

FIXED_BASE_URL = "https://ilinkai.weixin.qq.com"

# bot_token 有效期（秒）：72 小时。过期后需重新扫码授权。
# 2026-10-16 用户实测确认：token 存 72 小时后报 AuthenticationError。
WEIXIN_TOKEN_TTL_SEC = 72 * 3600
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 10)  # 132106 (v2.4.10)
BOT_TYPE = "3"
CHANNEL_VERSION = "2.4.10"
BOT_AGENT = "AlterEgo/2.4.10"

_SUBMITTED_RECEIPT = "已提交给目标应用，正在确认生成结果。"
_PROGRESS_TIMEOUT_SEC = 3.0


def _build_uin() -> str:
    """生成随机微信 UIN Header (Base64 uint32)"""
    val = random.randint(100000000, 999999999)
    return base64.b64encode(str(val).encode("utf-8")).decode("utf-8")


class WeixinClient:
    """封装腾讯 iLink Bot API 基础通信协议。"""

    def __init__(self, base_url: str = FIXED_BASE_URL):
        self.base_url = base_url.rstrip("/")
        # 发送互斥锁：避免多线程并发调用 send_message 抢占 session 导致腾讯服务端静默丢弃
        self._send_lock = threading.Lock()

    def _build_headers(self, token: str | None = None, content_length: int | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
            "X-WECHAT-UIN": _build_uin(),
            "User-Agent": f"OpenClaw/{CHANNEL_VERSION}",
        }
        if token and token.strip():
            headers["AuthorizationType"] = "ilink_bot_token"
            headers["Authorization"] = f"Bearer {token.strip()}"
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        return headers

    def _build_base_info(self) -> dict[str, str]:
        return {
            "channel_version": CHANNEL_VERSION,
            "bot_agent": BOT_AGENT,
        }

    def fetch_qrcode(self, bot_type: str = BOT_TYPE) -> dict[str, Any]:
        """获取登录二维码与扫描链接。"""
        url = f"{self.base_url}/ilink/bot/get_bot_qrcode?bot_type={bot_type}"
        headers = self._build_headers()
        body = {"local_token_list": []}
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def poll_qrcode_status(
        self,
        qrcode: str,
        base_url: str | None = None,
        timeout_sec: float = 35.0,
    ) -> dict[str, Any]:
        """长轮询二维码扫描状态。

        返回 status:
        - 'wait': 等待扫描
        - 'scaned': 已扫描正在验证
        - 'confirmed': 已在微信客户端授权确认
        - 'expired': 二维码已失效
        - 'scaned_but_redirect': 跨机房重定向
        """
        host = (base_url or self.base_url).rstrip("/")
        url = f"{host}/ilink/bot/get_qrcode_status?qrcode={qrcode}"
        headers = self._build_headers()
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except (httpx.TimeoutException, httpx.ReadTimeout):
            return {"status": "wait"}
        except Exception as e:
            logger.warning(f"poll_qrcode_status 网络异常重试: {e}")
            return {"status": "wait"}

    def get_updates(
        self,
        token: str,
        base_url: str | None = None,
        get_updates_buf: str = "",
        timeout_sec: float = 35.0,
    ) -> dict[str, Any]:
        """长轮询同步微信用户消息。"""
        host = (base_url or self.base_url).rstrip("/")
        url = f"{host}/ilink/bot/getupdates"
        body = {
            "get_updates_buf": get_updates_buf,
            "base_info": self._build_base_info(),
        }
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = self._build_headers(token=token, content_length=len(raw_body))
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                resp = client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                return resp.json()
        except (httpx.TimeoutException, httpx.ReadTimeout):
            return {"ret": 0, "msgs": [], "get_updates_buf": get_updates_buf}

    def send_message(
        self,
        token: str,
        to_user_id: str,
        from_user_id: str = "",
        text: str = "",
        context_token: str | None = None,
        base_url: str | None = None,
        timeout_sec: float = 15.0,
    ) -> dict[str, Any]:
        """向微信用户推送回复文本；timeout_sec 限制每个网络 I/O 阶段。"""
        import uuid
        host = (base_url or self.base_url).rstrip("/")
        url = f"{host}/ilink/bot/sendmessage"
        client_id = f"alterego:{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        msg_payload: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": 2,  # BOT
            "message_state": 2,  # FINISH
            "item_list": [
                {
                    "type": 1,  # TEXT
                    "text_item": {"text": text},
                }
            ],
        }
        if context_token:
            msg_payload["context_token"] = context_token
        body = {
            "msg": msg_payload,
            "base_info": self._build_base_info(),
        }
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = self._build_headers(token=token, content_length=len(raw_body))

        with self._send_lock:
            with httpx.Client(timeout=timeout_sec) as client:
                resp = client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                if data.get("ret") and data.get("ret") != 0:
                    logger.error(f"send_message 异常: {data}")
                return data


class WeixinChannel:
    """微信通道管理器：扫码绑定、后台消息消费、免鼠键审批联动。"""

    def __init__(
        self,
        data_dir: Path,
        orch: Any = None,
        approval_mgr: Any = None,
        session_store: Any = None,
        on_event: Callable[[str, dict], None] | None = None,
        tracer: Any = None,
        vault: Any = None,
        turn_lock: Any = None,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.creds_file = self.data_dir / "weixin_credentials.json"
        self.qr_img_path = self.data_dir / "weixin_qr.png"

        self.orch = orch
        self.turn_lock = turn_lock if turn_lock is not None else (
            getattr(orch, "turn_lock", None) or threading.RLock())
        if orch is not None:
            orch.turn_lock = self.turn_lock
        self.approval_mgr = approval_mgr
        self.session_store = session_store
        self.on_event = on_event
        self.tracer = tracer or getattr(orch, "tracer", None)
        self.vault = vault

        self.client = WeixinClient()
        self.context_tokens: dict[str, str] = {}
        self.get_updates_buf: str = ""
        self.credentials: dict[str, Any] | None = self.load_credentials()

        self.running = False
        self.active_qr: dict[str, Any] | None = None
        self.weixin_session_id: str | None = None

        self._login_thread: threading.Thread | None = None
        self._loop_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # 如果已有凭证，启动后台消息监听
        if self.is_connected():
            self.start_message_loop()

    def is_connected(self) -> bool:
        """有 bot_token 且未过期才算已连接。

        2026-10-16 修正：之前只检查 token 是否存在，不检查过期，
        导致 72 小时后用过期 token 调接口报 AuthenticationError。
        过期后返回 False，调用方应触发重新扫码流程。
        """
        if not (self.credentials and self.credentials.get("bot_token")):
            return False
        connected_at = self.credentials.get("connected_at", 0)
        if time.time() - connected_at > WEIXIN_TOKEN_TTL_SEC:
            logger.warning(
                "微信 bot_token 已过期（超过 72 小时），需要重新扫码授权。")
            return False
        return True

    def is_token_expired(self) -> bool:
        """token 是否已过期（有 token 但超 72 小时）。"""
        if not (self.credentials and self.credentials.get("bot_token")):
            return False
        connected_at = self.credentials.get("connected_at", 0)
        return time.time() - connected_at > WEIXIN_TOKEN_TTL_SEC

    def load_credentials(self) -> dict[str, Any] | None:
        candidates = [
            self.creds_file,
            self.data_dir.parent / "weixin" / "weixin_credentials.json",
            self.data_dir.parent / "data" / "weixin" / "weixin_credentials.json",
        ]
        for p in candidates:
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data and data.get("bot_token"):
                            # 旧文件没有 connected_at 时，用文件修改时间兜底
                            if "connected_at" not in data:
                                try:
                                    data["connected_at"] = p.stat().st_mtime
                                except OSError:
                                    data["connected_at"] = 0
                            # 恢复离线游标与各会话 context_token
                            if data.get("context_tokens") and isinstance(data["context_tokens"], dict):
                                self.context_tokens.update(data["context_tokens"])
                            if data.get("get_updates_buf"):
                                self.get_updates_buf = str(data["get_updates_buf"])
                            return data
                except Exception as e:
                    logger.warning(f"读取微信凭证失败 ({p}): {e}")
        return None

    def save_credentials(self, creds: dict[str, Any]):
        # 补时间戳：没有 connected_at 的旧格式凭证按当前时间算，
        # 避免 is_connected() 误判为过期。
        if "connected_at" not in creds:
            creds = {**creds, "connected_at": time.time()}
        creds = dict(creds)
        if "get_updates_buf" not in creds:
            creds["get_updates_buf"] = getattr(self, "get_updates_buf", "")
        else:
            self.get_updates_buf = str(creds["get_updates_buf"])
        if "context_tokens" not in creds:
            creds["context_tokens"] = dict(self.context_tokens)
        else:
            self.context_tokens.update(creds["context_tokens"])

        self.credentials = creds
        targets = [
            self.creds_file,
            self.data_dir.parent / "data" / "weixin" / "weixin_credentials.json",
            self.data_dir.parent / "weixin" / "weixin_credentials.json",
        ]
        for p in targets:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                # 原子写入：写入临时文件后 replace，避免进程中断导致 json 损坏
                tmp = p.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(creds, f, ensure_ascii=False, indent=2)
                tmp.replace(p)
            except Exception as e:
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(creds, f, ensure_ascii=False, indent=2)
                except Exception as ex2:
                    logger.error(f"保存微信凭证失败 ({p}): {ex2}")
        logger.info("微信凭证已安全持久化到本地。")

    def clear_credentials(self):
        with self._lock:
            self.credentials = None
            if self.creds_file.exists():
                self.creds_file.unlink(missing_ok=True)
            self.running = False

    def get_status(self) -> dict[str, Any]:
        """获取当前通道运行状态。"""
        has_qr = bool(self.active_qr and not self.is_connected())
        return {
            "connected": self.is_connected(),
            "bot_id": self.credentials.get("ilink_bot_id", "") if self.credentials else "",
            "user_id": self.credentials.get("ilink_user_id", "") if self.credentials else "",
            "has_active_qr": has_qr,
            "qrcode_url": self.active_qr.get("qrcode_img_content", "") if has_qr else "",
            "qr_image_exists": self.qr_img_path.exists(),
        }

    def start_qr_login(self, force: bool = False) -> dict[str, Any]:
        """发起微信扫码绑定流程，返回二维码链接并启动轮询。"""
        with self._lock:
            if not force and self.is_connected():
                return {
                    "status": "already_connected",
                    "message": "微信已绑定连接，无需重复扫码。",
                }

            # 1. 向腾讯服务拉取二维码
            res = self.client.fetch_qrcode()
            self.active_qr = res
            qr_url = res.get("qrcode_img_content", "")

            # 2. 生成本地 PNG 二维码图片供 Web 展示或用户扫描
            try:
                import qrcode
                img = qrcode.make(qr_url)
                img.save(str(self.qr_img_path))
            except Exception as e:
                logger.warning(f"生成二维码图片失败: {e}")

            # 3. 启动后台线程长轮询直到确认
            if not self._login_thread or not self._login_thread.is_alive():
                self._login_thread = threading.Thread(
                    target=self._poll_login_worker,
                    args=(res.get("qrcode"),),
                    daemon=True,
                )
                self._login_thread.start()

            return {
                "status": "waiting_scan",
                "qrcode": res.get("qrcode"),
                "qrcode_url": qr_url,
                "qr_image_path": str(self.qr_img_path),
            }

    def _poll_login_worker(self, qrcode_str: str):
        """后台轮询扫码状态线程。"""
        logger.info("开始轮询微信 ClawBot 扫码绑定状态...")
        curr_base = FIXED_BASE_URL
        deadline = time.time() + 480  # 最长 8 分钟

        while time.time() < deadline:
            if not self.active_qr or self.active_qr.get("qrcode") != qrcode_str:
                break
            try:
                st = self.client.poll_qrcode_status(qrcode_str, base_url=curr_base, timeout_sec=35.0)
                status = st.get("status")

                if status == "scaned":
                    logger.info("用户已扫描二维码，等待手机确认...")
                    if self.on_event:
                        self.on_event("weixin_login_progress", {"status": "scaned"})

                elif status == "scaned_but_redirect":
                    redir = st.get("redirect_host")
                    if redir:
                        curr_base = f"https://{redir}"
                        logger.info(f"微信机房重定向至: {curr_base}")

                elif status == "confirmed":
                    bot_token = st.get("bot_token")
                    bot_id = st.get("ilink_bot_id")
                    user_id = st.get("ilink_user_id")
                    base_url = st.get("baseurl") or curr_base

                    creds = {
                        "bot_token": bot_token,
                        "ilink_bot_id": bot_id,
                        "ilink_user_id": user_id,
                        "baseurl": base_url,
                        "connected_at": time.time(),
                    }
                    self.save_credentials(creds)
                    self.active_qr = None
                    logger.info(f"✅ 微信 ClawBot 成功绑定！用户: {user_id}")

                    if self.on_event:
                        self.on_event("weixin_login_progress", {"status": "confirmed", "user_id": user_id})

                    # 发送欢迎提醒到微信
                    welcome_msg = (
                        "🎉 AlterEgo 桌面智能体与微信连接成功！\n\n"
                        "💡 使用技巧：\n"
                        "1. 在此可直接发送任务指令控制电脑（如「打开 Clash」）；\n"
                        "2. 当电脑执行敏感操作触发授权时，系统会自动发通知到微信；\n"
                        "3. 收到授权卡后，直接在此回复「1」或「同意」即可免键鼠授权，保护桌面鼠标坐标绝对精准！"
                    )
                    try:
                        self.client.send_message(
                            token=bot_token,
                            to_user_id=user_id,
                            from_user_id="",
                            text=welcome_msg,
                            base_url=base_url,
                        )
                    except Exception as e:
                        logger.warning(f"发送欢迎消息失败: {e}")

                    # 启动消息循环
                    self.start_message_loop()
                    return

                elif status == "expired":
                    logger.info("二维码已过期，停止本轮轮询。")
                    self.active_qr = None
                    if self.on_event:
                        self.on_event("weixin_login_progress", {"status": "expired"})
                    return

            except Exception as e:
                logger.warning(f"轮询异常: {e}")

            time.sleep(1.0)

    def start_message_loop(self):
        """启动后台消息长轮询线程。"""
        with self._lock:
            if self.running:
                return
            self.running = True

        if not self._loop_thread or not self._loop_thread.is_alive():
            self._loop_thread = threading.Thread(
                target=self._message_loop_worker,
                daemon=True,
            )
            self._loop_thread.start()

    def stop_message_loop(self):
        self.running = False

    def _message_loop_worker(self):
        """持续消费来自微信的长轮询消息。"""
        logger.info("微信 ClawBot 消息后台服务已启动。")
        get_buf = getattr(self, "get_updates_buf", "") or (self.credentials.get("get_updates_buf", "") if self.credentials else "")
        backoff = 1.0

        while self.running and self.is_connected():
            creds = self.credentials or {}
            token = creds.get("bot_token")
            bot_id = creds.get("ilink_bot_id")
            base_url = creds.get("baseurl") or FIXED_BASE_URL

            if not token:
                break

            try:
                res = self.client.get_updates(
                    token=token,
                    base_url=base_url,
                    get_updates_buf=get_buf,
                    timeout_sec=35.0,
                )
                backoff = 1.0  # 成功，重置退避时间

                is_error = (res.get("ret") is not None and res.get("ret") != 0) or (
                    res.get("errcode") is not None and res.get("errcode") != 0
                )
                if not is_error:
                    new_buf = res.get("get_updates_buf")
                    if new_buf and new_buf != get_buf:
                        get_buf = new_buf
                        self.get_updates_buf = new_buf
                        if self.credentials:
                            self.save_credentials({**self.credentials, "get_updates_buf": new_buf, "context_tokens": self.context_tokens})
                    msgs = res.get("msgs", [])
                    for msg in msgs:
                        self._handle_inbound_message(msg, token, bot_id, base_url)
                else:
                    err_code = res.get("errcode") if res.get("errcode") is not None else res.get("ret")
                    err_msg = res.get("errmsg") or ""
                    if err_code == -14:  # STALE_TOKEN / session timeout
                        logger.warning("微信 token 已失效 (session timeout)，需重新扫码绑定。")
                        self.clear_credentials()
                        break
                    logger.warning(f"get_updates 返回异常: code={err_code}, errmsg={err_msg}")
                    time.sleep(2.0)

            except Exception as e:
                logger.warning(f"微信长轮询发生网络异常: {e}，将在 {backoff:.1f} 秒后重试")
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

    def _handle_inbound_message(self, msg: dict[str, Any], token: str, bot_id: str, base_url: str):
        """处理单条微信消息。支持免键鼠审批联动与任务执行。"""
        from_user = msg.get("from_user_id")
        context_token = msg.get("context_token")
        if context_token and from_user:
            if self.context_tokens.get(from_user) != context_token:
                self.context_tokens[from_user] = context_token
                if self.credentials:
                    self.save_credentials({**self.credentials, "context_tokens": self.context_tokens, "get_updates_buf": self.get_updates_buf})

        items = msg.get("item_list", [])
        for it in items:
            text_item = it.get("text_item") or {}
            user_text = (text_item.get("text") or "").strip()
            if not user_text:
                continue

            logger.info(f"收到来自微信用户 ({from_user}) 的消息: {user_text}")

            # 1. 优先检查是否是对待审批卡片的回复（1/同意 或 0/拒绝）
            if self._check_and_resolve_approval(user_text, token, from_user, bot_id, base_url, context_token):
                continue

            # 2. 常规 Agent 任务执行
            self._dispatch_agent_task(user_text, token, from_user, bot_id, base_url, context_token)

    def _check_and_resolve_approval(
        self,
        text: str,
        token: str,
        to_user: str,
        from_user: str,
        base_url: str,
        context_token: str | None,
    ) -> bool:
        """检查是否有待审批事项，并支持通过微信数字/指令决断。"""
        if not self.approval_mgr:
            return False

        pending_cards = getattr(self.approval_mgr, "_pending", {})
        if not pending_cards:
            return False

        # 匹配同意词：1, 同意, yes, y, ok, 确认, 通过, 批准
        approved_tokens = {"1", "同意", "yes", "y", "ok", "确认", "通过", "批准", "行", "准"}
        # 匹配拒绝词：0, 拒绝, no, n, 取消, 驳回, 别, 不行
        rejected_tokens = {"0", "拒绝", "no", "n", "取消", "驳回", "不", "别", "不行"}

        cleaned = text.strip().lower()
        if cleaned in approved_tokens or cleaned in rejected_tokens:
            is_approved = cleaned in approved_tokens
            # 取最早等待的一个审批卡
            with getattr(self.approval_mgr, "_lock"):
                target_aid = None
                target_card = None
                for aid, p in pending_cards.items():
                    if p.get("decision") is None:
                        target_aid = aid
                        target_card = p.get("card")
                        break

            if target_aid:
                action_name = getattr(target_card, "action_cn", "") or getattr(target_card, "action", "操作")
                self.approval_mgr.resolve_card(target_aid, approved=is_approved, scope="once")
                reply = (
                    f"✅ 已通过授权！Agent 正在电脑端继续执行：{action_name}"
                    if is_approved
                    else f"❌ 已拒绝执行该操作 ({action_name})，Agent 将调整执行路径。"
                )
                self.client.send_message(
                    token=token,
                    to_user_id=to_user,
                    from_user_id="",
                    text=reply,
                    context_token=context_token,
                    base_url=base_url,
                )
                # 同步微信审批决断至前端 UI
                if self.on_event:
                    self.on_event("message", {
                        "id": f"msg_wx_u_{int(time.time() * 1000)}",
                        "type": "user",
                        "role": "user",
                        "content": f"📱 [微信用户] {text.strip()} ({'同意授权' if is_approved else '拒绝授权'})",
                        "tool_calls": [],
                        "created_at": time.strftime("%H:%M", time.localtime()),
                    })
                    self.on_event("message", {
                        "id": f"msg_wx_a_{int(time.time() * 1000)}",
                        "type": "assistant_final",
                        "role": "agent",
                        "content": reply,
                        "tool_calls": [],
                        "created_at": time.strftime("%H:%M", time.localtime()),
                    })
                return True

        return False

    def _log_runtime_error(self, event: str, exc: Exception, **metadata):
        logger.error("%s (%s)", event, type(exc).__name__)
        if self.tracer:
            try:
                self.tracer.log_event(event, {
                    "error_type": type(exc).__name__, "channel": "weixin", **metadata,
                })
            except Exception:
                logger.error("weixin_runtime_log_write_failed")

    def _dispatch_agent_task(
        self,
        user_text: str,
        token: str,
        to_user: str,
        from_user: str,
        base_url: str,
        context_token: str | None,
    ):
        """派发任务给 Orchestrator，并将对话全链路实时同步到前端 UI 与会话数据库。"""
        if not self.orch:
            self.client.send_message(
                token=token,
                to_user_id=to_user,
                from_user_id="",
                text="⚠️ 本地 Agent 引擎未就绪。",
                context_token=context_token,
                base_url=base_url,
            )
            return

        def _run():
            with self.turn_lock:
                previous_session = getattr(self.orch, "session_id", None)
                previous_callback = getattr(self.orch, "on_history", None)
                previous_event = getattr(self.orch, "on_event", None)
                try:
                    _run_locked(previous_event)
                finally:
                    self.orch.on_event = previous_event
                    self.orch.on_history = previous_callback
                    self.orch.session_id = previous_session

        def _run_locked(previous_event):
            from agent.models import Message
            from agent.state_text import tool_summary, result_summary

            sid = self.weixin_session_id
            history_ready = self.session_store is None
            self.orch.on_history = None
            if self.session_store is not None:
                # A failed load must never reuse another session's live history.
                self.orch.history = []
            try:
                # 1. Find the session before accepting or persisting a message.
                if not sid and self.session_store:
                    rows = self.session_store.list_sessions(limit=1)
                    if rows:
                        sid = rows[0]["session_id"]
                    else:
                        sid = self.session_store.new_session(user_text[:15] or "微信会话")
                    self.weixin_session_id = sid
                self.orch.session_id = sid or f"weixin:{to_user}"

                # 2. Bind persistence only after the intended history was loaded.
                if sid and self.session_store:
                    self.orch.history = self.session_store.load_history(sid)
                    history_ready = True
                    self.orch.on_history = (
                        lambda history, session_id=sid: self.session_store.save_history(
                            session_id, history)
                    )

                # 3. Broadcast the accepted user message.
                if self.on_event:
                    self.on_event("message", {
                        "id": f"msg_wx_u_{int(time.time() * 1000)}",
                        "type": "user",
                        "role": "user",
                        "content": f"📱 [微信用户] {user_text}",
                        "tool_calls": [],
                        "created_at": time.strftime("%H:%M", time.localtime()),
                        "session_id": sid or "",
                    })

                user_msg = Message(role="user", content=user_text)
                self.orch.history.append(user_msg)
                if sid and self.session_store:
                    self.session_store.save_history(sid, self.orch.history)
                # The callback is scoped to this turn and never forwards model-supplied
                # text to Weixin. Mark before attempting delivery, including failures.
                active_session_id = self.orch.session_id
                progress_attempted = False
                progress_open = True
                progress_lock = threading.Lock()

                def _on_task_event(event, payload):
                    nonlocal progress_attempted
                    if callable(previous_event):
                        try:
                            previous_event(event, payload)
                        except Exception:
                            # Orchestrator.emit already treats UI callbacks as optional.
                            pass
                    if event != "task_progress" or not isinstance(payload, dict):
                        return
                    with progress_lock:
                        origin_id = user_msg.metadata.get("task_origin_id") or user_msg.id
                        if (not progress_open or progress_attempted
                                or payload.get("stage") != "submitted"
                                or payload.get("session_id") != active_session_id
                                or self.orch.session_id != active_session_id
                                or payload.get("task_origin_id") != origin_id):
                            return
                        progress_attempted = True
                        try:
                            result = self.client.send_message(
                                token=token, to_user_id=to_user, from_user_id="",
                                text=_SUBMITTED_RECEIPT, context_token=context_token,
                                base_url=base_url, timeout_sec=_PROGRESS_TIMEOUT_SEC,
                            )
                            if isinstance(result, dict) and result.get("ret") not in (None, 0):
                                raise RuntimeError("Weixin progress delivery rejected")
                        except Exception as progress_exc:
                            self._log_runtime_error(
                                "weixin_progress_delivery_error", progress_exc,
                                session_id=active_session_id,
                            )

                # 4. Agent 核心执行处理。A short synchronous send guarantees the
                # progress receipt finishes before the final reply; no late worker.
                self.orch.on_event = _on_task_event
                try:
                    raw_reply = self.orch.handle(user_text)
                finally:
                    with progress_lock:
                        progress_open = False
                reply = raw_reply.strip() if raw_reply else "本轮未生成回复，请查看任务状态。"
                if self.vault:
                    reply = self.vault.scrub(reply)

                # 5. 持久化最新会话历史
                if sid and self.session_store:
                    try:
                        self.session_store.save_history(sid, self.orch.history)
                    except Exception as _e:
                        self._log_runtime_error("weixin_history_save_error", _e, session_id=sid)

                # 6. 获取本轮工具调用信息
                tool_calls = []
                if hasattr(self.orch, "last_turn_tool_records"):
                    for rec in self.orch.last_turn_tool_records:
                        rec_copy = dict(rec)
                        rec_copy["summary"] = tool_summary(rec.get("tool", ""), rec.get("args"))
                        rec_copy["result_summary"] = result_summary(rec.get("result", ""), max_len=60)
                        tool_calls.append(rec_copy)

                # 7. 实时向前端 UI 广播 Agent 的最终答复
                if self.on_event:
                    self.on_event("message", {
                        "id": f"msg_wx_a_{int(time.time() * 1000)}",
                        "type": "assistant_final" if reply else "tool_step",
                        "role": "agent",
                        "content": reply,
                        "tool_calls": tool_calls,
                        "status": getattr(self.orch, "last_turn_status", "done"),
                        "created_at": time.strftime("%H:%M", time.localtime()),
                        "session_id": sid or "",
                    })

                # 8. 回传失败只影响消息交付，不能推翻已完成并保存的电脑任务。
                try:
                    self.client.send_message(
                        token=token,
                        to_user_id=to_user,
                        from_user_id="",
                        text=reply,
                        context_token=context_token,
                        base_url=base_url,
                    )
                except Exception as delivery_exc:
                    self._log_runtime_error(
                        "weixin_delivery_error", delivery_exc, session_id=sid or "")
            except Exception as e:
                self._log_runtime_error("weixin_task_error", e, session_id=sid or "")
                # 2026-10-16：AuthenticationError 99% 是 LLM key 无效
                # （openai 包抛的），不是微信 token 问题——微信推送失败
                # 在 notify_approval/send_message 里已单独捕获，不会到这里。
                # 在消息里给出来源提示，避免误判。
                err_type = type(e).__name__
                hint = ""
                if err_type == "AuthenticationError":
                    hint = "（通常是 LLM API key 无效，检查 vault/环境变量）"
                elif err_type in ("APIConnectionError", "Timeout"):
                    hint = "（网络连接问题）"
                err_msg = f"⚠️ 微信任务发生异常（{err_type}）{hint}，本轮未完成，请重试。"
                error_message = Message(role="assistant", content=err_msg)
                self.orch.history.append(error_message)
                self.orch.last_turn_status = "error"
                # A failed load is audited in runtime events, not by overwriting
                # an existing session with an incomplete or unrelated history.
                if history_ready and sid and self.session_store:
                    try:
                        self.session_store.save_history(sid, self.orch.history)
                    except Exception as save_exc:
                        self._log_runtime_error(
                            "weixin_error_history_save_error", save_exc, session_id=sid)
                if self.on_event:
                    try:
                        self.on_event("agent_state", {
                            "state": "error", "detail": err_msg,
                            "model": getattr(getattr(self.orch, "llm", None), "model", ""),
                        })
                        self.on_event("message", {
                            "id": error_message.id,
                            "type": "assistant_final",
                            "role": "agent",
                            "content": err_msg,
                            "tool_calls": [],
                            "status": "error",
                            "created_at": time.strftime("%H:%M", time.localtime()),
                            "session_id": sid or "",
                        })
                    except Exception as event_exc:
                        self._log_runtime_error("weixin_error_event_failed", event_exc)
                try:
                    self.client.send_message(
                        token=token,
                        to_user_id=to_user,
                        from_user_id="",
                        text=err_msg,
                        context_token=context_token,
                        base_url=base_url,
                    )
                except Exception as send_exc:
                    self._log_runtime_error("weixin_error_reply_failed", send_exc)

        threading.Thread(target=_run, daemon=True).start()

    def notify_approval(self, card: Any) -> str:
        """当系统触发敏感操作审批时，主动推送微信卡片提醒，实现免键鼠审批。

        返回推送状态，前端据此在审批卡上显示可见标记，不再静默：
        sent / skipped:not_connected / skipped:no_credentials / failed:<原因>
        """
        if not self.is_connected():
            return "skipped:not_connected"

        creds = self.credentials or {}
        token = creds.get("bot_token")
        bot_id = creds.get("ilink_bot_id")
        user_id = creds.get("ilink_user_id")
        base_url = creds.get("baseurl") or FIXED_BASE_URL
        context_token = self.context_tokens.get(user_id)

        if not token or not user_id or not bot_id:
            return "skipped:no_credentials"

        action_cn = getattr(card, "action_cn", "") or getattr(card, "action", "操作")
        summary_cn = getattr(card, "summary_cn", "") or getattr(card, "detail", "")
        intent_cn = getattr(card, "intent_cn", "") or "推进当前任务"
        risk_level = getattr(card, "risk_level", getattr(card, "risk", "L2"))

        notice = (
            "🛡️ 【桌面操作授权确认】\n"
            f"● 操作类型：{action_cn}\n"
            f"● 操作概要：{summary_cn}\n"
            f"● 操作目的：{intent_cn}\n"
            f"● 风险等级：{risk_level}\n\n"
            "👉 请在手机微信直接回复：\n"
            "【1】或「同意」：通过授权\n"
            "【0】或「拒绝」：拒绝操作\n"
            "（注：在微信中确认无需动电脑键鼠，保障桌面自动化坐标绝对精准）"
        )

        try:
            self.client.send_message(
                token=token,
                to_user_id=user_id,
                from_user_id=bot_id,
                text=notice,
                context_token=context_token,
                base_url=base_url,
            )
            logger.info("已将审批申请实时推送到用户手机微信。")
            return "sent"
        except Exception as e:
            logger.warning(f"推送微信审批申请失败: {e}")
            return f"failed:{e}"


if __name__ == "__main__":
    import shutil
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    root_dir = Path(__file__).resolve().parent.parent
    data_dir = root_dir / "data" / "weixin"
    data_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("🚀 AlterEgo 微信 ClawBot 通道启动器")
    print("协议标准：Tencent/openclaw-weixin (iLink Bot Protocol)")
    print("=" * 60)

    # 尝试加载完整 Agent 引擎（Orchestrator 与 ApprovalManager）
    orch = None
    approval_mgr = None
    try:
        from agent.main import load_config, resolve_llm_key, resolve_backup_llm_key
        from agent.llm import LLMClient
        from agent.tools_registry import ToolRegistry
        from agent.context import ContextBuilder
        from agent.orchestrator import Orchestrator
        from agent.approval import ApprovalManager
        from tools import register_all
        from vault import Vault

        cfg = load_config()
        vault = Vault.for_machine(root_dir)
        llm_key = resolve_llm_key(vault)
        backup_key = resolve_backup_llm_key(vault)
        llm = LLMClient.from_config(cfg, api_key=llm_key, fallback_api_key=backup_key)
        tools = ToolRegistry()
        register_all(tools, cfg, api_key=backup_key or llm_key)
        approval_mgr = ApprovalManager(db_path=str(root_dir / "approvals.db"))
        ctx = ContextBuilder(max_tokens=cfg.get("context", {}).get("max_context_tokens", 8000))
        orch = Orchestrator(llm=llm, tools=tools, ctx=ctx)
        print("✓ Agent 核心引擎与工具注册表就绪。")
    except Exception as err:
        print(f"⚠️ 以独立通道模式运行 (未注入完整桌面引擎: {err})")

    channel = WeixinChannel(data_dir=data_dir, orch=orch, approval_mgr=approval_mgr)

    if channel.is_connected():
        print(f"\n✅ 微信 ClawBot 已连接！绑定用户 ID: {channel.credentials.get('ilink_user_id')}")
        print("后台长连接运行中，您可以直接在微信中发送指令或等待操作授权。")
    else:
        print("\n⏳ 正在向腾讯微信 iLink 服务器申请绑定二维码...")
        login_res = channel.start_qr_login(force=True)
        qr_url = login_res.get("qrcode_url", "")
        img_path = login_res.get("qr_image_path", "")

        print("\n" + "=" * 60)
        print("📱 请使用手机微信扫码绑定 ClawBot：")
        print("操作步骤：手机微信 ->「我」->「设置」->「插件」->「微信 ClawBot」-> 点击「开始扫一扫」")
        print("🔗 绑定链接：", qr_url)
        print("🖼️ 二维码图片：", img_path)
        print("=" * 60)

        # 复制到脑图产物目录
        artifact_dir = Path(r"C:\Users\YouTube\.gemini\antigravity\brain\dcef2a6d-7416-41e3-ba37-29a4168944b0")
        if artifact_dir.exists() and Path(img_path).exists():
            shutil.copy2(img_path, artifact_dir / "weixin_qr.png")
            print(f"✓ 已将二维码同步至交互视图: {artifact_dir / 'weixin_qr.png'}")

    print("\n通道守护进程运行中... 按 Ctrl+C 退出。\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止微信通道...")
        channel.stop_message_loop()
        sys.exit(0)

