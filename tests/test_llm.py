"""LLM 클라이언트 (Gemini via GMS) — 외부 호출은 가짜로 대체."""
from __future__ import annotations

import pytest

from app.agent import llm as llm_mod
from app.agent.llm import LLMClient, LLMError

OK_BODY = {"candidates": [{"content": {"parts": [{"text": '{"intent":"new_meet","reason":"r","locs":["잠실","건대"],"prio":"food"}'}], "role": "model"}}]}


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body if body is not None else OK_BODY

    def json(self):
        if isinstance(self._b, Exception):
            raise self._b
        return self._b


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_BASE_URL", "https://gms.example/gmsapi/generativelanguage.googleapis.com/v1beta/")
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-flash-lite")
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, json=json, headers=headers)
        return sent.get("resp") or Resp()
    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    return sent


def test_gemini_request_shape_key_in_header_not_url(gemini):
    a = LLMClient().analyze("잠실이랑 건대", {}, "2026-11-19", "목")
    assert gemini["url"] == "https://gms.example/gmsapi/generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
    assert gemini["headers"] == {"x-goog-api-key": "test-llm"} and "test-llm" not in gemini["url"]
    body = gemini["json"]
    assert body["systemInstruction"]["parts"][0]["text"] and body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["temperature"] == 0
    assert a.intent == "new_meet" and a.locs == ["잠실", "건대"] and a.prio == "food"


def test_gemini_compose_returns_plain_text(gemini):
    gemini["resp"] = Resp(body={"candidates": [{"content": {"parts": [{"text": "추천 "}, {"text": "문장"}]}}]})
    assert LLMClient().compose({"status": "ok"}) == "추천 문장"
    assert "responseMimeType" not in gemini["json"]["generationConfig"]


@pytest.mark.parametrize("status,code", [(401, "NO_KEY"), (403, "NO_KEY"), (429, "LLM_ERROR"), (500, "LLM_ERROR")])
def test_gemini_http_errors_map_to_llm_error(gemini, status, code):
    gemini["resp"] = Resp(status=status, body={"error": "secret details"})
    with pytest.raises(LLMError) as e:
        LLMClient().compose({})
    assert e.value.code == code and "secret" not in e.value.message


def test_gemini_empty_candidates_and_bad_json(gemini):
    gemini["resp"] = Resp(body={"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(LLMError):
        LLMClient().compose({})
    gemini["resp"] = Resp(body={"candidates": [{"content": {"parts": [{"text": "JSON 아님"}]}}]})
    with pytest.raises(LLMError) as e:
        LLMClient().analyze("x", {}, "2026-11-19", "목")
    assert e.value.code == "BAD_OUTPUT"


def test_gemini_without_key_makes_no_request(gemini, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    with pytest.raises(LLMError) as e:
        LLMClient().compose({})
    assert e.value.code == "NO_KEY" and "url" not in gemini


def test_invalid_analysis_fields_are_dropped_not_invented(gemini):
    gemini["resp"] = Resp(body={"candidates": [{"content": {"parts": [{"text":
        '{"intent":"new_meet","reason":"r","locs":["잠실"," "],"date":"내일","start":"2시","t_max":1}'}]}}]})
    a = LLMClient().analyze("x", {}, "2026-11-19", "목")
    assert a.date is None and a.start is None and a.t_max is None and a.locs == ["잠실"]


def test_tool_declarations_strip_unsupported_default():
    from app.agent.llm import gemini_tool_declarations
    from app.tools import openai_tools
    decls = gemini_tool_declarations(openai_tools())
    assert {d["name"] for d in decls} == {"geo_loc", "find_stn", "find_spot", "get_route", "get_wx"}
    assert "default" not in str(decls)
