#!/bin/bash
# netlify-build.sh
#
# Runs automatically as Netlify's build step on every deploy (set as the
# "Build command" in Netlify's UI — see the setup instructions Claude
# gave alongside this file). Replaces the placeholder Supabase key in
# site/index.html with the real key, read from Netlify's own
# environment variables — the same pattern already used for the
# ShipStation credentials in netlify/functions/order-flow.js.
#
# WHY THIS EXISTS: every deploy used to require manually pasting the
# real Supabase key into index.html by hand before pushing — a step
# that has to be done correctly every single time, and eventually
# wasn't (2026-09-23: an old/wrong key got pasted in, causing a full
# dashboard outage — every table came back empty). This script removes
# that manual step entirely: the committed file keeps a placeholder
# (so the real key never sits in git history), and this script fills
# in the real value automatically at deploy time, every time, correctly.
#
# Fails loudly (set -e, explicit check) rather than silently deploying
# a broken page if the environment variable isn't set for any reason.

set -e

if [ -z "$SUPABASE_ANON_KEY" ]; then
  echo "ERROR: SUPABASE_ANON_KEY is not set in this Netlify site's environment variables."
  echo "Add it under Project configuration -> Environment variables before deploying."
  exit 1
fi

sed -i "s|YOUR_ANON_KEY_HERE|${SUPABASE_ANON_KEY}|g" site/index.html

# Confirm the substitution actually happened (catches a placeholder-text
# mismatch after some future edit, rather than silently deploying a
# still-broken page).
if grep -q "YOUR_ANON_KEY_HERE" site/index.html; then
  echo "ERROR: placeholder text still present after substitution — check"
  echo "that site/index.html still contains the expected placeholder string."
  exit 1
fi

echo "Supabase key injected into site/index.html successfully."
