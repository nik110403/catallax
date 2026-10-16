#!/bin/sh
# openbb-data entrypoint. If a free FRED key is supplied (OPENBB_FRED_API_KEY),
# write it into OpenBB's credential store so fred_series / cpi / the release
# calendar work; without it the keyless providers (federal_reserve / ecb /
# econdb) still serve curves + indicators. The key never leaves this container.
set -e

if [ -n "${OPENBB_FRED_API_KEY}" ]; then
  # $HOME, not /root: the hardened demo runs this image as a non-root user with HOME=/tmp.
  CRED_DIR="${HOME:-/root}/.openbb_platform"
  mkdir -p "$CRED_DIR"
  # Build the JSON with python (not shell string interpolation) so a key with a
  # quote/backslash/percent can't corrupt the file or trigger printf expansion.
  export CRED_DIR
  python -c 'import json,os;json.dump({"credentials":{"fred_api_key":os.environ["OPENBB_FRED_API_KEY"]}},open(os.environ["CRED_DIR"]+"/user_settings.json","w"))'
  echo "openbb-data: FRED key configured"
else
  echo "openbb-data: no FRED key — keyless providers only (federal_reserve/ecb/econdb)"
fi

exec uvicorn openbb_core.api.rest_api:app --host 0.0.0.0 --port "${OPENBB_API_PORT:-6900}"
