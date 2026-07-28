#!/bin/zsh
# Drives the 8-run backtest matrix: power {5,10}kW x saldering {on,off} x battery {27.9kWh, ~0 baseline}
# Sums the final "cost" column (positive = earning). Battery value = sum(battery) - sum(baseline).
cd "$(dirname "$0")"
PY=.venv/bin/python
export BT_START=20250701 BT_END=20260630 BT_STARTHOUR=0 BT_INITCHARGE=0 \
       BT_MINSOC=10 BT_RTE=90 BT_ETAX=0.1228 BT_XMLAVAIL=N BT_OVERWRITE=Y BT_PRICE_CACHE=price_cache
mkdir -p results
: > results/summary.tsv
print "power_kW\tsaldering\tbattery\tsum_cost_eur\trows" >> results/summary.tsv

for power in 5000 10000; do
  for sald in off on; do
    for cap in 27900 1; do
      [[ $sald == on ]] && nflag="-n" || nflag=""
      [[ $cap == 27900 ]] && btag="batt" || btag="base"
      ptag=$((power/1000))kw
      tag=${ptag}_${sald}_${btag}
      export BT_CAP=$cap BT_MAXCHG=$power BT_MAXDIS=$power
      $PY Marstek-planning.py -s -p -u -b -h $nflag > results/_run_${tag}.log 2>&1
      mv entsoe-output20250701.txt results/out_${tag}.txt
      line=$(LC_NUMERIC=C awk -v p=$((power/1000)) -v s=$sald -v b=$btag \
        'NR>1{c+=$NF} END{printf "%s\t%s\t%s\t%.2f\t%d",p,s,b,c,NR-1}' results/out_${tag}.txt)
      print "$line" >> results/summary.tsv
      print "done: $tag -> $line"
    done
  done
done
print "=== summary.tsv ==="
cat results/summary.tsv
