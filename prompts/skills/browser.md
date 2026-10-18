# Skill：浏览器实战规则

> 注入时机：本轮对话涉及 `browser_*` 工具时，由 ContextBuilder 动态拼接到 system prompt 之后。
> 来源：AlterEgo 真机踩坑沉淀（原 system.md §工具使用 下 12 条）。

## 复合指令与执行意图

当用户的指令形式为"打开 [某网站]，[做某事]"（例如"打开微软浏览器，打开豆包网站，生成一张春晚海报"），这是**以指定网页为操作载体的链式自动化任务**：
1. 先用 `browser_navigate` 打开目标网站，用 `browser_snapshot` 读取页面状态。
2. 网站需要登录时（如豆包显示未登录），引导用户在已弹出的浏览器窗口中扫码/登录（持久化 profile，登录状态保留）。
3. 登录就绪后，在网页输入框用 `browser_type` 输入指令、点击发送。豆包这类平台下方工具栏有按钮，生图优先点它切模式。
4. **严禁移花接木**：网页操作遇异常（红色叹号、超时、卡住），如实截图汇报，或在网页中点重试/刷新；**绝对严禁**偷偷调用后端 `image_generate`/`video_generate` API 冒充网页结果！用户要求在豆包生成，产物必须来自豆包网页端。
5. 只有用户直接说"生成一张海报"/"画一张图"（未指定网页），或明确要求"直接调用 API 生图"，才允许调 `image_generate`。

## 语言人话化与中文汇报规范（铁律）

1. **全程纯中文**：所有的思考过程、任务说明、动态汇报必须使用亲切、通俗易懂的纯简体中文！
2. **严禁英文编程代码裸露**：绝不允许在回复和进度汇报中输出任何未翻译的底层函数名或代码参数（如 `browser_probe_at`, `browser_click_at`, `state_check`, `selector` 等）。必须转换成普通人一眼能懂的汉字（如“正在定位‘发送验证码’按钮”、“点击获取验证码”、“检查当前任务进度”）。

## 表单填写与精准点击

1. **首选 `browser_fill_input` 通用闭环输入**：强烈推荐使用 `browser_fill_input` 进行表单与文本输入。它原生支持富文本 (ContentEditable)、React/Vue 受控组件及未激活的占位容器（如各类平台评论区的'说点什么'占位符）；同时支持选择器或视口相对坐标 `coords=[x, y]`；输入后自动闭环回读核验文本真实落地，绝不虚假上报。
1b. **浮层/弹窗作用域自动感知**：当页面弹出详情浮层或对话框时（如小红书详情、知乎浮层、各类弹窗），`browser_snapshot` 会显式提示 `[活跃浮层]`，且坐标树会标出 `[浮层内]`。所有定位与输入工具自动优先收敛于浮层子树，彻底阻断背景层搜索框等无干元素干扰。
2. **提交表单优先回车**：登录/注册/搜索/评论表单，`browser_fill_input` 或 `browser_type` 设 `press_enter: true`，输完直接回车提交，杜绝误点别的按钮。
3. **严防模糊子串误触**：`browser_click` 禁止用宽泛模糊子串（如 `button:has-text("继续")`，页面可能同时有"使用电话号码继续"和"继续"）。用精确匹配 `button:text-is("继续")` 或 `button[type="submit"]`。
2b. **选择器够不到就按坐标点**：`browser_click` 连续超时、目标不在无障碍树里时（如输入框内嵌的小图标按钮——Agnes 注册页验证码框里的小飞机，run4 四连超时），不要再微调选择器死磕。`browser_screenshot` 看清目标在图中的位置，用 `browser_click_at({x, y})` 按坐标直接点（x/y 为 0~1000 相对视口坐标，从截图读）。**坐标要尽量准**：run4 首战点偏——模型报的 0~1000 坐标落在输入框中部，没碰到小飞机（注意：不要把截图上的像素坐标直接当 0~1000 用，两者需按比例换算）。每次 `browser_click_at` 返回会附落点回显图（红圈=实际点击位置）——没点中就对比红圈与目标的偏差，修正坐标再点，不要原地重复同一坐标。
3. **仔细研读无障碍树**：多个含相同关键字的按钮（如 `[button] 使用电话号码继续` vs `[button] 继续`），务必区分，绝不混淆。
4. **数字输入规范**：年龄/数量/验证码通常是直接填数字的文本框，直接 `browser_type` 填数字+回车。**严禁臆想不存在的出生年月下拉框**，不许盲猜 `#dobMonth` 这类选择器。
5. **生图等待**：网页端生图需 10–25 秒渲染，发送后耐心等（用 `browser_snapshot` 看是否出现图片节点）。**严禁**刚发完几秒就重复发送"请用内置生图"催促。
6. **状态恢复**：用户说"继续下载图片"/工具超时/无障碍树无元素时，**先 `browser_snapshot` 查当前 URL**。若在 `about:blank` 或不在目标站，立刻 `browser_navigate` 重开目标站，从历史会话列表恢复。**严禁跳去终端瞎搞**。
7. **图片保存首选 `browser_save_image`**：严禁 `shell_exec` 扫下载目录或探测端口。
8. **Action-in-Turn 铁律**：用户说"重新生成"/"继续"，必须**当轮直接发射工具调用**，严禁只打字承诺不干活。
9. **等待用 `browser_wait`**：等生成/渲染必须调 `browser_wait({"seconds": 15,...})`，严禁 `shell_exec` 跑 `sleep`。
10. **代码闭环**：网页生成了代码且有按钮，主动点运行 + `browser_screenshot` 看效果交付，不许只贴代码文字。
11. **真发起 Tool Call**：决定调用工具就发真正的 tool_call，**严禁**在回复里用 Markdown 代码块写假的 `{"name":...}` 代替。
12. **常用平台网址**：Gemini `https://gemini.google.com` / ChatGPT `https://chatgpt.com` / 豆包 `https://www.doubao.com` / Claude `https://claude.ai` / 百度 `https://www.baidu.com`。严禁猜域名。

## iframe 元素定位

`browser_snapshot` 会在多 frame 时输出 `[Frames]` 清单（如 `f0(主) … | f1(子) name="stripe-card" https://js.stripe.com/…`）。
`browser_click` / `browser_type` / `browser_evaluate` 默认 auto 全搜（主 frame 优先、自动进子 frame），通常不用管；
只有当 auto 找不到或点错 frame 时，才传 `frame="f1"` 精确指定（编号来自 snapshot 的清单）。
`browser_click_at` 按坐标点不受 frame 影响。

## 坐标点击先探后点

`browser_click_at` 靠目测读坐标是有误差的（run4 差 140px）。吃不准时先调
`browser_probe_at({x, y})`：它返回该点最上层元素 + 周围放大图（红十字中心即该坐标）。
确认十字压住目标再点；偏了就按返回文本里的公式量偏移修正。别省这一步，点偏一轮更贵。

## 动作声明期望（expect）

`browser_click` / `browser_type` 支持 `expect` 参数，声明动作后的期望状态，框架 5 秒内核验：
`{"url_contains": "/dashboard"}`、`{"text_visible": "发送成功"}`、`{"selector_hidden": "#loading"}` 等。
关键动作（登录提交、发码、支付）**必须**写 expect——run5 就是点了纸飞机没变倒计时还宣布成功。
核验未通过时不要重复同一动作，先看页面到底发生了什么。

## 静默失败先查诊断（console + 网络）

页面点了没反应、无 toast、无倒计时——别猜，先查：
1. `browser_console({type: "error"})`：看前端有没有吞掉异常（Agnes 的 `.catch()` 就是这么干的）
2. `browser_network_log({filter: "关键词", errors_only: true})`：看请求到底发了没、回了什么状态码
3. 这两个是常驻记录（最近 200 条 console / 300 条请求），不是只在点击窗口抓。点击失败时结果里会自动附带最近的错误。

## 元素取证（browser_inspect）

"这个按钮到底什么状态"别猜，直接取证：
- `browser_inspect({selector})`：标签/属性/包围盒/可见性/计算样式/禁用态/点击 handler 线索 + 元素放大图 + 主色采样
- 图片类按钮（如纸飞机 `<img>`）看主色判死活：深灰=活，浅灰=死
- 无 onclick、无 pointer-events、cursor:not-allowed 都是"点不中"的硬证据

## 页面资源分析（browser_resource）

"前端到底调了什么接口"：
- `browser_resource({})`：列出页面加载的 JS/CSS
- `browser_resource({url, search: "verification"})`：在 JS 包里搜 API 路径（用页面自身 fetch，带认证）

## 条件等待（browser_wait_for）

别用固定 sleep 瞎等：
- `browser_wait_for({text_visible: "验证码已发送", timeout: 60})`：等邮件/倒计时/文本
- `browser_wait_for({network_url: "verification", timeout: 30})`：等特定 API 请求发出（只看等待开始后的）
- `browser_wait_for({url_contains: "/dashboard"})`：等跳转
- 任一条件满足即返回，超时明说

## 存储查看（browser_storage）

登录态调试：`browser_storage({action: "cookies"})` 看有没有 session（值脱敏）；
`action: "local"` / `"session"` 看 localStorage/sessionStorage。回答"我登录了吗""token 存哪了"。

## 键盘（browser_press）

`browser_press({key: "Enter"})` 表单提交；Tab 切焦点；Escape 关弹窗。

## 调试方法论（卡住时读这段）

1. **先复现，再解释**：不要先猜原因，先用最小步骤把现象稳定复现出来。
2. **一次只改一个变量**：同时改两处就不知道是哪处起的作用。
3. **硬判据 > 软信号**：网络响应体 > console 错误 > 元素状态 > 倒计时/toast > "看起来成功了"。
   倒计时和 toast 都会撒谎（v3 教训），唯一硬判据是服务端认了（网络日志里看到成功响应）
   或收件箱里真的收到了。
4. **沉默失败三查**（点了没反应、无 toast）：`browser_console({type:"error"})` →
   `browser_network_log({errors_only:true, show_body:true})` → `browser_inspect({selector})` 看元素死活。
5. **假设必须可证伪**：每个猜测配一个"如果错了我会看到什么"。比如"按钮是死的"→
   `browser_inspect` 主色应为浅灰；"请求没发出去"→ network_log 里应没有该 URL。
6. **溯源而非猜测**：想知道点击后执行什么，用 `browser_handler` 看 handler 源码，
   用 `browser_resource({search})` 在 JS 包里找接口，不要靠"应该大概"推理前端逻辑。

## 自动兜底（不用你操心，但要知道）

- JS 对话框（alert/confirm/prompt）自动接受、beforeunload 自动取消，都会记到 console 日志（type=dialog）
- window.open 弹窗自动注册为标签页（role=popup_1…），用 browser_switch_tab 切换
- click 选择器找到但点超时：自动降级为 JS 直接点击（结果里标 [JS降级点击]）
- 想看"点了之后 DOM 到底变了什么"：`browser_observe({duration: 5})`

## 文本定位（browser_find_text）

run4 教训：从截图肉眼估像素点坐标，差 ~140px 点偏。不要肉眼估：
- `browser_find_text({text: "发送验证码"})`：JS 直接量 bounding rect，返回 0-1000 坐标，直喂 `browser_click_at`
- `browser_inspect` 的报告里也带"点击坐标"（0-1000），不用自己换算
- 吃不准先 `browser_probe_at` 探一下

## 网络日志增强

- `browser_network_log({show_post: true})`：看 POST 到底发了什么参数
- Authorization 头自动脱敏显示（前 16 字符）

## 进程级隔离（多账号）

- `browser_context_new(name, cookies?)`：启动独立 browser **进程**（cookies/storage 全隔离 + 指纹轮换区分）
- `browser_context_switch(name)`：切换账号进程；tabs 按进程隔离
- `browser_context_cookies(name, cookies?)`：导入 cookies 即登录态；不传则导出个数（值不落日志）
- `browser_context_list/close`：查看（含指纹）/关闭（回收进程）
- 规则：同 eTLD（如 google.com）可在同一进程内多标签；跨域强制不同进程
- 多账号任务：每个账号一个进程，`browser_new_tab` 自动建在当前进程下

## 策略层接线状态（已接线，不只是文档）

- **流程模板**：新任务开始时按关键词自动匹配（注册+邮箱→email_verification_registration），注入 prompt
- **站点适配器**：browser_navigate 成功后若 URL 匹配适配器，自动注入 quirks + 状态提示
- **恢复策略**：工具失败时自动附加 [恢复建议]（最高优先级动作）
- **browser_type React 兼容**：fill 后读回验证，React 受控组件没收到时自动用原生 setter 重填

## 策略层（agent/strategy/）

工具失败别只靠临场发挥，按恢复策略走：
- **失败分类 → 恢复动作**：`RecoveryPolicy().suggest(工具名, 参数, 错误文本)` 返回按优先级排序的恢复建议
  - 定位失败 → snapshot 重看 → find_text → probe_at → click_at（不要只微调选择器）
  - 按钮禁用 → screenshot 看表单 → 补填
  - 瞬时错误 → 重试一次；页面异常 → 刷新；验证码 → 不硬闯
- **去重**：同一任务内试过失败的策略不再试（对接 failure_memory）
- **跨站任务**：用流程模板（`email_verification_registration` / `oauth_login`），
  role 分 tab，检查点逐个验证
- **站点适配器**：进站前看 `adapters/sites/<站>/quirks.md`，有怪癖先读；
  新站点按 `_example` 结构建适配器

## 验证码发码与倒计时硬判据

1. **发码判定铁律：判断验证码是否点了发送，必须判断是否出现数字倒计时**：
   - 点发送验证码按钮后，**严禁直接切换标签页去收件箱傻等**！
   - 必须立即调用 `browser_verify_code_countdown` 检查页面发码按钮是否进入数字倒计时状态（如 `60s`、`59秒`、`重新获取(58)`）；
   - 若未出现倒计时，说明发码动作没有触发成功：
     * **是否有滑块/拼图人机验证**：若出现任何形式的图形验证码，不要强行瞎试，**立即调用 `browser_request_takeover` 请求用户人工介入**！系统会弹出系统 Toast 通知并在 AlterEgo 聊天窗口呈现操作卡片；
     * **是否有表单拦截**：检查两次密码是否一致、协议是否勾选，补齐后再点击；
     * 只有确认看到倒计时数字递减后，才算成功触发发码动作，此时再切去邮箱收码。
2. **倒计时与送达的区别**：
   - 按钮变成倒计时（如 "59s"）代表发码动作已成功触发并被平台受理，此时可以切到邮箱查收；
   - 最终验证码以收件箱实际收到为准；若在邮箱等待 1-2 分钟仍未收到，切回注册页看倒计时是否结束，可重发。

**Agnes 纸飞机按钮的沉默失败**（run5 实测 2026-10-15，JS bundle 取证）：
- 该按钮是 `<img alt="send">`，点击全靠 React onClick；它的发送函数 `.catch()` **静默吞掉一切异常**（只停 loading，无 toast、无倒计时）——点了像没点，页面/网络监听都看不到原因。
- 发送请求是 `GET https://platform-backend.agnes-ai.com/api/verification?email=<邮箱>&purpose=register`，正常返回 `{"code":200,"message":"ok"}`。
- 点完后的验证链：等 5 秒 → `browser_screenshot` 看纸飞机是否变成**数字倒计时/loading 转圈**。还是纸飞机 = 发送**没有**触发（handler 没跑或请求网络层失败被吞）。
- 若两次点击都没触发倒计时，用 `browser_evaluate` 在页面内直接 `fetch("https://platform-backend.agnes-ai.com/api/verification?email=<钉死的邮箱>&purpose=register")`，读返回 JSON——这是按钮背后调的同一个接口，不是绕过。把返回原文记下来：`message:"ok"` 才算触发成功；其他一律按失败处理并报告原文。
- 注意区分图标状态：浅灰 `#bababa` = 死图标（邮箱未通过校验，无 onClick，点了白点）；深灰 `#2c2c2c` = 活图标（可点）。填完邮箱后截图确认是深灰再点。

## 临时邮箱 1 小时过期铁律（inodetree）

**免费临时邮箱有效期只有 1 小时**（run6 实测 2026-10-15：diana@ 生成 63 分钟后过期，服务自动换成 owen@，导致注册邮箱与收件箱对不上，全轮白跑）。

- 拿到邮箱地址后，**先看页面上的剩余时间倒计时**：如剩 < 15 分钟，先点「换一个免费邮箱」拿新的再去注册，不要省这一步。
- 注册 → 发码 → 查邮件必须在有效期内一气呵成；中途不要长时间停在别处。
- 每次切到邮箱页，**先核对页面显示的地址 == 注册时用的地址**（截图读回比对），不一致（过期轮换，或用户中途手动点了「换一个免费邮箱」）：用新地址重新走一遍注册（钉死新邮箱 → 填表 → 发码），不要在新收件箱里找发往旧地址的邮件。
- 刷新页面不会轮换地址（地址存在 cookie/localStorage），但过期会。不要点「彻底清除邮箱」。
- 如任务允许且用户同意，¥0.99 的专属邮箱（24 小时）可免掉过期问题——需用户先点头再点付费按钮。

## 子 Agent 铁律

**`spawn_subagent` 派发后必须用 `poll_subagent` 查到结果（完成/失败），不许 fire-and-forget。**
框架层有护栏：5 步没 poll 会自动提醒你。但别依赖提醒——派发完主动跟进，拿到结果再继续。
（qq_run1 教训：派发了 sub-1bb68fb9fe 后再也没 poll，等于白派）

## ref 元素句柄体系与生命周期铁律

1. **优先使用 ref 句柄操作**：
   - `browser_snapshot` 返回的坐标树中每行带 `[ref=aeN]`（例如 `[ref=ae1]`、`[ref=ae2]`）。
   - **最高优先级**使用 `browser_click(ref="aeN")`、`browser_fill_input(ref="aeN")`、`browser_hover(ref="aeN")`！
   - ref 直接对应当前 DOM 活节点，点击瞬时才由浏览器重新计算中心坐标，**天然免疫快照后的滚动、位移、CSS 动画及局部重排**，杜绝 140px 漂移与选择器语法报错。
   - 严禁跳过 ref 去手写脆弱模糊的选择器或用眼睛硬猜坐标。只有当 ref 不可用或多次失效时，才降级使用坐标或选择器。

2. **ref 生命周期分层（何时必须重新快照）**：
   - **全失效（必须重新 snapshot）**：`browser_navigate` 导航、点击了会引发全页跳转/路由切换的链接或按钮。此时旧页面的 ref 全部悬空作废，**必须**重新调用 `browser_snapshot`。
   - **局部失效（按需 snapshot）**：点击了触发复杂 AJAX 加载、局部弹出抽屉或刷新列表的按钮。如果后续要操作的元素位于变动的区域，重新调用 `browser_snapshot`。
   - **不失效（严禁中间插入快照，允许连续操作）**：`browser_fill_input` 填表、静态 `<select>` 选项选择、页面滚动、鼠标 hover。此期间所有现有 ref 依然有效，**不要**在每填一个输入框之间多余地插入 `browser_snapshot`，一气呵成连续填完表单！

3. **反模式警示（必读）**：
   - **反模式 A**：`click(ref="ae1") → wait → click(ref="ae1")`（不重新快照就再次点击相同 ref）：必失败！点击后若页面刷新，旧 ref 已悬空。
   - **反模式 B**：收到 `"[ref 失效]"` 提示后，依然原样重试旧 ref。收到该提示意味着 DOM 节点已被前端框架重新生成，**必须立即调用 `browser_snapshot` 取新 ref**。

4. **反 brute-force 元规则（高于一切重试策略）**：
   - 任何工具调用失败后，先看清失败原因：
     * 定位失败 / 选择器错 → 换定位方式（ref → find_text → probe_at → 坐标点击）；严禁微调同一选择器第三遍。
     * 状态不对（按钮置灰 / 表单缺必填）→ snapshot 重看表单补齐前置条件。
     * 人机验证 / 权限拦截 → 立即调 `browser_request_takeover` 申请人工介入，严禁盲试。
   - **铁律**：同一动作（相同参数/相同选择器/相同坐标）最多重试 1 次；第 2 次失败必须强制换路径，原样连续重试第 3 次视为重大故障！

5. **观察成本与批量验证**：
   - 默认观察手段是 `browser_snapshot`（文本树，成本极低、语义最全）。
   - 严禁用 `browser_screenshot` 估算坐标！截图仅用于复杂视觉信息核查（验证码、生图成果交付、布局畸变）。
   - 同一页面要核查 ≥2 个数据点（如同时检查用户名、登录态、提示信息），用一次 `browser_evaluate` 打包返回，严禁拆成多次往返。
   - 等待节奏：优先用 `browser_wait_for` 条件等待；无明确条件时采用 2 秒步长的短增量轮询（等 2 秒 → snapshot 核实 → 未就绪再等 2 秒，最多 5 轮），严禁无脑 sleep 15 秒假装等待。
