"""LLM 클라이언트 (GPT, OpenAI 호환 Chat Completions) — 외부 호출은 가짜 ChatOpenAI 로 대체."""
from __future__ import annotations

import pytest

from app.agent.llm import LLMClient, LLMError

ANALYSIS = '{"intent":"new_meet","reason":"r","locs":["잠실","건대"],"prio":"food"}'


class Msg:
    def __init__(self, content):
        self.content = content


class FakeChat:
    inits: list[dict] = []
    calls: list[dict] = []
    reply = ANALYSIS
    error: Exception | None = None

    def __init__(self, **kw):
        FakeChat.inits.append(kw)
        self._bound: dict = {}

    def bind(self, **kw):
        b = FakeChat.__new__(FakeChat)
        b._bound = kw
        return b

    def invoke(self, messages):
        FakeChat.calls.append({"messages": messages, "bound": self._bound})
        if FakeChat.error:
            raise FakeChat.error
        return Msg(FakeChat.reply)


@pytest.fixture
def gpt(monkeypatch):
    import langchain_openai
    FakeChat.inits, FakeChat.calls, FakeChat.reply, FakeChat.error = [], [], ANALYSIS, None
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChat)
    monkeypatch.setenv("LLM_BASE_URL", "https://gms.example/gmsapi/api.openai.com/v1")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    return FakeChat


def test_request_uses_configured_model_endpoint_and_json_mode(gpt):
    a = LLMClient().analyze("잠실이랑 건대", {}, "2026-11-19", "목")
    init = gpt.inits[0]
    assert init["model"] == "gpt-4o-mini" and init["base_url"] == "https://gms.example/gmsapi/api.openai.com/v1"
    assert init["api_key"] == "test-llm" and init["temperature"] == 0
    call = gpt.calls[0]
    assert call["bound"] == {"response_format": {"type": "json_object"}}
    assert call["messages"][0][0] == "system" and call["messages"][1][0] == "human"
    assert a.intent == "new_meet" and a.locs == ["잠실", "건대"] and a.prio == "food"


def test_compose_returns_plain_text_without_json_mode(gpt):
    gpt.reply = "  추천 문장  "
    assert LLMClient().compose({"status": "ok"}) == "추천 문장"
    assert gpt.calls[0]["bound"] == {}


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__("secret details")
        self.status_code = status


@pytest.mark.parametrize("status,code", [(401, "NO_KEY"), (403, "NO_KEY"), (429, "LLM_ERROR"), (500, "LLM_ERROR")])
def test_http_errors_map_to_llm_error_without_details(gpt, status, code):
    gpt.error = _HttpError(status)
    with pytest.raises(LLMError) as e:
        LLMClient().compose({})
    assert e.value.code == code and "secret" not in e.value.message


def test_bad_json_is_bad_output(gpt):
    gpt.reply = "JSON 아님"
    with pytest.raises(LLMError) as e:
        LLMClient().analyze("x", {}, "2026-11-19", "목")
    assert e.value.code == "BAD_OUTPUT"


def test_without_key_makes_no_request(gpt, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    with pytest.raises(LLMError) as e:
        LLMClient().compose({})
    assert e.value.code == "NO_KEY" and not gpt.inits and not gpt.calls


def test_empty_base_url_and_model_fall_back_to_defaults(gpt, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    LLMClient().compose({})
    assert gpt.inits[0]["base_url"] == "https://api.openai.com/v1" and gpt.inits[0]["model"] == "gpt-4o-mini"


def test_invalid_analysis_fields_are_dropped_not_invented(gpt):
    gpt.reply = '{"intent":"new_meet","reason":"r","locs":["잠실"," "],"date":"내일","start":"2시","t_max":1}'
    a = LLMClient().analyze("x", {}, "2026-11-19", "목")
    assert a.date is None and a.start is None and a.t_max is None and a.locs == ["잠실"]


def test_failure_is_logged_without_key_and_not_exposed(gpt, caplog):
    gpt.error = _HttpError(429)
    with caplog.at_level("WARNING"):
        with pytest.raises(LLMError) as e:
            LLMClient().compose({})
    assert "status=429" in caplog.text and "gpt-4o-mini" in caplog.text and "test-llm" not in caplog.text
    assert "secret" not in e.value.message


@pytest.mark.parametrize("raw", [
    "https://gms.ssafy.io/gmsapi/api.openai.com/v1/chat/completions",
    "https://gms.ssafy.io/gmsapi/api.openai.com/v1/chat/completions/",
    "https://gms.ssafy.io/gmsapi/api.openai.com/v1/",
    "https://gms.ssafy.io/gmsapi/api.openai.com/v1",
])
def test_base_url_is_normalized_to_v1(gpt, monkeypatch, raw):
    monkeypatch.setenv("LLM_BASE_URL", raw)
    LLMClient().compose({})
    assert gpt.inits[0]["base_url"] == "https://gms.ssafy.io/gmsapi/api.openai.com/v1"


def test_analysis_prompt_includes_calendar_for_weekday_lookup(gpt):
    LLMClient().analyze("토요일 2시에 잠실이랑 건대", {}, "2026-11-20", "금")
    user = gpt.calls[0]["messages"][1][1]
    assert "2026-11-20 (금)" in user and "2026-11-21 (토)" in user and "2026-11-28 (토)" in user
    assert "2026-12-04" not in user  # 14일까지만
