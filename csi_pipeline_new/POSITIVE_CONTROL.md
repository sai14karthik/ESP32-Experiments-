# Physical setup — positive control

Goal of this build: make the obstruction the **largest** thing that changes
between recordings. In the previous attempts it was not. Room drift between
two empty sessions measured 0.122 RMS while the object measured 0.055, and the
object's own signature anti-correlated with itself across rounds (r = −0.518).
Everything below exists to invert that ratio.

Frequencies assume channel 11 → 2.462 GHz, λ = 12.2 cm.

```
        3.0 m  (10 ft)
  |<--------------------------->|
  |                             |
 [TX]......  water  ......... [RX]      <- all three at 1.0 m height
  |         (midpoint)          |
  |         1.5 m               |
==+=============================+==  floor
  stand                      stand

  first Fresnel zone: 30 cm radius at the midpoint
  keep a 60 cm radius clear of everything along the whole line
```

## Requirements

| Parameter | Value | Why |
|---|---|---|
| TX–RX separation | **3.0 m** (min 2.5 m) | You had 0.76 m. At that range the Fresnel zone is 15 cm and a 5 cm placement error rewrites the result. At 3 m the zone is 30 cm, so the same error is half as significant. |
| Antenna height | **1.0 m**, both identical | Puts the bottom of the Fresnel zone 70 cm above the floor. On a tabletop the surface reflection has nearly the same path length as the direct ray and dominates. |
| Height match | within **2 cm** | A height mismatch tilts the link and changes which reflections combine. |
| Antenna orientation | both **vertical**, same orientation | Polarization must match. Do not have one board flat and one upright. |
| Antenna overhang | antenna end **past the edge** of whatever holds it, ≥ 2 cm air | These boards radiate from a PCB trace at one end, not a whip. That trace needs clear air. Resting it on a box, a table, or anything with metal in it detunes the antenna and skews its pattern. |
| Board rigidity | taped so it **cannot rotate**, even a few degrees | A trace antenna has real nulls in its pattern. A whip would shrug off a small rotation between blocks; this does not. Un-fixed boards are the leading cause of empty-vs-empty drift on this hardware. |
| Cable dressing | taped down at **both ends**, identical every block | The USB shield carries common-mode current and radiates. The cable is electrically part of the antenna. A cable that hangs differently between blocks is recorded as channel change. |
| Clearance around the line | **60 cm radius**, whole length | Twice the Fresnel radius. Nothing — furniture, walls, metal, monitors — inside that cylinder. |
| Distance to nearest wall | **≥ 1 m** | Wall reflections are stable only if nothing near them moves. |
| Object | **10–20 L of water** in plastic | Water is the strongest common absorber at 2.4 GHz. Cardboard is nearly transparent — a box is not an obstruction, it is decoration. |
| Object diameter | **≥ 28 cm** | Comparable to the 30 cm zone radius. A 19 L (5 gal) jug is ~28 cm × 50 cm and is ideal. |
| Object position | **exact midpoint**, 1.5 m from each board | Maximum Fresnel radius, so the obstruction fraction is highest and least sensitive to small errors. |
| Object height | vertical **centre at 1.0 m** | This is the one that likely broke the last run. A 50 cm jug needs a ~75 cm stool. If the water sits below or above the beam it does nothing. |
| Placement repeatability | tape the base outline, **±2 cm** | For the positive control only — see the note at the bottom. |

## Build order

1. **Stands.** Two identical supports, 1.0 m tall — camera tripods, mic stands, or two identical stacks of boxes. Not a table. Not one tripod and one box stack.

2. **Boards.** These have no external antenna — the radiator is a PCB trace at
   one end of the board, so "antenna height" means the height of *that end*.
   Mount each board upright with the antenna end pointing up and hanging past
   the edge of the stand, so there is at least 2 cm of air around it and
   nothing metal within ~20 cm. Both facing the same way. Tape each board down
   so it cannot shift **or rotate** — a few degrees is enough to move you into
   a different part of the antenna pattern, and that shift will show up as
   drift between two empty blocks. Mark both stand footprints on the floor.

3. **Separation.** Measure 3.0 m between the two *antenna ends*, not the stand
   bases or the board centres.

4. **Cables.** Route each USB cable straight back, away from the link axis, and tape it to the stand and to the floor. A loose cable is an antenna, and if it hangs differently between blocks that difference is recorded as signal. Keep the Mac mini at least 1 m off the axis.

5. **Object platform.** A stool or box at the midpoint whose top puts the water container's *centre* at 1.0 m. Tape the container's base outline on it.

6. **Room.** Close the door. Turn off fans. Nothing else moving, no one else walking. Same lighting and same door position every block — a door swinging open between blocks is a large reflector moving.

7. **Your own position.** You are the strongest scatterer in the room. Leave the room during every recording, or stand at a single taped spot at least 2 m off the axis and do not move. It must be the *same* place for empty blocks and object blocks — otherwise your body is the feature.

## Verify before recording anything

Record 20 s empty, then:

```sql
psql -d csi -c "SELECT s.label, count(*), round(avg(c.rssi),2) AS avg,
  round(stddev(c.rssi),2) AS sd, min(c.rssi), max(c.rssi)
  FROM csi_samples c JOIN csi_sessions s ON s.id=c.session_id
  WHERE s.label LIKE 'check%' GROUP BY s.label;"
```

Two things must be true before you continue:

- **RSSI between −40 and −50 dBm.** The rig that produced a 3.53x ratio measured
  **−44.4 dBm** at 3 m, so that is the number to aim near. Do not read a few dB
  either side as a distance error: with a PCB trace antenna, board orientation
  alone moves RSSI several dB, so ±3 dB tells you almost nothing about
  separation. What matters is that you are not pinned near −32 dBm, as the
  earliest runs were — at that strength the direct path swamps everything.
- **RSSI standard deviation > 0.3.** Your first runs had sd = 0.00 — 461 packets
  at exactly the same integer. A channel that never varies is not reporting a
  channel. The validated rig ran sd ≈ 0.5.

## Stability floor — run this before trusting any capture

An obstruction can only be detected if it moves the channel more than the room
moves on its own. Measure the room's own movement first, with nothing ever
placed in the link:

```bash
./run_positive_control.sh --prefix stab --rounds 2 --duration 45 --settle 20 \
  --object "NOTHING — leave the link clear, touch nothing, just wait outside"
uv run --group csi python probe_check.py --like 'stab_%'
```

Every block is empty, but you still leave and re-enter on the normal cycle.
Read the **absolute RMS**, not the ratio:

| RMS | Meaning |
|---|---|
| ≈ 0.05 | Matches the validated rig. Good — proceed to a real capture. |
| ≈ 0.2 | The empty room drifts as much as an object does. Nothing will work until this is fixed: check board rotation, cable movement, antenna overhang, then the room itself. |

For reference, `pc_` measured 0.0548 empty-vs-empty, against 0.1933 for a 20 L
water container — a 3.53x ratio. A later attempt on a re-built rig measured
0.2367 empty-vs-empty against 0.0865 for a bag: 0.37x, and undetectable.

## Then

```bash
./run_positive_control.sh
uv run --group csi python probe_check.py --like 'pc_%'
```

The number that matters is the cross-round correlation of the object
signature. It must be **positive**. If a 20 L water container at 3 m still
anti-correlates with itself, the problem is not placement and we look at
whether HT40 CSI on this firmware is comparable across sessions at all.

## Note on the tape

Taping the object position is right **for this test only**. Here the question
is "can this setup detect anything reproducibly", so placement variance should
be minimised.

For the real training capture the opposite holds: vary object position, object
type, and TX/RX separation deliberately. Anything held fixed is something the
model will memorise instead of learning the object. Do not carry the tape mark
forward.
