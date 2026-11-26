"""FastEmbed BAAI/bge-small-en-v1.5 semantic embeddings for evidence records."""
import hashlib
import logging
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np

from .ingest import Evidence, EvidenceEmbedding

logger = logging.getLogger("forensight.embeddings")

MODEL_NAME = "BAAI/bge-small-en-v1.5"
VECTOR_DIM = 384

_model = None
_model_failed = False


def get_model():
    """Lazily load the FastEmbed model on CPU. Fall back gracefully if loading fails."""
    global _model, _model_failed
    if _model is not None:
        return _model
    if _model_failed:
        return None
    try:
        from fastembed import TextEmbedding
        from pathlib import Path

        cache_path = Path(__file__).resolve().parents[2] / ".cache" / "fastembed"
        cache_dir = str(cache_path) if cache_path.exists() else None

        logger.info("Initializing FastEmbed with model %s on CPU (cache_dir=%s)", MODEL_NAME, cache_dir)
        try:
            if cache_dir:
                _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=cache_dir, local_files_only=True)
            else:
                _model = TextEmbedding(model_name=MODEL_NAME)
        except Exception:
            _model = TextEmbedding(model_name=MODEL_NAME, cache_dir=cache_dir)
        return _model
    except Exception as exc:
        _model_failed = True
        logger.warning(
            "FastEmbed model %s failed to load (%s); falling back to literal search.",
            MODEL_NAME,
            exc,
            exc_info=True,
        )
        return None


def is_available() -> bool:
    """Check if FastEmbed is available and loaded."""
    return get_model() is not None


def evidence_text(e: Evidence) -> str:
    """Create a standardized textual representation of an Evidence record for embedding."""
    parts = []
    if e.type:
        parts.append(f"Type: {e.type}")
    if e.actor and e.target:
        parts.append(f"From {e.actor} to {e.target}")
    elif e.actor:
        parts.append(f"Actor: {e.actor}")
    elif e.target:
        parts.append(f"Target: {e.target}")
    if e.source_file:
        parts.append(f"Source: {e.source_file}")
    if e.summary:
        parts.append(f"Summary: {e.summary}")
    return " | ".join(parts) if parts else (e.summary or "")


def compute_fingerprint(text: str) -> str:
    """Deterministic hash of the text content to detect changes."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def serialize_vector(vector: np.ndarray) -> bytes:
    """Convert float32 vector to raw bytes (384 * 4 = 1536 bytes). Works with SQLite and PostgreSQL."""
    return np.asarray(vector, dtype=np.float32).tobytes()


def deserialize_vector(raw: bytes) -> np.ndarray:
    """Convert raw bytes back to a float32 numpy array."""
    return np.frombuffer(raw, dtype=np.float32)


def sync_case_embeddings(session, case_id: str) -> int:
    """Ensure persistent embeddings exist and are up to date for all evidence in a case.

    Uses text fingerprints to only embed missing or updated records.
    Works seamlessly across SQLite and PostgreSQL.
    """
    model = get_model()
    if model is None or not case_id:
        return 0

    try:
        evidence_records = (
            session.query(Evidence)
            .filter_by(case_id=case_id)
            .order_by(Evidence.id)
            .all()
        )
        if not evidence_records:
            return 0

        existing_rows = (
            session.query(EvidenceEmbedding)
            .filter_by(case_id=case_id)
            .all()
        )
        existing_map = {row.evidence_id: row for row in existing_rows}

        to_embed = []
        current_eids = set()

        for ev in evidence_records:
            current_eids.add(ev.evidence_id)
            text = evidence_text(ev)
            fp = compute_fingerprint(text)
            existing_row = existing_map.get(ev.evidence_id)
            if existing_row is None or existing_row.text_fingerprint != fp:
                to_embed.append((ev, text, fp, existing_row))

        # Delete any orphaned embeddings for evidence that was removed
        orphans = [eid for eid in existing_map if eid not in current_eids]
        if orphans:
            session.query(EvidenceEmbedding).filter(
                EvidenceEmbedding.case_id == case_id,
                EvidenceEmbedding.evidence_id.in_(orphans),
            ).delete(synchronize_session=False)

        if not to_embed:
            return 0

        # Batch embed missing or changed texts
        texts = [item[1] for item in to_embed]
        vectors = list(model.embed(texts))
        now = datetime.utcnow()

        for (ev, text, fp, existing_row), vec in zip(to_embed, vectors):
            raw_bytes = serialize_vector(vec)
            if existing_row:
                existing_row.text_fingerprint = fp
                existing_row.embedding = raw_bytes
                existing_row.updated_at = now
            else:
                session.add(
                    EvidenceEmbedding(
                        case_id=case_id,
                        evidence_id=ev.evidence_id,
                        text_fingerprint=fp,
                        embedding=raw_bytes,
                        updated_at=now,
                    )
                )

        session.commit()
        logger.info(
            "Synchronized %d embeddings for case %s (total evidence: %d)",
            len(to_embed),
            case_id,
            len(evidence_records),
        )
        return len(to_embed)
    except Exception as exc:
        logger.warning("Error synchronizing embeddings for case %s: %s", case_id, exc, exc_info=True)
        session.rollback()
        return 0


def semantic_search(
    session,
    case_id: str,
    query_text: str,
    top_k: int = 20,
    min_similarity: float = 0.65,
    relative_margin: float = 0.15,
) -> List[Tuple[Evidence, float]]:
    """Perform semantic vector search using FastEmbed BAAI/bge-small-en-v1.5.

    Returns a list of (Evidence, similarity_score) sorted descending by similarity.
    Uses an absolute min_similarity cutoff and a relative margin from the top
    score to ensure high precision and prevent noise matches.
    If the model fails to load or error occurs, logs error and returns [].
    """
    model = get_model()
    if model is None or not case_id or not query_text or not query_text.strip():
        return []

    try:
        # Ensure case embeddings are current
        sync_case_embeddings(session, case_id)

        rows = (
            session.query(EvidenceEmbedding, Evidence)
            .join(Evidence, Evidence.evidence_id == EvidenceEmbedding.evidence_id)
            .filter(EvidenceEmbedding.case_id == case_id)
            .all()
        )
        if not rows:
            return []

        # Generate query vector with FastEmbed's query_embed
        query_vec = list(model.query_embed([query_text.strip()]))[0]
        query_vec = np.asarray(query_vec, dtype=np.float32)
        q_norm = np.linalg.norm(query_vec)
        if q_norm == 0:
            return []

        # Build matrix from stored embeddings
        vectors = np.array([deserialize_vector(emb.embedding) for emb, ev in rows], dtype=np.float32)
        v_norms = np.linalg.norm(vectors, axis=1) * q_norm
        v_norms = np.where(v_norms == 0, 1e-9, v_norms)

        # Cosine similarity
        similarities = np.dot(vectors, query_vec) / v_norms

        max_sim = float(np.max(similarities)) if len(similarities) > 0 else 0.0
        effective_threshold = max(min_similarity, max_sim - relative_margin)

        # Filter by threshold and rank
        matches = []
        for idx, sim in enumerate(similarities):
            if sim >= effective_threshold:
                matches.append((rows[idx][1], float(sim)))

        matches.sort(key=lambda item: item[1], reverse=True)
        return matches[:top_k]
    except Exception as exc:
        logger.warning(
            "Semantic search failed for query %r in case %s: %s",
            query_text,
            case_id,
            exc,
            exc_info=True,
        )
        return []
