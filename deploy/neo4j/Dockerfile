# Neo4j Community for semigraph — official image + a seed/restore-on-boot hook.
# The benchmarked graph dump is baked in (deploy/neo4j/seed/neo4j.dump) and
# loaded by restore-and-start.sh the first time a volume boots with this dump.
FROM neo4j:2026.07.1-community
COPY seed/neo4j.dump /seed/neo4j.dump
RUN sha256sum /seed/neo4j.dump | cut -c1-16 > /seed/neo4j.dump.sha
COPY restore-and-start.sh /restore-and-start.sh
RUN chmod +x /restore-and-start.sh
ENTRYPOINT ["tini", "-g", "--", "/restore-and-start.sh"]
CMD ["neo4j"]
