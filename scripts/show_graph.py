"""LangGraph 그래프 구조 출력 (검증 4.2, 4.1 Tool 명세 목록 포함).

실행: python scripts/show_graph.py
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(errors="replace")  # cp949 콘솔에서 특수문자로 죽지 않게

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.agent.llm import LLMClient  # noqa: E402
from app.container import Container  # noqa: E402
from app.tools import list_specs  # noqa: E402

if __name__ == "__main__":
    c = Container(llm=LLMClient(), logging_enabled=False)
    print("=== 메인 Agent 그래프 (Mermaid) ===")
    print(c.agent.graph_mermaid())
    print("\n=== 등록된 Tool 명세 (이름 · 설명 · 입력 스키마) ===")
    for s in list_specs():
        print(f"- {s['name']} [{s['kind']}] {s['description']}")
        print("  입력:", json.dumps(s["input_schema"], ensure_ascii=False))
