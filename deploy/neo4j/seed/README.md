# Graph seed

`neo4j.dump` (git-ignored, ~28 MB) is the benchmarked knowledge graph exported from the
Neo4j Desktop DBMS in Community-compatible *aligned* format with its schema:

```
# Desktop DBMS bin/, database stopped (STOP DATABASE neo4j WAIT in the system db)
neo4j-admin database copy neo4j alignedcopy --to-format=aligned --copy-schema --force
neo4j-admin database dump alignedcopy --to-path=<dir> --overwrite-destination=true
mv <dir>/alignedcopy.dump deploy/neo4j/seed/neo4j.dump
```

The image bakes the dump in; `restore-and-start.sh` loads it on the first boot of a volume
(and again whenever the dump changes — the marker under `/data/.seeded` carries its hash).
Re-seeding replaces the whole `neo4j` database, including the service ledger/cache.
