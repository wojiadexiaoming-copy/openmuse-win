# OpenRouter 站点怪癖与实战规则

## 1. "Welcome to OpenRouter" 是 5 步向导，不是可关闭弹窗
- **现象**：登录或新会话进入 OpenRouter 后，全屏弹出 "Welcome to OpenRouter" 模态框，底部有 5 个小圆点进度指示器，包含 "Individual" / "Organization" 选项及右下角紫色的 "Next" 按钮。页面上**没有任何 Dismiss、关闭、X 按钮**。
- **致命误区**：顶部的 `Dismiss` 按钮（约 @(968,94)）是 ElevenLabs 公告条的关闭按钮，与向导弹窗无关。寻找 Dismiss / close 关弹窗必然死循环导致任务中断。
- **应对**：
  1. 严禁寻找 `Dismiss`、`close` 或尝试关掉它；
  2. 按照步骤一步步走完向导：点击 `button:has-text("Individual")`（或从坐标树点击 Individual 卡片），然后点击 `button:text-is("Next")`（若点击未反应，可配合 `browser_click_at` 依据坐标树点击 Next 按钮），直到向导 5 步完成；
  3. 完成向导后，才可进入后台创建 API Key。

## 2. 浏览器已有登录态导致无法直接注册新账号
- **现象**：用户要求"使用指定邮箱注册 OpenRouter"，但直接访问 `openrouter.ai/sign-up` 时，由于浏览器 profile 存有 session，网页直接跳过注册重定向到工作区或引导页，页面左侧可见 `Sign out` 按钮和已存在的 API Key。
- **应对**：
  1. 识别到当前已有登录态时，若用户的目标是使用特定域名邮箱全新注册账号，必须先点击左下角 `Sign out` 彻底退出登录；
  2. 退出登录后再次访问 `https://openrouter.ai/sign-up`，才能看到真实的注册邮箱/密码输入表单并开始发码流程。
