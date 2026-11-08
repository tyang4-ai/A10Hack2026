"""RunState: everything one run knows, as one serializable Pydantic model, plus read helpers."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..contract import m
from ..ids import IdAllocator

PageRanges = list[list[int]]        # [[150, 156], [170, 172]]: 1-based PDF page indices, inclusive


class RunState(BaseModel):
    run: m.Run
    needs: list[m.DataNeed] = []
    sources: list[m.Source] = []
    extract_pages: dict[str, PageRanges] = {}             # source_id -> pages extractors read
    need_pages: dict[str, dict[str, PageRanges]] = {}     # need_id -> source_id -> pages
    policy: m.Policy | None = None
    documents: list[m.Document] = []
    pages: dict[str, list[str]] = {}                      # doc_id -> page texts, page n is index n-1
    items: dict[str, m.Item] = {}                         # item id -> DataItem | Conclusion | Extrapolation | Chart
    holes: list[m.Hole] = []
    agents: dict[str, m.Agent] = {}                       # agent_id -> Agent
    comments: list[m.Comment] = []
    summary: m.HumanReport | None = None
    ids: IdAllocator = Field(default_factory=IdAllocator)
    events: list[m.Event] = []

    def data_items(self) -> list[m.DataItem]:
        raise NotImplementedError("B-store-api")

    def conclusions(self) -> list[m.Conclusion]:
        raise NotImplementedError("B-store-api")

    def extrapolations(self) -> list[m.Extrapolation]:
        raise NotImplementedError("B-store-api")

    def charts(self) -> list[m.Chart]:
        raise NotImplementedError("B-store-api")

    def sources_by_id(self) -> dict[str, m.Source]:
        raise NotImplementedError("B-store-api")

    def plan(self) -> m.Plan:
        raise NotImplementedError("B-store-api")

    def report(self) -> m.AgentReport:
        raise NotImplementedError("B-store-api")

    def agent_pages(self, doc_id: str) -> list[str]:
        """Page texts an agent may read: empty for quarantined documents."""
        raise NotImplementedError("B-store-api")
