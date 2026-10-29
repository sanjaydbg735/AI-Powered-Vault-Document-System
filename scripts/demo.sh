#!/usr/bin/env bash
# End-to-end demo: upload -> wait for processing -> insights -> multi-doc chat (JSON + SSE) -> metrics.
# Usage: API=http://localhost:18000 KEY=dev-key ./scripts/demo.sh
set -euo pipefail

API="${API:-http://localhost:18000}/api/v1"
KEY="${KEY:-dev-key}"
H=(-H "X-API-Key: $KEY")
J=(-H "Content-Type: application/json")
DIR="$(cd "$(dirname "$0")/.." && pwd)"
py() { python3 -c "import sys, json; d = json.load(sys.stdin); $1"; }

[ -d "$DIR/samples" ] || python3 "$DIR/scripts/make_samples.py"

echo "== 1. Upload documents"
IDS=()
for f in "$DIR"/samples/*; do
  id=$(curl -sf "${H[@]}" -F "file=@$f" "$API/documents" | py 'print(d["document"]["id"])')
  echo "  $(basename "$f") -> $id"
  IDS+=("$id")
done

echo "== 2. Wait until ready (async pipeline)"
for id in "${IDS[@]}"; do
  for _ in $(seq 1 60); do
    s=$(curl -sf "${H[@]}" "$API/documents/$id" | py 'print(d["status"], d["analysis_status"])')
    [[ "$s" == "ready completed" || "$s" == failed* ]] && break
    sleep 1
  done
  echo "  $id: $s"
done

echo "== 3. AI insights for the first document"
curl -sf "${H[@]}" "$API/documents/${IDS[0]}/insights" |
  py 'print("  title:", d["title"]); print("  category:", d["category"], "tags:", d["tags"]); print("  summary:", d["summary"][:300]); print("  suggested:", d["suggested_questions"][:3])'

echo "== 4. Chat across all documents"
SID=$(curl -sf "${H[@]}" "${J[@]}" -d '{"title":"Demo"}' "$API/chat/sessions" | py 'print(d["id"])')
ask() {
  echo; echo "  Q: $1"
  curl -sf "${H[@]}" "${J[@]}" -d "$(python3 -c 'import json,sys; print(json.dumps({"question": sys.argv[1]}))' "$1")" \
    "$API/chat/sessions/$SID/messages" |
    py 'a = d["answer"]; print("  A:", a["content"].replace("\n", "\n     ")); print("  citations:", [(c["source_id"], c["filename"], c["pages"]) for c in a["citations"]]); print("  rewritten:", a["standalone_query"]); print("  follow-ups:", a["suggested_questions"])'
}
ask "How much notice is needed to terminate the services agreement?"
ask "And what happens if invoices are paid late?"
ask "What was Northwind's Q3 revenue and what are the main risks?"
ask "How many weeks of parental leave do primary caregivers get?"
ask "Who won the 2018 football world cup?"

echo; echo "== 5. Streaming answer (SSE)"
curl -sN "${H[@]}" "${J[@]}" -d '{"question":"What is the remote work policy?","stream":true}' \
  "$API/chat/sessions/$SID/messages" | grep -E '^event: (meta|done)' | sort | uniq -c

echo; echo "== 6. Metrics"
curl -sf "${H[@]}" "$API/metrics/documents" | py 'print("  documents:", d["by_status"], d["by_category"])'
curl -sf "${H[@]}" "$API/metrics/processing" | py 'print("  stage p95 ms:", {k: v["p95_ms"] for k, v in d["stages"].items()}); print("  e2e:", d["end_to_end_ms"])'
curl -sf "${H[@]}" "$API/metrics/usage" | py 'print("  cost USD:", d["total_cost_usd"], "calls:", d["total_calls"], "chat:", d["chat"])'
