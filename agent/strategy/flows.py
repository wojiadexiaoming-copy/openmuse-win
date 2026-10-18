"""跨站任务编排模板。

多站点任务（如"用临时邮箱注册某站"）的通用流程模式。
模板是数据，不是硬编码逻辑：描述角色分工、步骤序列、检查点、恢复点。

用 role 管理标签页（browser_new_tab 的 role 参数）：
每个逻辑站点独占一个 tab，禁止在同一 tab 来回 navigate 切站。

模板使用方式：
1. 任务开始时，LLM 识别任务模式，选用对应模板
2. 按模板的 steps 执行，每步仍由 LLM 根据实时页面状态决策具体工具调用
3. checkpoints 是必须验证的点（用 expect / wait_for / state_pin）
4. 失败时查 recovery 策略
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FlowStep:
    """模板中的一步：描述做什么、用什么工具、验证什么。"""
    name: str
    description: str
    tools: list[str] = field(default_factory=list)
    verify: str = ""       # 验证方式描述（如 "expect text_visible"）
    recovery_hint: str = ""  # 这步失败时的特别提示


@dataclass
class FlowTemplate:
    """跨站流程模板。"""
    name: str
    description: str
    roles: dict[str, str]          # role → 职责描述
    steps: list[FlowStep]
    checkpoints: list[str] = field(default_factory=list)  # 必须钉死的检查点


# --- 模板：邮箱验证码注册（通用模式） ---
# 覆盖：任何"邮箱收验证码完成注册"的站点。不含单站点专用选择器。
EMAIL_VERIFICATION_REGISTRATION = FlowTemplate(
    name="email_verification_registration",
    description="邮箱验证码注册通用流程：邮箱站拿地址 → 目标站填表发码 → 邮箱站收码 → 目标站填码完成",
    roles={
        "mailbox": "邮箱站：获取地址、收验证码",
        "target": "目标站：填注册表、发码、填码",
    },
    steps=[
        FlowStep("open_mailbox", "开邮箱站标签页（role=mailbox），获取可用地址",
                 tools=["browser_new_tab", "browser_snapshot"],
                 verify="拿到地址文本",
                 recovery_hint="地址拿不到就换一家邮箱服务，不要死磕"),
        FlowStep("pin_email", "从截图读回地址，state_pin 钉死为 email",
                 tools=["browser_screenshot", "state_pin"],
                 verify="state_check email 一致",
                 recovery_hint="每次切回邮箱页先核对地址：过期或用户手动换了都会变"),
        FlowStep("open_target", "开目标站标签页（role=target），进注册页",
                 tools=["browser_new_tab", "browser_snapshot"],
                 verify="注册表单可见"),
        FlowStep("fill_form", "填邮箱（assert_pinned_key 校验）及其他必填项",
                 tools=["browser_type", "browser_select"],
                 verify="填完截图确认值与框对应（防填错框）",
                 recovery_hint="填完先 screenshot 肉眼确认，别急着点发送"),
        FlowStep("send_code", "点发送验证码，调用 browser_verify_code_countdown 确认倒计时启动",
                 tools=["browser_click_at", "browser_click", "browser_verify_code_countdown", "browser_request_takeover"],
                 verify="必须通过 browser_verify_code_countdown 确认出现倒计时数字（如 59s、重新获取(58)），未出现倒计时严禁切换标签页",
                 recovery_hint="未进入倒计时两步排查：1. 若弹出滑块/安全验证，立即调用 browser_request_takeover 请求人工介入；2. 若表单有报错未填完整，先补填表单再点击"),
        FlowStep("wait_code", "切邮箱站等码（wait_for text_visible，1-2 分钟）",
                 tools=["browser_switch_tab", "browser_wait_for"],
                 verify="看到验证码文本",
                 recovery_hint="超时先刷新邮箱页；确认发码接口真的成功了（network_log 看状态码）"),
        FlowStep("fill_code", "读码 → state_pin 钉死 → 切目标站填码提交",
                 tools=["browser_screenshot", "state_pin", "browser_switch_tab",
                        "browser_type", "browser_click"],
                 verify="提交后 expect 成功标志（url_contains 或成功文本）",
                 recovery_hint="码填错：确认读的是最新一封；码过期重发"),
    ],
    checkpoints=[
        "email 地址 state_pin 钉死，每次用前核对",
        "发码判定铁律：发码后必须看到倒计时数字（如 59s/重新获取(58)），未出现倒计时严禁去邮箱傻等",
        "验证码 state_pin 钉死，防止读错",
        "最终成功以业务目标为准（登录态/成功页），不是'流程走完了'",
    ],
)

# --- 模板：OAuth 第三方登录（通用模式） ---
OAUTH_LOGIN = FlowTemplate(
    name="oauth_login",
    description="OAuth 第三方登录通用流程：目标站 → 跳第三方授权 → 回跳目标站",
    roles={
        "target": "目标站",
    },
    steps=[
        FlowStep("click_oauth", "点第三方登录按钮（expect url_contains 第三方域名）",
                 tools=["browser_click"],
                 verify="URL 跳到第三方授权页",
                 recovery_hint="弹窗形式：window.open 会自动注册为 popup_N 标签页"),
        FlowStep("authorize", "第三方页点授权/登录（可能要输账号密码）",
                 tools=["browser_snapshot", "browser_type", "browser_click"],
                 verify="授权成功"),
        FlowStep("back_to_target", "等回跳目标站（wait_for url_contains 目标域名）",
                 tools=["browser_wait_for"],
                 verify="目标站已登录（browser_storage 看 session cookie）",
                 recovery_hint="没回跳：检查弹窗是否还开着；第三方是否报了错（console）"),
    ],
    checkpoints=[
        "授权前确认是用户授权过的账号体系，不要用频道账号做测试登录",
        "回跳后用 browser_storage 确认登录态，不要只看页面文案",
    ],
)

FLOW_TEMPLATES: dict[str, FlowTemplate] = {
    EMAIL_VERIFICATION_REGISTRATION.name: EMAIL_VERIFICATION_REGISTRATION,
    OAUTH_LOGIN.name: OAUTH_LOGIN,
}


def get_template(name: str) -> FlowTemplate | None:
    return FLOW_TEMPLATES.get(name)


def match_template(user_text: str) -> FlowTemplate | None:
    """按任务描述关键词匹配流程模板。返回 None 表示无匹配，走通用流程。"""
    text = (user_text or "").lower()
    # 邮箱验证码注册：含"注册" + 邮箱/验证码
    if any(k in text for k in ("注册", "register", "signup", "sign up")) \
            and any(k in text for k in ("邮箱", "验证码", "email")):
        return FLOW_TEMPLATES["email_verification_registration"]
    # OAuth 第三方登录
    if any(k in text for k in ("第三方登录", "oauth", "扫码登录", "用微信登录", "用谷歌登录")):
        return FLOW_TEMPLATES["oauth_login"]
    return None


def template_prompt(t: FlowTemplate) -> str:
    """模板转 prompt 文本：任务开始时注入。"""
    lines = [f"[流程模板: {t.name}] {t.description}", "角色分工:"]
    for role, desc in t.roles.items():
        lines.append(f"  - {role}: {desc}")
    lines.append("步骤:")
    for i, s in enumerate(t.steps, 1):
        lines.append(f"  {i}. {s.name}: {s.description}")
        if s.verify:
            lines.append(f"     验证: {s.verify}")
    if t.checkpoints:
        lines.append("检查点（必须遵守）:")
        for c in t.checkpoints:
            lines.append(f"  - {c}")
    return "\n".join(lines)
