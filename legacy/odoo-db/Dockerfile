# Odoo's DB with the legacy time-zone names. Debian 13 moved aliases such as Asia/Calcutta (what Edge reports in
# India, and what Odoo stores as a user's tz on first login) into tzdata-legacy. Without it, every report grouped
# by date fails with: time zone "Asia/Calcutta" not recognized.
FROM postgres:16
RUN apt-get update && apt-get install -y --no-install-recommends tzdata-legacy && rm -rf /var/lib/apt/lists/*
