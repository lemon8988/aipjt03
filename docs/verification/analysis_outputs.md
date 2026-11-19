# 요청 분석 결과 (검증 4.2: 조건 추출, 임의 채움 여부)

실제 LLM 호출 · 기준일 2026-11-20(금) · 값이 `null` 인 조건은 요청에 없어서 비워 둔 것(임의로 채우지 않음)

| 케이스 | 요청 | intent | locs | purpose | date | start | end | prio | t_max | g_max |
|---|---|---|---|---|---|---|---|---|---|---|
| 기준·선호가 모두 있는 요청 | 잠실이랑 건대 사는 둘이 토요일 오후 2시에 데이트, 할 거 많은 곳으로 | `new_meet` | `['잠실', '건대']` | `데이트` | `2026-11-21` | `14:00` | `null` | `things` | `null` | `null` |
| 제약(이동시간 상한)이 있는 요청 | 수원이랑 의정부 사는 둘이 내일 저녁 7시에 만날래, 이동은 60분 넘지 않게 | `new_meet` | `['수원', '의정부']` | `null` | `2026-11-21` | `19:00` | `null` | `travel` | `60` | `null` |
| 날짜·시작 시각이 없는 요청 (임의로 채우면 안 됨) | 송도랑 마석에서 모일 장소 추천해줘 | `new_meet` | `['송도', '마석']` | `null` | `null` | `null` | `null` | `null` | `null` | `null` |
| 종료 시각만 있는 요청 (시작 시각을 지어내면 안 됨) | 잠실에서 7시 전에 끝나야 해 | `edit_cond` | `null` | `null` | `null` | `null` | `19:00` | `null` | `null` | `null` |
| Tool 이 필요 없는 요청 | 안녕 | `chat` | `null` | `null` | `null` | `null` | `null` | `null` | `null` | `null` |

※ 위치·날짜·시각이 부족한 요청은 Agent 가 Tool 을 부르기 전에 되묻는다 (docs/screenshots/10_chat_S7_missing.png, 11_chat_S10_no_invention.png).
