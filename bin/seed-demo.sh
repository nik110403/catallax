#!/usr/bin/env bash
# Seed a running Catallax demo stack through its public HTTP API only.
# Idempotent: every item is looked up first and created only if missing.
#
#   NGINX_HOST_PORT=8090   host port of the nginx container (default 8090)
#   CATALLAX_URL=...       full API base, overrides the port (loopback http, or https
#                          with SEED_ALLOW_REMOTE=1)
#   SHARE_PASSPHRASE=...   log in via POST /auth/login when the share gate is on
#   WAIT_SECS=20           how long to wait for a first backtest/lab job to start
#
# Requires: curl, jq. All content is synthetic (persona + sample ledger only).
set -euo pipefail

BASE="${CATALLAX_URL:-http://127.0.0.1:${NGINX_HOST_PORT:-8090}/api/v1}"
# Writes demo content and may send a passphrase: loopback only, unless https and opted in.
case "$BASE" in
  http://127.0.0.1[:/]*|http://localhost[:/]*|http://\[::1\][:/]*) ;;
  https://*) [ "${SEED_ALLOW_REMOTE:-}" = 1 ] || { echo "seed-demo: remote target $BASE needs SEED_ALLOW_REMOTE=1" >&2; exit 1; } ;;
  *) echo "seed-demo: refusing $BASE (loopback http or opted-in https only)" >&2; exit 1 ;;
esac
WAIT_SECS="${WAIT_SECS:-20}"
TMP="$(mktemp -d)"; JAR="$TMP/jar"; trap 'rm -r "$TMP"' EXIT
CREATED=0; EXISTED=0

api() { # api METHOD PATH [json-body]  -> body on stdout, fails on non-2xx
  local m="$1" p="$2" out="$TMP/out" code
  if [ $# -ge 3 ]; then
    # Body goes through a file so a passphrase never shows up in the process list.
    printf '%s' "$3" >"$TMP/body"
    code="$(curl -sS -o "$out" -w '%{http_code}' -b "$JAR" -c "$JAR" -X "$m" -H 'Content-Type: application/json' --data-binary @"$TMP/body" "$BASE$p")"
  else
    code="$(curl -sS -o "$out" -w '%{http_code}' -b "$JAR" -c "$JAR" -X "$m" "$BASE$p")"
  fi
  if [[ "$code" != 2* ]]; then echo "seed-demo: $m $p -> HTTP $code: $(head -c 300 "$out")" >&2; return 1; fi
  cat "$out"
}
made()   { CREATED=$((CREATED+1)); echo "  created  $1"; }
exists() { EXISTED=$((EXISTED+1)); echo "  exists   $1"; }

curl -fsS "$BASE/health" >/dev/null || { echo "seed-demo: stack not reachable at $BASE" >&2; exit 1; }

sess="$(api GET /session)"
if [ "$(jq -r .demo_mode <<<"$sess")" = true ]; then
  echo "seed-demo: DEMO_MODE=true makes every mutation read-only (403); nothing to seed" >&2; exit 1
fi
# Share gate (same call the frontend makes: POST /auth/login {passphrase, remember}).
if [ "$(jq -r .auth_required <<<"$sess")" = true ]; then
  [ -n "${SHARE_PASSPHRASE:-}" ] || { echo "seed-demo: share gate is on; set SHARE_PASSPHRASE" >&2; exit 1; }
  export SHARE_PASSPHRASE
  api POST /auth/login "$(jq -nc '{passphrase:env.SHARE_PASSPHRASE,remember:false}')" >/dev/null
  echo "logged in (share gate)"
fi

echo "signals"
# Counts are taken in an assignment so a failed GET aborts (errexit) instead of reading as 0.
n="$(api GET /signals | jq -e '.data.signals | length')"
if [ "$n" -gt 0 ]; then exists "signals (POST /signals/seed skipped)"
else api POST /signals/seed >/dev/null; made "POST /signals/seed"; fi

# Backtests and lab runs need the optional 'backtest' / 'lab' compose profile workers.
# The first new job of each kind is a probe: if it does not leave 'queued' within WAIT_SECS
# the worker is not running and the rest of that kind is skipped. The probe stays queued and
# runs once the profile is started; reruns find it by name and do not re-create it.
job_started() { # job_started GET-path
  local i s
  for ((i=0; i<WAIT_SECS; i++)); do
    s="$(api GET "$1" | jq -r .status)" || return 1
    [ -n "$s" ] && [ "$s" != null ] || return 1
    [ "$s" != queued ] && return 0; sleep 1
  done; return 1
}
seed_jobs() { # seed_jobs LABEL LIST_PATH POST_PATH GET_PREFIX PROFILE  (stdin: one JSON config per line)
  local label="$1" list="$2" post="$3" getp="$4" prof="$5" cfg name id probed=0 skip=0 have
  local jobs; jobs="$(api GET "$list")"; have="$(jq -c '[.jobs[].name]' <<<"$jobs")"
  echo "$label"
  # A job already stuck in 'queued' means the worker is down: do not pile on more.
  if [ "$(jq '[.jobs[]|select(.status=="queued")]|length' <<<"$jobs")" -gt 0 ]; then skip=1; fi
  while IFS= read -r cfg; do
    name="$(jq -r .name <<<"$cfg")"
    if jq -e --arg n "$name" 'index($n)' <<<"$have" >/dev/null; then exists "$name"; continue; fi
    if [ "$skip" = 1 ]; then echo "  skipped  $name ('$prof' profile worker not running)"; continue; fi
    id="$(api POST "$post" "$cfg" | jq -er .job_id)"; made "$name ($id)"
    if [ "$probed" = 0 ]; then
      probed=1
      job_started "$getp/$id" || { skip=1; echo "  note     '$prof' worker did not pick up the job in ${WAIT_SECS}s; remaining $label skipped (start the '$prof' compose profile and rerun)"; }
    fi
  done
}

seed_jobs "backtests" /backtests /backtests /backtests backtest <<'JSON'
{"name":"demo: momentum, monthly, top quintile","signal_id":"xs_mom_252_21","start":"2025-06-30","end":"2026-09-30","rebalance":"M","quantile":5,"long_short":false,"costs_bps":10}
{"name":"demo: low-vol, monthly, long-short","signal_id":"xs_lowvol_63","start":"2025-06-30","end":"2026-09-30","rebalance":"M","quantile":3,"long_short":true,"costs_bps":10}
JSON

seed_jobs "lab runs" /lab/runs /lab/factors /lab/runs lab <<'JSON'
{"name":"demo: momentum factor study, 21d horizon","factor":"momentum","horizon_days":21,"quantiles":5}
JSON

echo "journal"
have="$(api GET '/journal/entries?limit=1000' | jq -c '[.entries[].title]')"
jr() { # jr title subject probability review_date view thesis invalidation sizing_note
  if jq -e --arg n "$1" 'index($n)' <<<"$have" >/dev/null; then exists "$1"; return; fi
  api POST /journal/entries "$(jq -nc --arg t "$1" --arg s "$2" --argjson p "$3" --arg r "$4" --arg v "$5" --arg th "$6" --arg i "$7" --arg z "$8" \
    '{title:$t,subject:$s,probability:$p,review_date:$r,view:$v,thesis:$th,invalidation:$i,sizing_note:$z}')" >/dev/null
  made "$1"
}
jr "Demo persona: keep the broad index fund as the core" VWCE 0.7 2027-03-31 \
   "Broad global equity stays the core holding; no reason to rotate on a short-term view." \
   "Low cost, wide diversification, and the plan is a 10+ year horizon. Single-stock risk is better taken in small satellite positions." \
   "A sustained rise in fund costs, or a change in the plan's time horizon." \
   "Core stays the largest position; satellites capped at a small share of the portfolio."
jr "Demo persona: mega-cap tech concentration check" MSFT 0.55 2027-01-31 \
   "Large-cap tech weight in the satellite sleeve is acceptable but should not grow unchecked." \
   "AAPL and MSFT already dominate the US sleeve; adding more would raise concentration without adding diversification." \
   "The two names exceed a third of the equity portfolio at a quarterly review." \
   "Hold current size; rebalance toward the core fund if the cap is breached."
jr "Demo persona: crypto stays a small, fixed slice" BTC 0.5 2027-02-28 \
   "Keep the crypto allocation small and rebalance on a schedule rather than on price moves." \
   "High volatility argues for a hard cap and mechanical rebalancing instead of conviction-based sizing." \
   "Allocation drifts above its cap for two consecutive quarter-ends." \
   "Fixed small percentage of net worth; trim back to target each quarter."

echo "notes"
have="$(api GET '/notes/?limit=1000' | jq -c '[.notes[].title]')"
nt() { # nt kind title one_sentence why_it_matters action risk_invalidation subjects-json
  if jq -e --arg n "$2" 'index($n)' <<<"$have" >/dev/null; then exists "$2"; return; fi
  api POST /notes/ "$(jq -nc --arg k "$1" --arg t "$2" --arg o "$3" --arg w "$4" --arg a "$5" --arg r "$6" --argjson s "$7" \
    '{kind:$k,title:$t,one_sentence:$o,why_it_matters:$w,action:$a,risk_invalidation:$r,subjects:$s}')" >/dev/null
  made "$2"
}
nt thesis "Demo thesis: a boring core, small satellites" \
   "A low-cost global index fund as the core with small, capped satellite positions beats frequent rotation for a long-horizon saver." \
   "Costs and behaviour dominate outcomes over long horizons; the structure limits both." \
   "Review allocation once a quarter and rebalance only when a band is breached." \
   "A structural change in fees, taxes or the investment horizon." '["VWCE"]'
nt research_note "Demo note: how to read the signal scoreboard" \
   "The scoreboard ranks logged signals by rank information coefficient; a high IC on a short window is weak evidence." \
   "It keeps the Quant tab honest: signals earn trust through out-of-sample grading, not backtest curves." \
   "Check the horizon and sample size before trusting any single score." \
   "Overlapping windows inflate apparent significance." '["AAPL","MSFT"]'
nt research_note "Demo note: currency exposure of a EUR-based portfolio" \
   "A EUR-based investor holding USD assets carries FX risk that the Portfolio risk lens makes visible." \
   "Currency moves can outweigh a small equity move over a quarter." \
   "Compare the FX drag panel with the allocation view before adding USD exposure." \
   "A sharp change in the EUR/USD regime." '["EURUSD=X"]'

echo "watchlist"
n="$(api GET /watchlist | jq -e length)"
if [ "$n" -gt 0 ]; then exists "watchlist (already populated, left untouched)"
else
  api PUT /watchlist '[{"symbol":"AAPL"},{"symbol":"MSFT"},{"symbol":"VWCE.DE"},{"symbol":"BTC-USD"},{"symbol":"^GSPC"}]' >/dev/null
  made "PUT /watchlist (5 symbols)"
fi

echo "done: $CREATED created, $EXISTED already present"
