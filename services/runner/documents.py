"""Job-scoped document storage (SPEC.md Sections 10, 11).

Real PDF/email parsing is Gemini's job in the deployed system (Cloud
Storage + the Gen AI SDK's document understanding). This module only holds
already-extracted per-page text so the rest of Phase 3 — citation
resolution, budget enforcement, ownership binding — can be built and tested
without a live document-understanding pipeline. Swap ``register_document``'s
caller for a real ingestion step later; nothing downstream needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.shared.store import Store

DOCUMENTS = "documents"

MAX_PAGES_PER_DOCUMENT = 10
MAX_BYTES_PER_DOCUMENT = 5 * 1024 * 1024


class DocumentError(Exception):
    pass


@dataclass
class DocumentInput:
    document_id: str
    owner_uid: str
    media_type: str
    pages: tuple[str, ...]  # already-extracted per-page text
    size_bytes: int


def register_document(store: Store, doc: DocumentInput) -> dict:
    if len(doc.pages) == 0:
        raise DocumentError("a document must have at least one page")
    if len(doc.pages) > MAX_PAGES_PER_DOCUMENT:
        raise DocumentError(f"document exceeds the {MAX_PAGES_PER_DOCUMENT}-page limit")
    if doc.size_bytes > MAX_BYTES_PER_DOCUMENT:
        raise DocumentError(f"document exceeds the {MAX_BYTES_PER_DOCUMENT}-byte limit")

    def txn(t):
        existing = t.get(DOCUMENTS, doc.document_id)
        if existing is not None:
            raise DocumentError(f"document {doc.document_id} already exists")
        record = {
            "document_id": doc.document_id,
            "owner_uid": doc.owner_uid,
            "media_type": doc.media_type,
            "page_count": len(doc.pages),
            "pages": list(doc.pages),
            "size_bytes": doc.size_bytes,
        }
        t.set(DOCUMENTS, doc.document_id, record)
        return record

    return store.run_transaction(txn)


def get_document_for_job(store: Store, *, job_id: str, owner_uid: str, document_id: str) -> dict:
    """Ownership- and job-assignment-bound read (SPEC.md Section 4: "Context
    cannot cross owners or assignments"). Never resolves a document that
    isn't both owned by this caller *and* explicitly listed on this job."""
    job = store.peek("jobs", job_id)
    if job is None or job["owner_uid"] != owner_uid:
        raise DocumentError("job not found or not owned")
    if document_id not in job.get("allowed_documents", ()):
        raise DocumentError(f"document {document_id!r} is not assigned to job {job_id!r}")

    document = store.peek(DOCUMENTS, document_id)
    if document is None or document["owner_uid"] != owner_uid:
        raise DocumentError("document not found or not owned")
    return document
