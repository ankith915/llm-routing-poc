-- Demo warehouse schema for SQL-generation tasks. The tables are loaded from
-- logs.json, metrics.json and incidents.json at validation time, so a generated
-- query is executed against real rows and compared with a reference query.
CREATE TABLE incidents (
  incident_id TEXT PRIMARY KEY,
  service     TEXT NOT NULL,
  severity    TEXT NOT NULL,          -- HIGH | MEDIUM | LOW
  status      TEXT NOT NULL,          -- OPEN | RESOLVED
  description TEXT,
  created_at  TEXT NOT NULL           -- ISO-8601
);
CREATE TABLE logs (
  ts         TEXT NOT NULL,           -- ISO-8601
  service    TEXT NOT NULL,
  host       TEXT NOT NULL,
  level      TEXT NOT NULL,           -- INFO | WARN | ERROR
  message    TEXT NOT NULL,
  latency_ms INTEGER
);
CREATE TABLE metrics (
  ts                 TEXT NOT NULL,
  service            TEXT NOT NULL,
  cpu_percent        REAL,
  memory_percent     REAL,
  latency_ms         REAL,
  db_connections     INTEGER,
  db_max_connections INTEGER
);
