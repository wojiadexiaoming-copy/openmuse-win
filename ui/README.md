# ui/ 说明

- `index.html` / `takeover.html` / `static/`：完整 H5 客户端（按 spec §31 定稿，正常三栏 + 接管小浮窗），接口定义见 `api.md`。
- `launcher.py`：pywebview 桌面壳，自动拉起多线程 REST/SSE 服务端与原生桌面客户端窗口。
- `server.py`：基于 Bottle + WSGIServer 的 REST/SSE 双向通信后端，支持会话、聊天流式响应、审批流决策、截屏回显与任务跟踪。
- `takeover_window.py`：独立子进程置顶小浮窗，支持实时倒计时、快捷键及接管事件通知。

启动诊断：双击项目根目录的 run_ui.bat。控制台会即时显示当前初始化阶段；日志保存在数据目录 logs/startup-日期时间-PID.log（默认项目根目录的 logs）。页面加载前每30秒记录线程堆栈；页面加载后停止取证。端口占用、HTTP未就绪及桌面内核异常会明确显示失败阶段。若卡在 Starting launcher 且没有 bootstrap，说明尚未进入 Python 启动器。
