# docker/postgres/Dockerfile  (M1 - Dustin Dowell)
#
# PostgreSQL 16 with pg_stat_statements (bundled) and pg_cron (installed from
# the PGDG apt repository that the official image already configures).
# The official image does not ship pg_cron, so this small custom image is the
# mitigation listed in the proposal's risk table.
FROM postgres:16.4-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16-cron \
 && rm -rf /var/lib/apt/lists/*

COPY postgresql.conf /etc/postgresql/postgresql.conf
COPY initdb/10_project_init.sh /docker-entrypoint-initdb.d/10_project_init.sh

# Strip CR in case the files were checked out on Windows with core.autocrlf=true;
# a bash script with CRLF endings fails inside the Linux container.
RUN sed -i 's/\r$//' /etc/postgresql/postgresql.conf /docker-entrypoint-initdb.d/10_project_init.sh \
 && chmod 755 /docker-entrypoint-initdb.d/10_project_init.sh
