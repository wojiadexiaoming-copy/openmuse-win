"""P0-2 测试：localhost API 鉴权与 CORS 收紧。

测试指标（patch-localhost-auth.md）：
1. 无 token 请求 /api/sessions -> 401
2. 错误 token -> 401
3. 正确 token (Bearer / X-AlterEgo-Token / ?token=) -> 200
4. Origin: https://evil.com 预检 -> 不返回 Access-Control-Allow-Origin
5. /api/health 无 token -> 200（且不泄露敏感信息）
"""
import io
import json
import pytest

from ui.server import AgentServer


@pytest.fixture
def test_server(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTEREGO_LLM_API_KEY", "sk-mock-auth-key")
    server = AgentServer(data_dir=tmp_path)
    return server


def _call_api(app, method="GET", path="/", headers=None, body=None):
    raw_body = json.dumps(body).encode("utf-8") if body else b""
    status_box = []
    resp_headers = []

    def start_response(status, response_headers, exc_info=None):
        status_box.append(status)
        resp_headers.extend(response_headers)

    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "wsgi.input": io.BytesIO(raw_body),
        "wsgi.errors": io.StringIO(),
        "CONTENT_LENGTH": str(len(raw_body)),
        "CONTENT_TYPE": "application/json",
    }
    if "?" in path:
        p, q = path.split("?", 1)
        env["PATH_INFO"] = p
        env["QUERY_STRING"] = q

    if headers:
        for k, v in headers.items():
            env["HTTP_" + k.upper().replace("-", "_")] = v

    resp_data = b"".join(app(env, start_response))
    status_str = status_box[0] if status_box else "500 Internal Error"
    headers_dict = {k: v for k, v in resp_headers}
    return status_str, headers_dict, resp_data.decode("utf-8")


def test_auth_no_token(test_server):
    status, headers, body = _call_api(test_server.app, "GET", "/api/sessions")
    assert status.startswith("401"), f"Expected 401, got {status}"
    data = json.loads(body)
    assert data.get("error") == "unauthorized"


def test_auth_bad_token(test_server):
    headers = {"Authorization": "Bearer bad-token-xyz"}
    status, _, body = _call_api(test_server.app, "GET", "/api/sessions", headers=headers)
    assert status.startswith("401")
    data = json.loads(body)
    assert data.get("error") == "unauthorized"


def test_auth_valid_token_bearer(test_server):
    headers = {"Authorization": f"Bearer {test_server.api_token}"}
    status, _, body = _call_api(test_server.app, "GET", "/api/sessions", headers=headers)
    assert status.startswith("200")
    data = json.loads(body)
    assert isinstance(data, list)


def test_auth_valid_token_custom_header(test_server):
    headers = {"X-AlterEgo-Token": test_server.api_token}
    status, _, body = _call_api(test_server.app, "GET", "/api/sessions", headers=headers)
    assert status.startswith("200")


def test_auth_valid_token_query_param(test_server):
    status, _, body = _call_api(test_server.app, "GET", f"/api/sessions?token={test_server.api_token}")
    assert status.startswith("200")


def test_cors_evil_origin_preflight(test_server):
    headers = {
        "Origin": "https://evil.com",
        "Access-Control-Request-Method": "POST",
    }
    status, resp_headers, _ = _call_api(test_server.app, "OPTIONS", "/api/chat", headers=headers)
    assert "Access-Control-Allow-Origin" not in resp_headers


def test_cors_allowed_origin(test_server):
    headers = {
        "Origin": "http://127.0.0.1:5000",
        "Authorization": f"Bearer {test_server.api_token}",
    }
    status, resp_headers, _ = _call_api(test_server.app, "GET", "/api/sessions", headers=headers)
    assert status.startswith("200")
    assert resp_headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:5000"


def test_health_no_token(test_server):
    status, _, body = _call_api(test_server.app, "GET", "/api/health")
    assert status.startswith("200")
    data = json.loads(body)
    assert data.get("status") == "ok"
    assert test_server.api_token not in body
