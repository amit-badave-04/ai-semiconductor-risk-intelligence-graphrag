CREATE CONSTRAINT company_cik IF NOT EXISTS FOR (c:Company) REQUIRE c.cik IS UNIQUE;

CREATE CONSTRAINT company_name IF NOT EXISTS FOR (c:Company) REQUIRE c.name IS UNIQUE;

CREATE CONSTRAINT filing_accession IF NOT EXISTS FOR (f:Filing) REQUIRE f.accession_no IS UNIQUE;

CREATE CONSTRAINT section_key IF NOT EXISTS FOR (s:FilingSection) REQUIRE s.section_key IS UNIQUE;

CREATE CONSTRAINT metric_id IF NOT EXISTS FOR (m:Metric) REQUIRE m.metric_id IS UNIQUE;

CREATE CONSTRAINT risk_id IF NOT EXISTS FOR (r:RiskFactor) REQUIRE r.risk_id IS UNIQUE;

CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (e:EvidenceSpan) REQUIRE e.chunk_id IS UNIQUE;

CREATE CONSTRAINT product_name IF NOT EXISTS FOR (p:Product) REQUIRE p.name IS UNIQUE;

CREATE CONSTRAINT exportcontrol_rule IF NOT EXISTS FOR (x:ExportControl) REQUIRE x.rule_id IS UNIQUE;

CREATE INDEX company_ticker IF NOT EXISTS FOR (c:Company) ON (c.ticker);

CREATE INDEX filing_date IF NOT EXISTS FOR (f:Filing) ON (f.filing_date);

CREATE INDEX metric_period IF NOT EXISTS FOR (m:Metric) ON (m.period_end);

CREATE INDEX supplies_temporal IF NOT EXISTS FOR ()-[r:SUPPLIES_TO]-() ON (r.start_date, r.end_date, r.status);

CREATE INDEX depends_temporal IF NOT EXISTS FOR ()-[r:DEPENDS_ON]-() ON (r.start_date, r.end_date, r.status);

CREATE INDEX discloses_temporal IF NOT EXISTS FOR ()-[r:DISCLOSES_RISK]-() ON (r.start_date, r.end_date, r.status);

CREATE INDEX affected_temporal IF NOT EXISTS FOR ()-[r:AFFECTED_BY]-() ON (r.start_date, r.end_date, r.status);

CREATE VECTOR INDEX evidence_embedding IF NOT EXISTS FOR (e:EvidenceSpan) ON (e.embedding)
    OPTIONS {indexConfig: {`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}};

CREATE VECTOR INDEX risk_embedding IF NOT EXISTS FOR (r:RiskFactor) ON (r.embedding)
    OPTIONS {indexConfig: {`vector.dimensions`: 1024, `vector.similarity_function`: 'cosine'}};
