# Xiaomi BE3600 2.5G (RD15) — fixing the dead 2.5G port under OpenWrt

> **Hardware:** Xiaomi Router BE3600 2.5G, hardware model **RD15** — IPQ5332,
> 256 MB RAM. See [Identifying your device](#identifying-your-device).
>
> **No support is provided.** Read [About this repository](#about-this-repository)
> before opening an issue.

The unofficial OpenWrt images floating around for the **Xiaomi Router BE3600 2.5G
(RD15, IPQ5332)** leave the 2.5 Gb/s port completely dead: `eth1` never appears
and `dmesg` fills up with PHY errors, once per second, forever.

The cause is a **wrong MDIO address in the device tree**. The 2.5G PHY is a
Qualcomm **QCA8081** that answers at **MDIO address 12**, but the device tree
declares it at address **0**. The kernel already ships the right driver — only
three numbers are wrong.

This repository contains the diagnosis and a patch script. **It contains no
firmware images**; see [Why no binaries](#why-there-are-no-binaries-here).

Result after the fix, on real hardware:

```
nss-dp 3a500000.dp2 eth1: PHY Link up speed: 2500

# ethtool eth1
        Speed: 2500Mb/s
        Duplex: Full
        Link detected: yes
```

## Identifying your device

This applies to the **RD15** hardware revision — the BE3600 2.5G, the one with a
2.5 Gb/s socket and three gigabit ones. The **RD16** (512 MB RAM) is a different
board and was not looked at.

The vendor device tree does not spell out "RD15" (it says `Xiamo Be3600` —
the manufacturer's own typo), so check one of these:

On **stock Xiaomi firmware**, over SSH or telnet:

```console
# grep HARDWARE /etc/config/fw_ver
        option HARDWARE 'RD15'
# nvram get model
RD15
```

On the **vendor OpenWrt build** this fix targets:

```console
# cat /proc/device-tree/model
Xiamo Be3600, Inc. IPQ5332/AP-MI04.1-C2
# cat /etc/openwrt_release
DISTRIB_RELEASE='19.07-SNAPSHOT'
DISTRIB_TARGET='ipq53xx/ipq53xx_32'
DISTRIB_REVISION='R24.7'
DISTRIB_DESCRIPTION='Nwrt (QSDK 12.4) '
```

Kernel `5.4.213`, image built 2024-06-27. Other builds may differ; the patch
script refuses to touch anything whose values are not exactly what it expects.

## Symptoms

```
libphy: PHY 90000.mdio:00 not found
nss-dp: probe of 3a500000.dp2 failed with error -14
ssdk_phy_driver_init[373]:INFO:dev_id = 0, phy_adress = 0, phy_id = 0xffffffff phytype doesn't match
hsl_phy_phydev_get[805]:ERROR:phy_addr 0 phydev is NULL      <- repeats every second
```

`-14` is `-ENODEV`: `dp2`, the netdev behind the 2.5G port, cannot probe because
no PHY answers at the declared address. Full before/after logs are in
[`logs/`](logs/).

## The fix

Three device-tree properties hold the address. **All three matter**, and this is
the part that costs time if you find only some of them: the first two make the
PHY bind, but `eth1` still will not exist. Only the third creates the interface.

| Property | Effect |
| --- | --- |
| `/soc/mdio@90000/ethernet-phy@0/reg` | registers the PHY on the MDIO bus |
| `.../ess-switch@3a000000/qcom,port_phyinfo/port@0/phy_address` | SSDK port ↔ PHY map |
| `/soc/dp2/qcom,phy-mdio-addr` | **creates `eth1`** via `qca_nss_dp` |

Each is a 4-byte cell, so the device tree keeps its exact size and the image can
be patched in place. Two consequences:

* the FIT's `fdt@1` **crc32 and sha1 must be recomputed** — U-Boot verifies them
  at boot (`Verifying Hash Integrity ... crc32+ sha1+ OK`);
* **nothing needs recomputing at the UBI level** — every volume in these images
  is *dynamic*, and dynamic volumes carry no per-LEB data CRC. The script
  asserts this rather than assuming it.

`patch_phy.py` does all of it, and refuses to run on an image whose values are
not what it expects.

```console
$ ./patch_phy.py --dry-run vendor-image.bin
kernel volume: 31 LEBs, 3936256 bytes
device tree:   55999 bytes at volume offset 0x3a0edc
original hashes verified -- extraction is faithful
  patch //soc/mdio@90000/ethernet-phy@0/reg: 0 -> 12   (MDIO registration)
  patch //soc/ess-instance/ess-switch@3a000000/qcom,port_phyinfo/port@0/phy_address: 0 -> 12   (SSDK port<->PHY map)
  patch //soc/dp2/qcom,phy-mdio-addr: 0 -> 12   (eth1 creation by qca_nss_dp)
new fdt@1 hashes: crc32=237e29c8 sha1=8a8a6eba576359a9e2320cb7af25be6f2dad0e5f
dry run -- nothing written
```

Before writing anything the script re-verifies the vendor image's *own* stored
hashes against the device tree it extracted. If those match, the UBI and FIT
parsing is byte-exact — a cheap and strong self-check.

## Applying it

### Live, on a running router (recommended)

The device tree lives in the UBI volume `kernel`, which is **not mounted** — the
kernel is loaded into RAM at boot. So it can be rewritten in place, with no
bootloader, no full reflash, and no cable moving. The rootfs and your
configuration are untouched.

```sh
./patch_phy.py --extract-kernel vendor-image.bin patched.bin
scp patched.bin.fit root@192.168.1.1:/tmp/k.fit
ssh root@192.168.1.1 'ubiupdatevol /dev/ubi0_1 /tmp/k.fit'
# verify the readback before rebooting
ssh root@192.168.1.1 'dd if=/dev/ubi0_1 bs=4096 2>/dev/null | head -c 3862220 | md5sum'
ssh root@192.168.1.1 reboot
```

Keep the original volume first, so you can roll back with the same command:

```sh
ssh root@192.168.1.1 'dd if=/dev/ubi0_1 bs=4096' | head -c 3862220 > kernel-orig.fit
```

> Older dropbear builds only offer `ssh-rsa`. If your client refuses to connect,
> add `-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa`.

### Full image, via the bootloader

Use `patched.bin` with the U-Boot web recovery, as you would any full image.
See [Recovery](#recovery).

## Hardware topology

Worth knowing, because the naming is misleading — the U-Boot log says
"start yt 8821 init", which sends you looking for a Motorcomm YT8821 that has
nothing to do with the 2.5G port.

| SSDK port | Hardware | Linux netdev |
| --- | --- | --- |
| port 2 (`dp1`) | fixed 2500base-x SERDES link → **YT9215** switch → 3 gigabit ports | `eth0` |
| port 1 (`dp2`) | **QCA8081** PHY → 2.5G port | `eth1` |

`eth0 carrier=1` permanently is normal: it is the fixed internal link to the
switch chip, independent of whether any gigabit socket has a cable.

## How the PHY was located

[`tools/mdio-scan.sh`](tools/mdio-scan.sh), run on the router, walks all 32 MDIO
addresses using the vendor's `ssdk_sh`:

```
addr  0: 0xffff 0xffff            <- nothing, yet this is what the DT declares
addr 12: 0x004d 0xd101            <- QCA8081
addr 29: 0xdead 0xdead            <- YT9215 switch (its driver's sentinel)
```

`0x004DD101` is the QCA8081 PHY ID, per the
[upstream kernel driver](https://lore.kernel.org/netdev/20210816113440.22290-1-luoj@codeaurora.org/).

To prove address 12 really drives the 2.5G socket rather than something else,
plug a cable in and re-read register 1 (BMSR); bit 2 is link status:

```
before: 0x7949   (bit2 = 0, no link)
after:  0x796d   (bit2 = 1, link up)
```

## Recovery

The modified U-Boot on these devices **only brings up the 2.5G port** — it
starts `E_P0` and leaves the gigabit switch ports initialised but stopped. Any
bootloader recovery therefore needs the cable on the 2.5G socket, which is the
opposite of what you need once Linux is running.

1. Cable from your PC to the router's **2.5G** port; give your PC `192.168.1.2/24`
2. Interrupt autoboot over serial (115200 8N1) to reach the `IPQ5332#` prompt
3. `httpd 192.168.1.1`
4. `curl -F "firmware=@patched.bin" http://192.168.1.1/`

A serial adapter is strongly recommended for any work on these devices.

## ⚠️ Network loops

Once `eth1` exists, **do not connect the 2.5G port and a gigabit port to the
same switch**. They end up bridged, and U-Boot also forwards between them during
boot. The resulting broadcast storm hit the freshly-enabled port during this
work and produced:

```
Unable to handle kernel paging request at virtual address 0f617038
Kernel panic - not syncing: Fatal exception in interrupt
PC is at [qca_nss_dp+0x17000]
```

Recovered from the U-Boot prompt with no data loss, but do not reproduce it.
Enabling STP (`uci set network.lan.stp='1'`) is worth doing as a guard rail; one
uplink cable remains the actual rule.

## Why there are no binaries here

The patched images are derivatives of Xiaomi's proprietary OpenWrt build and
carry Qualcomm QSDK binary blobs (`qca-ssdk`, `qca_nss_*`, the wlan drivers).
Redistributing them is not something this repository is in a position to do, and
the base images themselves come from an unattributed third-party package.

The patch script removes the need: apply it to whichever image you already have.

## Scope and caveats

* Tested on **one** unit: BE3600 2.5G, RD15, IPQ5332, 256 MB RAM.
* Against vendor OpenWrt `19.07-SNAPSHOT`, kernel `5.4.213`, build dated
  2024-06-27. Other builds may differ; the script will refuse to patch anything
  whose values are not exactly `0`.
* Not related to mainline OpenWrt, which
  [does not support this device](https://forum.openwrt.org/t/does-xiaomi-be3600-rd15-support-openwrt/188234)
  — ipq53xx is unported and 256 MB of RAM is considered a dead end upstream.
* The RD16 variant (512 MB) is a different board and was not looked at.

## About this repository

The diagnosis and the code here were produced by **Claude** (Anthropic's Claude
Opus, run through Claude Code), working with me on the actual device: reading
the vendor device tree, scanning the MDIO bus over SSH, writing and verifying
the patch script, and confirming the result on the running router over a serial
console. Every log in this repository was captured from that hardware.

I am publishing it because it works and nobody had documented it — not because
I can maintain it. **I will not be able to provide support.** I do not have the
background to debug someone else's device, and issues or questions will most
likely go unanswered. Please do not read silence as rudeness; it is honesty
about what I can offer.

So: take what is useful, fork it, adapt it. Everything needed to redo the
reasoning from scratch is written down above — the MDIO scan, the three
properties, the hash and UBI constraints — precisely so that nobody has to
depend on me. And read [Scope and caveats](#scope-and-caveats) before flashing
anything: this was verified on one unit, not on yours.

## Credits

The device-tree diagnosis, the MDIO scan and `patch_phy.py` are original work.

The base firmware images, the enlarged partition table (`mibib`) and the
modified U-Boot come from a third-party Chinese package of unclear authorship;
none of that is included or claimed here. The U-Boot web recovery is
[pepe2k/u-boot_mod](https://github.com/pepe2k/u-boot_mod). SSH on stock firmware
is enabled with [openwrt-xiaomi/xmir-patcher](https://github.com/openwrt-xiaomi/xmir-patcher).

## License

GPL-2.0-only. See [LICENSE](LICENSE).
