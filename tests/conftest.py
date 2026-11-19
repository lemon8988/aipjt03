"""테스트 공통 설정.

주의: 아래 FakeWorld 의 카카오 응답은 공개 문서 기준으로 '가정한' 형식이다.
실제 응답과의 일치는 scripts/check_apis.py 로 확인해야 한다 (기획 검증).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.agent.llm import Analysis
from app.container import Container, set_container
from app.tools import base, http
from app.utils.geo import now_kst

BASE = "https://dapi.kakao.com"


def doc(id_, name, x, y, distance=0, addr="서울 어딘가"):
    return {"id": id_, "place_name": name, "address_name": addr, "road_address_name": "", "x": str(x),
            "y": str(y), "distance": str(distance), "place_url": f"http://place.map.kakao.com/{id_}",
            "category_name": ""}


class FakeWorld:
    """카카오 로컬·Open-Meteo 를 흉내 내는 가짜 외부 세계."""

    def __init__(self):
        self.rain = 10
        self.keyword = {
            "잠실역": [doc("j1", "잠실역 2호선", 127.1001, 37.5133), doc("j2", "잠실역 8호선", 127.1003, 37.5135)],
            "건대역": [doc("g1", "건대입구역 2호선", 127.0703, 37.5404), doc("g2", "건대입구역 7호선", 127.0705, 37.5405)],
        }
        self.stations = [  # 중심(≈127.085, 37.527) 주변의 가까운 역들
            doc("a1", "석촌역 8호선", 127.1060, 37.5055, 900), doc("a2", "삼전역 9호선", 127.0880, 37.5045, 1200),
            doc("a3", "어린이대공원역 7호선", 127.0742, 37.5480, 1500), doc("a4", "뚝섬유원지역 7호선", 127.0670, 37.5312, 1800),
            doc("a5", "강변역 2호선", 127.0946, 37.5351, 2000), doc("a6", "송파나루역 9호선", 127.1099, 37.5102, 2400),
        ]
        self.calls: list[str] = []

    def far(self, n=6, distance=2000):
        """참여자에게서 아주 먼 역들 → 제약을 충족할 수 없다."""
        return [doc(f"f{i}", f"먼역{i} 1호선", 127.20 + i * 0.01, 37.62, distance) for i in range(n)]

    def request_json(self, url, *, params=None, headers=None):
        self.calls.append(url)
        if "open-meteo" in url:
            return {"daily": {"precipitation_probability_max": [self.rain],
                              "temperature_2m_max": [21.0], "temperature_2m_min": [11.0]}}
        path = url.replace(BASE, "")
        if "keyword" in path:
            return {"documents": self.keyword.get(params["query"], []), "meta": {"total_count": 0}}
        code = params["category_group_code"]
        if code == "SW8":
            docs = [d for d in self.stations if float(d["distance"]) <= params["radius"]]
            return {"documents": docs, "meta": {"is_end": True, "total_count": len(docs)}}
        total = int(float(params["x"]) * 1000) % 40 + 5
        docs = [doc(f"{code}{i}", f"{code}장소{i}", params["x"], params["y"], 100 * (i + 1)) for i in range(3)]
        return {"documents": docs, "meta": {"total_count": total, "pageable_count": min(total, 45), "is_end": False}}


class FakeLLM:
    def __init__(self, *scripts: dict):
        self.queue = list(scripts)
        self.compose_calls = 0
        self.analyze_calls = 0
        self.diagnose_calls: list[tuple[str, list]] = []
        self.diagnose_error: Exception | None = None

    def analyze(self, message, context, today, weekday):
        self.analyze_calls += 1
        return Analysis(**self.queue.pop(0))

    def compose(self, facts):
        self.compose_calls += 1
        return "요약 문장"

    def chat_reply(self, message):
        return "안녕하세요"

    def diagnose(self, summary, warnings):
        self.diagnose_calls.append((summary, warnings))
        if self.diagnose_error:
            raise self.diagnose_error
        return "메인 Agent 의 계획 문제입니다. 후보 반경을 조정하세요. 세 번째 문장입니다. 네 번째 문장은 잘려야 합니다."


def future_date(days=3) -> str:
    return (now_kst().date() + timedelta(days=days)).isoformat()


def meet(**kw) -> dict:
    """new_meet 분석 결과 기본값."""
    base = {"intent": "new_meet", "reason": "새 약속 요청", "locs": ["잠실", "건대"], "date": future_date(),
            "start": "14:00", "prio": "things"}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    for k in ("T_MAX", "G_MAX", "FAIL_INJECT", "CALL_BUDGET", "ROUTE_WORKERS", "MAX_REPLAN", "DISABLE_LOGGING_AGENT", "ANOMALY_INJECT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KAKAO_REST_KEY", "test-kakao")
    monkeypatch.setenv("LLM_API_KEY", "test-llm")
    monkeypatch.setenv("ROUTE_PROVIDER", "mock")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    base.reset_injection()
    yield
    set_container(None)


@pytest.fixture
def world(monkeypatch):
    w = FakeWorld()
    monkeypatch.setattr(http, "request_json", w.request_json)
    return w


@pytest.fixture
def make_container(tmp_path, world):
    def _make(*scripts, logging_enabled=True):
        llm = FakeLLM(*scripts)
        c = Container(llm=llm, data_dir=tmp_path / "data", logging_enabled=logging_enabled,
                      diagnosis_background=False)  # 테스트는 진단을 동기로 실행해 결과를 바로 확인한다
        set_container(c)
        return c
    return _make
