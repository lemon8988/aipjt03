"""외부 API 호출 공통부. 상태 코드를 Tool 실패 코드로 바꾼다. 테스트에서 이 함수를 대체한다."""
from __future__ import annotations

import logging
from typing import Any

import requests

from app.config import settings
from app.tools.base import ToolError

log = logging.getLogger(__name__)


def request_json(url: str, *, params: dict | None = None, headers: dict | None = None) -> Any:
    """GET → JSON. 타임아웃·5xx 는 같은 인자로 1회만 재시도한다 (설계서 3장)."""
    last: ToolError | None = None
    for _ in range(2):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=settings().http_timeout)
        except requests.Timeout:
            last = ToolError("TIMEOUT", "외부 API 응답 시간이 초과되었습니다.")
            continue
        except requests.RequestException:
            last = ToolError("API_ERROR", "외부 API에 연결하지 못했습니다.")
            continue
        code = r.status_code
        if code == 200:
            try:
                return r.json()
            except ValueError:
                raise ToolError("API_ERROR", "외부 API 응답 형식이 올바르지 않습니다.") from None
        # 원인 파악용 서버 로그 (키·좌표가 담긴 쿼리는 남기지 않고 경로와 응답 앞부분만)
        log.warning("외부 API 실패: HTTP %s %s body=%s", code, url.split("?")[0], " ".join((r.text or "")[:200].split()))
        if code == 400:
            raise ToolError("BAD_INPUT", "외부 API가 요청 형식을 거부했습니다.")
        if code in (401, 403):
            raise ToolError("NO_KEY", "API 키가 없거나 유효하지 않습니다. 키와 서비스 활성화를 확인하세요.")
        if code == 429:
            raise ToolError("RATE_LIMIT", "외부 API 호출 한도를 초과했습니다.")
        last = ToolError("API_ERROR", f"외부 API 오류 (HTTP {code})")
    assert last is not None
    raise last
