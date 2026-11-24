"""Small QuickBooks Online API client: Bearer auth, token refresh on 401, backoff on 429/5xx.

    python quickbooks_client.py hello      # prints the connected company's name: the first thing to get working

Design notes
  * 401 -> refresh the token once and retry the same request.
  * 429 -> wait and retry. 5xx -> retry only for reads (GET). A POST that failed with a 5xx may still have been
    applied, so it is NOT retried blindly; the caller checks for an existing document instead.
  * `http` is injectable so tests run without a network.
"""
import sys
import time

from qb_auth import (QBAuthError, TokenStore, load_config, refresh_tokens)

BASES = {"sandbox": "https://sandbox-quickbooks.api.intuit.com",
         "production": "https://quickbooks.api.intuit.com"}


class QuickBooksError(Exception):
    def __init__(self, status, message):
        super().__init__(f"QuickBooks API error {status}: {message}")
        self.status = status
        self.message = message


def fault_message(resp):
    """Pull a readable message out of QuickBooks' {"Fault": {"Error": [...]}} error body."""
    try:
        err = resp.json()["Fault"]["Error"][0]
        parts = [err.get("Message"), err.get("Detail")]
        return ": ".join(p for p in parts if p) + (f" (code {err['code']})" if err.get("code") else "")
    except Exception:
        return (getattr(resp, "text", "") or "")[:300] or "no details"


class QuickBooksClient:
    def __init__(self, store, refresher, environment="sandbox", minorversion="75", http=None,
                 sleep=time.sleep, max_retries=3):
        if environment not in BASES:
            raise ValueError(f"environment must be one of {list(BASES)}")
        self.store, self.refresher = store, refresher
        self.environment, self.minorversion = environment, str(minorversion)
        self.sleep, self.max_retries = sleep, max_retries
        if http is None:
            import requests
            http = requests.request
        self.http = http

    def _tokens(self):
        t = self.store.load()
        if not t or not t.get("access_token") or not t.get("realm_id"):
            raise QBAuthError("not connected yet: run  python qb_auth.py connect")
        return t

    def request(self, method, path, params=None, body=None):
        refreshed, attempt = False, 0
        while True:
            t = self._tokens()
            url = f"{BASES[self.environment]}/v3/company/{t['realm_id']}{path}"
            headers = {"Authorization": f"Bearer {t['access_token']}", "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            resp = self.http(method, url, headers=headers, params={**(params or {}), "minorversion": self.minorversion},
                             json=body, timeout=30)
            code = resp.status_code
            if code == 401 and not refreshed:
                self.refresher()
                refreshed = True
                continue
            retryable = code == 429 or (code >= 500 and method.upper() == "GET")
            if retryable and attempt < self.max_retries:
                self.sleep(2 ** attempt)
                attempt += 1
                continue
            if code >= 400:
                raise QuickBooksError(code, fault_message(resp))
            return resp.json()

    def query_list(self, sql, entity):
        """Run a QuickBooks query and return the list of `entity` rows (empty list when nothing matches)."""
        data = self.request("GET", "/query", params={"query": sql})
        return data.get("QueryResponse", {}).get(entity, [])

    def create(self, entity, body):
        """POST a new entity (for example 'Estimate') and return the created object."""
        data = self.request("POST", f"/{entity.lower()}", body=body)
        return data[entity]

    def company_name(self):
        realm = self._tokens()["realm_id"]
        return self.request("GET", f"/companyinfo/{realm}")["CompanyInfo"]["CompanyName"]


def build_client(cfg=None):
    cfg = cfg or load_config()
    store = TokenStore()
    return QuickBooksClient(store, lambda: refresh_tokens(store, cfg), environment=cfg["environment"],
                            minorversion=cfg["minorversion"])


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "hello":
            print("Connected company:", build_client().company_name())
        else:
            print(__doc__)
    except (QBAuthError, QuickBooksError) as e:
        sys.exit(f"ERROR: {e}")
