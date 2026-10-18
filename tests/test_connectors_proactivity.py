"""§28 Connectors + §23 Proactivity 测试。"""
import tempfile, os


def test_connector_dataclass():
    from agent.connectors import Connector
    c = Connector(name="t", client_id_ref="vault://x",
                  client_secret_ref="vault://y",
                  auth_url="https://a.com", token_url="https://t.com")
    assert c.name == "t" and c.scopes == []


def test_gmail_preset():
    from agent.connectors import PRESET_CONNECTORS
    g = PRESET_CONNECTORS["gmail"]
    assert "gmail.readonly" in g.scopes[0]
    assert "accounts.google.com" in g.auth_url


def test_auth_link_needs_vault():
    from agent.connectors import ConnectorManager
    m = ConnectorManager(vault=None)
    try:
        m.get_auth_link("gmail")
        assert False, "无 vault 应报错"
    except RuntimeError:
        pass


def test_auth_link_format():
    from agent.connectors import ConnectorManager

    class FakeVault:
        def resolve(self, ref, caller=None):
            return {"vault://gmail_client_id": "CID"}.get(ref, "X")
    m = ConnectorManager(vault=FakeVault())
    link = m.get_auth_link("gmail")
    assert "accounts.google.com" in link
    assert "client_id=CID" in link
    assert "response_type=code" in link


def test_proactivity_add_pending():
    from agent.proactivity import ProactivityStore
    p = tempfile.mktemp(suffix=".json")
    s = ProactivityStore(path=p)
    iid = s.add("阶跃限免", "阶跃又限免了", priority="high")
    assert iid.startswith("pi-")
    items = s.pending()
    assert len(items) == 1 and items[0].topic == "阶跃限免"


def test_proactivity_expired():
    from agent.proactivity import ProactivityStore
    import time
    p = tempfile.mktemp(suffix=".json")
    s = ProactivityStore(path=p)
    s.add("旧消息", "过期了", ttl_hours=-1)  # 已过期
    assert s.pending() == []


def test_proactivity_pick():
    from agent.proactivity import ProactivityStore, pick_for_user
    p = tempfile.mktemp(suffix=".json")
    s = ProactivityStore(path=p)
    s.add("阶跃星辰限免", "API 免费到月底", priority="high")
    s.add("天气", "今天晴", priority="low")
    # 相关话题 → 挑中 high 的
    picked = pick_for_user(s, "阶跃星辰最近有什么活动")
    assert picked is not None and picked.priority == "high"
    s.mark_delivered(picked.item_id)
    # 完全不相关 + 非 high → 不推
    picked2 = pick_for_user(s, "今天吃啥")
    assert picked2 is None
