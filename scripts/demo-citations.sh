#!/usr/bin/env bash
# Citation-tracing demo against a running stack.
#
#   ./scripts/demo-citations.sh
#   ARCHIVE_URL=http://localhost:8080 SOURCE_URL=http://localhost:8081
#
# Scenario:
#   1. collect two days on v1 and register a monthly report's citation list;
#   2. re-collect on v2 (title revision) and v3 (time revision + withdrawal);
#   3. restore everything on v4 and show the first evidence is kept;
#   4. restart the archive service and trace the same report again.
set -euo pipefail

ARCHIVE_URL="${ARCHIVE_URL:-http://localhost:8080}"
SOURCE_URL="${SOURCE_URL:-http://localhost:8081}"
REPORT="EIA-MONTHLY-2026-11"

banner() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

banner "0. Reset the source to v1 (the archive DB must be reset separately)"
echo "note: the archive never rolls a day version backwards, so a re-run needs a"
echo "      fresh archive: 'docker compose down -v' (or delete ARCHIVE_DB_PATH)."

collect_until_settled() {
  local day="$1"
  for _ in $(seq 1 20); do
    local out status
    out=$(curl -fsS -X POST "$ARCHIVE_URL/api/collection/rounds" \
      -H 'content-type: application/json' -d "{\"day\": \"$day\"}")
    status=$(echo "$out" | jq -r .status)
    case "$status" in
      collected|needs_manual_review)
        echo "$out" | jq -c '{day, status, day_version, notices_collected}'
        return 0 ;;
    esac
  done
  echo "day $day never settled" >&2
  return 1
}

banner "1. Reset source to v1"
curl -fsS -X POST "$SOURCE_URL/v1/admin/version" \
  -H 'content-type: application/json' -d '{"version": "v1"}' | jq .

banner "2. Collect the cited days until each is settled"
collect_until_settled 2026-11-19
collect_until_settled 2026-11-21

banner "3. Register the monthly report citation list"
curl -fsS -X POST "$ARCHIVE_URL/api/citation-registrations" \
  -H 'content-type: application/json' -d @- <<'JSON' | jq .
{
  "report_no": "EIA-MONTHLY-2026-11",
  "citations": [
    {
      "day": "2026-11-19",
      "announcement_no": "HB-EIA-2026-1119-01",
      "title": "云岭风电场二期工程环境影响报告书公示",
      "published_at": "2026-11-19T08:00:00Z"
    },
    {
      "day": "2026-11-19",
      "announcement_no": "HB-EIA-2026-1119-02",
      "title": "青石镇污水处理厂提标改造竣工验收公示",
      "published_at": "2026-11-19T12:00:00Z"
    },
    {
      "day": "2026-11-21",
      "announcement_no": "HB-EIA-2026-1121-05",
      "title": "桃李乡养殖场粪污处理设施公示",
      "published_at": "2026-11-21T20:00:00Z"
    }
  ]
}
JSON

banner "3a. A mismatched list is rejected (409), nothing is stored"
curl -sS -o /tmp/eia-run/mismatch.json -w 'HTTP %{http_code}\n' \
  -X POST "$ARCHIVE_URL/api/citation-registrations" \
  -H 'content-type: application/json' -d @- <<'JSON'
{
  "report_no": "EIA-MONTHLY-2026-BAD",
  "citations": [
    {
      "day": "2026-11-19",
      "announcement_no": "HB-EIA-2026-1119-01",
      "title": "与现行归档不符的标题",
      "published_at": "2026-11-19T08:00:00Z"
    }
  ]
}
JSON
jq . /tmp/eia-run/mismatch.json

banner "4. Switch to v2 and re-collect: notice -01 gets a revised title"
curl -fsS -X POST "$SOURCE_URL/v1/admin/version" \
  -H 'content-type: application/json' -d '{"version": "v2"}' >/dev/null
collect_until_settled 2026-11-19
collect_until_settled 2026-11-21
curl -fsS "$ARCHIVE_URL/api/citation-registrations/$REPORT" \
  | jq '{citations: (.citations | length), evidence}'

banner "5. Switch to v3: -02 changes publication time; -05 is withdrawn"
curl -fsS -X POST "$SOURCE_URL/v1/admin/version" \
  -H 'content-type: application/json' -d '{"version": "v3"}' >/dev/null
# A bounded partial round must not create withdrawal evidence yet.
curl -fsS -X POST "$ARCHIVE_URL/api/collection/rounds" \
  -H 'content-type: application/json' \
  -d '{"day": "2026-11-21", "max_queries": 1}' \
  | jq -c '{status, queries_used, pending: (.pending_ranges | length)}'
echo "evidence while 2026-11-21 is still incomplete:"
curl -fsS "$ARCHIVE_URL/api/citation-registrations/$REPORT" | jq '.evidence | length'
collect_until_settled 2026-11-19
collect_until_settled 2026-11-21
echo "evidence after the whole v3 day is collected:"
curl -fsS "$ARCHIVE_URL/api/citation-registrations/$REPORT" | jq .

banner "6. Switch to v4: originals restored; first evidence is immutable"
curl -fsS -X POST "$SOURCE_URL/v1/admin/version" \
  -H 'content-type: application/json' -d '{"version": "v4"}' >/dev/null
collect_until_settled 2026-11-19
collect_until_settled 2026-11-21
curl -fsS "$ARCHIVE_URL/api/citation-registrations/$REPORT" | jq .

banner "7. Swagger UI"
echo "archive: $ARCHIVE_URL/docs"
echo "source:  $SOURCE_URL/docs"
