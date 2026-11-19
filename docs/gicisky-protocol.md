# Gicisky BLE e-paper tags: protocol notes

Status: **research only. Nothing in this document has been verified on hardware.** Every
claim comes from reading public community implementations (see [Sources](#sources)) and is
tagged with how well it is supported:

| Tag | Meaning |
| --- | --- |
| **[multi]** | Confirmed by more than one independent source. |
| **[single]** | Found in one source only (the two eigger repositories count as one source, because they share an author and code lineage). |
| **[inferred]** | Not stated by any source; worked out from the sources or from BLE/bleak behaviour. |
| **[conflict]** | Sources disagree; both readings are given and a hardware test decides. |

This document is the input for workstream F (`gateway/drivers/gicisky.py`). It is written in
our own words; no code from any source is reproduced. OpenEPaperLink (CC BY-NC-SA 4.0) was
read to understand the protocol only.

Section 8 of `docs/PLAN.md` requires this file to exist, with its sources and their licences,
before any M4 driver code is written.

Gicisky tags are also sold as **PickSmart**. Gicisky is a trademark of its owner; this is an
independent description of a protocol that the vendor does not document.

---

## 1. Advertising

### 1.1 What identifies a Gicisky tag

| Item | Value | Support |
| --- | --- | --- |
| Advertisement type | Connectable advertising; the tag must be connected to for an update. | [multi] |
| Manufacturer company ID | `0x5053` (decimal 20563). On air the AD structure reads `FF 53 50 …`, because the ID is little-endian, so it shows as ASCII "SP". | [multi] |
| Manufacturer payload length | Exactly **5 bytes** after the company ID (7 bytes if you count the company ID, which is how OpenEPaperLink counts). Anything else is not a stock Gicisky tag. | [multi] |
| Local name | **Unknown.** No source filters by name, and the atc1441 uploader accepts any device. S8's README reports names of the form `NEMR` + 8 characters; see Q11. Do not rely on the name. | open question Q11 |
| Advertised service UUIDs | hass-gicisky only parses the manufacturer data while iterating over the advertised service UUIDs, so tags seem to advertise at least one 16-bit service UUID. hass-ble-esl lists the GATT service as one of `0xFEF0`, `0xFDF0` or `0xFCF0`. | [inferred] / [single] |
| Company ID shared with other products | hass-ble-esl fixed a discovery bug for "devices sharing manufacturer ID", and the hass-gicisky tracker has an ESL-29BWR sold under the Gicisky name that is not a Gicisky tag. Filter on company ID **and** a 5-byte payload **and** a known device ID. | [single] |

With bleak, `AdvertisementData.manufacturer_data` is a dict keyed by company ID, so the
driver looks up `0x5053` and gets the 5 payload bytes `d[0]..d[4]` with the company ID
already stripped. [inferred, from bleak's API]

### 1.2 Manufacturer payload layout (5 bytes, after company ID)

| Byte | Meaning | Encoding | Support |
| --- | --- | --- | --- |
| `d[0]` | Hardware word, low byte | see 1.3 | [multi] (hass-gicisky, hass-ble-esl, atc1441, OEPL) |
| `d[1]` | Battery voltage | unsigned, units of 0.1 V (`0x1D` = 29 = 2.9 V); `battery_mv = d[1] * 100` | [multi] (hass-gicisky, OEPL) |
| `d[2]`, `d[3]` | Firmware version | 16-bit **big-endian**, `(d[2] << 8) \| d[3]`, e.g. `0x8101` | [multi] (hass-gicisky, OEPL) |
| `d[4]` | Hardware word, high byte | see 1.3 | [multi] |

The **hardware word** is `hw = (d[4] << 8) | d[0]`. The **device ID** that selects the model
is `hw & 0x3FFF` (the top two bits are flags). [multi]

Battery resolution is 100 mV, which is coarse. hass-gicisky maps 2.5 V to 0 % and 2.9 V to 100 %
linearly and calls the tag "low" at or below 2.5 V; it notes that BLE keeps working below
2.5 V but the e-paper refresh becomes unreliable. That range is one author's choice. [single]

Example from hass-gicisky's comments for a 2.9" tag: payload `33 1D 81 01 40` gives
`hw = 0x4033`, device ID `0x0033`, battery 2.9 V, firmware `0x8101`. [single]

### 1.3 Hardware word bit fields

atc1441's uploader and OpenEPaperLink both decode the hardware word into fields. They agree on
most of it:

| Bits | Field | Values | Support |
| --- | --- | --- | --- |
| 0 | "single/double image" flag | read but not used by either source | [multi] |
| 1–2 | Colours | 0 = BW, 1 = BWR, 2 = BWY, 3 = BWRY | [multi] |
| 3–4 | Panel type | 0 = TFT, 1 = EPA, 2 = EPA1, 3 = EPA2 (labels only) | [multi] |
| 5–10 | Resolution index | see the table below | [multi] |
| 12–13 | Extra colour bits | atc1441 adds `(hw >> 10) & 12` to the colour value (4 = BWRGBYO); OEPL computes it and ignores it | [multi], unused for our tags |
| 14 | Compression disable | 1 = send uncompressed, 0 = tag accepts compressed data | [multi] (atc1441, OEPL); hass masks it off |
| 15 | Unknown | — | — |

Resolution index (native panel geometry, **width × height as the panel scans it**):

| Index | Native W × H |
| --- | --- |
| 0 | 104 × 212 |
| **1** | **128 × 296** (the 2.9" panels) |
| 2 | 400 × 300 |
| 3 | 384 × 640 |
| 4 | 640 × 960 |
| 5 | 132 × 250 |
| 6 | 96 × 196 |
| 7 | 480 × 640 |
| 8 | 128 × 250 |
| 9 | 800 × 480 |
| 10 | 480 × 280 |

[multi] for index 1; the table as a whole comes from atc1441 and matches OEPL's 11 entries.

**Mirroring: [conflict].** atc1441 turns on mirroring from the panel type (types 0, 1 and 3
mirror, type 2 does not) and ignores bit 8. OpenEPaperLink takes mirroring from **bit 8**
(which overlaps the resolution field) and never from the panel type. hass-gicisky and
hass-ble-esl use neither: they hold a fixed per-ID table, and no 2.9" entry there is mirrored.
See section 2 and Q2.

**The driver should not decode the fields** to pick a model. Match the full device ID against
the explicit table in section 2 and treat unknown IDs as unsupported. That is what hass-gicisky
and hass-ble-esl do, and it avoids the mirroring conflict. Use the bit fields only as a
cross-check and in log messages. [inferred recommendation]

---

## 2. Model identification (2.9" and related)

Device IDs below come from the ID list in hass-gicisky's `devices.py` (from the vendor's own
model names). The decoded fields are worked out from section 1.3.

| Device ID | Vendor model name | Res. idx | Panel type | Colours | Palette (ours) | In hass-gicisky / hass-ble-esl table? | Support |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `0x0028` | EPA_LCD_296x128_BW | 1 | 1 (EPA) | 0 | `BW` | yes: rotate 90, no mirror, 1 plane | [single] for settings |
| `0x002B` | EPA_LCD_296x128_BWR | 1 | 1 (EPA) | 1 | `BWR` | **no**: hass-gicisky would reject this tag | ID [single]; settings unknown |
| `0x002E` | EPA_LCD_296x128_BWRY | 1 | 1 (EPA) | 3 | `BWRY` | yes: rotate 90, no mirror, 2 bpp | [single] for settings |
| `0x0030` | EPA_LCD_296x128_BW_1 | 1 | 2 (EPA1) | 0 | `BW` | no | ID [single] |
| `0x0033` | EPA_LCD_296x128_1_BWR | 1 | 2 (EPA1) | 1 | `BWR` | yes: rotate 90, no mirror, 2 planes | [single] for settings; hass-ble-esl docs mark it verified on hardware |
| `0x02CE` | EPA_LCD_296x152_BWRY | — | — | — | — | no; a different 2.9" panel (296×152), not our `TagType` | ID [single] |

Mirror predictions for the 2.9" IDs:

| Device ID | atc1441 (panel type) | OEPL (bit 8) | hass / hass-ble-esl |
| --- | --- | --- | --- |
| `0x0028` | mirror | no mirror | no mirror |
| `0x002B` | mirror | no mirror | (not listed) |
| `0x002E` | mirror | no mirror | no mirror |
| `0x0030` | no mirror | no mirror | (not listed) |
| `0x0033` | no mirror | no mirror | no mirror |

Only `0x0033` is consistent across all three. Note that atc1441's "mirror" flips its own
portrait canvas, while hass rotates a landscape image first, so the readings may describe the
same panel differently. **The two tags we receive decide this (Q1, Q2).**

### Orientation for 296 × 128

- Our `TagType` for these tags is landscape, `width=296, height=128`.
- The panel's native scan is **128 pixels per line, 296 lines** (resolution index 1 is 128 × 296). [multi]
- hass-gicisky / hass-ble-esl rotate the landscape image by **90° counter-clockwise** (PIL `rotate(90, expand=True)`), giving a 128 × 296 portrait image, and then pack it row by row. [single]
- atc1441 draws on a 128 × 296 portrait canvas directly and packs it row by row, with an optional horizontal flip. [single]; it agrees with hass on the native line length and line count, and leaves the orientation to whoever draws the canvas.

Written as a direct mapping from our landscape PNG `P(x, y)` (x = 0..295 left to right,
y = 0..127 top to bottom), the hass reading is:

> Output lines `v = 0..295`; each line holds pixels `u = 0..127`; pixel `(u, v)` of the
> output is `P(x = 295 − v, y = u)`.
> In words: **take the landscape image column by column, starting at the rightmost column
> (x = 295) and moving left; within each column go from top (y = 0) to bottom (y = 127).**

[inferred from the hass rotation; the rotation direction itself is single source and must be
confirmed with a test pattern, see Q2]

If a variant turns out to need mirroring, mirroring along a line reverses `u`
(equivalent to reading each column bottom to top), and mirroring across lines reverses `v`
(equivalent to starting at the leftmost column). The four combinations are the four
candidates to try on hardware.

---

## 3. GATT services and characteristics

| Item | Value | Support |
| --- | --- | --- |
| Primary service | `0000fef0-0000-1000-8000-00805f9b34fb` (16-bit `0xFEF0`) | [multi] (atc1441, OEPL, hass-ble-esl) |
| Alternative service UUIDs | `0xFDF0` and `0xFCF0` are also accepted by hass-ble-esl | [single] |
| Command characteristic | `0xFEF1`: host writes commands, tag sends replies as **notifications** on the same characteristic | [multi] |
| Image characteristic | `0xFEF2`: host writes image parts; no notifications | [multi]; S8's README lists `fef2` as "write + notify", but its code subscribes only on `FEF1`; Q5 checks the properties |
| Discovery rule used by hass | Take all characteristics of services whose UUID starts with `0000f`, sort by the 16-bit short UUID, lowest is command, next is image; fewer than two is an error | [single]; consistent with fixed `FEF1`/`FEF2` |
| Write type | **Write without response** for both characteristics (hass-gicisky, hass-ble-esl, OEPL). atc1441 uses Web Bluetooth's plain `writeValue`, which picks the mode from the characteristic's properties | [multi] for without-response; S8 uses **write with response** on both and caps the part size at 512 bytes for that reason [single]; so both modes may work [inferred]; check properties on hardware, Q5 |
| MTU | OEPL requests MTU 255. Each image part write is 244 bytes, so it needs an ATT MTU of at least **247** (244 + 3). No other source negotiates MTU explicitly | [single] for 255; the 247 minimum is [inferred] |

Recommendation for the driver [inferred]: look characteristics up by UUID (`FEF1`/`FEF2`)
first and fall back to the hass sort rule only if `FEF0` is missing. With bleak, call
`start_notify` on the command characteristic before the first write. Write with
`response=False`, unless the characteristic's properties show it lacks
`write-without-response`. On BlueZ the MTU exchange happens automatically, but bleak's
`client.mtu_size` reports 23 (with a warning) until the BlueZ backend has acquired the MTU,
so it is not a reliable check right after `connect()`. Do not fail a push on that value.
Either acquire the MTU explicitly on BlueZ before reading it, or detect a too-small MTU from
the tag's behaviour (the same part requested again and again, Q10) and map that to
`protocol`. [inferred, from bleak's BlueZ backend]

---

## 4. Image update sequence

The tag drives the transfer: after the handshake, it tells the host, part by part, which part
it wants next. All commands go to `FEF1`, all image data to `FEF2`, and all replies arrive as
notifications on `FEF1`.

### 4.1 Step by step

| # | Host sends (on) | Bytes | Expected reply (notify on FEF1) | Support |
| --- | --- | --- | --- | --- |
| 0 | connect, discover services, subscribe to notifications on `FEF1`, short settle delay | — | — | [multi] |
| 1 | START (FEF1) | `01` | `01 LL HH`: the tag's maximum message size for image writes, uint16 little-endian. Observed value `01 F4 00` = 244 bytes | [multi] (hass checks exactly `01 F4 00`; atc1441 reads bytes 1–2 as the size; OEPL accepts any `01`) |
| 2 | SIZE (FEF1) | `02` + total image byte count as **uint32 LE** + trailer (see 4.2) | `02` (first byte) | [multi] |
| 3 | IMAGE START (FEF1) | `03` | `05 00 NN NN NN NN`: the tag asks for part `N` (uint32 LE); the first request is part 0 | [multi] |
| 4 | PART (FEF2) | part index `N` as **uint32 LE** (4 bytes) + up to 240 bytes of image data (bytes `N*240 .. N*240+239`) | `05 00 NN NN NN NN` asking for the next part, or `05 08` when done | [multi] |
| 5 | repeat step 4 for whatever part the tag requests | | | [multi] |
| 6 | completion | — | `05 08`: the tag has all the data | [multi] (atc1441, OEPL, hass-ble-esl; hass-gicisky ignores it) |
| 7 | disconnect | — | — | [multi]; all sources disconnect right after `05 08` |

Concrete example for a `0x0033` (2.9" BWR, uncompressed) tag [inferred from the layouts above]:

```
-> FEF1  01
<- FEF1  01 F4 00
-> FEF1  02 00 25 00 00 00 00 00        (9472 bytes = 0x00002500)
<- FEF1  02 ...
-> FEF1  03
<- FEF1  05 00 00 00 00 00              (send part 0)
-> FEF2  00 00 00 00 <240 bytes>
<- FEF1  05 00 01 00 00 00              (send part 1)
...
-> FEF2  27 00 00 00 <112 bytes>        (part 39, the last, 9472 - 39*240 = 112)
<- FEF1  05 08                          (done)
```

### 4.2 SIZE command trailer: [conflict]

| Source | Uncompressed planes | Packed 2 bpp | Compressed |
| --- | --- | --- | --- |
| atc1441 | 8 bytes: `02 SS SS SS SS 00 00 00` | (no 2 bpp mode) | same 8 bytes |
| hass-gicisky / hass-ble-esl | 8 bytes: `02 SS SS SS SS 00 00 00` | same 8 bytes | QuickLZ mode: 6 bytes `02 SS SS SS SS 01` |
| OEPL | 6 bytes: `02 SS SS SS SS 00` | 8 bytes: `02 SS SS SS SS 00 00 00` | 6 bytes `02 SS SS SS SS 00` |

`SS` is the total payload byte count (after any compression), uint32 LE, in every source.
The 8-byte form for uncompressed data is used by two sources (atc1441, hass), and every source
uses it for 2 bpp, so the driver should send **the 8-byte form with three zero bytes**. The
6th byte probably works as a "compressed" flag. [inferred] Q6 checks that the tag accepts it.

### 4.3 Part size

- Fixed at **240 data bytes + 4 index bytes = 244** in hass-gicisky, hass-ble-esl and OEPL. [multi]
- atc1441 (latest commit "Allow display to set image data message size") and S8 both take the size from the START reply: part data = reply value − 4. S8 accepts reply values from 8 to 512. [multi] The observed reply value is 244, so both readings give 240.
- Driver recommendation [inferred]: parse the uint16 from the START reply and use `value − 4` as the part size. Refuse (`protocol`) if it is 0, larger than `mtu − 3 − 4`, or the reply is malformed. Log it if it is not 244.
- Number of parts = `ceil(total_bytes / part_size)`; the last part is shorter, with no padding. [multi]

### 4.4 Flow control, retries and failure signals

| Situation | What sources do | Support |
| --- | --- | --- |
| Tag requests part `N` | Send exactly part `N`, whatever was sent before. atc1441 and hass follow the tag's request; OEPL aborts if `N` is not the next sequential part | [multi] for "tag picks the part"; OEPL is stricter |
| Tag requests the same part again | The tag could not accept it. hass-ble-esl resends with a linear back-off of 0.05 s × (repeat − 1) and gives up after 6 consecutive requests for the same part; hass-gicisky gives up after 3 | [single] |
| Tag requests a part past the end | hass treats it as complete. hass-ble-esl also returns success. S8 treats a request for the first index past the end as complete if every part was sent, and raises an error for any higher index | [single] (S8 counted separately: its check is its own code); risky, see Q7 |
| `05 08` before the last part was sent | hass-ble-esl treats it as an error ("completion after part …") | [single] |
| `05 xx`, with `xx` neither `00` nor `08` | atc1441 logs an error and aborts. No source documents what the values mean | [single] |
| START not answered | hass-ble-esl: the tag may drop the first START right after subscribing; it waits 0.2 s after subscribing and resends START up to 3 times, with 0.4 s timeouts. Observed answer times were 0.05–0.19 s | [single] |
| No notification for a long time | OEPL abandons the upload after 30 s of silence. hass-ble-esl uses a 10 s reply timeout; hass-gicisky 5 s | [multi] that a silence timeout is needed; values differ |
| Unexpected reply to START / SIZE / IMAGE START | hass raises an error and the write fails | [single] |

**Success criterion for our driver [inferred, required by `TagDriver`]:** return `ok` only
after `05 08` has arrived **and** every part `0..last` has been sent at least once. Treat a
request past the end without `05 08` as a failure until Q7 shows the tag never sends `05 08`.
hass-gicisky's "break and succeed on a malformed reply" is a bug, not the protocol; do not
copy that behaviour.

### 4.5 Compression

- Our 2.9" tags advertise with bit 14 set (the example payload gives `hw = 0x4033`), which means "send uncompressed". hass-gicisky and hass-ble-esl send the 2.9" panels uncompressed. [multi]
- Larger panels use a line-header format (`0x75` blocks, legacy) or QuickLZ (7.5" and 10.2"). **Out of scope for v1**; not described further. The driver should refuse (`rejected`, detail "compressed panels not supported") any tag whose bit 14 is clear. [inferred recommendation]

---

## 5. Image packing

All formats are a **continuous bit stream over the whole image, MSB first**: the first pixel
is bit 7 of the first byte. Lines are not padded separately; for 128-pixel lines this makes
no difference, since 128 is a multiple of 8 and of 4. [multi] (hass, atc1441)

Pixel order is the line order of section 2: 296 lines of 128 pixels, which in terms of our
landscape PNG means column by column, starting at x = 295, each column from y = 0 down to 127.
[inferred; rotation single source]

### 5.1 BW and BWR: bit planes (1 bit per pixel)

| Plane | Bit = 1 means | Bit = 0 means | Size for 296 × 128 | Support |
| --- | --- | --- | --- | --- |
| Plane 1, black/white | **white** | black (or "not white") | 4736 bytes (296 lines × 16 bytes) | [multi] (hass, atc1441) |
| Plane 2, red (only for BWR) | **red** | not red | 4736 bytes | [multi] (hass, atc1441) |

- The payload is plane 1 followed immediately by plane 2 (BWR, 9472 bytes), or plane 1 only (BW, 4736 bytes). [multi]
- For a red pixel, both sources leave the BW plane bit at 0 (hass: "not white"; atc1441: the luminance of pure red is below its threshold). [multi] Whether the panel cares what the BW bit holds under a red pixel is Q9.
- hass-ble-esl has an inverted "black plane" option (1 = black) for some other models. None of the 2.9" entries use it. [single]

### 5.2 BWRY: 2 bits per pixel

hass-gicisky and hass-ble-esl pack `0x002E` (2.9" BWRY) as 2 bits per pixel, 4 pixels per
byte, first pixel in bits 7–6. They do **not** use bit planes: [single, but hass-ble-esl's
docs say the 2.9" BWRY was verified on hardware]

| 2-bit code | Colour |
| --- | --- |
| `00` | black |
| `01` | white |
| `10` | yellow |
| `11` | red |

Size for 296 × 128: 296 lines × 32 bytes = **9472 bytes**, with 4 pixels per byte.

**[conflict]**: atc1441's uploader has no 2 bpp mode and sends BW plus red planes for every
colour count. OEPL has a separate "packed 2 bit" mode, chosen per hardware type from a table
we did not read. Two sources say 2 bpp for BWRY (hass, and the existence of OEPL's mode), and
one source shows no BWRY handling at all. Use 2 bpp for `0x002E`, and settle it with Q8.

### 5.3 Mapping our palette indices (`shared/tag_types.py`)

The driver receives a palette PNG whose pixel values are indices into `PALETTE_COLOURS`.
It should map indices directly and never threshold RGB values: our renderer is exact, while
the thresholds in the sources (128 or 170) exist to cope with anti-aliased input.
[inferred recommendation]

| Our palette | Index | Colour | BW plane bit | Red plane bit | 2 bpp code |
| --- | --- | --- | --- | --- | --- |
| `BW` | 0 | white | 1 | — | — |
| `BW` | 1 | black | 0 | — | — |
| `BWR` | 0 | white | 1 | 0 | — |
| `BWR` | 1 | black | 0 | 0 | — |
| `BWR` | 2 | red | 0 | 1 | — |
| `BWRY` | 0 | white | — | — | `01` |
| `BWRY` | 1 | black | — | — | `00` |
| `BWRY` | 2 | red | — | — | `11` |
| `BWRY` | 3 | yellow | — | — | `10` |

The driver must check that the PNG is exactly `tag_type.width × tag_type.height` (296 × 128),
uses palette indices within range, and that `tag_type.palette` matches the colours the tag
advertises (section 2). Otherwise it returns `rejected` without connecting. See section 9.

---

## 6. Timing

No source reports a measured end-to-end update time for these tags. Everything here is to be
measured (Q3).

| Item | Value | Support |
| --- | --- | --- |
| Settle after subscribing to notifications | hass-gicisky waits 1.0 s; hass-ble-esl 0.2 s plus START retries | [single] each |
| Delay after connecting | OEPL waits about 5 s after connecting before service discovery; probably specific to the ESP32 stack | [single] |
| START answer time | 0.05–0.19 s observed | [single] (hass-ble-esl) |
| Delay between parts | None needed: each part is sent when the tag asks for it. atc1441 processes each notification 50 ms after it arrives; hass-ble-esl has optional pacing, default 0 | [multi] |
| Reply timeout per step | 5 s (hass-gicisky), 10 s (hass-ble-esl); OEPL treats 30 s of silence as failure | [multi] that one is needed |
| Connect timeout | OEPL: 30 s stack timeout, 45 s guard | [single] |
| Data volume, 2.9" BWR/BWRY | 9472 bytes = 40 parts; BW 4736 bytes = 20 parts | [inferred] from section 5 |
| Expected transfer time | 40 round trips at roughly 30–100 ms each ≈ 1–4 s, plus connect and discovery (1–5 s) | [inferred] |
| Panel refresh after `05 08` | Unknown. Three-colour e-paper typically takes 10–30 s; the tag is probably busy and not connectable while refreshing | [inferred] |
| Re-advertising after an update | Unknown; matters for back-to-back pushes | Q3 |

Budget against `PUSH_TIMEOUT_S = 60`, phases in order: targeted scan ≤ 8 s, connect ≤ 12 s,
handshake (START, SIZE, IMAGE START) ≤ 6 s, transfer ≤ 28 s, disconnect ≤ 2 s. That totals
56 s, leaving 4 s of margin inside the overall limit. With one connection per adapter, pushes to several tags are serialised, which
fits the "within 2 minutes" acceptance target only for a handful of tags per price edit.
[inferred]

Reliability notes from the hass-gicisky tracker [single]: failures came mostly from poor BLE
proxies and non-default scan parameters, connection handshakes sometimes took up to about
20 s, and retries with a delay cleared most failures. We use a local adapter, not a proxy, but
the queue's retries (M3) remain necessary.

---

## 7. Battery and status reporting

- **Battery:** only in the advertisement, `d[1]` in 0.1 V units (section 1.2). [multi] No source reads battery over GATT.
- **Firmware version:** advertisement `d[2..3]`, big-endian. [multi]
- **Hardware word:** advertisement `d[0]` and `d[4]`. [multi]
- **RSSI:** from the advertisement report (bleak `AdvertisementData.rssi`). [inferred]
- **No GATT status read** is documented. `status()` must therefore come from a scan. [inferred]
- Battery percentage: no source has a calibrated curve. hass's linear 2.5–2.9 V range is a guess. Recommend reporting `battery_mv` and leaving `battery_pct` as `None` until Q4 gives real readings. [inferred recommendation]

---

## 8. Open questions for the hardware session (wave 3)

Each item states how to resolve it with the two tags. Record results here, replacing the
source tags with **[hardware]**.

| # | Question | How to test |
| --- | --- | --- |
| Q1 | Which variant did we receive (`0x002B`, `0x0033`, `0x002E`, other)? PLAN section 12 also asks this. | Scan with bleak; log the full manufacturer payload, hardware word, decoded fields and firmware for each tag. Compare with the label on the box and the PCB. |
| Q2 | Rotation and mirroring. | Push a test PNG with an asymmetric marker: a black "F" in the top-left corner, a red bar along the top edge, a small square at the bottom-right. Try the hass mapping (section 2) first; if it is wrong, try the other three flip combinations. Record which combination is upright for each device ID. |
| Q3 | Real timings: connect, handshake, transfer, refresh, and how soon the tag accepts the next connection. | Time each phase with `time.monotonic()` in `push_png.py`; repeat 50 pushes at 3 m (the M4 criterion) and note failures by phase. Also try 10 m. |
| Q4 | Is battery in the advertisement correct, and how does it fall? | Compare `d[1]` with a multimeter on the cell; log it daily during the pilot. |
| Q5 | Characteristic properties: write-without-response support, notify vs indicate. S8 writes with response; if both modes are offered, time a push in each. Also check whether `FEF2` offers notify (S8's README says so). | Print `char.properties` for `FEF1`/`FEF2` after connecting. |
| Q6 | Does the tag accept the 8-byte SIZE command, the 6-byte one, or both? | Send the 8-byte form (default). If the reply is not `02`, try the 6-byte form `02 SS SS SS SS 00`. |
| Q7 | Is `05 08` always sent after the last part? Does the tag ever request a part past the end? | Log every notification during the 50-push run. |
| Q8 | Is BWRY really 2 bpp (if we have a BWRY tag)? S8, a driver written only for `0x002E`, sends 2 bpp with the codes of section 5.2 (first pixel in the top two bits; the codes come from hass, S1) and shows a photo of a tag displaying it, which is hardware evidence for 2 bpp. Our pilot tags are BWR, so this may stay untested. | Push a 4-colour stripe pattern as 2 bpp; if it shows noise, try BW plus red planes. |
| Q9 | Does the BW bit under a red pixel matter? | Push red areas with BW bit 0, then with BW bit 1, and compare the colour. |
| Q10 | MTU on the shop box (BlueZ) and on a Windows dev laptop. | Log `client.mtu_size`; confirm that 244-byte writes without response arrive intact (tag requests the next part rather than repeating). |
| Q11 | Local name and advertised service UUIDs. S8 reports a local name of the form `NEMR` + 8 characters (example `NEMR92833721`) and that the last four octets of the address are printed on the tag's label. If those 8 characters are the same four octets, they are hex digits and can include A–F [inferred]. One source only; the name may differ by model. | Log `AdvertisementData.local_name` and `service_uuids`. Decide whether the scan filter can use them. |
| Q12 | What do `05 xx` error codes mean? | Provoke errors: a wrong SIZE (one byte too few), an out-of-order part, a short part. Log the replies. |
| Q13 | Is START sometimes dropped right after subscribing? | Count START retries over the 50-push run. |
| Q14 | Does disconnecting immediately after `05 08` ever abort the refresh? | During the 50-push run, check that each pushed image is the one shown. |

---

## 9. Mapping to `TagDriver` (`gateway/drivers/base.py`)

`vendor = "gicisky"`.
`vendor_id` = the tag's BLE MAC address as BlueZ reports it, upper case with colons
(`AA:BB:CC:DD:EE:FF`). [inferred; on macOS bleak gives a UUID instead, but macOS is not a
target]

### `discover(timeout_s)`

1. Run a `BleakScanner` for `timeout_s` with a detection callback.
2. For each advertisement with manufacturer data `0x5053` of exactly 5 bytes, decode section 1.2.
3. Keep only device IDs in the driver's model table (section 2). Log unknown IDs at info level with the raw payload, so new variants show up in discovery reporting.
4. Return one `SeenTag` per address (the latest advertisement): `vendor_id` = MAC, `model` = the `tag_type.model` value the driver's table gives for that device ID (the core matches `(vendor, model)` against the `tag_type` table; `SeenTagReport` docstring). It is not the tag type key, `rssi`, `battery_mv = d[1] * 100`, `battery_pct = None` (section 7), `seen_at` = UTC now.

### `push_image(vendor_id, tag_type, png)`

1. Decode the PNG. Check size, mode (palette) and index range against `tag_type` (section 5.3). On a mismatch → `rejected`.
2. Find the device and its current advertisement with a short targeted scan (≤ 8 s, see section 6). `BleakScanner.find_device_by_address` returns only the `BLEDevice`, not the advertisement, so use `BleakScanner.find_device_by_filter` with a filter that matches the address and keeps the `AdvertisementData` it is called with, or `BleakScanner.discover(return_adv=True)`. Not found → `not_found`.
3. From that advertisement, look up the device ID; if the tag's colours do not match `tag_type.palette`, or bit 14 is clear (compressed panels) → `rejected`. The device ID also selects rotation, mirroring and packing.
4. Pack the image (section 5) into `payload`.
5. Connect, look up `FEF1`/`FEF2`, subscribe to notifications (section 3); see section 3 on the MTU.
6. Run section 4.1: START (with retries), SIZE (8-byte form), IMAGE START, then the part loop driven by the tag's requests, with a per-reply timeout and a repeated-part limit.
7. On `05 08` with all parts sent → `ok`.
8. Always disconnect in a `finally` block, and wrap the whole call in `asyncio.timeout(PUSH_TIMEOUT_S)` (PLAN section 8, reliability). One push at a time per adapter (a driver-level `asyncio.Lock`).

### `status(vendor_id)`

Short scan for that address. If seen → `TagStatus(reachable=True, rssi, battery_mv, battery_pct=None)`;
if not seen → `TagStatus(reachable=False, …None)`. No GATT connection. [inferred]

### Failure → `PushError`

| Failure | Code |
| --- | --- |
| Address not seen in the scan; BlueZ "device not found"; connection refused because the device is gone | `not_found` |
| Overall 60 s limit hit; connect timed out; no notification within the per-step reply timeout (START after all retries, SIZE, IMAGE START, or a part request) | `timeout` |
| PNG does not match `tag_type` (size, palette, indices); tag's advertised colours or compression flag do not match what we can send; tag replies `05 xx` with an error status; tag rejects SIZE (reply other than `02`) | `rejected` |
| Malformed or unexpected notification (wrong first byte, too short, `05 08` before all parts were sent, a part index out of range); same part requested more often than the repeat limit (often a too-small MTU, see section 3 and Q10); START size unusable; required characteristics missing; MTU too small; unexpected disconnect mid-transfer; any other `BleakError` during the session | `protocol` |

A PNG that does not match `tag_type` (size, palette or index range) is `rejected` without
contacting the tag. This is part of the `TagDriver.push_image` contract in
`gateway/drivers/base.py`.

---

## Sources

All sources were read on 2026-11-18, except S8 (2026-11-19). "Commit" is the latest commit touching the files that
were read, as shown by the repository's history page at that time.

| # | Name | URL | Commit / version read | Licence (SPDX) | What we took from it |
| --- | --- | --- | --- | --- | --- |
| S1 | hass-gicisky (Home Assistant custom integration, eigger; archived) | https://github.com/eigger/hass-gicisky | `f144d16b30e59069e1f9e8a1375781fc33e1eefa` (v5.5.1, 2026-09-22); files `custom_components/gicisky/gicisky_ble/{parser,devices,writer}.py`, `manifest.json` | MIT | Advertisement layout and device-ID formula; device-ID list with vendor model names; per-model rotation, mirroring and packing; command sequence and reply checks; 240-byte parts; bit-plane and 2 bpp packing; timeouts and settle delay. Short byte-layout facts only; no code copied. |
| S2 | hass-ble-esl (successor of S1, eigger), PickSmart protocol module | https://github.com/eigger/hass-ble-esl | `a322b4f8fa8f9e04527259ca3f3e4a7d75cf03d5` (2026-10-08) for `custom_components/ble_esl/esl_ble/picksmart/{const,wire,writer,devices,image}.py`; `docs/models.md` at `main` the same day | MIT | `05 08` = done and `05 00` = next part; alternative service UUIDs; START retry and settle timings with observed answer times; repeated-part limit and back-off; 10 s reply timeout; models marked verified on hardware; manufacturer-ID collision fix. Counted with S1 as one source. |
| S3 | atc1441, ATC_GICISKY_ESL (information repository) | https://github.com/atc1441/ATC_GICISKY_ESL | `e54fcc94969ac786c92e6a0e7562a3d744e15fb6` (2024-04-25) | GPL-3.0-only (as shown by GitHub; the variant was not checked) | Pointer to the uploader (S4); confirms the reverse engineering is by atc1441 and that PickSmart is the same product. Demo files not used. |
| S4 | atc1441, ATC1441 BLE E-Paper Uploader (Web Bluetooth page) | https://github.com/atc1441/atc1441.github.io/blob/main/ATC_GICISKY_Paper_Image_Upload.html (the hosted copy at atc1441.github.io was blocked by the session network) | `53f1f63b92e65ad9ad666d9870d789b732adad32` (2025-02-25, "Allow display to set image data message size") | **None stated** (no licence in the repository or the file). Treated as all rights reserved: read for understanding only. No code or text copied; facts only (the resolution-index table in section 1.3 is restated as interoperability facts). | Service and characteristic UUIDs; full reply handling including `05 08` and the abort on other `05 xx`; part size from the START reply; 8-byte SIZE form; hardware-word bit fields and resolution table; mirror-from-panel-type rule; BW and red plane polarity. |
| S5 | OpenEPaperLink, ESP32 access point firmware, BLE support | https://github.com/OpenEPaperLink/OpenEPaperLink (`ESP32_AP-Flasher/src/ble_filter.cpp`, `ble_writer.cpp`) | `ble_writer.cpp` at `a2a2f22005d5aa5aaff09cf729ef1f6f1a7777c1` (2026-10-21); `ble_filter.cpp` at `df2031b401e05f29bb2c9adbd29982f65daa58c3` (2026-11-02) | **CC-BY-NC-SA-4.0 (non-commercial).** Used only to understand the protocol. No code, no snippets, and no tables copied; everything here is our own wording. | Independent confirmation of the advertisement layout, UUIDs, write without response, 240-byte parts, the `05 08` completion, and bit 14 = no compression. Its readings that differ: 6-byte SIZE form, bit-8 mirroring, strict sequential parts, MTU 255, 30 s silence timeout, 5 s post-connect wait. |
| S6 | ha-bleep (magicus), Home Assistant integration for PickSmart/Gicisky | https://github.com/magicus/ha-bleep | `main` as of 2026-11-18 (hash not shown on the page) | **None stated** | Nothing usable: the integration code is a stub. README only (296 × 128 the one model marked verified; BW and BWR only). |
| S7 | hass-gicisky issue tracker, issues #20 and #21 | https://github.com/eigger/hass-gicisky/issues/20, https://github.com/eigger/hass-gicisky/issues/21 | read 2026-11-18 | Discussion, no licence | Look-alike tags under the Gicisky name that use another protocol; field reliability notes (proxies, retries, handshakes of up to about 20 s). |
| S8 | gicisky-296-bwry (RealPotatoe), Python BLE driver for the 2.9" BWRY tag, also on PyPI | https://github.com/RealPotatoe/gicisky-296-bwry | `6abc1f2fc390892faa83ecd4f2e1e2aef5f65769` (read 2026-11-19); `README.md`, `src/gicisky_296_bwry/{device,encoder}.py` | MIT | **Derived from S1:** its README says its protocol and colour/rotation profile were worked out with reference to hass-gicisky, and its code credits hass for the SIZE trailer and the colour classification. So for the command sequence, the 8-byte SIZE form and the 2 bpp codes it restates S1 and does not count as a second source. Its own behaviour, counted separately: part size read from the START reply (8–512 accepted; hass checks for exactly `01 F4 00`); write with response, with parts capped at 512 bytes; completion accepted only after every part was sent, with the first index past the end also taken as completion (section 4.4); no MTU handling; no service UUID lookup. Defaults: connect 30 s, 10 s per reply, 300 s per upload, 3 retries per part, 0.5 s after subscribing. No advertisement parsing; README reports the `NEMR` local name (Q11). A README photo shows a tag displaying an image (Q8). Facts only; no code copied. |

Sources that could not be read, because the session network blocked the host:

- `atc1441.github.io` (the hosted uploader; the same file was read from its GitHub repository, S4).
- `community.home-assistant.io`, thread "Support for Gicisky e-ink BLE ESL labels" (credits atc1441 with the reverse engineering, according to search result snippets). It may hold timing reports; worth reading in the wave 3 local session.

**Method caveat:** the files were read through a web-fetch tool that returns a model-written
summary of each page, not the raw file. Every byte value above was asked for specifically and
cross-checked between sources where possible, but the driver author (workstream F) should
reread S1 and S2 directly (both MIT) before relying on any **[single]** item. Do not reread S5
for implementation.
