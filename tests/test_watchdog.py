"""M3 测试：单实例锁与看门狗心跳（spec §18.6）。"""
import time
from agent.singleton import SingleInstanceLock
from agent.watchdog import HeartbeatSender, WatchdogChecker


def test_single_instance_lock(tmp_path):
    lock_file = tmp_path / "app.lock"
    l1 = SingleInstanceLock(lock_file)
    assert l1.acquire() is True

    # 第二个实例尝试获取同一个锁文件应该失败
    l2 = SingleInstanceLock(lock_file)
    assert l2.acquire() is False

    # 释放后可以再次获取
    l1.release()
    assert l2.acquire() is True
    l2.release()


def test_watchdog_heartbeat(tmp_path):
    hb_file = tmp_path / "heartbeat.txt"
    sender = HeartbeatSender(hb_file, interval_sec=0.2)
    checker = WatchdogChecker(hb_file, timeout_sec=1.0)

    assert checker.is_alive() is False

    sender.start()
    time.sleep(0.1)
    assert checker.is_alive() is True

    # 停止后删除心跳
    sender.stop()
    assert checker.is_alive() is False
