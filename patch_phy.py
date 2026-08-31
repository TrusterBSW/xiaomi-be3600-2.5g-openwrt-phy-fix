#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""
Fix the 2.5G PHY MDIO address in a Xiaomi BE3600 2.5G (RD15) OpenWrt UBI image.

The vendor images declare the 2.5G port's PHY at MDIO address 0. The chip is a
Qualcomm QCA8081 that actually answers at address 12, so the PHY is never bound
and the eth1 netdev is never created:

    libphy: PHY 90000.mdio:00 not found
    nss-dp: probe of 3a500000.dp2 failed with error -14
    ssdk_phy_driver_init: phy_adress = 0, phy_id = 0xffffffff phytype doesn't match
    hsl_phy_phydev_get[805]:ERROR:phy_addr 0 phydev is NULL   (once per second)

Three device-tree properties carry the wrong address. All three are required:
the first two make the PHY bind, the third makes qca_nss_dp create eth1.

    /soc/mdio@90000/ethernet-phy@0/reg                             MDIO registration
    .../ess-switch@3a000000/qcom,port_phyinfo/port@0/phy_address   SSDK port<->PHY map
    /soc/dp2/qcom,phy-mdio-addr                                    eth1 creation

Each is a 4-byte cell, so the device tree keeps its exact size and the image can
be patched in place. Two things still have to be recomputed:

  * the crc32 and sha1 hashes of the FIT's fdt@1 node, which U-Boot verifies
    at boot ("Verifying Hash Integrity ... crc32+ sha1+ OK");
  * nothing at the UBI level -- every volume in these images is *dynamic*, and
    dynamic volumes carry no per-LEB data CRC. The script asserts this.

Usage:
    ./patch_phy.py vendor-image.bin patched-image.bin
    ./patch_phy.py --dry-run vendor-image.bin
    ./patch_phy.py --extract-kernel vendor-image.bin patched-image.bin

--extract-kernel additionally writes <patched-image>.fit, the patched `kernel`
UBI volume alone. That is the fastest way to apply the fix to a running device,
since the device tree lives in a volume that is not mounted:

    scp patched-image.fit root@<router>:/tmp/k.fit
    ssh root@<router> 'ubiupdatevol /dev/ubi0_1 /tmp/k.fit && reboot'

Verified on: Xiaomi BE3600 2.5G (RD15), IPQ5332, vendor OpenWrt 19.07-SNAPSHOT,
kernel 5.4.213. Result: eth1 at 2500Mb/s full duplex.
"""

import argparse
import hashlib
import struct
import sys
import zlib

PEB_SIZE = 0x20000          # physical erase block
DATA_OFFSET = 0x1000        # EC header + VID header
LEB_SIZE = PEB_SIZE - DATA_OFFSET
KERNEL_VOL_ID = 1
CORRECT_PHY_ADDR = 12

UBI_VOL_TYPE_DYNAMIC = 1

TARGETS = (
    ('mdio@90000/ethernet-phy@0', 'reg', 'MDIO registration'),
    ('qcom,port_phyinfo/port@0', 'phy_address', 'SSDK port<->PHY map'),
    ('dp2', 'qcom,phy-mdio-addr', 'eth1 creation by qca_nss_dp'),
)


def map_volume(image, vol_id):
    """Return [(lnum, file_offset_of_data)] for one UBI volume, sorted by lnum."""
    blocks = []
    for peb in range(0, len(image), PEB_SIZE):
        if image[peb:peb + 4] != b'UBI#':
            continue
        vid_off, data_off = struct.unpack_from('>II', image, peb + 16)
        vid = image[peb + vid_off:peb + vid_off + 64]
        if vid[:4] != b'UBI!':
            continue
        if vid[4] != UBI_VOL_TYPE_DYNAMIC:
            sys.exit('error: UBI volume is not dynamic; per-LEB CRCs would need '
                     'recomputing and this script does not do that')
        this_vol, lnum = struct.unpack_from('>II', vid, 8)
        if this_vol == vol_id:
            blocks.append((lnum, peb + data_off))
    if not blocks:
        sys.exit('error: UBI volume %d not found -- is this a BE3600 UBI image?' % vol_id)
    return sorted(blocks)


def read_volume(image, blocks):
    return b''.join(bytes(image[off:off + LEB_SIZE]) for _, off in blocks)


def logical_to_file(blocks, offset):
    leb, rem = divmod(offset, LEB_SIZE)
    return blocks[leb][1] + rem


def write_logical(image, blocks, offset, data):
    """Write across LEB boundaries, which are not contiguous in the file."""
    for i, byte in enumerate(data):
        image[logical_to_file(blocks, offset + i)] = byte


def parse_fdt(blob):
    """Minimal flattened-device-tree walker.

    Returns [(path, property_name, value, value_offset)].
    """
    if blob[:4] != b'\xd0\x0d\xfe\xed':
        sys.exit('error: not a flattened device tree')
    (_, _, off_struct, off_strings, _, _, _, _,
     size_strings, size_struct) = struct.unpack_from('>10I', blob, 0)
    strings = blob[off_strings:off_strings + size_strings]
    pos, end, path, props = off_struct, off_struct + size_struct, [], []
    while pos < end:
        token, = struct.unpack_from('>I', blob, pos)
        pos += 4
        if token == 1:                                  # FDT_BEGIN_NODE
            name = blob[pos:blob.index(b'\0', pos)].decode('latin1')
            pos += (len(name) + 4) & ~3
            path.append(name)
        elif token == 2:                                # FDT_END_NODE
            path.pop()
        elif token == 3:                                # FDT_PROP
            length, name_off = struct.unpack_from('>II', blob, pos)
            pos += 8
            name = strings[name_off:strings.index(b'\0', name_off)].decode('latin1')
            props.append(('/'.join(path), name, blob[pos:pos + length], pos))
            pos += (length + 3) & ~3
        elif token == 9:                                # FDT_END
            break
    return props


def find_unique(props, path_suffix, name):
    hits = [p for p in props if p[0].endswith(path_suffix) and p[1] == name]
    if len(hits) != 1:
        sys.exit('error: expected exactly one %s/%s, found %d'
                 % (path_suffix, name, len(hits)))
    return hits[0]


def main():
    ap = argparse.ArgumentParser(
        description='Fix the 2.5G PHY MDIO address in a Xiaomi BE3600 2.5G OpenWrt image.')
    ap.add_argument('source', help='vendor UBI image')
    ap.add_argument('output', nargs='?', help='patched UBI image (omit with --dry-run)')
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would change, write nothing')
    ap.add_argument('--extract-kernel', action='store_true',
                    help='also write <output>.fit, the patched kernel volume alone')
    args = ap.parse_args()
    if not args.dry_run and not args.output:
        ap.error('an output path is required unless --dry-run is given')

    image = bytearray(open(args.source, 'rb').read())
    blocks = map_volume(image, KERNEL_VOL_ID)
    volume = read_volume(image, blocks)
    print('kernel volume: %d LEBs, %d bytes' % (len(blocks), len(volume)))

    fit = parse_fdt(volume)
    fdt_node = find_unique(fit, 'images/fdt@1', 'data')
    crc_node = find_unique(fit, 'images/fdt@1/hash@1', 'value')
    sha_node = find_unique(fit, 'images/fdt@1/hash@2', 'value')
    if find_unique(fit, 'images/fdt@1/hash@1', 'algo')[2].rstrip(b'\0') != b'crc32':
        sys.exit('error: hash@1 is not crc32')
    if find_unique(fit, 'images/fdt@1/hash@2', 'algo')[2].rstrip(b'\0') != b'sha1':
        sys.exit('error: hash@2 is not sha1')

    dtb_off, dtb = fdt_node[3], bytearray(fdt_node[2])
    print('device tree:   %d bytes at volume offset 0x%x' % (len(dtb), dtb_off))

    # If the stored hashes match what we extracted, our UBI/FIT reading is exact.
    if struct.unpack('>I', crc_node[2])[0] != zlib.crc32(bytes(dtb)) & 0xffffffff:
        sys.exit('error: stored crc32 does not match extracted device tree')
    if sha_node[2] != hashlib.sha1(bytes(dtb)).digest():
        sys.exit('error: stored sha1 does not match extracted device tree')
    print('original hashes verified -- extraction is faithful')

    for suffix, name, why in TARGETS:
        path, _, value, offset = find_unique(parse_fdt(dtb), suffix, name)
        current = struct.unpack('>I', value)[0]
        if current == CORRECT_PHY_ADDR:
            sys.exit('error: /%s/%s is already %d -- image looks already patched'
                     % (path, name, CORRECT_PHY_ADDR))
        if current != 0:
            sys.exit('error: /%s/%s is %d, expected 0 -- unknown image variant'
                     % (path, name, current))
        struct.pack_into('>I', dtb, offset, CORRECT_PHY_ADDR)
        print('  patch /%s/%s: %d -> %d   (%s)' % (path, name, current, CORRECT_PHY_ADDR, why))

    new_crc = struct.pack('>I', zlib.crc32(bytes(dtb)) & 0xffffffff)
    new_sha = hashlib.sha1(bytes(dtb)).digest()
    print('new fdt@1 hashes: crc32=%s sha1=%s' % (new_crc.hex(), new_sha.hex()))

    if args.dry_run:
        print('dry run -- nothing written')
        return

    write_logical(image, blocks, dtb_off, bytes(dtb))
    write_logical(image, blocks, crc_node[3], new_crc)
    write_logical(image, blocks, sha_node[3], new_sha)
    open(args.output, 'wb').write(bytes(image))
    print('wrote %s (%d bytes)' % (args.output, len(image)))

    if args.extract_kernel:
        patched = read_volume(image, map_volume(image, KERNEL_VOL_ID))
        fit_size, = struct.unpack_from('>I', patched, 4)   # FIT header totalsize
        path = args.output + '.fit'
        open(path, 'wb').write(patched[:fit_size])
        print('wrote %s (%d bytes) -- for ubiupdatevol /dev/ubi0_1' % (path, fit_size))


if __name__ == '__main__':
    main()
