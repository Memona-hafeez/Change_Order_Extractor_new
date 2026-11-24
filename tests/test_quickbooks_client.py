import pytest
from qb_auth import (QBAuthError, TokenStore, load_config, parse_callback, refresh_tokens, connect)
from quickbooks_client import QuickBooksClient, QuickBooksError


class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._d, self.text = code, data or {}, str(data)

    def json(self):
        return self._d


def make(tmp_path, responses):
    store = TokenStore(tmp_path / "t.json")
    store.save({"access_token": "old", "refresh_token": "r1", "realm_id": "123"})
    calls, sleeps = [], []

    def http(method, url, **kw):
        calls.append((method, url, kw))
        return responses.pop(0)

    def refresher():
        t = store.load()
        t["access_token"] = "new"
        store.save(t)

    c = QuickBooksClient(store, refresher, http=http, sleep=sleeps.append)
    return c, calls, sleeps, store


def test_bearer_url_and_minorversion(tmp_path):
    c, calls, _, _ = make(tmp_path, [Resp(200, {"QueryResponse": {"Customer": [{"Id": "1"}]}})])
    assert c.query_list("select 1", "Customer") == [{"Id": "1"}]
    method, url, kw = calls[0]
    assert url == "https://sandbox-quickbooks.api.intuit.com/v3/company/123/query"
    assert kw["headers"]["Authorization"] == "Bearer old" and kw["params"]["minorversion"] == "75"


def test_empty_query_result(tmp_path):
    c, *_ = make(tmp_path, [Resp(200, {"QueryResponse": {}})])
    assert c.query_list("q", "Customer") == []


def test_401_refreshes_once_then_retries(tmp_path):
    c, calls, _, _ = make(tmp_path, [Resp(401), Resp(200, {"QueryResponse": {}})])
    c.query_list("q", "X")
    assert calls[1][2]["headers"]["Authorization"] == "Bearer new"


def test_second_401_raises(tmp_path):
    c, *_ = make(tmp_path, [Resp(401), Resp(401, {"Fault": {"Error": [{"Message": "AuthFailure", "code": "3200"}]}})])
    with pytest.raises(QuickBooksError) as e:
        c.query_list("q", "X")
    assert e.value.status == 401 and "AuthFailure" in str(e.value)


def test_429_backs_off(tmp_path):
    c, calls, sleeps, _ = make(tmp_path, [Resp(429), Resp(429), Resp(200, {"QueryResponse": {}})])
    c.query_list("q", "X")
    assert sleeps == [1, 2] and len(calls) == 3


def test_post_500_is_not_retried(tmp_path):
    c, calls, _, _ = make(tmp_path, [Resp(500, {"Fault": {"Error": [{"Message": "boom"}]}})])
    with pytest.raises(QuickBooksError):
        c.create("Estimate", {"a": 1})
    assert len(calls) == 1


def test_get_500_is_retried(tmp_path):
    c, calls, _, _ = make(tmp_path, [Resp(503), Resp(200, {"QueryResponse": {}})])
    c.query_list("q", "X")
    assert len(calls) == 2


def test_create_returns_entity_and_posts_json(tmp_path):
    c, calls, *_ = make(tmp_path, [Resp(200, {"Estimate": {"Id": "55"}})])
    assert c.create("Estimate", {"x": 1}) == {"Id": "55"}
    assert calls[0][0] == "POST" and calls[0][1].endswith("/estimate") and calls[0][2]["json"] == {"x": 1}


def test_not_connected(tmp_path):
    c = QuickBooksClient(TokenStore(tmp_path / "none.json"), lambda: None, http=lambda *a, **k: None)
    with pytest.raises(QBAuthError):
        c.query_list("q", "X")


# --- auth ---
class FakeAuth:
    def __init__(self, cfg):
        self.access_token, self.refresh_token, self.realm_id = None, None, None

    def refresh(self, refresh_token):
        assert refresh_token == "r1"
        self.access_token, self.refresh_token = "A2", "r2"

    def get_authorization_url(self, scopes, state_token=None):
        self.state = state_token
        return "https://intuit.example/auth"

    def get_bearer_token(self, code, realm_id=None):
        assert code == "CODE"
        self.access_token, self.refresh_token = "A1", "R1"


CFG = {"client_id": "i", "client_secret": "s", "redirect_uri": "http://localhost:8000/callback",
       "environment": "sandbox", "minorversion": "75"}


def test_refresh_saves_rotated_token(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    store.save({"access_token": "A0", "refresh_token": "r1", "realm_id": "123"})
    t = refresh_tokens(store, CFG, FakeAuth)
    assert t["access_token"] == "A2" and t["refresh_token"] == "r2" and t["realm_id"] == "123"
    assert store.load()["refresh_token"] == "r2"


def test_refresh_failure_message(tmp_path):
    class Bad(FakeAuth):
        def refresh(self, refresh_token):
            raise RuntimeError("invalid_grant")
    store = TokenStore(tmp_path / "t.json")
    store.save({"refresh_token": "r1"})
    with pytest.raises(QBAuthError, match="connect"):
        refresh_tokens(store, CFG, Bad)


def test_parse_callback():
    url = "http://localhost:8000/callback?code=CODE&state=S&realmId=999"
    assert parse_callback(url, "S") == ("CODE", "999")
    with pytest.raises(QBAuthError):
        parse_callback(url, "OTHER")
    with pytest.raises(QBAuthError):
        parse_callback("/callback?error=access_denied&state=S", "S")
    with pytest.raises(QBAuthError):
        parse_callback("/callback?state=S", "S")


def test_connect_saves_tokens(tmp_path):
    store = TokenStore(tmp_path / "t.json")
    holder = {}

    def factory(cfg):
        holder["c"] = FakeAuth(cfg)
        return holder["c"]

    def wait(_uri):
        return f"/callback?code=CODE&realmId=777&state={holder['c'].state}"

    connect(CFG, store, factory, open_browser=False, scopes=["x"], wait_fn=wait)
    t = store.load()
    assert t["realm_id"] == "777" and t["refresh_token"] == "R1" and t["environment"] == "sandbox"


def test_load_config():
    env = {"QB_CLIENT_ID": "a", "QB_CLIENT_SECRET": "b", "QB_REDIRECT_URI": "http://localhost:8000/callback"}
    assert load_config(env)["environment"] == "sandbox"
    with pytest.raises(QBAuthError):
        load_config({})
    with pytest.raises(QBAuthError):
        load_config({**env, "QB_ENVIRONMENT": "prod"})
