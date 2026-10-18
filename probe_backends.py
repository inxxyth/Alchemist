import time
from neo4j import GraphDatabase
import psycopg2

print("neo4j probe", flush=True)
t0 = time.time()
d = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "msoe_graph_2026"))
with d.session() as s:
    rows = list(s.run(
        "MATCH (rf:RiskFlag {type: $risk})-[r:ADDRESSED_BY]->(st:Standard)-[:PART_OF]->(p:Policy) "
        "RETURN p.title AS t, st.clause_number AS c LIMIT 3",
        risk="VACCINATION_EXPIRED",
    ))
print(" neo4j rows", rows, "in", round(time.time() - t0, 2), "s", flush=True)
d.close()

print("pg probe", flush=True)
t0 = time.time()
c = psycopg2.connect(host="localhost", port=5432, dbname="msoe_db",
                     user="msoe_admin", password="msoe_secure_2026")
cur = c.cursor()
cur.execute("SELECT count(*) FROM attendance_logs")
print(" attendance count", cur.fetchone(), "in", round(time.time() - t0, 2), "s", flush=True)

t0 = time.time()
cur.execute("""
    WITH att AS (
        SELECT student_id,
               ROUND(100.0 * SUM(CASE WHEN status IN ('present','late') THEN 1 ELSE 0 END) / COUNT(*), 2) AS rate
        FROM attendance_logs GROUP BY student_id
    )
    SELECT count(*) FROM att WHERE rate < 85
""")
print(" chronic count", cur.fetchone(), "in", round(time.time() - t0, 2), "s", flush=True)
c.close()
print("done", flush=True)
