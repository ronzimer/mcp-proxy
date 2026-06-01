from typing import Optional, Dict, Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams,
    Distance,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    Range,
)
from sentence_transformers import SentenceTransformer


def _deep_remove_keys(obj: Any, keys_to_remove: set) -> Any:
    if isinstance(obj, dict):
        return {
            k: _deep_remove_keys(v, keys_to_remove)
            for k, v in obj.items()
            if k not in keys_to_remove
        }
    if isinstance(obj, list):
        return [_deep_remove_keys(x, keys_to_remove) for x in obj]
    return obj


def normalize_tools_call_params(
    params: dict,
    ignore_argument_keys: set,
    default_argument_values: dict,
) -> dict:
    """
    Normalize tools/call params to stabilize semantic text across clients.
    Expected shape:
      { "name": "<tool_name>", "arguments": { ... } }
    """
    tool_name = params.get("name")
    args = params.get("arguments")
    if not isinstance(args, dict):
        args = {}

    if default_argument_values:
        for k, v in default_argument_values.items():
            if k not in args:
                args[k] = v

    args_norm = _deep_remove_keys(args, ignore_argument_keys or set())

    return {"name": tool_name, "arguments": args_norm}


def build_semantic_text(
    server_id: str,
    params: dict,
    normalize: bool = False,
    ignore_argument_keys: Optional[set] = None,
    default_argument_values: Optional[dict] = None,
) -> str:
    """
    Build canonical text for semantic caching.
    Example:
      server=open-meteo | tool=get_weather | city=Beer Sheva | day=today
    """
    if normalize:
        params = normalize_tools_call_params(
            params=params,
            ignore_argument_keys=ignore_argument_keys or set(),
            default_argument_values=default_argument_values or {},
        )

    tool_name = params.get("name", "unknown_tool")
    args = params.get("arguments")
    if not isinstance(args, dict):
        args = {}

    parts = [f"server={server_id}", f"tool={tool_name}"]

    for key in sorted(args.keys()):
        parts.append(f"{key}={args[key]}")

    return " | ".join(parts)


class SemanticCache:
    def __init__(
        self,
        qdrant_url: str = "http://localhost:6333",
        collection_name: str = "semantic_cache",
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        score_threshold: float = 0.85,
    ):
        self.collection_name = collection_name
        self.score_threshold = score_threshold

        self.client = QdrantClient(url=qdrant_url)
        self.model = SentenceTransformer(model_name)

        self._ensure_collection()

    def _ensure_collection(self) -> None:
        collections = self.client.get_collections().collections
        exists = any(c.name == self.collection_name for c in collections)

        if not exists:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=384, distance=Distance.COSINE),
            )

    def clear(self) -> None:
        """
        Clear all semantic-cache entries by recreating the Qdrant collection.

        This is useful for experiment isolation, when we want a truly cold
        semantic-cache run and not just a cleared SQLite exact cache.
        """
        collections = self.client.get_collections().collections
        exists = any(c.name == self.collection_name for c in collections)

        if exists:
            self.client.delete_collection(collection_name=self.collection_name)

        self._ensure_collection()

    def cleanup_expired(self, now_ts: float, batch_size: int = 256) -> int:
        """
        Delete expired semantic-cache entries from Qdrant.

        SQLite exact-cache entries are physically removed by the proxy GC loop.
        Qdrant entries also need physical cleanup; otherwise old vectors are
        ignored at search time but still remain in the collection forever.
        """
        deleted_total = 0

        while True:
            points, _next_offset = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(
                            key="expires_at",
                            range=Range(lte=now_ts),
                        )
                    ]
                ),
                limit=batch_size,
                with_payload=False,
                with_vectors=False,
            )

            point_ids = [p.id for p in points]
            if not point_ids:
                break

            self.client.delete(
                collection_name=self.collection_name,
                points_selector=point_ids,
            )
            deleted_total += len(point_ids)

            if len(point_ids) < batch_size:
                break

        return deleted_total

    def _embed(self, text: str) -> list[float]:
        return self.model.encode(text, normalize_embeddings=True).tolist()

    def search(
        self,
        server_id: str,
        tool_name: str,
        text: str,
        now_ts: float,
    ) -> Optional[Dict[str, Any]]:
        vector = self._embed(text)

        results = self.client.query_points(
            collection_name=self.collection_name,
            query=vector,
            query_filter=Filter(
                must=[
                    FieldCondition(key="server_id", match=MatchValue(value=server_id)),
                    FieldCondition(key="tool_name", match=MatchValue(value=tool_name)),
                ]
            ),
            limit=3,
            with_payload=True,
        )

        if not results.points:
            return None

        for point in results.points:
            if point.score < self.score_threshold:
                continue

            payload = point.payload or {}
            expires_at = payload.get("expires_at")

            if expires_at is None:
                continue

            try:
                expires_at = float(expires_at)
            except (TypeError, ValueError):
                continue

            if expires_at <= now_ts:
                continue

            return {
                "score": point.score,
                "text": payload.get("text"),
                "response": payload.get("response"),
                "created_at": payload.get("created_at"),
                "expires_at": expires_at,
            }

        return None

    def store(
        self,
        point_id: int,
        server_id: str,
        tool_name: str,
        text: str,
        response: dict,
        created_at: float,
        expires_at: float,
    ) -> None:
        vector = self._embed(text)

        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "server_id": server_id,
                        "tool_name": tool_name,
                        "text": text,
                        "response": response,
                        "created_at": created_at,
                        "expires_at": expires_at,
                    },
                )
            ],
        )