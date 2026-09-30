"""Graph plumbing unit tests — client wrapper, schema application, reset_graph.

Everything here runs against fake drivers: no Neo4j, no network.
"""

import re
from pathlib import Path

import pytest

from semigraph.artifacts import read_schema_cypher
from semigraph.config import Settings
from semigraph.graph import client, schema

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeResult(list):
    """A list of row dicts that also behaves like a neo4j Result."""

    def consume(self):
        return None

    def data(self):
        return [dict(r) for r in self]


class FakeSession:
    def __init__(self, driver, config):
        self.driver, self.config = driver, config

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, parameters=None, **params):
        self.driver.runs.append((self.config.get("database"), " ".join(query.split()), params))
        return FakeResult(self.driver.responder(" ".join(query.split())))


class FakeDriver:
    """Records every session config and query; ``responder`` fakes result rows."""

    def __init__(self, responder=lambda q: []):
        self.responder = responder
        self.session_configs: list[dict] = []
        self.runs: list[tuple] = []
        self.queries: list[tuple] = []
        self.closed = False

    def session(self, **config):
        self.session_configs.append(config)
        return FakeSession(self, config)

    def verify_connectivity(self):
        return "verified"

    def get_server_info(self):
        return type("Info", (), {"agent": "Neo4j/fake"})()

    def execute_query(self, query, *args, **kwargs):
        self.queries.append((query, args, kwargs))
        return "eager"

    def close(self):
        self.closed = True

    def custom_method(self):
        return "delegated"

    @property
    def statements(self) -> list[str]:
        return [q for _, q, _ in self.runs]


# ------------------------------------------------------------------ wrapper

class TestDatabaseDriver:
    def test_session_without_database_gets_the_configured_one(self):
        inner = FakeDriver()
        client.DatabaseDriver(inner, "sgtest").session()
        assert inner.session_configs == [{"database": "sgtest"}]

    def test_other_session_options_are_preserved(self):
        inner = FakeDriver()
        client.DatabaseDriver(inner, "sgtest").session(default_access_mode="r", fetch_size=50)
        assert inner.session_configs == [
            {"default_access_mode": "r", "fetch_size": 50, "database": "sgtest"}]

    @pytest.mark.parametrize("explicit", ["system", "neo4j", None])
    def test_an_explicitly_passed_database_is_never_overridden(self, explicit):
        inner = FakeDriver()
        client.DatabaseDriver(inner, "sgtest").session(database=explicit)
        assert inner.session_configs == [{"database": explicit}]

    def test_empty_database_setting_means_the_servers_home_database(self):
        inner = FakeDriver()
        client.DatabaseDriver(inner, "").session()
        assert inner.session_configs == [{}]

    def test_everything_else_is_delegated(self):
        inner = FakeDriver()
        wrapped = client.DatabaseDriver(inner, "sgtest")
        assert wrapped.verify_connectivity() == "verified"
        assert wrapped.get_server_info().agent == "Neo4j/fake"
        assert wrapped.custom_method() == "delegated"
        wrapped.close()
        assert inner.closed

    def test_unknown_attribute_raises_attribute_error(self):
        with pytest.raises(AttributeError):
            client.DatabaseDriver(FakeDriver(), "sgtest").no_such_attribute

    def test_execute_query_is_pinned_to_the_configured_database(self):
        inner = FakeDriver()
        client.DatabaseDriver(inner, "sgtest").execute_query("RETURN 1", {"a": 1})
        query, args, kwargs = inner.queries[0]
        assert (query, args, kwargs) == ("RETURN 1", ({"a": 1},), {"database_": "sgtest"})

    def test_execute_query_keeps_an_explicit_database(self):
        inner = FakeDriver()
        client.DatabaseDriver(inner, "sgtest").execute_query("RETURN 1", database_="system")
        assert inner.queries[0][2] == {"database_": "system"}

    def test_context_manager_closes_the_wrapped_driver(self):
        inner = FakeDriver()
        with client.DatabaseDriver(inner, "sgtest") as drv:
            assert drv.database == "sgtest"
        assert inner.closed

    def test_wrapped_exposes_the_raw_driver(self):
        inner = FakeDriver()
        assert client.DatabaseDriver(inner, "sgtest").wrapped is inner

    def test_run_cypher_runs_in_the_configured_database(self):
        inner = FakeDriver(responder=lambda q: [{"n": 1}])
        rows = client.run_cypher(client.DatabaseDriver(inner, "sgtest"), "RETURN 1 AS n", x=2)
        assert rows == [{"n": 1}]
        assert inner.runs == [("sgtest", "RETURN 1 AS n", {"x": 2})]

    def test_run_cypher_hands_session_config_to_the_session_never_to_the_query(self):
        inner = FakeDriver(responder=lambda q: [{"n": 1}])
        client.run_cypher(client.DatabaseDriver(inner, "sgtest"), "RETURN 1 AS n",
                          session_config_=client.NO_UNRECOGNIZED_NOTIFICATIONS, x=2)
        assert inner.session_configs == [{**client.NO_UNRECOGNIZED_NOTIFICATIONS, "database": "sgtest"}]
        assert inner.runs == [("sgtest", "RETURN 1 AS n", {"x": 2})]

    def test_run_cypher_without_session_config_opens_a_default_session(self):
        inner = FakeDriver()
        client.run_cypher(inner, "RETURN 1")
        assert inner.session_configs == [{}]

    def test_the_quiet_session_config_disables_only_unrecognized_notifications(self):
        # 01N50/01N51/01N52 (a label, relationship type or property key the database has never seen) only: no
        # severity floor, so deprecation, performance and every other notification still reach the logs.
        from neo4j import NotificationClassification

        assert dict(client.NO_UNRECOGNIZED_NOTIFICATIONS) == {
            "notifications_disabled_classifications": (NotificationClassification.UNRECOGNIZED,)}


class TestGetDriver:
    def test_default_settings_target_the_neo4j_database(self, monkeypatch):
        inner = FakeDriver()
        monkeypatch.setattr(client.GraphDatabase, "driver", lambda *a, **k: inner)
        settings = Settings(_env_file=None)
        assert settings.neo4j_database == "neo4j"
        client.get_driver(settings).session()
        assert inner.session_configs == [{"database": "neo4j"}]

    def test_database_comes_from_settings(self, monkeypatch):
        inner = FakeDriver()
        monkeypatch.setattr(client.GraphDatabase, "driver", lambda *a, **k: inner)
        drv = client.get_driver(Settings(_env_file=None, neo4j_database="sgtest"))
        drv.session()
        assert inner.session_configs == [{"database": "sgtest"}]

    def test_database_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("NEO4J_DATABASE", "sgenv")
        assert Settings(_env_file=None).neo4j_database == "sgenv"

    def test_unreachable_server_raises_setup_guidance_and_closes_the_driver(self, monkeypatch):
        class Down(FakeDriver):
            def verify_connectivity(self):
                raise OSError("connection refused")

        inner = Down()
        monkeypatch.setattr(client.GraphDatabase, "driver", lambda *a, **k: inner)
        with pytest.raises(RuntimeError, match="Neo4j is not reachable"):
            client.get_driver(Settings(_env_file=None))
        assert inner.closed


# ------------------------------------------------------------------- schema

ROLE_FILTERS = {
    "evidence_embedding": ["is_current", "retrievable", "filer_cik", "form", "valid_from", "valid_to"],
    "risk_embedding": ["is_current", "filer_cik", "valid_from", "valid_to"],
    # M4: uploaded-document chunks, searched ONLY inside one workspace (docs/v2/M4_PLAN.md 4.4)
    "user_chunk_embedding": ["workspace_id", "is_current", "document_id", "version", "valid_from", "valid_to"],
}


def vector_rows(**overrides):
    """SHOW INDEXES rows for both vector indexes; ``overrides`` replaces a row's properties."""
    return [{"name": n, "type": "VECTOR", "properties": overrides.get(n, ["embedding", *f])}
            for n, f in ROLE_FILTERS.items()]


def indexes_responder(rows):
    return lambda q: rows if q.startswith("SHOW INDEXES") else []


class TestSchemaFile:
    def test_both_copies_of_the_schema_are_identical(self):
        packaged = REPO_ROOT / "src" / "semigraph" / "artifacts" / "schema.cypher"
        root_copy = REPO_ROOT / "artifacts" / "schema.cypher"
        assert packaged.read_bytes() == root_copy.read_bytes()

    def test_expected_vector_filters_are_parsed_from_the_ddl(self):
        assert schema.expected_vector_filters(read_schema_cypher()) == ROLE_FILTERS

    def test_ddl_declares_snapshot_uniqueness_and_the_fulltext_indexes(self):
        ddl = read_schema_cypher()
        assert re.search(r"CREATE CONSTRAINT \w+ IF NOT EXISTS FOR \(s:Snapshot\) REQUIRE s\.id IS UNIQUE", ddl)
        assert "CREATE INDEX filing_status IF NOT EXISTS FOR (f:Filing) ON (f.status)" in ddl
        for name, prop in (("evidence_text_ft", "e.text"), ("risk_summary_ft", "rf.summary")):
            assert re.search(rf"CREATE FULLTEXT INDEX {name} IF NOT EXISTS FOR \(\w+:\w+\) ON EACH \[{re.escape(prop)}\]", ddl)
        assert ddl.count("standard-no-stop-words") == 2

    def test_ddl_statements_contain_no_stray_semicolons(self):
        # apply_schema splits on ';' — a ';' inside a comment or string would corrupt it
        statements = schema.split_statements(read_schema_cypher())
        assert statements and all(s.startswith("CREATE ") for s in statements)


class TestApplySchema:
    def test_fresh_database_applies_every_statement(self):
        drv = FakeDriver(indexes_responder([]))
        n = schema.apply_schema(drv)
        applied = [s for s in drv.statements if s.startswith("CREATE")]
        assert n == len(applied) == len(schema.split_statements(read_schema_cypher()))

    def test_up_to_date_filtered_indexes_are_accepted(self):
        drv = FakeDriver(indexes_responder(vector_rows()))
        assert schema.apply_schema(drv) > 0

    def test_stale_unfiltered_vector_index_raises_before_applying_anything(self):
        drv = FakeDriver(indexes_responder(vector_rows(evidence_embedding=["embedding"])))
        with pytest.raises(RuntimeError, match=r"evidence_embedding.*build-graph --rebuild"):
            schema.apply_schema(drv)
        assert not [s for s in drv.statements if s.startswith("CREATE")]

    def test_index_missing_only_some_filter_properties_is_stale(self):
        partial = ["embedding", "is_current", "filer_cik"]
        drv = FakeDriver(indexes_responder(vector_rows(risk_embedding=partial)))
        with pytest.raises(RuntimeError, match=r"risk_embedding.*valid_from"):
            schema.apply_schema(drv)

    def test_other_vector_indexes_are_ignored(self):
        rows = vector_rows() + [{"name": "someone_elses", "type": "VECTOR", "properties": ["embedding"]}]
        assert schema.apply_schema(FakeDriver(indexes_responder(rows))) > 0


# ---------------------------------------------------------------- reset_graph

class TestResetGraph:
    def run_reset(self, **kwargs):
        drv = FakeDriver(lambda q: [{"n": 7}] if "RETURN count(n)" in q else [])
        result = schema.reset_graph(drv, **kwargs)
        return drv, result

    def test_drops_the_named_vector_and_fulltext_indexes(self):
        drv, _ = self.run_reset()
        dropped = {s.split()[2] for s in drv.statements if s.startswith("DROP INDEX")}
        assert dropped == {"evidence_embedding", "risk_embedding", "evidence_text_ft", "risk_summary_ft"}
        assert all(s.endswith("IF EXISTS") for s in drv.statements if s.startswith("DROP INDEX"))

    def test_indexes_are_dropped_before_the_nodes_are_deleted(self):
        drv, _ = self.run_reset()
        first_delete = next(i for i, s in enumerate(drv.statements) if "DETACH DELETE" in s)
        last_drop = max(i for i, s in enumerate(drv.statements) if s.startswith("DROP INDEX"))
        assert last_drop < first_delete

    def test_service_state_is_kept_by_default_and_deleted_in_batches(self):
        drv, _ = self.run_reset()
        delete = next(s for s in drv.statements if "DETACH DELETE" in s)
        assert "STARTS WITH 'Svc'" in delete
        assert re.search(r"IN TRANSACTIONS OF \d+ ROWS", delete)

    def test_upload_workspaces_are_private_state_a_rebuild_keeps_too(self):
        drv, _ = self.run_reset()
        delete = next(s for s in drv.statements if "DETACH DELETE" in s)
        count = next(s for s in drv.statements if "RETURN count(n)" in s)
        assert "STARTS WITH 'User'" in delete and "STARTS WITH 'User'" in count
        assert not any("user_chunk_embedding" in s for s in drv.statements)

    def test_keep_service_state_false_deletes_everything(self):
        drv, _ = self.run_reset(keep_service_state=False)
        delete = next(s for s in drv.statements if "DETACH DELETE" in s)
        assert "Svc" not in delete

    def test_reports_the_number_of_nodes_it_removed(self):
        _, result = self.run_reset()
        assert result == {"deleted_nodes": 7}

    def test_constraints_and_range_indexes_are_left_alone(self):
        drv, _ = self.run_reset()
        assert not [s for s in drv.statements if "CONSTRAINT" in s]
        assert {s.split()[2] for s in drv.statements if s.startswith("DROP INDEX")}.isdisjoint(
            {"company_ticker", "filing_date", "filing_status"})



class TestUserWorkspaceSchema:
    """M4: upload workspaces are User*-labelled private state (docs/v2/M4_PLAN.md 4.4)."""

    def test_private_label_prefixes_cover_service_state_and_workspaces(self):
        assert schema.PRIVATE_LABEL_PREFIXES == ("Svc", "User") and schema.SERVICE_LABEL_PREFIX == "Svc"
        assert schema.is_private_label("SvcAnswer") and schema.is_private_label("UserChunk")
        assert not schema.is_private_label("EvidenceSpan") and not schema.is_private_label("Company")
        assert schema.private_label_predicate("n") == "any(l IN labels(n) WHERE l STARTS WITH 'Svc' OR l STARTS WITH 'User')"

    def test_the_ddl_declares_the_workspace_identities_lookups_and_the_filtered_vector_index(self):
        ddl = read_schema_cypher()
        for label, var, prop, name in (("UserWorkspace", "w", "workspace_id", "user_workspace_id"),
                                       ("UserDocument", "d", "document_id", "user_document_id"),
                                       ("UserVersion", "v", "version_key", "user_version_key"),
                                       ("UserChunk", "c", "chunk_id", "user_chunk_id"),
                                       ("UserJob", "j", "job_id", "user_job_id")):
            assert f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR ({var}:{label}) REQUIRE {var}.{prop} IS UNIQUE" in ddl
        assert "CREATE INDEX user_workspace_expires IF NOT EXISTS FOR (w:UserWorkspace) ON (w.expires_at)" in ddl
        assert "CREATE INDEX user_chunk_ws IF NOT EXISTS FOR (c:UserChunk) ON (c.workspace_id)" in ddl
        assert re.search(r"CREATE VECTOR INDEX user_chunk_embedding IF NOT EXISTS FOR \(c:UserChunk\) ON \(c\.embedding\)", ddl)

    def test_the_workspace_index_is_not_one_a_graph_rebuild_drops(self):
        assert "user_chunk_embedding" not in schema.REBUILD_INDEXES


class TestRiskItemSchema:
    """M1b: RiskItem is the lineage unit; its identity and lookups are declared in the schema."""

    def test_riskitem_has_a_uniqueness_constraint_and_a_lookup_index(self):
        ddl = read_schema_cypher()
        assert "CREATE CONSTRAINT riskitem_id IF NOT EXISTS FOR (i:RiskItem) REQUIRE i.item_id IS UNIQUE" in ddl
        assert "FOR (i:RiskItem) ON (i.filer_cik, i.accession_no)" in ddl


class TestRiskPassageSchema:
    """M1b step 4: the change layer below the item (passages) is unique by id and looked up by filing pair."""

    def test_riskpassage_has_a_uniqueness_constraint_and_a_pair_index(self):
        ddl = read_schema_cypher()
        assert "CREATE CONSTRAINT riskpassage_id IF NOT EXISTS FOR (p:RiskPassage) REQUIRE p.passage_id IS UNIQUE" in ddl
        assert "CREATE INDEX riskpassage_pair IF NOT EXISTS FOR (p:RiskPassage) ON (p.filer_cik, p.newer_accession)" in ddl

    def test_the_repo_copy_and_the_packaged_copy_of_the_schema_are_byte_identical(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        assert (root / "artifacts" / "schema.cypher").read_bytes() == (root / "src" / "semigraph" / "artifacts" / "schema.cypher").read_bytes()

    def test_the_risk_disclosure_edge_index_no_longer_lists_the_retired_end_date(self):
        assert "FOR ()-[r:DISCLOSES_RISK]-() ON (r.start_date, r.status)" in read_schema_cypher()
