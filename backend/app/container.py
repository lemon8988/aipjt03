"""의존 객체 조립. 로깅 Agent 는 선택 사항이라, 없어도 메인 Agent 는 동일하게 동작한다."""
from __future__ import annotations

from app.agent.llm import LLMClient
from app.agent.main_agent import MainAgent
from app.agent.memory import MemoryStore
from app.config import settings
from app.evaluation import EvalStore, RunEvaluator
from app.events.bus import EventBus
from app.logging_agent import LogStore, LoggingAgent
from app.store.plans import PlanStore


class Container:
    def __init__(self, llm=None, data_dir=None, logging_enabled: bool | None = None,
                 diagnosis_background: bool = True):
        s = settings()
        data = data_dir or s.data_dir
        self.bus = EventBus()
        self.logging_enabled = (not s.disable_logging) if logging_enabled is None else logging_enabled
        llm = llm or LLMClient()
        self.evals = EvalStore(data / "evals")
        evaluator = RunEvaluator(self.evals, getattr(llm, "diagnose", None), background=diagnosis_background)
        self.logging_agent = LoggingAgent(self.bus, data / "logs", evaluator) if self.logging_enabled else None
        self.log_store = LogStore(data / "logs")
        self.plans = PlanStore(data / "plans.db")
        self.memories = MemoryStore()
        # 메인 Agent 는 직전 실행의 판정을 조회만 한다 (로깅 Agent 구현에는 의존하지 않음)
        self.agent = MainAgent(self.bus, llm, self.memories,
                               eval_lookup=self.evals.latest if self.logging_enabled else None)


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = Container()
    return _container


def set_container(c: Container | None) -> None:
    global _container
    _container = c
