#!/bin/bash
# Complete 2021 + 2022 (halves 2,3,4) today; stop once half >= 5
cd /home/ubuntu/mitene_download
for i in 1 2 3 4 5 6; do
  /home/ubuntu/.hermes/hermes-agent/venv/bin/python3 sync_year.py >> logs/year_sync.log 2>&1
  HALF=$(/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -c "import json;print(json.load(open('.year_sync.json')).get('half',1))")
  echo "[loop] after run $i: half=$HALF" >> logs/year_sync.log
  [ "$HALF" -ge 5 ] && break
done
echo "[loop] done at half=$HALF" >> logs/year_sync.log