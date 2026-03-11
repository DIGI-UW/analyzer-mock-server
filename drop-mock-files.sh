#!/usr/bin/env bash
# Drop mock analyzer files into import directories for harness E2E testing.
#
# Prerequisites: Docker running with dev.docker-compose (volume/analyzer-imports mounted).
# Usage: ./drop-mock-files.sh [base-dir]
#   base-dir: Path to analyzer-imports (default: ../../volume/analyzer-imports from script dir)
#
# Creates incoming dirs and drops one mock file per analyzer:
#   e2e-csv/incoming, e2e-qs/incoming, e2e-tecan/incoming, e2e-multiskan/incoming,
#   e2e-fluorocycler/incoming, e2e-dtprime/incoming

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${1:-$SCRIPT_DIR/../../volume/analyzer-imports}"

mkdir -p "$BASE_DIR/e2e-csv/incoming" "$BASE_DIR/e2e-qs/incoming" \
  "$BASE_DIR/e2e-tecan/incoming" "$BASE_DIR/e2e-multiskan/incoming" \
  "$BASE_DIR/e2e-fluorocycler/incoming" "$BASE_DIR/e2e-dtprime/incoming"

cd "$SCRIPT_DIR"
TS=$(date +%Y%m%d_%H%M%S)

python3 generate_file.py -t quantstudio7 -o "$BASE_DIR/e2e-qs/incoming/QS7_$TS.xls" -c 2
python3 generate_file.py -t tecan_f50 -o "$BASE_DIR/e2e-tecan/incoming/tecan_$TS.csv" -c 2
python3 generate_file.py -t multiskan_fc -o "$BASE_DIR/e2e-multiskan/incoming/multiskan_$TS.csv" -c 2
python3 generate_file.py -t fluorocycler_xt -o "$BASE_DIR/e2e-fluorocycler/incoming/FC-XT_$TS.xlsx" -c 2
python3 generate_file.py -t dtprime -o "$BASE_DIR/e2e-dtprime/incoming/Resultat_DT-Prime-$TS.xml" -c 2

# E2E-CSV uses simple CSV - create with generic template if available, else placeholder
if [ -f templates/hain_fluorocycler.json ]; then
  python3 generate_file.py -t hain_fluorocycler -o "$BASE_DIR/e2e-csv/incoming/e2e_$TS.csv" -c 2
else
  echo "SampleID,TestCode,Result" > "$BASE_DIR/e2e-csv/incoming/e2e_$TS.csv"
  echo "SIM-001,VL,1500" >> "$BASE_DIR/e2e-csv/incoming/e2e_$TS.csv"
  echo "SIM-002,VL,0" >> "$BASE_DIR/e2e-csv/incoming/e2e_$TS.csv"
fi

echo "Dropped mock files to $BASE_DIR"
ls -la "$BASE_DIR"/*/incoming/
