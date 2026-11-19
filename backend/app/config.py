"""설정 로딩. 모든 설정값은 여기 한 곳에서 읽는다 (설계서 6장: 호출 결과를 보고 조정)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    kakao_key: str
    llm_key: str
    llm_base_url: str
    llm_model: str
    llm_provider: str
    route_provider: str
    fail_inject: str
    anomaly_inject: str
    disable_logging: bool
    data_dir: Path
    # 제약·판단 기본값
    t_max: int
    g_max: int
    rain_threshold: int
    default_stay_h: int
    max_participants: int
    # 재계획: replan_n 번째 시도에서 사용할 반경·후보 수 (인덱스 0 = 최초 시도)
    radius_steps: tuple[int, ...]
    k_steps: tuple[int, ...]
    max_replan: int
    # 운영
    call_budget: int
    wx_max_days: int
    http_timeout: float
    route_workers: int


def settings() -> Settings:
    data_dir = Path(os.getenv("DATA_DIR") or ROOT / "data")
    return Settings(
        kakao_key=os.getenv("KAKAO_REST_KEY", "").strip(),
        llm_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip(),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini").strip(),
        llm_provider=os.getenv("LLM_PROVIDER", "openai").strip().lower(),
        route_provider=os.getenv("ROUTE_PROVIDER", "kakao").strip().lower(),
        fail_inject=os.getenv("FAIL_INJECT", "").strip(),
        anomaly_inject=os.getenv("ANOMALY_INJECT", "").strip().lower(),
        disable_logging=os.getenv("DISABLE_LOGGING_AGENT", "0") == "1",
        data_dir=data_dir,
        t_max=_int("T_MAX", 90),
        g_max=_int("G_MAX", 30),
        rain_threshold=_int("RAIN_THRESHOLD", 60),
        default_stay_h=_int("DEFAULT_STAY_H", 3),
        max_participants=_int("MAX_PARTICIPANTS", 4),
        radius_steps=(3000, 5000, 8000, 8000),
        k_steps=(8, 8, 8, 12),
        max_replan=_int("MAX_REPLAN", 3),
        call_budget=_int("CALL_BUDGET", 100),
        wx_max_days=_int("WX_MAX_DAYS", 14),
        http_timeout=float(os.getenv("HTTP_TIMEOUT", "5") or 5),
        route_workers=_int("ROUTE_WORKERS", 8),
    )
