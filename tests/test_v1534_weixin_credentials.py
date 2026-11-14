"""v1.5.34 微信凭证持久化回归。

根因：save_credentials 的 legacy 清理把 data_dir.parent/"weixin" 当冗余路径，
在生产布局 data_dir=<root>/data/weixin 下它恰好就是规范文件本身，写完即自删。
本文件守住：
1. 生产布局下保存后文件仍在、可回读、可跨实例重载（P0）；
2. 落盘失败时保留历史凭证，不清空最后的可恢复副本（P0）；
3. 真实历史路径 <root>/weixin/ 能被加载（P1）；
4. 状态区分「无凭证 / 已过期 / 临期」（P1/P2）；
5. TTL 与临期阈值可配置，非法值回退默认（P2）。
"""
import json
import time

import pytest

import channels.weixin as wx
from channels.weixin import (
    WEIXIN_TOKEN_TTL_SEC,
    WEIXIN_TOKEN_WARN_SEC,
    WeixinChannel,
)


@pytest.fixture(autouse=True)
def no_background_network(monkeypatch):
    monkeypatch.setattr(WeixinChannel, "start_message_loop", lambda self: None)


def _creds(**over):
    data = {
        "bot_token": "token-prod",
        "ilink_bot_id": "bot-prod",
        "ilink_user_id": "user-prod",
        "baseurl": "https://ilinkai.weixin.qq.com",
    }
    data.update(over)
    return data


def _production_layout(tmp_path):
    root = tmp_path / "repo"
    data_dir = root / "data" / "weixin"
    data_dir.mkdir(parents=True)
    return root, data_dir


def test_save_survives_production_layout_and_cleans_real_legacy(tmp_path):
    root, data_dir = _production_layout(tmp_path)
    legacy_dir = root / "weixin"
    legacy_dir.mkdir()
    legacy_root = legacy_dir / "weixin_credentials.json"
    legacy_root.write_text(json.dumps(_creds(bot_token="old-token")), encoding="utf-8")
    legacy_top = root / "weixin_credentials.json"
    legacy_top.write_text(json.dumps(_creds(bot_token="older-token")), encoding="utf-8")

    ch = WeixinChannel(data_dir=data_dir)
    canonical = data_dir / "weixin_credentials.json"
    assert not canonical.exists()

    assert ch.save_credentials(_creds()) is True
    assert canonical.exists(), "生产布局下 save_credentials 不得删掉规范文件"
    assert json.loads(canonical.read_text(encoding="utf-8"))["bot_token"] == "token-prod"
    assert not legacy_root.exists(), "真实历史路径应在落盘校验通过后清理"
    assert not legacy_top.exists()
    assert ch.credential_source == str(canonical)

    # 模拟长轮询反复保存：旧实现在第二次 save 就会把文件删掉
    for _ in range(3):
        assert ch.save_credentials(dict(ch.credentials)) is True
        assert canonical.exists()

    ch2 = WeixinChannel(data_dir=data_dir)
    assert ch2.is_connected()
    assert ch2.credentials["bot_token"] == "token-prod"
    assert ch2.get_status()["credential_file_exists"] is True


def test_failed_save_keeps_legacy_credentials(tmp_path, monkeypatch):
    root, data_dir = _production_layout(tmp_path)
    legacy = root / "weixin" / "weixin_credentials.json"
    legacy.parent.mkdir()
    legacy.write_text(
        json.dumps(_creds(bot_token="legacy-token", connected_at=time.time())),
        encoding="utf-8",
    )

    ch = WeixinChannel(data_dir=data_dir)
    assert ch.is_connected()

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(wx.json, "dump", _boom)
    assert ch.save_credentials(_creds()) is False
    assert ch.last_save_error
    assert legacy.exists(), "写入失败时不得清理历史凭证"
    assert not (data_dir / "weixin_credentials.json").exists()


def test_loads_credential_from_real_legacy_root(tmp_path):
    root, data_dir = _production_layout(tmp_path)
    legacy = root / "weixin" / "weixin_credentials.json"
    legacy.parent.mkdir()
    legacy.write_text(
        json.dumps(_creds(bot_token="legacy-token", connected_at=time.time())),
        encoding="utf-8",
    )

    ch = WeixinChannel(data_dir=data_dir)
    assert ch.is_connected()
    assert ch.credentials["bot_token"] == "legacy-token"
    assert ch.credential_source == str(legacy)
    assert not (data_dir / "weixin_credentials.json").exists(), "读取历史凭证不应静默搬家"


def test_status_distinguishes_missing_and_expired(tmp_path):
    ch = WeixinChannel(data_dir=tmp_path / "data" / "weixin")
    st = ch.get_status()
    assert st["credential_missing"] is True
    assert st["credential_file_exists"] is False
    assert st["token_expired"] is False
    assert st["expires_at"] is None

    ch.save_credentials(
        _creds(connected_at=time.time() - WEIXIN_TOKEN_TTL_SEC - 3600))
    st = ch.get_status()
    assert st["credential_missing"] is False
    assert st["credential_file_exists"] is True
    assert st["token_expired"] is True
    assert st["expires_soon"] is False
    assert st["remaining_hours"] == 0
    assert st["token_ttl_hours"] == 72


def test_configurable_ttl_and_expiry_warning(tmp_path):
    data_dir = tmp_path / "data" / "weixin"
    ch = WeixinChannel(data_dir=data_dir, token_ttl_sec=10 * 3600, token_warn_sec=2 * 3600)
    ch.save_credentials(_creds(connected_at=time.time() - 9 * 3600))
    st = ch.get_status()
    assert st["connected"] is True
    assert st["expires_soon"] is True
    assert st["token_ttl_hours"] == 10
    assert st["token_warn_hours"] == 2
    assert 0.5 < st["remaining_hours"] <= 1.1

    ch2 = WeixinChannel(data_dir=data_dir, token_ttl_sec=10 * 3600)
    ch2.credentials = dict(ch.credentials, connected_at=time.time() - 11 * 3600)
    assert ch2.is_connected() is False
    assert ch2.get_status()["token_expired"] is True


def test_invalid_ttl_config_falls_back_to_default(tmp_path):
    ch = WeixinChannel(data_dir=tmp_path, token_ttl_sec=-5, token_warn_sec="abc")
    assert ch.token_ttl_sec == WEIXIN_TOKEN_TTL_SEC
    assert ch.token_warn_sec == WEIXIN_TOKEN_WARN_SEC

    ch2 = WeixinChannel(data_dir=tmp_path, token_ttl_sec=1 * 3600, token_warn_sec=5 * 3600)
    assert ch2.token_warn_sec == 1 * 3600, "临期阈值不得超过有效期"
