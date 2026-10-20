import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import posthog
import app.services.embeddings
import app.services.reranker
import app.services.vectorstore


class _FakeChroma:
    def first_chunks(self, file_id=None, limit=5):
        return []

    def count(self):
        return 0


class _FakeEmbeddings:
    def embed_texts(self, texts):
        return [[0.1, 0.2] for _ in texts]


class _FakeReranker:
    def rerank(self, query, candidates, top_n=5):
        return []


with (
    patch("app.services.vectorstore.ChromaClient", _FakeChroma),
    patch("app.services.embeddings.EmbeddingClient", _FakeEmbeddings),
    patch("app.services.reranker.CohereReranker", _FakeReranker),
):
    from app.routes import chat


class ChatRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.request = lambda query: SimpleNamespace(
            query=query, file_id="file-1", top_k=5, use_vector=True, use_es=True
        )
        self.overview_chunks = [
            {"id": f"overview-{i}", "text": f"Overview chunk {i}", "meta": {"page": 0}}
            for i in range(1, 4)
        ]

    def test_passive_merge_does_not_add_overview_scores(self):
        merged = chat._merge_overview_and_candidates(
            self.overview_chunks,
            [{"id": "raw", "text": "raw candidate", "score": 0.02}],
            limit=4,
            use_overview_scores=False,
        )

        self.assertNotIn("rerank_score", merged[0])
        self.assertNotIn("rerank_score", merged[-1])
        self.assertEqual(merged[-1]["score"], 0.02)

    def test_summary_route_keeps_ordered_overview_scores_and_sources(self):
        with (
            patch.object(chat.emb_client, "embed_texts") as embed,
            patch.object(chat.reranker, "rerank") as rerank,
            patch.object(chat, "_es_search", return_value=[]),
            patch.object(chat.chroma, "first_chunks", return_value=self.overview_chunks) as first_chunks,
        ):
            selected, sources = chat.retrieve_and_rank(self.request("总结一下"))

        embed.assert_not_called()
        rerank.assert_not_called()
        first_chunks.assert_called_once_with("file-1", limit=8)
        self.assertEqual([source.relevance_score for source in sources], [0.98, 0.95])
        self.assertEqual(len(sources), 2)
        self.assertEqual(selected[0]["rerank_score"], 0.98)

    def test_async_pure_summary_skips_embedding_and_rerank(self):
        async def inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        async def run():
            with (
                patch.object(chat.emb_client, "embed_texts") as embed,
                patch.object(chat.reranker, "rerank") as rerank,
                patch.object(chat, "_es_search", return_value=[]),
                patch.object(chat.chroma, "first_chunks", return_value=self.overview_chunks) as first_chunks,
                patch.object(chat.asyncio, "to_thread", side_effect=inline_to_thread),
            ):
                result = await chat.retrieve_and_rank_async(self.request("总结一下"))
                embed.assert_not_called()
                rerank.assert_not_called()
                first_chunks.assert_called_once_with("file-1", 8)
                return result

        selected, sources = asyncio.run(run())
        self.assertEqual([source.relevance_score for source in sources], [0.98, 0.95])
        self.assertEqual(len(selected), 3)

    def test_author_header_moves_after_summary_content_and_gets_lower_score(self):
        author_header = {
            "id": "authors",
            "text": "HugoTouvron∗ LouisMartin† KevinStone‡",
            "meta": {"page": 0},
        }
        abstract = {
            "id": "abstract",
            "text": "Abstract We introduce a language model and describe its evaluation.",
            "meta": {"page": 0},
        }
        intro = {"id": "intro", "text": "Introduction: the work addresses a key challenge."}

        self.assertTrue(chat._is_author_header_chunk(author_header))
        self.assertFalse(chat._is_author_header_chunk(abstract))
        merged = chat._merge_overview_and_candidates(
            [author_header, abstract, intro], [], limit=3
        )

        self.assertEqual([doc["id"] for doc in merged], ["abstract", "intro", "authors"])
        self.assertEqual(merged[0]["rerank_score"], 0.98)
        self.assertEqual(merged[2]["rerank_score"], 0.50)

    def test_toc_filter_author_order_and_clean_preview(self):
        toc = {"id": "toc", "text": "Contents 1 Introduction 3 . . . 5"}
        author_abstract = {
            "id": "author-abstract",
            "text": "HugoTouvron∗ LouisMartin† KevinStone‡ " + ("authors " * 35)
            + "Abstract Llama 2 is a collection of pretrained language models.",
        }
        clean_abstract = {
            "id": "clean-abstract",
            "text": "Llama 2, a collection of pretrained language models for dialogue.",
        }

        self.assertTrue(chat._is_toc_chunk(toc))
        self.assertTrue(chat._is_author_header_chunk(author_abstract))
        merged = chat._merge_overview_and_candidates(
            [toc, author_abstract, clean_abstract], [], limit=3
        )
        self.assertEqual([doc["id"] for doc in merged], ["clean-abstract", "author-abstract"])
        sources = chat.format_source_docs(merged, top_n=2, apply_threshold=False)
        author_source = next(source for source in sources if source.doc_id == "author-abstract")
        self.assertTrue(author_source.text.startswith("Abstract"))

    def test_toc_only_overview_is_preserved_as_last_resort(self):
        toc = {"id": "toc", "text": "Contents 1 Introduction 3 . . . 5"}
        merged = chat._merge_overview_and_candidates([toc], [], limit=2)
        self.assertEqual([doc["id"] for doc in merged], ["toc"])

    def test_evaluation_sources_lead_with_query_match_and_differ_from_summary(self):
        author_header = {
            "id": "authors",
            "text": "HugoTouvron∗ LouisMartin† KevinStone‡",
            "meta": {"page": 0},
        }
        abstract = {
            "id": "abstract",
            "text": "Abstract We introduce a language model and describe its evaluation.",
            "meta": {"page": 0},
        }
        intro = {"id": "intro", "text": "Introduction: the work addresses a key challenge."}
        conclusion = {
            "id": "conclusion",
            "text": "The results show improved instruction following and safety.",
            "rerank_score": 0.91,
        }
        finalizations = [([], [], [conclusion])]

        with (
            patch.object(chat.emb_client, "embed_texts", return_value=[[0.1, 0.2]]),
            patch.object(chat, "_vector_search", return_value=[]),
            patch.object(chat, "_es_search", return_value=[]),
            patch.object(chat, "_merge_candidates", return_value=[conclusion]),
            patch.object(chat, "_finalize_ranking", side_effect=finalizations),
            patch.object(chat.chroma, "first_chunks", return_value=[author_header, abstract, intro]),
        ):
            _, summary_sources = chat.retrieve_and_rank(self.request("总结一下这个文档"))
            _, evaluation_sources = chat.retrieve_and_rank(
                self.request("你认为这个文档有没有价值")
            )

        self.assertEqual([source.doc_id for source in summary_sources], ["abstract", "intro"])
        self.assertEqual([source.doc_id for source in evaluation_sources], ["conclusion", "abstract"])
        self.assertNotEqual(
            [source.doc_id for source in summary_sources],
            [source.doc_id for source in evaluation_sources],
        )

    def test_passive_sync_fallback_keeps_context_but_returns_no_weak_sources(self):
        candidates = [{"id": "raw", "text": "raw candidate", "score": 0.02}]
        with (
            patch.object(chat.emb_client, "embed_texts", return_value=[[0.1, 0.2]]),
            patch.object(chat, "_vector_search", return_value=[]),
            patch.object(chat, "_es_search", return_value=[]),
            patch.object(chat, "_merge_candidates", return_value=candidates),
            patch.object(chat, "_finalize_ranking", return_value=([], [], candidates)),
            patch.object(chat.chroma, "first_chunks", return_value=self.overview_chunks[:2]),
        ):
            selected, sources = chat.retrieve_and_rank(self.request("谈谈它的影响"))

        self.assertTrue(selected)
        self.assertFalse(any(doc.get("rerank_score") in (0.98, 0.95) for doc in selected))
        self.assertEqual(sources, [])

    def test_passive_async_fallback_matches_sync_scoring_policy(self):
        candidates = [{"id": "raw", "text": "raw candidate", "score": 0.02}]

        async def inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        async def run():
            with (
                patch.object(chat, "_vector_search", return_value=[]),
                patch.object(chat, "_es_search", return_value=[]),
                patch.object(chat, "_merge_candidates", return_value=candidates),
                patch.object(chat.chroma, "first_chunks", return_value=self.overview_chunks[:2]),
                patch.object(chat.asyncio, "to_thread", side_effect=inline_to_thread),
            ):
                return await chat.retrieve_and_rank_async(self.request("谈谈它的影响"))

        selected, sources = asyncio.run(run())
        self.assertTrue(selected)
        self.assertFalse(any(doc.get("rerank_score") in (0.98, 0.95) for doc in selected))
        self.assertEqual(sources, [])

    def test_source_formatting_treats_none_scores_as_zero(self):
        sources = chat.format_source_docs(
            [{"id": "1", "text": "abc", "rerank_score": None, "score": None}],
            apply_threshold=False,
        )

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].relevance_score, 0.0)

    def test_content_analysis_phrases_are_not_persona_chitchat(self):
        self.assertFalse(chat.is_chitchat_or_persona_query("你觉得摘要部分说明了什么？"))
        self.assertFalse(chat.is_chitchat_or_persona_query("你怎么看结论？"))
        self.assertTrue(chat.is_chitchat_or_persona_query("请介绍一下你自己"))

    def test_sync_embedding_failure_falls_back_to_es_and_first_chunks(self):
        request = self.request("介绍一下llama2的参数")
        with (
            patch.object(
                chat.emb_client,
                "embed_texts",
                side_effect=RuntimeError("Server disconnected without sending a response."),
            ),
            patch.object(chat, "_vector_search") as vector_search,
            patch.object(chat, "_es_search", return_value=[]) as es_search,
            patch.object(chat, "_merge_candidates", return_value=[]),
            patch.object(chat, "_finalize_ranking", return_value=([], [], [])),
            patch.object(chat.chroma, "first_chunks", return_value=self.overview_chunks[:2]),
        ):
            selected, sources = chat.retrieve_and_rank(request)

        vector_search.assert_not_called()
        es_search.assert_called_once_with(request)
        self.assertEqual(len(selected), 2)
        self.assertEqual(sources, [])

    def test_async_embedding_failure_falls_back_to_es_and_first_chunks(self):
        request = self.request("介绍一下llama2的参数")

        async def run():
            with (
                patch.object(
                    chat.emb_client,
                    "embed_texts",
                    side_effect=RuntimeError("Server disconnected without sending a response."),
                ),
                patch.object(chat, "_vector_search") as vector_search,
                patch.object(chat, "_es_search", return_value=[]) as es_search,
                patch.object(chat, "_merge_candidates", return_value=[]),
                patch.object(chat.chroma, "first_chunks", return_value=self.overview_chunks[:2]),
            ):
                result = await chat.retrieve_and_rank_async(request)
                return result, vector_search, es_search

        (selected, sources), vector_search, es_search = asyncio.run(run())
        vector_search.assert_not_called()
        es_search.assert_called_once_with(request)
        self.assertEqual(len(selected), 2)
        self.assertEqual(sources, [])


class EmbeddingRetryTests(unittest.TestCase):
    def test_chroma_posthog_telemetry_is_disabled(self):
        self.assertTrue(posthog.disabled)
        posthog.capture("test-event", {})

    def test_chroma_client_explicitly_disables_anonymized_telemetry(self):
        class _Collection:
            def count(self):
                return 0

        class _Client:
            def get_or_create_collection(self, name):
                return _Collection()

        with patch.object(
            app.services.vectorstore, "PersistentClient", return_value=_Client()
        ) as persistent_client:
            app.services.vectorstore.ChromaClient()

        settings = persistent_client.call_args.kwargs["settings"]
        self.assertFalse(settings.anonymized_telemetry)

    def test_embedding_http_pool_reuses_connections_with_bounded_timeouts(self):
        http_client = app.services.embeddings.EmbeddingClient._create_http_client()
        try:
            pool = http_client._transport._pool
            self.assertEqual(pool._max_keepalive_connections, 5)
            self.assertEqual(pool._max_connections, 10)
            self.assertEqual(pool._keepalive_expiry, 60.0)
            self.assertEqual(http_client.timeout.connect, 6.0)
            self.assertEqual(http_client.timeout.read, 6.0)
            self.assertEqual(http_client.timeout.write, 6.0)
            self.assertEqual(http_client.timeout.pool, 5.0)
        finally:
            http_client.close()

    def test_embed_texts_retries_remote_protocol_error_then_succeeds(self):
        class _TransientThenSuccess:
            calls = 0

            def embed(self, **kwargs):
                self.calls += 1
                self.asserted_retries = kwargs["request_options"]["max_retries"]
                self.asserted_timeout = kwargs["request_options"].get("timeout_in_seconds")
                if self.calls == 1:
                    raise httpx.RemoteProtocolError("Server disconnected without sending a response")
                return SimpleNamespace(embeddings=[[0.1, 0.2]])

        client = object.__new__(app.services.embeddings.EmbeddingClient)
        client.provider = "cohere"
        client.model = "test-model"
        client.client = _TransientThenSuccess()

        with patch("app.services.embeddings.time.sleep") as sleep:
            embeddings = client.embed_texts(["query"])

        self.assertEqual(embeddings, [[0.1, 0.2]])
        self.assertEqual(client.client.calls, 2)
        self.assertEqual(client.client.asserted_retries, 0)
        self.assertIsNone(client.client.asserted_timeout)
        sleep.assert_called_once_with(0.2)

    def test_batch_embedding_gets_extended_request_timeout(self):
        class _Success:
            request_options = None

            def embed(self, **kwargs):
                self.request_options = kwargs["request_options"]
                return SimpleNamespace(embeddings=[[0.1, 0.2], [0.3, 0.4]])

        client = object.__new__(app.services.embeddings.EmbeddingClient)
        client.provider = "cohere"
        client.model = "test-model"
        client.client = _Success()

        self.assertEqual(len(client.embed_texts(["one", "two"])), 2)
        self.assertEqual(client.client.request_options["timeout_in_seconds"], 15.0)

    def test_embed_texts_stops_after_one_manual_retry(self):
        class _AlwaysDisconnected:
            calls = 0

            def embed(self, **kwargs):
                self.calls += 1
                raise httpx.RemoteProtocolError("Server disconnected without sending a response")

        client = object.__new__(app.services.embeddings.EmbeddingClient)
        client.provider = "cohere"
        client.model = "test-model"
        client.client = _AlwaysDisconnected()

        with patch("app.services.embeddings.time.sleep") as sleep:
            with self.assertRaises(httpx.RemoteProtocolError):
                client.embed_texts(["query"])

        self.assertEqual(client.client.calls, 2)
        self.assertEqual(sleep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
