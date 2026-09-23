from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from django.test import TestCase

from ai_orchestrator.models import DocumentChunk
from ai_orchestrator.services.embedding_processor import EmbeddingService
from ai_orchestrator.services.industry_deal_comparison import IndustryDealComparisonService
from ai_orchestrator.services.pipeline_registry import PipelineRegistryService
from ai_orchestrator.services.report_section_evidence import ICReportSectionEvidenceService
from deals.models import Deal, DealDocument, DealRelationshipContext


class IndustryDealComparisonTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        PipelineRegistryService.ensure_report_pipeline_defaults()
        cls.target = Deal.objects.create(title="Target Foods", sector="Food")
        cls.competitor = Deal.objects.create(title="Rival Foods", sector="Food")
        cls.peer = Deal.objects.create(title="Peer Foods", sector="Food")
        cls.unrelated = Deal.objects.create(title="Unrelated Labs", sector="Software")
        DealRelationshipContext.objects.create(
            deal=cls.target,
            related_deal=cls.competitor,
            relationship_type=DealRelationshipContext.RelationshipType.COMPETITOR,
        )
        cls.target_doc = DealDocument.objects.create(deal=cls.target, title="Target deck")
        cls.competitor_doc = DealDocument.objects.create(deal=cls.competitor, title="Rival deck")
        cls.peer_doc = DealDocument.objects.create(deal=cls.peer, title="Peer deck")
        cls.unrelated_doc = DealDocument.objects.create(deal=cls.unrelated, title="Unrelated deck")
        cls.target_chunk = DocumentChunk.objects.create(
            deal=cls.target, source_type="document", source_id=str(cls.target_doc.id),
            content="Target sells packaged foods to supermarkets.",
        )
        cls.competitor_chunk = DocumentChunk.objects.create(
            deal=cls.competitor, source_type="document", source_id=str(cls.competitor_doc.id),
            content="Rival sells packaged foods to supermarkets in FY25 through a national distributor network and competes for the same retail shelf space.",
        )
        cls.peer_chunk = DocumentChunk.objects.create(
            deal=cls.peer, source_type="document", source_id=str(cls.peer_doc.id),
            content="Peer sells packaged foods through distributors in FY25 and serves grocery chains with a comparable customer buying process.",
        )
        cls.unrelated_chunk = DocumentChunk.objects.create(
            deal=cls.unrelated, source_type="document", source_id=str(cls.unrelated_doc.id),
            content="Unrelated software revenue was INR 20 crore, generated from enterprise cloud subscriptions rather than consumer food distribution.",
        )

    def test_comparison_scopes_and_labels_ranked_source_chunks(self):
        embedding = MagicMock()
        embedding.search_global_chunks.return_value = [
            self.competitor_chunk, self.peer_chunk, self.unrelated_chunk, self.target_chunk,
        ]

        result = IndustryDealComparisonService(
            deal=self.target, embedding_service=embedding
        ).retrieve("market rivals")

        self.assertEqual(result["chunks"], [self.competitor_chunk, self.peer_chunk, self.unrelated_chunk])
        self.assertEqual(result["candidate_deal_count"], 3)
        self.assertEqual(
            result["source_info"][str(self.competitor_doc.id)]["relationship"],
            "linked competitor",
        )
        self.assertEqual(
            result["source_info"][str(self.peer_doc.id)]["relationship"],
            "semantic peer candidate",
        )
        search = embedding.search_global_chunks.call_args_list[0]
        self.assertFalse(search.kwargs["rerank"])
        self.assertEqual(search.kwargs["source_types"], ["document"])
        self.assertEqual(search.kwargs["exclude_deal_ids"], [str(self.target.id)])
        self.assertNotIn("deal_ids", search.kwargs)
        self.assertNotIn("source_ids", search.kwargs)

    def test_industry_context_cites_peer_separately_from_target(self):
        embedding = MagicMock()
        embedding.search_global_chunks.side_effect = [
            [self.target_chunk], [self.competitor_chunk, self.peer_chunk],
        ]

        result = ICReportSectionEvidenceService(
            deal=self.target, documents=[self.target_doc], embedding_service=embedding,
            max_tokens=4_000,
        ).retrieve("Industry Overview")

        self.assertIn("Internal database comparison evidence", result["context"])
        self.assertIn("Peer company: Rival Foods", result["context"])
        self.assertIn("semantic peer candidate", result["context"])
        self.assertEqual(result["citations"]["1"]["title"], "Target deck")
        self.assertEqual(result["citations"]["2"]["title"], "Rival Foods: Rival deck")
        self.assertEqual(result["metadata"]["our_deal_comparison"]["selected_chunk_count"], 2)

    def test_comparison_reranks_merged_pool_in_vm_sized_batches(self):
        embedding = MagicMock()
        embedding.reranker_model = "bge-reranker"
        embedding.search_global_chunks.side_effect = [
            [self.competitor_chunk, self.peer_chunk],
            [self.peer_chunk],
            [self.competitor_chunk],
        ]
        embedding.reranker.rerank.return_value = [
            {"index": 0, "score": 0.2}, {"index": 1, "score": 0.9},
        ]

        result = IndustryDealComparisonService(
            deal=self.target, embedding_service=embedding,
        ).retrieve("market rivals")

        self.assertEqual(result["chunks"], [self.peer_chunk, self.competitor_chunk])
        self.assertEqual(embedding.reranker.rerank.call_count, 1)
        self.assertEqual(len(embedding.reranker.rerank.call_args.kwargs["documents"]), 2)
        self.assertTrue(all(
            call.kwargs["rerank"] is False
            for call in embedding.search_global_chunks.call_args_list
        ))

    def test_comparison_never_exceeds_vm_batch_size(self):
        embedding = MagicMock()
        embedding.reranker_model = "bge-reranker"
        embedding.reranker.rerank.side_effect = [
            [{"index": index, "score": float(index)} for index in range(32)],
            [{"index": 0, "score": 100.0}],
        ]
        items = [
            {"chunk": SimpleNamespace(id=uuid4(), content=f"Evidence {index}"), "score": 1 / (index + 1)}
            for index in range(33)
        ]
        last_item = items[-1]

        ranked = IndustryDealComparisonService(
            deal=self.target, embedding_service=embedding,
        )._rerank(items, "compare companies")

        self.assertEqual([len(call.kwargs["documents"]) for call in embedding.reranker.rerank.call_args_list], [32, 1])
        self.assertEqual(ranked[0], last_item)

    def test_financial_evidence_is_retrieved_globally_without_peer_scoping(self):
        financial_chunk = DocumentChunk.objects.create(
            deal=self.competitor, source_type="document", source_id=str(self.competitor_doc.id),
            content=("Financial model historic actuals and projections. FY21 revenue was INR 12 crore, "
                     "EBITDA was INR 1.1 crore and PAT was INR 0.66 crore, per page 30."),
            metadata={"chunk_kind": "normalized_text", "chunk_index": 900},
        )
        embedding = MagicMock()
        calls = {"global": 0}

        def search(query, *, limit, rerank, **filters):
            self.assertFalse(rerank)
            if filters.get("deal_ids"):
                return [self.competitor_chunk]
            calls["global"] += 1
            if "source tables" in query:
                return [financial_chunk]
            return [self.competitor_chunk, self.peer_chunk]

        embedding.search_global_chunks.side_effect = search
        result = IndustryDealComparisonService(
            deal=self.target, embedding_service=embedding,
        ).retrieve("market rivals")

        self.assertIn(financial_chunk, result["chunks"])
        self.assertEqual(result["source_info"][str(self.competitor_doc.id)]["relationship"], "linked competitor")
        self.assertEqual(calls["global"], 3)

    def test_no_peer_evidence_keeps_industry_section_available(self):
        embedding = MagicMock()
        embedding.search_global_chunks.side_effect = [[self.target_chunk], []]

        result = ICReportSectionEvidenceService(
            deal=self.target, documents=[self.target_doc], embedding_service=embedding,
        ).retrieve("Industry Overview")

        self.assertIn("Target sells packaged foods", result["context"])
        self.assertEqual(result["metadata"]["our_deal_comparison"]["selected_chunk_count"], 0)

    def test_reverse_competitor_link_is_discovered(self):
        self.competitor.sector = "Retail"
        self.competitor.save(update_fields=["sector"])
        DealRelationshipContext.objects.filter(deal=self.target).delete()
        DealRelationshipContext.objects.create(
            deal=self.competitor,
            related_deal=self.target,
            relationship_type=DealRelationshipContext.RelationshipType.COMPETITOR,
        )
        embedding = MagicMock()
        embedding.search_global_chunks.return_value = [self.competitor_chunk]

        result = IndustryDealComparisonService(
            deal=self.target, embedding_service=embedding
        ).retrieve("competitive positioning")

        self.assertEqual(result["chunks"], [self.competitor_chunk])
        self.assertEqual(
            result["source_info"][str(self.competitor_doc.id)]["relationship"],
            "linked competitor",
        )

    def test_global_search_filters_target_before_ranking(self):
        vector = [0.1] * 1024
        DocumentChunk.objects.filter(id__in=[
            self.target_chunk.id, self.competitor_chunk.id, self.peer_chunk.id,
        ]).update(embedding=vector)
        service = EmbeddingService.__new__(EmbeddingService)
        service._get_embedding = MagicMock(return_value=vector)

        results = service.search_global_chunks(
            "packaged foods supermarkets", limit=10, rerank=False,
            source_types=["document"], exclude_deal_ids=[str(self.target.id)],
        )

        self.assertEqual(
            {chunk.id for chunk in results},
            {self.competitor_chunk.id, self.peer_chunk.id},
        )

    def test_business_metric_survives_but_legal_boilerplate_does_not(self):
        service = IndustryDealComparisonService
        self.assertTrue(service._substantive(
            SimpleNamespace(content="FY25 revenue grew 35% to INR 120 crore."), "Peer deck.pdf",
        ))
        self.assertFalse(service._substantive(
            SimpleNamespace(content="Permitted uses include adapting information for internal reports and analyses."),
            "NDA Peer.pdf",
        ))
