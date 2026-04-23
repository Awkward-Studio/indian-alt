import os
from pprint import pprint

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from unittest.mock import MagicMock

from ai_orchestrator.services.flow_config import UniversalChatFlowService
from ai_orchestrator.services.universal_chat import UniversalChatService


QUESTIONS = [
    "Tell me about Man Matters funding ask and repeat behavior",
    "Which consumer or wellness deals mention a funding ask and strong repeat customer behavior?",
    "Give me 3 deals with funding ask and strong repeat customer behavior",
    "Find 3 fintech or payments deals with meaningful revenue or ARR metrics",
    "What are the main themes in the microfinance industry across our deal pipeline?",
    "List 3 consumer brands with strong repeat business",
]


def print_header(title: str):
    print("\n" + "=" * 96)
    print(title)
    print("=" * 96)


def stage_header(title: str):
    print(f"\n[{title}]")


def summarize_deals(deals, limit: int = 8):
    print(f"count={len(deals)}")
    for idx, deal in enumerate(deals[:limit], 1):
        print(
            {
                "rank": idx,
                "title": deal.title,
                "industry": deal.industry,
                "sector": deal.sector,
                "funding_ask": deal.funding_ask,
                "retrieval_score": getattr(deal, "_retrieval_score", None),
                "deal_rerank": (getattr(deal, "_retrieval_components", None) or {}).get("deal_rerank"),
                "components": getattr(deal, "_retrieval_components", None),
            }
        )


def summarize_chunks(chunks, service: UniversalChatService, limit: int = 8):
    serialized_chunks = [service._serialize_chunk(item) for item in chunks[:limit]]
    print(f"count={len(chunks)}")
    for idx, chunk in enumerate(serialized_chunks, 1):
        print(
            {
                "rank": idx,
                "deal": chunk.get("deal"),
                "source_type": chunk.get("source_type"),
                "source_title": chunk.get("source_title"),
                "score": chunk.get("score"),
                "document_summary": (chunk.get("document_metadata") or {}).get("document_summary"),
                "metric_names": (chunk.get("document_metadata") or {}).get("metric_names"),
                "preview": (chunk.get("text") or "").replace("\n", " ")[:260],
            }
        )


def run():
    service = UniversalChatService(
        MagicMock(),
        flow_config=UniversalChatFlowService.build_default_config(),
        flow_version=None,
    )

    for query in QUESTIONS:
        print_header(f"QUERY: {query}")
        plan = service._heuristic_plan(query)

        stage_header("PLAN")
        pprint(plan)

        stage_header("DEAL PROFILE SEARCH")
        semantic_matches = service.embed_service.search_deal_profiles(
            " | ".join(plan.get("rag_queries") or [plan["user_query"]]),
            limit=max(plan.get("deal_limit", 8) * 3, 24),
            filters=plan.get("deal_filters", {}),
        )
        summarize_deals(semantic_matches)

        stage_header("DEAL SELECTION")
        deals = service._get_candidate_deals(plan)
        summarize_deals(deals)

        stage_header("CHUNK SEARCH")
        chunks, diagnostics = service._search_ranked_chunks(plan, deals)
        summarize_chunks(chunks, service)

        stage_header("DIAGNOSTICS")
        pprint(diagnostics)


if __name__ == "__main__":
    run()
