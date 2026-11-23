-- 00_roles.sql  (run as cluster superuser, connected to database "postgres")
-- Creates the three least-privilege login roles and the project database.
-- Passwords come from psql variables (-v) so no secret is stored in the repo:
--   psql -v app_pw=... -v collector_pw=... -v reader_pw=... -f sql/00_roles.sql
SELECT format('CREATE ROLE shop_app LOGIN PASSWORD %L', :'app_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'shop_app') \gexec
SELECT format('CREATE ROLE monitor_collector LOGIN PASSWORD %L', :'collector_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monitor_collector') \gexec
SELECT format('CREATE ROLE monitor_reader LOGIN PASSWORD %L', :'reader_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'monitor_reader') \gexec

-- collector needs to see every session's state/PIDs (not just its own)
GRANT pg_read_all_stats TO monitor_collector;

SELECT format('CREATE DATABASE %I', :'dbname')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'dbname') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO shop_app, monitor_collector, monitor_reader', :'dbname') \gexec
