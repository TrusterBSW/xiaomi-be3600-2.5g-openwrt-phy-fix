#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
#
# Scan the SoC MDIO bus and print the PHY ID at every address.
# Run this ON the router (it needs ssdk_sh from the vendor image).
#
# This is how the QCA8081 was located. Expected output on a BE3600 2.5G:
#
#   addr  0: 0xffff 0xffff            <- nothing, yet this is what the DT declares
#   addr 12: 0x004d 0xd101            <- QCA8081, the 2.5G PHY
#   addr 29: 0xdead 0xdead            <- YT9215 switch (its driver's sentinel)
#
# PHY ID = (reg2 << 16) | reg3. Useful values:
#   0x004dd101  Qualcomm QCA8081   2.5G/1G, SGMII+
#   0x004dd0b1  Qualcomm QCA8072/5
#
# To confirm which physical socket a PHY drives, plug a cable into it and
# re-read register 1 (BMSR): bit 2 is link status.
#   before: 0x7949  (bit2=0, no link)
#   after:  0x796d  (bit2=1, link up)

read_reg() {
    printf 'debug phy get %d %d\nquit\n' "$1" "$2" \
        | ssdk_sh 2>/dev/null | grep -o '0x[0-9a-f]*' | head -1
}

if ! command -v ssdk_sh >/dev/null 2>&1; then
    echo "ssdk_sh not found -- run this on the router, not on your PC" >&2
    exit 1
fi

echo "MDIO bus scan (PHY ID registers 2 and 3)"
addr=0
while [ "$addr" -le 31 ]; do
    id1=$(read_reg "$addr" 2)
    id2=$(read_reg "$addr" 3)
    case "$id1" in
        0xffff|'') note='' ;;
        0xdead)    note='   <- switch driver sentinel' ;;
        *)         note='   <- PHY present' ;;
    esac
    printf 'addr %2d: %s %s%s\n' "$addr" "${id1:-0xffff}" "${id2:-0xffff}" "$note"
    addr=$((addr + 1))
done
