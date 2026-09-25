# Graph seed

`neo4j.dump` (git-ignored, ~110 MB) is the knowledge graph the Fly database boots with.

## v1.1 (current): built and dumped on Neo4j Community 2026.07.1

`semigraph build-graph --rebuild` writes an aligned-format store on Community, so it is dumped as is
(no `copy` step: Community has no `neo4j-admin database copy`). The dump includes the transaction
logs, which a rebuild inflates (a 300+ MB dump for a ~50 MB graph). Prune them first:

```
# neo4j.conf (local dev install)
db.tx_log.rotation.retention_policy=keep_none
db.checkpoint.interval.time=10s        # temporary
# start the server, do one trivial write (CREATE + DELETE a scratch node), wait ~30 s, stop the server
neo4j-admin database info neo4j        # expect: Store needs recovery: false
neo4j-admin database dump neo4j --to-path=../dump-out --overwrite-destination=true   # a relative path: "C:" parses as a URI scheme
cp ../dump-out/neo4j.dump deploy/neo4j/seed/neo4j.dump
```

Before shipping, load it into a throwaway install of the same version and run the invariants
(`scripts/verify_graph.py`): `neo4j-admin database load neo4j --from-path=... --overwrite-destination=true`,
start it on spare ports, expect `0 invariant failure(s)` and the intended snapshot id.

The v1 seed (Desktop, block-format -> aligned copy) is kept outside git as `data/backups/fly-seed-v1.neo4j.dump`
for rollback.

## v1 (historical): exported from Neo4j Desktop

Desktop is Enterprise "block" format, so it needs an aligned copy first:

```
# Desktop DBMS bin/, database stopped (STOP DATABASE neo4j WAIT in the system db)
neo4j-admin database copy neo4j alignedcopy --to-format=aligned --copy-schema --force
neo4j-admin database dump alignedcopy --to-path=<dir> --overwrite-destination=true
mv <dir>/alignedcopy.dump deploy/neo4j/seed/neo4j.dump
```

## How the image uses it

The image bakes the dump in; `restore-and-start.sh` loads it on the first boot of a volume
(and again whenever the dump changes — the marker under `/data/.seeded` carries its hash).
Re-seeding replaces the whole `neo4j` database, including the service ledger/cache and the kill switch.
