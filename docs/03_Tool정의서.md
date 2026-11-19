# Tool 정의서

약속 장소 플래너 · 구현: `backend/app/tools/` · 실제 호출 결과: [verification/tool_checks.md](verification/tool_checks.md)

## 1. 개요

| 구분 | Tool | 외부 API | 용도 |
|---|---|---|---|
| 외부 API 툴 | `geo_loc` | 카카오 로컬 (키워드 검색) | 위치 이름 → 좌표 |
| 외부 API 툴 | `find_stn` | 카카오 로컬 (카테고리 검색 SW8) | 중심 좌표 주변 후보 지하철역 |
| 외부 API 툴 | `get_route` | 카카오맵 대중교통 경로 조회 | 참여자 위치 → 후보 역 이동시간 |
| 외부 API 툴 | `find_spot` | 카카오 로컬 (카테고리·키워드 검색) | 후보 역 주변 장소와 반경 내 건수 |
| 외부 API 툴 | `get_wx` | Open-Meteo | 약속 날짜 강수확률·기온 |
| 로컬 서버 API 툴 | (없음) | - | 심화 F3022(RAG)·F3023(캘린더)를 선택하지 않아 구현하지 않음 |

Agent 에는 **이름·설명·입력 스키마만** 넘긴다. 설명은 기능이 아니라 "언제 쓰는지"를 적는다. 필수 Tool 3개(후보 조회 `find_stn`, 상세정보 `get_route`, 보조 정보 `find_spot`)는 모두 외부 API 툴이며, `geo_loc`·`get_wx` 는 입력을 만들기 위한 보조 Tool 이다.

## 2. 공통 응답 형식

모든 Tool 은 예외를 밖으로 던지지 않고 같은 형식으로 반환한다 (`call_tool` 이 모든 예외를 실패 응답으로 바꾼다).

```json
{ "ok": true,  "data": { ... }, "meta": { "basis": "조회 기준 설명", "notes": ["부가 설명"] } }
{ "ok": false, "error": { "code": "NO_RESULT", "message": "사유", "detail": null } }
```

- `meta.basis`: 요청한 기준과 실제 조회한 기준이 다르면 그 차이를 적는다 (예: 이동시간은 "조회 시점 기준", 거리가 가까워 도보로 추정한 경우 "도보 추정").
- `error.detail`: 처리에 필요한 추가 정보 (예: `AMBIGUOUS` 의 후보 목록). 내부 예외 메시지는 담지 않는다.

## 3. 실패 코드와 처리 방식

| 코드 | 의미 | 발생 Tool | Agent 처리 |
|---|---|---|---|
| `NO_KEY` | API 키 없음/유효하지 않음 (HTTP 401·403 포함) | 카카오 계열 | **재계획 없이** 설정 안내를 표시하고 중단 |
| `BAD_INPUT` | 필수 인자 누락·형식 오류 (HTTP 400 포함) | 전체 | 재계획 없이 되묻기/오류 처리 |
| `NO_RESULT` | 검색 결과 없음 | geo_loc, find_stn, find_spot, get_wx(값 null) | find_stn: 재계획 대상(반경 확대). find_spot: 개수 0 으로 점수 반영. geo_loc: 위치를 더 구체적으로 되묻기. get_wx: 날씨 판단 생략 |
| `NO_ROUTE` | 두 지점 사이 대중교통 경로 없음 | get_route | **해당 후보만 제외**하고 제외 사유 표기 (값을 지어내지 않음) |
| `AMBIGUOUS` | 같은 이름의 위치가 여럿 | geo_loc | 후보를 선택 버튼으로 보여 주고 되묻기 (재계획 아님) |
| `OUT_OF_RANGE` | 예보 범위 밖 날짜 (오늘+14일 초과) | get_wx | 날씨 판단 생략, 그 사실을 응답에 표기 |
| `RATE_LIMIT` | 호출 한도 초과 (HTTP 429, 요청당 호출 예산 초과 포함) | 전체 | 재계획 없이 잠시 후 재시도 안내 |
| `API_ERROR`, `TIMEOUT` | 외부 API 오류·시간 초과 | 전체 | 같은 인자로 **1회만 재시도**, 실패하면 해당 후보를 "판정 불가"로 제외 (geo_loc·find_stn 실패는 안내 후 중단) |

## 4. Tool 상세

### 4.1 `geo_loc` — 위치 해석

- **언제 쓰는가**: 사용자가 말한 위치(역명·동네·건물)를 좌표로 바꿔야 할 때. 같은 이름이 여러 곳이면 `AMBIGUOUS` 로 후보를 돌려준다.
- **외부 API**: 카카오 로컬 `GET /v2/local/search/keyword.json`

| 입력 | 타입 | 필수 | 기본 | 설명 |
|---|---|---|---|---|
| `q` | string | ✔ | - | 위치 이름 |
| `size` | integer | | 5 | `AMBIGUOUS` 시 돌려줄 후보 수 (1~15) |

- **해석 규칙**: ① `q`+"역" 을 지하철역(SW8)으로 먼저 검색해 이름이 일치하는 역을 찾는다 (노선별 중복은 역명으로 합침, 같은 이름이라도 1.5km 이상 떨어져 있으면 별개). 정확히 1곳이면 확정, 여러 곳이면 `AMBIGUOUS`. ② 역이 없으면 일반 키워드 검색으로, 이름이 정확히 일치하는 1건이거나 결과가 1건이면 확정, 아니면 `AMBIGUOUS`.
- **출력**: `data.items[{ id, name, addr, x, y }]` (확정 시 1건). x=경도, y=위도.
- **실패**: `NO_RESULT`(결과 없음), `AMBIGUOUS`(`error.detail.candidates`), `BAD_INPUT`, `NO_KEY`, `API_ERROR`/`TIMEOUT`
- **다음 Tool 입력**: 참여자 좌표 → 평균(중심 좌표) → `find_stn`, `get_wx` / 참여자 좌표 → `get_route.from`

### 4.2 `find_stn` — 후보 역 조회 (필수: 후보 조회 Tool)

- **언제 쓰는가**: 참여자 좌표의 중심 주변에서 만남 후보 지하철역이 필요할 때.
- **외부 API**: 카카오 로컬 `GET /v2/local/search/category.json` (`category_group_code=SW8`, `sort=distance`)

| 입력 | 타입 | 필수 | 기본 | 설명 |
|---|---|---|---|---|
| `x`, `y` | number | ✔ | - | 중심 좌표 |
| `radius_m` | integer | | **3000** | 검색 반경 (100~20000 으로 보정) |
| `size` | integer | | 15 | 최대 역 수 (노선별 중복 때문에 최대 2페이지 조회) |

- **출력**: `data.stations[{ id, name, x, y, dist_m }]` 가까운 순. 같은 역의 노선별 중복은 역명(`○○역`)으로 합친다. `meta.basis` = "중심 좌표 반경 {r}m", 요청 반경을 보정했으면 `meta.notes` 에 밝힌다.
- **기본값**: 반경을 주지 않으면 3000m (검증: [tool_checks.md](verification/tool_checks.md) — `basis: 중심 좌표 반경 3000m`)
- **실패**: `NO_RESULT`(반경 안에 역 없음 → Agent 가 반경 확대 재계획), `BAD_INPUT`, `NO_KEY`, `API_ERROR`/`TIMEOUT`
- **다음 Tool 입력**: `stations[].{x,y}` → `get_route.to`, `find_spot.{x,y}`

### 4.3 `get_route` — 이동시간 조회 (필수: 상세정보 조회 Tool)

- **언제 쓰는가**: 참여자 위치에서 후보 역까지의 대중교통 이동시간(분)과 환승 수가 필요할 때. 조회 시점 기준이며 약속 시각의 운행과 다를 수 있다.
- **외부 API**: 카카오맵 대중교통 경로 조회 `GET /v2/routing/publictraffic` (파라미터 `start_x`, `start_y`, `end_x`, `end_y`)

| 입력 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `from` | `{x, y}` | ✔ | 출발 좌표 (참여자 위치) |
| `to` | `{x, y}` | ✔ | 도착 좌표 (후보 역) |

- **출력**: `data{ min, transfers, mode, type?, alternatives? }`
  - `min`: 분 (응답 `totalTime` 은 **초 단위**라 분으로 환산, 경로가 여러 개면 **최단**)
  - `mode`: `transit`(대중교통) / `same_place`(출발지=도착지, 0분) / `walk_estimate`(도보 추정)
  - `meta.basis`: "조회 시점 기준 대중교통 최단 경로 (약속 시각의 실제 운행과 다를 수 있음)"
- **응답 상태 매핑** (실제 호출로 확인): `OK` → 성공, `EQUAL_POINTS` → 0분, `NO_RESULTS`·`STARTNODES_NULL`·`ENDNODES_NULL` → `NO_ROUTE`
- **도보 추정**: 직선거리가 500m 이하인데 경로가 없으면(실측: 약 130~300m 구간에서 방향에 따라 `NO_RESULTS`) 후보를 잃지 않도록 도보로 추정하되(직선거리×1.3 ÷ 분속 75m), `mode=walk_estimate`·`meta.basis`·`meta.notes` 로 추정임을 밝히고 화면에도 "(도보 추정)" 을 표기한다.
- **판단에 필요한 속성을 확인할 수 없는 경우**: `NO_ROUTE`/`API_ERROR` 면 값을 채우지 않고 그 후보를 "판단하지 않은 채" 제외하며 사유를 응답에 적는다.
- **실패**: `NO_ROUTE`, `BAD_INPUT`, `NO_KEY`, `RATE_LIMIT`, `API_ERROR`/`TIMEOUT`
- **개발용 모의 제공자**: `ROUTE_PROVIDER=mock` (직선거리 기반 추정, `meta.basis` 에 "모의 추정(개발용, 실제 이동시간 아님)" 표기). 제출 검증은 모두 `kakao` 로 수행했다.

### 4.4 `find_spot` — 주변 장소 검색 (필수: 보조 정보 검색 Tool)

- **언제 쓰는가**: 후보 역 주변의 음식점·카페·문화시설·관광명소를 찾거나, 우선순위 점수에 쓸 반경 내 개수가 필요할 때. 어떤 영화·행사를 하는지 같은 정보는 알려주지 않는다.
- **외부 API**: 카카오 로컬 `GET /v2/local/search/category.json` (또는 `q` 가 있으면 keyword.json)

| 입력 | 타입 | 필수 | 기본 | 설명 |
|---|---|---|---|---|
| `x`, `y` | number | ✔ | - | 후보 역 좌표 |
| `cat` | string | ✔ | - | `음식점`(FD6) / `카페`(CE7) / `문화시설`(CT1) / `관광명소`(AT4) |
| `radius_m` | integer | | 500 | 반경 (50~20000) |
| `q` | string | | - | 키워드(선택) |
| `size` | integer | | 5 | 돌려줄 장소 수 (1~15) |

- **출력**: `data{ items[{ id, name, cat, addr, url, dist_m }], total, pageable }`
  - `total` = 응답 `meta.total_count` (반경 내 전체 건수, 점수 계산에 사용). `pageable_count` 는 최대 45 로 잘려 비교가 안 되므로 쓰지 않는다 (실측: 음식점 total 372 / pageable 45).
- **결과 없음**: `NO_RESULT` — Agent 는 이를 **개수 0** 으로 점수에 반영한다 (실패가 아님).
- **실패**: `NO_RESULT`, `BAD_INPUT`(잘못된 `cat`), `NO_KEY`, `RATE_LIMIT`, `API_ERROR`/`TIMEOUT`. 개수를 확인하지 못한 후보는 "판정 불가"로 제외한다.
- **주의**: 장소 검색은 어떤 영화·행사를 하는지 알려 주지 않으므로 장소와 후보를 임의로 묶지 않는다.

### 4.5 `get_wx` — 날씨 조회

- **언제 쓰는가**: 약속 날짜의 강수확률로 실내·야외 장소를 가를 때.
- **외부 API**: Open-Meteo `GET /v1/forecast` (`daily=precipitation_probability_max,temperature_2m_max,temperature_2m_min`, 키 불필요)

| 입력 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `x`, `y` | number | ✔ | 중심 좌표 (한 번만 호출) |
| `date` | string | ✔ | `YYYY-MM-DD` |

- **출력**: `data{ rain_pct, tmax, tmin }`, `meta.notes` 에 "Weather data by Open-Meteo.com (CC BY 4.0)"
- **실패**: `OUT_OF_RANGE`(오늘+14일 초과), `BAD_INPUT`(과거·형식 오류), `NO_RESULT`(강수확률 null), `API_ERROR`/`TIMEOUT`
- **처리**: 실패하면 날씨 판단만 생략하고(`notes` 에 사유 표기) 추천은 계속한다. 강수확률 60% 이상이면 `관광명소` 를 일괄 제외 (실내외 구분 데이터가 없어 임의 판단 대신 근거를 밝힘).

## 5. Tool 로 두지 않은 기능

사용자가 이미 정한 값을 다루는 작업은 모델의 판단이 필요 없으므로 Tool 이 아니라 백엔드 API/실행 흐름이 직접 수행한다.

- 계획 저장·조회·수정·삭제 (`/api/plans`), 실행 로그 조회·삭제 (`/api/logs`)
- 제약 판단·점수 계산·재계획 결정 (규칙/계산식, LLM 미사용)
- 직선거리 기반 후보 압축(상위 K), 같은 역 노선 합치기

## 6. Tool 등록 및 확장 방법

1. `backend/app/tools/` 에 모듈을 만들고, 입력을 검증해 `ToolError(code, message)` 를 던지거나 `ok(...)` 를 반환하는 함수를 작성한다. 외부 호출은 `tools/http.py` 의 `request_json` 을 쓴다 (상태 코드 → 실패 코드 변환, 타임아웃/5xx 1회 재시도).
2. 모듈 끝에서 `register(ToolSpec(name, description, input_schema, fn, required, kind))` 로 등록한다. `kind` 는 `external` / `local`. **설명에는 언제 쓰는지**를 적는다.
3. `tools/__init__.py` 에 모듈을 import 하면 레지스트리(`REGISTRY`)에 등록된다. `list_specs()` 가 Agent 에 노출할 명세(이름·종류·설명·입력 스키마)를 만들고, `openai_tools()` 가 LLM Tool 호출 형식으로 변환한다.
4. 호출은 항상 `call_tool(name, args)` 를 거친다: 필수 인자 확인 → (선택) 실패 주입 → 실행 → 모든 예외를 실패 응답으로 변환.
5. 메인 Agent 의 계획(`PLAN_STEPS`)과 실행 노드에서 호출 순서·값 전달을 연결한다.

**실패 주입(검증용)**: 환경변수 `FAIL_INJECT="get_route:NO_ROUTE:3"` → 해당 Tool 호출 3번에 1번을 지정한 코드로 실패시킨다 (예: 대표 시나리오 S4).

## 7. 검증 요약

- 등록 명세 출력, Tool 별 정상·실패 호출 23건 모두 예외 없이 형식대로 반환: [verification/tool_checks.md](verification/tool_checks.md) (`python scripts/verify_tools.py`)
- 자동 테스트: `tests/test_tools.py` (실패 형식, 키 없음 시 외부 호출 0회, 예외 변환, 도보 추정 등)
- 외부 API 응답 구조(필드명·타입) 증빙: [api_shapes/](api_shapes/) (값은 약관상 저장하지 않음)
