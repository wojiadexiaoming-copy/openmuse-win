import pytest
from tools.browser.worker import BrowserWorker


def test_sec05_cloud_metadata_blocked():
    worker = BrowserWorker.__new__(BrowserWorker)
    worker.allowed_domains = None
    r = worker._check_url_security("http://169.254.169.254/latest/meta-data")
    assert r is not None
    assert "域名不在白名单" in r or "169.254" in r


def test_sec05_cdp_port_blocked():
    worker = BrowserWorker.__new__(BrowserWorker)
    worker.allowed_domains = None
    r = worker._check_url_security("http://localhost:9222/json")
    assert r is not None
    assert "9222" in r


def test_sec05_sensitive_file_blocked():
    worker = BrowserWorker.__new__(BrowserWorker)
    worker.allowed_domains = None
    r = worker._check_url_security("file:///etc/passwd")
    assert r is not None
    assert "敏感路径" in r

    r2 = worker._check_url_security("file:///C:/Users/Administrator/.ssh/id_rsa")
    assert r2 is not None
    assert "敏感路径" in r2


def test_sec05_allowed_domains_whitelist():
    worker = BrowserWorker.__new__(BrowserWorker)
    worker.allowed_domains = ["baidu.com", "bing.com"]

    assert worker._check_url_security("https://www.baidu.com/s?wd=test") is None
    assert worker._check_url_security("https://bing.com") is None

    evil = worker._check_url_security("https://evil-attacker.com")
    assert evil is not None
    assert "域名不在白名单" in evil
