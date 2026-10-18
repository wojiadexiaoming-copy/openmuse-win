"""真人贝塞尔曲线鼠标轨迹生成器与 Cloudflare Turnstile 物理穿透。

方案3 & 方案5 核心实现：
1. generate_human_curve: 基于三阶贝塞尔曲线 + Fitts's Law 速度曲线 + 微抖动（Micro-tremor）生成拟人鼠标轨迹；
2. human_mouse_move: 逐帧驱动 CDP 物理移动事件，满足反爬系统对鼠标运动熵（Mouse Entropy）的校验；
3. human_mouse_click: 拟人起停停顿、按键回弹保持（Dwell Time）与点击后微位移；
4. auto_solve_turnstile: 自动探测 Cloudflare Turnstile / 验证码 iframe，以真人轨迹拟合坐标穿透点击，尝试自动解盾。
"""
from __future__ import annotations

import logging
import math
import random
import time
from typing import Any

logger = logging.getLogger("alterego.browser.human_curve")


def generate_human_curve(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    steps: int | None = None,
) -> list[tuple[float, float, float]]:
    """生成平滑的真人贝塞尔曲线鼠标移动轨迹。
    
    返回: list of (x, y, delay_ms)
    """
    dx = end_x - start_x
    dy = end_y - start_y
    dist = math.hypot(dx, dy)

    if dist < 2.0:
        return [(float(end_x), float(end_y), 10.0)]

    if steps is None:
        steps = int(max(15, min(50, dist / 18.0)))

    # 计算三阶贝塞尔控制点 P1 与 P2
    # 模拟手腕/手臂生理弧度：产生垂直于弦向量的自然弧度偏置
    nx = -dy / dist
    ny = dx / dist
    arc_factor = random.uniform(-0.25, 0.25) * dist
    p0 = (start_x, start_y)
    p3 = (end_x, end_y)
    p1 = (
        start_x + 0.33 * dx + arc_factor * nx + random.uniform(-4.0, 4.0),
        start_y + 0.33 * dy + arc_factor * ny + random.uniform(-4.0, 4.0),
    )
    p2 = (
        start_x + 0.67 * dx + arc_factor * nx + random.uniform(-4.0, 4.0),
        start_y + 0.67 * dy + arc_factor * ny + random.uniform(-4.0, 4.0),
    )

    points: list[tuple[float, float, float]] = []

    # 40% 概率在目标点前产生 1~3 像素的微小过冲 (Overshoot)，随后在最后2步修正回目标
    has_overshoot = random.random() < 0.4 and dist > 40.0
    overshoot_step = steps - 3 if has_overshoot else -1
    overshoot_x = end_x + random.uniform(-2.5, 2.5)
    overshoot_y = end_y + random.uniform(-2.5, 2.5)

    for i in range(1, steps + 1):
        u = i / float(steps)
        # Fitts's Law 速度曲线：Smoothstep (u^2 * (3 - 2u)) 起步平缓、中段高速、接近目标平缓减速
        t = u * u * (3.0 - 2.0 * u)

        # 三阶贝塞尔计算
        cx = (
            (1.0 - t) ** 3 * p0[0]
            + 3.0 * (1.0 - t) ** 2 * t * p1[0]
            + 3.0 * (1.0 - t) * t**2 * p2[0]
            + t**3 * p3[0]
        )
        cy = (
            (1.0 - t) ** 3 * p0[1]
            + 3.0 * (1.0 - t) ** 2 * t * p1[1]
            + 3.0 * (1.0 - t) * t**2 * p2[1]
            + t**3 * p3[1]
        )

        # 衰减的微抖动（距离目标越近，手部控制越精准，抖动越小）
        jitter = (1.0 - t) * random.uniform(-1.0, 1.0)
        cx += jitter
        cy += jitter

        if i == overshoot_step:
            cx = overshoot_x
            cy = overshoot_y
        elif i == steps:
            cx = end_x
            cy = end_y

        # 中段帧间隔短（~6ms），两端起止帧间隔稍长（~14ms）
        delay = 6.0 + 8.0 * (1.0 - math.sin(t * math.pi)) + random.uniform(-1.0, 1.5)
        points.append((round(cx, 2), round(cy, 2), max(4.0, delay)))

    return points


def human_mouse_move(page: Any, target_x: float, target_y: float):
    """带生理熵值（Bezier + Fitts's Law）平滑移动鼠标到目标坐标。"""
    if not hasattr(page, "mouse") or not hasattr(page.mouse, "move"):
        return

    cur_pos = getattr(page, "_current_mouse_pos", None)
    if not cur_pos or not isinstance(cur_pos, (tuple, list)) or len(cur_pos) != 2:
        # 默认起点：页面自然视口偏上方或居中位置
        cur_pos = (random.uniform(150.0, 300.0), random.uniform(100.0, 200.0))

    start_x, start_y = cur_pos
    trajectory = generate_human_curve(start_x, start_y, target_x, target_y)

    for px, py, delay_ms in trajectory:
        try:
            page.mouse.move(px, py)
        except Exception:
            break
        time.sleep(delay_ms / 1000.0)

    page._current_mouse_pos = (target_x, target_y)


def human_mouse_click(
    page: Any,
    target_x: float,
    target_y: float,
    delay_ms: int | None = None,
):
    """真人模式移动并点击目标位置（平滑曲线 + 犹豫停顿 + 按键回弹 + 点击后微移）。"""
    if not hasattr(page, "mouse") or not hasattr(page.mouse, "click"):
        return

    # 1. 真人贝塞尔移动
    human_mouse_move(page, target_x, target_y)

    # 2. 点击前生理视力锁定犹豫（60ms ~ 160ms）
    time.sleep(random.uniform(0.06, 0.16))

    # 3. 物理按键下压与保持 (Dwell Time: 50ms ~ 110ms)
    hold = (delay_ms / 1000.0) if delay_ms else random.uniform(0.05, 0.11)
    hold_ms = int(hold * 1000)
    try:
        page.mouse.click(target_x, target_y, delay=hold_ms)
    except TypeError:
        # Mock 或旧版本不支持 delay 参数
        try:
            page.mouse.click(target_x, target_y)
        except Exception:
            pass
    except Exception:
        try:
            if hasattr(page.mouse, "down") and hasattr(page.mouse, "up"):
                page.mouse.down()
                time.sleep(hold)
                page.mouse.up()
            else:
                page.mouse.click(target_x, target_y)
        except Exception:
            pass

    # 4. 点击后真人鼠标绝非死寂僵住，有微小惯性偏离（1~3px）
    drift_x = target_x + random.uniform(-2.5, 2.5)
    drift_y = target_y + random.uniform(-2.5, 2.5)
    try:
        page.mouse.move(drift_x, drift_y)
        page._current_mouse_pos = (drift_x, drift_y)
    except Exception:
        pass


def auto_solve_turnstile(page: Any, timeout_sec: float = 10.0) -> bool:
    """自动检测并穿透 Cloudflare Turnstile 验证码。
    
    检测页面是否存在 Cloudflare Turnstile 挑战组件，定位复选框绝对屏幕坐标，
    以真人轨迹与物理事件下发点击，并轮询等待验证自动通过。
    成功通过返回 True，未出现或超时未通过返回 False。
    """
    if not page or not hasattr(page, "frames"):
        return False

    # 1. 快速排查是否存在 Cloudflare 挑战框架或 DOM 标记
    has_cf = False
    try:
        frames = getattr(page, "frames", []) or []
        for f in frames:
            url = getattr(f, "url", "") or ""
            if "challenges.cloudflare.com" in url or "turnstile" in url:
                has_cf = True
                break
    except Exception:
        has_cf = False

    if not has_cf:
        try:
            cf_el = page.query_selector(
                'iframe[src*="challenges.cloudflare.com"], '
                'iframe[src*="turnstile"], '
                '.cf-turnstile, '
                '[id*="turnstile"], '
                '[id*="cf-chl"]'
            )
            has_cf = cf_el is not None
        except Exception:
            has_cf = False

    if not has_cf:
        return False

    logger.info("[Turnstile] 检测到页面存在 Cloudflare 安全验证组件，正在尝试拟人自动穿透...")

    # 2. 定位 Turnstile iframe 元素在主视口中的绝对物理包围盒
    iframe_box = None
    try:
        iframe_el = page.query_selector(
            'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]'
        )
        if iframe_el:
            iframe_box = iframe_el.bounding_box()
    except Exception as e:
        logger.debug("[Turnstile] 查询 iframe 包围盒异常: %s", e)

    if not iframe_box or iframe_box.get("width", 0) < 10 or iframe_box.get("height", 0) < 10:
        logger.debug("[Turnstile] 未能获取有效的 iframe 几何尺寸: %s", iframe_box)
        return False

    # Cloudflare Turnstile 标配控件：
    # 尺寸约 300x65，复选框位于左侧约 28~36 像素，垂直居中
    cb_x = iframe_box["x"] + min(32.0, iframe_box["width"] * 0.15)
    cb_y = iframe_box["y"] + iframe_box["height"] / 2.0

    # 3. 拟人贝塞尔轨迹划向复选框并完成物理点击
    logger.info("[Turnstile] 正在以拟人贝塞尔轨迹点击验证码复选框: (%.1f, %.1f)", cb_x, cb_y)
    try:
        human_mouse_click(page, cb_x, cb_y)
    except Exception as e:
        logger.warning("[Turnstile] 模拟点击发生异常: %s", e)
        return False

    # 4. 轮询等待验证通过（检测凭证生成、iframe 消失或页面标题/URL 变化）
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(0.6)
        try:
            # 判据 A: 页面标题不再是“请稍后”或“Just a moment...”
            title = (getattr(page, "title", lambda: "")() or "").strip()
            if title and not any(k in title for k in ("稍后", "Just a moment", "Security check")):
                logger.info("[Turnstile] 页面标题已刷新为正常内容: %s，验证已通过！", title)
                return True

            # 判据 B: cf-turnstile-response 隐藏 input 已填充有效 token
            token_val = page.evaluate("""() => {
                const el = document.querySelector('[name="cf-turnstile-response"]');
                return el ? (el.value || '') : '';
            }""")
            if token_val and len(token_val) > 20:
                logger.info("[Turnstile] 成功提取到 cf-turnstile-response 挑战签名令牌，验证已通过！")
                return True

            # 判据 C: 页面上不再存在 Cloudflare 挑战 iframe
            still_has_cf = False
            for f in getattr(page, "frames", []):
                if "challenges.cloudflare.com" in (getattr(f, "url", "") or ""):
                    still_has_cf = True
                    break
            if not still_has_cf:
                logger.info("[Turnstile] Cloudflare 挑战子框架已从 DOM 中解挂，验证已通过！")
                return True
        except Exception:
            pass

    logger.info("[Turnstile] 在 %.1f 秒内未自动完成，可能需要手动交互或图片拼图", timeout_sec)
    return False
