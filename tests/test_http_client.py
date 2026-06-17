from syncronizer.http.client import HttpClient


def test_api_key_sent_in_default_header():
    c = HttpClient(base_url="https://x", api_key="secret123")
    assert c.session.headers["X-API-Key"] == "secret123"
    c.close()


def test_api_key_custom_header_name():
    c = HttpClient(api_key="k", api_key_header="apikey")
    assert c.session.headers["apikey"] == "k"
    assert "X-API-Key" not in c.session.headers
    c.close()


def test_no_api_key_no_header():
    c = HttpClient()
    assert "X-API-Key" not in c.session.headers
    c.close()


def test_bearer_token_and_api_key_coexist():
    c = HttpClient(api_key="k", token="t")
    assert c.session.headers["X-API-Key"] == "k"
    assert c.session.headers["Authorization"] == "Bearer t"
    c.close()
