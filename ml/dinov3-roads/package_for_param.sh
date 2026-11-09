#!/bin/bash
# Pack the chip set and a 1 km2 inference clip into single archives.
#
#   bash package_for_param.sh C:/ongole_roads C:/param_upload
#
# Why archives rather than `scp -r` of the directory: the chip set is roughly
# 40,000 small files (tif, png, mask, two geojson per chip). scp opens a new
# round trip per file, so a 9 GB directory can take many hours, while the same
# bytes as one tar stream in a fraction of that - and Lustre, which is what
# /scratch is, is markedly happier writing a few big files than 40,000 small
# ones. Tar is used without compression because the tifs and pngs are already
# compressed; gzip would burn CPU for a percent or two.

set -euo pipefail
SRC="${1:-C:/ongole_roads}"
OUT="${2:-C:/param_upload}"
mkdir -p "$OUT"

[ -d "$SRC" ] || { echo "no chip set at $SRC - run chip_dataset.py first"; exit 1; }

echo "=== chip set ==="
du -sh "$SRC"
find "$SRC" -type f | wc -l | xargs echo "files:"

# --force-local, or GNU tar reads "C:/ongole_roads" as host C, path
# /ongole_roads and fails with "Cannot connect to C: resolve failed". The
# colon in a Windows drive letter is the same character it uses for rsh
# targets, and it guesses remote first.
tar --force-local -C "$(dirname "$SRC")" -cf "$OUT/ongole_roads.tar" "$(basename "$SRC")"
ls -la "$OUT/ongole_roads.tar" | awk '{printf "  ongole_roads.tar  %.1f GB\n", $5/1073741824}'

# A checksum so a truncated transfer is caught on arrival rather than halfway
# through the first epoch.
( cd "$OUT" && sha256sum ongole_roads.tar > ongole_roads.tar.sha256 )
echo "  checksum written"

echo
echo "Upload:"
echo "  scp -P 4422 $OUT/ongole_roads.tar{,.sha256} <user>@paramrudra.iitp.ac.in:/scratch/<user>/"
echo
echo "On PARAM:"
echo "  cd /scratch/\$USER && sha256sum -c ongole_roads.tar.sha256 && tar -xf ongole_roads.tar"
