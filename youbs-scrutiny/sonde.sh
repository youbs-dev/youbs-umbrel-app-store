#!/usr/bin/env bash
# Sonde SMART : liste ce que smartctl arrive a lire sur chaque disque.
# Les modes sat et usbjmicron ne font que lire. Le mode jmb39x (disques
# physiques derriere la puce RAID) ecrit puis efface le secteur 33 du volume :
# il n'est essaye que si /config/autoriser-jmb39x existe.
out=/out/sonde.txt
{
  echo "Sonde du $(date '+%Y-%m-%d %H:%M:%S')"
  smartctl --version | head -1
  echo
  echo "===== smartctl --scan-open"
  smartctl --scan-open
  for d in /dev/sd?; do
    [ -b "$d" ] || continue
    for t in sat usbjmicron usbjmicron,x; do
      echo
      echo "===== $d -d $t (lecture seule)"
      smartctl -i -H -A -d "$t" "$d"
    done
    echo
    echo "===== $d secteur 33 (lecture seule, doit etre vide pour jmb39x)"
    dd if="$d" bs=512 skip=33 count=1 2>/dev/null | od -A d -t x1 | head -5
    if [ -e /config/autoriser-jmb39x ]; then
      for n in 0 1 2 3 4; do
        echo
        echo "===== $d -d jmb39x,$n"
        smartctl -x -d "jmb39x,$n" "$d"
      done
    else
      echo
      echo "===== mode jmb39x non autorise (dossier data/config/autoriser-jmb39x absent)"
    fi
  done
  for d in /dev/nvme?; do
    [ -c "$d" ] || continue
    echo
    echo "===== $d"
    smartctl -i -H "$d"
  done
} > "$out" 2>&1
chmod 644 "$out"
