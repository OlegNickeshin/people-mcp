from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.rows import dict_row

from server.config import MODEL_NAME

TABLES = {
    "profiles": ("profiles", "profile_chunks", "profile_id"),
    "projects": ("projects", "project_chunks", "project_id"),
}


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


def lock_publishers(conn):
    # One transaction lock keeps this small MVP's ownership changes atomic with
    # publication writes and OAuth grants. Public search/read never take it.
    conn.execute("SELECT pg_advisory_xact_lock(724639102)")


class Repository:
    def __init__(self, database_url: str, embedder=None):
        self.database_url = database_url
        self.embedder = embedder

    def migrate(self):
        migrations = Path(__file__).resolve().parent.parent / "migrations"
        with psycopg.connect(self.database_url) as conn:
            for migration in sorted(migrations.glob("[0-9]*.sql")):
                conn.execute(migration.read_text())
            conn.execute("INSERT INTO index_metadata VALUES ('embedding_model', %s) ON CONFLICT DO NOTHING", (MODEL_NAME,))
            stored = conn.execute("SELECT value FROM index_metadata WHERE key='embedding_model'").fetchone()[0]
            if stored != MODEL_NAME:
                raise RuntimeError("Embedding model changed; reindex existing content before using another model")

    @contextmanager
    def connection(self):
        with psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5) as conn:
            register_vector(conn)
            yield conn

    def get(self, kind: str, identifier: str):
        table, _, _ = TABLES[kind]
        with self.connection() as conn:
            row = conn.execute(sql.SQL("SELECT * FROM {} WHERE id::text=%s OR slug=%s").format(sql.Identifier(table)),
                               (identifier, identifier)).fetchone()
        if row is None:
            raise NotFound()
        return row

    def save(self, kind: str, data: dict, identifier: str | None = None, *, publisher_id=None):
        table, chunks, owner = TABLES[kind]
        with self.connection() as conn:
            if publisher_id is not None:
                lock_publishers(conn)
                if not conn.execute("SELECT 1 FROM oauth_publishers WHERE id=%s", (publisher_id,)).fetchone():
                    raise Forbidden("Connection ownership changed; retry with the current connection")
            old = None
            if identifier is not None:
                old = conn.execute(sql.SQL("SELECT * FROM {} WHERE id::text=%s OR slug=%s FOR UPDATE").format(
                    sql.Identifier(table)), (identifier, identifier)).fetchone()
                if old is None:
                    raise NotFound()
                if publisher_id is not None:
                    ownership = conn.execute(sql.SQL("SELECT publisher_id FROM publication_owners WHERE {}=%s").format(
                        sql.Identifier(owner)), (old["id"],)).fetchone()
                    if ownership is None or str(ownership["publisher_id"]) != str(publisher_id):
                        raise Forbidden("You can only update your own publications")
            fields = {k: data.get(k, old[k] if old else None) for k in ("slug", "content", "contact")}
            if old and all(old[k] == fields[k] for k in fields):
                return old
            # Embedding failures roll back the publication and all its chunks together.
            reindex = old is None or fields["content"] != old["content"]
            indexed = self.embedder.index(fields["content"]) if reindex else None
            if old:
                row = conn.execute(sql.SQL(
                    "UPDATE {} SET slug=%s, content=%s, contact=%s, updated_at=clock_timestamp() WHERE id=%s RETURNING *"
                ).format(sql.Identifier(table)), (*fields.values(), old["id"])).fetchone()
            else:
                row = conn.execute(sql.SQL("INSERT INTO {} (id,slug,content,contact) VALUES (%s,%s,%s,%s) RETURNING *").format(
                    sql.Identifier(table)), (uuid4(), *fields.values())).fetchone()
                if publisher_id is not None:
                    conn.execute(sql.SQL("INSERT INTO publication_owners (publisher_id,{}) VALUES (%s,%s)").format(
                        sql.Identifier(owner)), (publisher_id, row["id"]))
            if indexed is not None:
                conn.execute(sql.SQL("DELETE FROM {} WHERE {}=%s").format(sql.Identifier(chunks), sql.Identifier(owner)), (row["id"],))
                statement = sql.SQL("INSERT INTO {} (id,{},text,embedding) VALUES (%s,%s,%s,%s)").format(
                    sql.Identifier(chunks), sql.Identifier(owner))
                with conn.cursor() as cursor:
                    cursor.executemany(statement, [(uuid4(), row["id"], text, vector) for text, vector in indexed])
            return row

    def search(self, kind: str, query: str, limit: int, min_score: float):
        table, chunks, owner = TABLES[kind]
        vector = self.embedder.query(query)
        # Exact pgvector search is sufficient for a small public MVP corpus.
        # Rank within each entity BEFORE limiting, so a long profile cannot crowd others out.
        statement = sql.SQL("""
            WITH scored AS (
                SELECT id, {owner} AS entity_id, text, 1 - (embedding <=> %s) AS score FROM {chunks}
            ), ranked AS (
                SELECT *, row_number() OVER (PARTITION BY entity_id ORDER BY score DESC,id) AS position
                FROM scored WHERE score >= %s
            ), candidates AS (
                SELECT * FROM ranked WHERE position <= 3
            )
            SELECT e.*, max(c.score) AS score,
                jsonb_agg(jsonb_build_object('id',c.id,'text',c.text,'score',c.score)
                          ORDER BY c.score DESC,c.id) AS matched_chunks
            FROM {table} e JOIN candidates c ON c.entity_id=e.id
            GROUP BY e.id ORDER BY score DESC,e.id LIMIT %s
        """).format(owner=sql.Identifier(owner), chunks=sql.Identifier(chunks), table=sql.Identifier(table))
        with self.connection() as conn:
            rows = conn.execute(statement, (vector, min_score, limit)).fetchall()
        result = []
        for row in rows:
            matched = row.pop("matched_chunks")
            score = row.pop("score")
            result.append({owner: row["id"], "entity": row, "score": score,
                           "matched_chunks": matched, "why": [m["text"] for m in matched]})
        return result
