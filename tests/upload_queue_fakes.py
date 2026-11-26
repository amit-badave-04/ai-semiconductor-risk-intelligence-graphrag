"""Shared doubles for the upload job tests after the keyword-ready split (R15, R16): a job now ends at ``keyword_ready`` and its
chunks wait in the embedding queue, so a test that wants the whole story drains the queue on its own thread (``drain``).

``install`` wraps a fake repo's ``put_version`` (it must have ``put_version``, ``embedded``, ``workspace_tokens``) so the chunks it
stores can be read back by the queue through fakes of ``repo.pending_chunks``, ``set_chunk_embedding`` and ``finish_embedding``,
and makes the day's embedding-job counter always say yes (``serve.store`` is never called). No database, no thread, no network.
"""

from __future__ import annotations

from semigraph.serve import store
from semigraph.uploads import jobs, repo


class PendingStore:
    def __init__(self, fake_repo) -> None:
        self.repo = fake_repo
        self.chunks: dict[tuple[str, int], list[dict]] = {}
        self.writes: list[tuple[str, int]] = []
        self.finished: list[tuple[str, int]] = []
        self.day_reservations = 0
        self.day_allowed = True

    def put_version(self, original):
        def wrapper(driver, ws, **kw):
            original(driver, ws, **kw)
            self.chunks[(kw["document_id"], kw["version"])] = [
                {"chunk_id": c["chunk_id"], "seq": c["seq"], "text": c["text"], "tokens": c["tokens"], "text_hash": c["text_hash"],
                 "priority": c.get("priority", 99), "state": "pending" if c.get("embedding") is None else "done"}
                for c in kw["chunks"]]
        return wrapper

    def pending_chunks(self, driver, ws, document_id, version):
        rows = [c for c in self.chunks.get((document_id, version), []) if c["state"] == "pending"]
        return [dict(c) for c in sorted(rows, key=lambda c: (c["priority"], c["seq"]))]

    def set_chunk_embedding(self, driver, ws, chunk_id, embedding, tokens):
        for (doc, version), rows in self.chunks.items():
            for c in rows:
                if c["chunk_id"] == chunk_id and c["state"] == "pending":
                    c["state"] = "done"
                    self.repo.workspace_tokens += tokens
                    self.repo.embedded.setdefault(doc, {})[c["text_hash"]] = list(embedding)
                    self.writes.append((doc, version))
                    return True
        return False

    def finish_embedding(self, driver, ws, document_id, version):
        self.finished.append((document_id, version))
        skipped = 0
        for c in self.chunks.get((document_id, version), []):
            if c["state"] == "pending":
                c["state"] = "skipped"
                skipped += 1
        return skipped

    def reserve_day(self, driver, limit, day):
        self.day_reservations += 1
        return self.day_allowed


def install(monkeypatch, fake_repo) -> PendingStore:
    pending = PendingStore(fake_repo)
    monkeypatch.setattr(repo, "put_version", pending.put_version(fake_repo.put_version))
    monkeypatch.setattr(repo, "pending_chunks", pending.pending_chunks)
    monkeypatch.setattr(repo, "set_chunk_embedding", pending.set_chunk_embedding)
    monkeypatch.setattr(repo, "finish_embedding", pending.finish_embedding)
    monkeypatch.setattr(store, "reserve_daily_upload", pending.reserve_day)
    return pending


def drain(app) -> int:
    """Runs every queued embedding task on the calling thread; returns how many ran."""
    queue, ran = jobs.embed_queue_for(app), 0
    while queue.run_next():
        ran += 1
    return ran
