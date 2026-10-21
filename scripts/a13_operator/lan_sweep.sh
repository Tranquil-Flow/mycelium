#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Read-only LAN discovery: is the laptop on this /24 under a different address?
# Every action here is a probe that changes nothing (ping + arp reads).  No interface is touched --
# see ERRATA/decisions 04:45: a state-changing command used as a probe took the laptop off-network.
set -u
SUBNET=10.192.190
echo "=== sweeping $SUBNET.1-254 (bounded) ==="
for i in $(seq 1 254); do
  ping -c 1 -W 200 "$SUBNET.$i" >/dev/null 2>&1 &
done
wait
echo "=== ARP table: any of the laptop's known MACs? ==="
echo "  wi-fi  ac:07:75:40:b3:76"
echo "  dock   42:a8:e6:1e:ca:d6 / :d7 / :d9"
arp -an | grep -i "ac:07:75:40:b3:76\|42:a8:e6:1e:ca" || echo "  none of the laptop's MACs are on this LAN"
echo "=== all neighbours found ==="
arp -an | awk '{print "  " $2, $4}' | head -20
echo "=== mDNS (no Tailscale involved) ==="
ping -c 2 -t 3 member-mac.local 2>&1 | tail -2
