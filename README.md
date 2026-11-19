# 약속 장소 플래너

참여자 2~4명의 **위치와 날짜·시작 시각**을 대화로 입력하면, 이동 부담이 작은 **만남 역**과 그 **주변 장소**를 추천하고 약속 계획으로 저장·관리하는 Agent 기반 플래너 서비스입니다. (SSAFY AI 코어 프로젝트 3회차 · 도메인: 약속 장소)

- **핵심 흐름**: 위치 해석 → 후보 역 조회 → 참여자별 이동시간 조회 → 제약 판단 → 우선순위 정렬 → 주변 장소 추천 → 계획 저장
- **Agent 가 하는 일**: 요청을 분석해 필요한 Tool 을 호출하고, 이동시간·편차·날씨를 사용자의 제약과 비교해 충족하는 후보가 없으면 검색 범위를 넓혀 **재계획**합니다. 대화 맥락을 기억해 "같은 조건에서 음식점 많은 쪽으로" 같은 후속 요청을 처리합니다.
- **관찰 가능성**: 메인 Agent 의 실행 과정은 이벤트로 발행되고, 별도의 **로깅 Agent** 가 구독해 세션별로 저장·평가합니다.
- **외부 API 3종**: 카카오 로컬(위치·후보 역·주변 장소), 카카오맵 대중교통 경로 조회(이동시간), Open-Meteo(날씨)

## 전체 기능 설명

### 필수 기능 (F3001~F3019) — 모두 구현

| 영역 | 요구사항 | 기능 |
|---|---|---|
| Tool | F3001~F3004 | Tool 5종 등록·실행 (`geo_loc` 위치 해석, `find_stn` 후보 역, `get_route` 이동시간, `find_spot` 주변 장소, `get_wx` 날씨). 모든 Tool 은 성공/실패를 같은 형식으로 반환하고 예외를 밖으로 던지지 않음 |
| 메인 Agent | F3005~F3011 | 요청 분석(날짜·시각·위치·우선순위·제약 추출, 없는 값은 되묻기) → 계획 → Tool 호출·결과 통합 → 제약 판단·점수 → 재계획(상한 3회, 결과 재사용) → 응답(추천·근거·제외 사유), 대화 맥락 유지 |
| 로깅 | F3012~F3014 | 단계·Tool 호출·LLM 판단·재계획 이벤트 발행, 로깅 Agent 가 세션별 저장(LLM 미사용), 목록·상세·삭제 조회(수정 API 없음) |
| 계획 | F3015~F3016 | 계획 저장·조회·수정·삭제, 같은 날짜에 시간이 겹치면 저장은 하되 겹침을 알림 |
| 서버·화면 | F3017~F3019 | FastAPI(대화·계획·로그 API), 대화·계획 관리·실행 로그 화면, 실행 단계·Tool 호출 이력·재계획 지점·처리 중·오류 안내 표시 |

### 심화 기능

| 번호 | 기능 | 상태 |
|---|---|---|
| **F3020** | **로깅 Agent 이상 징후 판단** — 반복 호출·재계획 과다·계획 외 호출을 규칙으로 판정, 근거 없으면 "판정 불가", 이상 실행에만 LLM 원인 진단 | **구현** (아래 '심화 기능 적용 요약') |
| F3021 | Planner·Worker 구조 전환 | 선택하지 않음 |
| F3022 | RAG 검색 Tool | 선택하지 않음 (이 도메인에서 쓸 문서가 없음) |
| F3023 | 캘린더 연동 Tool | 선택하지 않음 |

### 비기능 (NF3001~NF3007)

API 키는 환경변수(`.env`)로만 관리, 화면에 키·오류 상세 비노출, Tool 실패해도 서비스 지속, 재계획 상한 3회, 프론트엔드/백엔드 분리와 모듈 분리, 외부 API 출처·이용 조건 기록 (확인 결과는 [docs/05_검증결과.md](docs/05_검증결과.md) 4.8).

## 구조

```
backend/app/
  agent/        main_agent.py (LangGraph 그래프), rules.py (제약 판단·점수: LLM 미사용), llm.py, memory.py
  tools/        base.py (공통 응답·레지스트리), kakao_local.py, route.py, weather.py, http.py
  events/       bus.py (발행/구독)
  logging_agent.py   구독자: 세션별 JSONL 저장 + 실행 종료 시 평가기 호출 (수집·저장에 LLM 미사용)
  evaluation.py      (F3020) 실행별 이상 징후 판정(규칙) + 이상일 때만 LLM 원인 진단
  store/plans.py     계획 저장소 (SQLite)
  api/          chat.py, plans.py, logs.py        main.py (FastAPI)
frontend/       index.html (대화) · plans.html (계획 관리) · logs.html (실행 로그) — 순수 HTML/CSS/JS
scripts/        check_apis.py (API 호출 확인) · verify_tools.py · show_graph.py · show_analysis.py · capture_evidence.py · build_verification_doc.py
tests/          자동 테스트 112개 (가짜 외부 API·가짜 LLM)
docs/           제출 문서 5종 + 증빙 (screenshots, samples, verification, api_shapes)
```

LLM 은 **요청 분석**, **응답 문장 생성**, **이상 실행의 원인 진단** 세 곳에서만 쓰고, 계획·실행·판단은 규칙/계산식입니다. 로깅 Agent 의 수집·저장·판정에는 LLM 을 호출하지 않습니다.

## 실행 방법

개발·검증 환경: Windows, Python 3.14 (명세 요구 3.10+ — 3.10 은 직접 검증하지 못함), Chrome(스크린샷 수집 시).
`requirements.txt` 는 명세서 1.2 의 필수 라이브러리 버전으로 고정했고, 그 버전으로 새 가상환경을 만들어 테스트 112개 통과를 확인했습니다.

```bash
pip install -r requirements.txt
copy .env.example .env        # macOS/Linux: cp .env.example .env  → 키를 채운다 (.env 는 절대 커밋·제출하지 않음)

# 1) API 연결 확인 (키 발급 후)
python scripts/check_apis.py            # 카카오 로컬·대중교통, Open-Meteo
python scripts/check_apis.py --llm      # LLM 연결 + Tool 선택

# 2) 백엔드 (터미널 1)   API 문서: http://localhost:8000/docs
cd backend && uvicorn app.main:app --reload --port 8000

# 3) 프론트엔드 (터미널 2)   화면: http://localhost:5500
cd frontend && python -m http.server 5500

# 자동 테스트
python -m pytest -q
```

프론트엔드는 `frontend/config.js` 의 `API_BASE`(기본 `http://localhost:8000`)로 백엔드를 호출합니다. 화면에 상태 배너가 뜨면(키 누락, 로깅 Agent 꺼짐 등) 안내대로 `.env` 를 확인하세요.

## API 키 발급 및 설정 절차 요약

| 키 | 발급 | `.env` |
|---|---|---|
| 카카오 REST API 키 | [developers.kakao.com](https://developers.kakao.com) → 애플리케이션 생성(**앱은 1개만**, 무료 쿼터는 처음 활성화한 앱 기준) → 앱 키의 REST API 키 → 카카오맵·로컬 API **활성화** | `KAKAO_REST_KEY` |
| LLM 키 | SSAFY GMS 발급 키 | `LLM_API_KEY`, 그리고 `LLM_PROVIDER=gemini`, `LLM_BASE_URL=https://gms.ssafy.io/gmsapi/generativelanguage.googleapis.com/v1beta`, `LLM_MODEL=gemini-2.5-flash-lite` (OpenAI 호환 엔드포인트는 `LLM_PROVIDER=openai`) |
| Open-Meteo | **키 불필요** | - |

`ROUTE_PROVIDER=kakao`(기본)가 실제 대중교통 이동시간입니다. `mock` 은 직선거리 기반 **모의값**(개발용)이며 화면에 배너가 표시됩니다.

## 사용한 외부 API의 출처와 이용 조건 (NF3007)

| API | 출처 | 용도 | 이용 조건 |
|---|---|---|---|
| 카카오 로컬 | [카카오 개발자 문서](https://developers.kakao.com/docs/latest/ko/local/dev-guide) | 위치 해석, 후보 역, 주변 장소 | 호출 한도: 앱 설정 기준 **전체 API 3,000,000건**(사용자 확인). **응답 저장 금지** → 실시간 호출만 하고, 저장하는 것은 사용자가 확정한 이름·시각뿐(좌표·주소·URL 미저장) |
| 카카오맵 대중교통 경로 조회 | [카카오맵 REST API](https://developers.kakao.com/docs/ko/kakaomap/rest-api) | 참여자→후보 역 이동시간 | 같은 REST API 키·한도. 응답 저장 금지(문의글 기준이며 공식 답변은 확인하지 못해 보수적으로 해석). 출발 시각을 지정할 수 없어 **조회 시점 기준**. 요청 1건에 약 16회 호출(후보 8곳 × 참여자 2명) |
| Open-Meteo | [open-meteo.com](https://open-meteo.com/en/docs) | 약속 날짜 강수확률·기온 | 키 불필요, **비상업 무료, CC BY 4.0**(응답 `meta.notes` 에 출처 표기). 예보는 오늘+14일까지만 사용 |
| LLM (GMS 경유 Gemini) | [SSAFY GMS](https://gms.ssafy.io/web/) | 요청 분석·응답 문장·이상 진단 | 교육용 GMS 키(환경변수). 키는 소스·제출물에 포함하지 않음 |

## 심화 기능 적용 요약 (F3020: 로깅 Agent 이상 징후 판단)

실행(요청 1건)이 끝나면(`run_end`) 로깅 Agent 가 그 실행의 이벤트만 모아 판정합니다.

| 규칙 | 경고 조건 |
|---|---|
| `repeated_call` 반복 호출 | 같은 Tool 이 같은 입력(키 순서 무관)으로 한 실행에서 2회 이상 |
| `excess_replan` 재계획 과다 | 재계획이 상한(`MAX_REPLAN`, 기본 3)에 도달 |
| `unplanned_call` 계획 외 호출 | 계획 단계가 정한 Tool 목록에 없는 Tool 호출 (계획 없이 호출한 경우 포함) |

- **판정에는 LLM 을 쓰지 않습니다.** 결과는 `ok`(정상) / `anomaly`(이상) / `unknown`(**판정 불가**). 실행 시작 이벤트가 없는 등 근거가 없으면 '이상 없음'이 아니라 `unknown` 으로 남깁니다.
- **LLM 원인 진단은 `anomaly` 인 실행에만** 붙습니다(세 문장 이내). 정상·판정 불가 실행에는 LLM 을 호출하지 않습니다. 진단은 `diagnosis` 필드에 **판정과 분리해 저장**하고 `warning_count` 에 섞지 않으며, 백그라운드로 실행되어 응답을 지연시키지 않습니다. 진단이 실패해도 판정과 실행 로그는 그대로 남습니다.
- **메인 Agent 의 참조**: 메인 Agent 는 `eval_lookup` 으로 직전 실행 판정을 조회해 분석 단계 종료 이벤트에 `prev_eval`·`prev_warnings` 로 기록합니다. 현재는 참조·기록만 하며 흐름을 바꾸지는 않습니다(F3021 미선택).
- 조회: `GET /api/logs/{session_id}/evaluations`, `GET /api/logs`(세션별 `latest_verdict`·`anomaly_runs`), `POST /api/chat` 응답의 `evaluation`. 실행 로그 화면에서 판정 배지·규칙별 결과·진단(판정과 구분 표기)을 볼 수 있습니다.
- **검증용 이상 주입**: 실제 서버에서 이상 실행을 만들기 위해 `ANOMALY_INJECT=repeat_call | unplanned_call | missing_events` 환경변수를 둡니다(운영에서는 비워 둠). 필수 기능의 결과(정상 실행 `ok`)와 비교한 결과는 [docs/05_검증결과.md](docs/05_검증결과.md) 4.6.
- 한계: '재계획 과다'를 상한 도달로 정의했기 때문에, 사용자 조건이 현실적으로 충족 불가한 요청(예: 수원+춘천)도 `anomaly` 로 판정됩니다. LLM 진단이 이를 '사용자 조건 문제'로 구분하도록 프롬프트에 기준을 넣었습니다.

## 제출 문서와 증빙

| 문서 | 내용 |
|---|---|
| [docs/01_요구사항정의서.md](docs/01_요구사항정의서.md) | 도메인 선택 근거, 요구사항 표(F3001~, NF3001~), 기획 검증 결과·설계 변경 |
| [docs/02_Agent설계문서.md](docs/02_Agent설계문서.md) | 실행 단계·입출력, 제약 판단 계산식, 재계획 조건, 이벤트·로깅 Agent, Memory, F3020 |
| [docs/03_Tool정의서.md](docs/03_Tool정의서.md) | Tool 5종의 입출력·실패 케이스·등록/확장 방법 |
| [docs/04_API명세서.md](docs/04_API명세서.md) | 백엔드 API 경로와 요청·응답 형식 (계획·실행 로그 관리 포함) |
| [docs/05_검증결과.md](docs/05_검증결과.md) | 4장 확인 항목별 결과·스크린샷, 대표 시나리오 기록, 전체 실행 로그 |
| [docs/06_셀프체크리스트.md](docs/06_셀프체크리스트.md) | **미완료** — 3일차 오후에 제공되는 양식으로 작성 필요 |
| `docs/screenshots/` · `docs/samples/` · `docs/verification/` | 스크린샷, 실행 로그 샘플(전체·재계획), 시나리오 기록 JSON, Tool 확인 결과 |

최초 기획서: [약속 장소 플래너 — 요구사항·Tool·Agent 설계서.md](약속%20장소%20플래너%20—%20요구사항·Tool·Agent%20설계서.md)

증빙 재생성: `python scripts/verify_tools.py` · `python scripts/show_analysis.py` · `python scripts/capture_evidence.py`(`pip install -r requirements-dev.txt`, 포트 8000·5500 비워 둘 것) · `python scripts/build_verification_doc.py`

## 실제 호출에서 확인된 대표 요청

실제 LLM·카카오·날씨 API 로 실행한 결과입니다 (2026-11-20 기준 다음 토요일, 이동시간·장소 수는 실시간 데이터라 값이 달라질 수 있음).

| 요청 | 결과 |
|---|---|
| "잠실이랑 건대 사는 둘이 토요일 오후 2시에 데이트, 할 거 많은 곳으로" | 한 번에 충족 → 건대입구역(잠실 13분, 건대 6분) |
| "같은 조건에서 음식점 많은 쪽으로" | Tool 호출 0회, 캐시 재사용 26건, 우선순위만 갱신 |
| "안녕" | 계획 단계 건너뜀, Tool 0회 |
| "송도랑 마석에서 모일 장소 추천해줘" | 날짜·시작 시각 되묻기 → "토요일 3시, 이동 부담 적게" 후 용산역 추천 |
| "중앙이랑 건대 사는 둘이 토요일 2시, 이동 적게" | 중앙역 모호 → 선택 버튼 → **재계획 1회 후 충족** (대공원역) |
| "수원이랑 춘천 사는 둘이 토요일 2시" | 재계획 3회 후 상한 도달, 가장 근접한 후보(양수역 94분, 4분 초과)와 완화 버튼 제시 |
| "잠실에서 7시 전에 끝나야 해" | 위치·날짜·시작 시각을 지어내지 않고 되묻기 |

## 설계 결정과 환경변수

- 날씨는 Open-Meteo. 날짜가 오늘+14일 밖이면 `OUT_OF_RANGE`, 값이 null 이면 날씨 판단 생략.
- 강수확률 60% 이상이면 "관광명소" 카테고리를 일괄 제외(실내외 구분 데이터가 없어 임의 판단 대신 근거를 응답에 밝힘).
- 재계획 조건: 충족 후보 0개(후보 역이 없는 경우 포함). 이동시간 조회가 **전부 실패**한 경우는 재계획하지 않음.
- 종료 시각을 말하지 않으면 `null` 로 저장하고 겹침 판정에만 시작+3시간 사용.
- 직선 500m 이하인데 대중교통 경로가 없으면 **도보 추정**으로 대체하고 표기.
- 요청당 외부 호출 예산 100회(`CALL_BUDGET`), `get_route` 는 가장 먼 참여자부터 호출하고 상한 초과 시 나머지를 생략.
- 검증용 주입: `FAIL_INJECT="get_route:NO_ROUTE:3"`(Tool 실패), `ANOMALY_INJECT=...`(이상 징후), `DISABLE_LOGGING_AGENT=1`(로깅 Agent 끄기).

## 알려진 한계

- 이동시간은 조회 시점 기준이며 약속 시각의 실제 운행과 다를 수 있습니다 (출발 시각 지정 불가).
- "음식점 많게/할 거 많게"는 평점이 아니라 반경 500m 안의 검색 건수 기준입니다.
- Memory 는 프로세스 메모리라 서버를 재시작하면 대화 맥락이 사라집니다.
- 요청 분석의 우선순위는 모델이 해석할 수 있습니다 (예: "이동은 60분 넘지 않게" → 이동 부담 적게).
- 심화 F3021~F3023 은 선택하지 않았고, 셀프 체크리스트는 공식 양식을 받은 뒤 작성해야 합니다.
