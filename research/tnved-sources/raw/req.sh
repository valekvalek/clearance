#!/bin/bash
# обёртка: каждый запрос пишется в requests.log (счётчик лимита 150), пауза 2 с
D=/Users/hw_095/Documents/proekt/clearance/research/tnved-sources/raw
mode=$1; url=$2; out=$3
sleep 2
echo "$(date +%H:%M:%S) $mode $url" >> $D/requests.log
if [ "$mode" = HEAD ]; then curl -sIL -m 30 -A "tnved-research/1.0 (contact: project owner)" "$url" | grep -iE "^HTTP|content-length|content-type|location" | tail -6
else curl -sL -m 60 -A "tnved-research/1.0 (contact: project owner)" -o "${out:-/dev/stdout}" -w "HTTP %{http_code} size %{size_download}\n" "$url"; fi
