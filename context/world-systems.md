# POPWHEELS CITY — World Systems (seed / context doc)

Companion to the PROJECT CITY seed (§04 art, §05 citygen, §07 render, §08 optimization).
Covers the four systems that turned a fixed-size diorama into an endless waterfront
metropolis without breaking bake-once / blit-many.

> Constants below are reconstructed from prior sessions on `project-city.html` /
> `popwheels-city.html`, not from a re-read of current source. Function names and
> algorithms are accurate; numeric constants may have drifted. Verify before treating
> any number as normative.

---

## 01 — Procedural chunked terrain

The world is infinite. Ground bakes in `CH×CH`-tile chunks on demand at a fixed bake
scale, two phases each, LRU-evicted, blitted under the camera transform. Nothing outside
the project district is authored.

Predecessor was two full-screen phase canvases sized to the map — fine for a fitted
static camera, broken the moment pan/zoom is allowed, because the map edge (void) enters
the viewport. On a phone the viewport is a large fraction of the whole map. A bigger
canvas is the same bug with a bigger allocation.

**What "infinite" requires:**

- **Roads are a predicate, not a table.**
  `isRoad(x,y) = ((x−pad) mod STRD)===0 || ((y−pad) mod STRD)===0`, floored modulo so
  negative coords work.
- **Blocks are a lookup with a synthesized fallback.** `blockMap` holds authored blocks
  keyed `bx|by`; `blockAt(bx,by)` falls back to an ambient block whose style/height come
  from `hash2(bx,by,SEED)`. No allocation, no persistence.
- **Every ground branch is a pure function of `(x,y,SEED,phase)`.** Adjacent chunks bake
  independently, in arbitrary order, seconds apart, and must agree exactly at the shared
  border. The coordinate hash is the only channel between them.

**Chunk pipeline:**

1. Key `` `${cx}|${cy}|${phase}|${SEED}|${world.RL}` ``. `RL` is in the key because the
   shoreline derives from the district front line and *moves when the district grows* —
   omit it and stale land chunks float over new water.
2. Hit → `c.last = ++chunkGen`. The monotonic counter is the entire LRU; evict by `last`
   when over cap.
3. Miss → `if(chunkBudget<=0) return null`. Caller skips that chunk this frame; it
   appears next frame. This is the async half — camera motion never blocks on raster.
4. Bake at fixed `BAKE`, **not** `cam.s`. Baking at camera scale invalidates the whole
   cache every zoom step. Bake once, scale on blit; quarter-step camera snap still
   governs seam-free display.
5. Rasterize `CH²` tiles via `drawGroundTile(g,gi,hw,hh,x,y,phase)`; `gi(a,b)` maps tile
   coords to chunk-local canvas space with a 1px margin. Branch order: water → shoreline
   band / boardwalk → esplanade → road → block interior.
6. Cross-border overlays (edge beam, pilings, foam) are drawn by the same deterministic
   curve function inside *every* chunk they pass through, over-drawn past the edge and
   clipped by the canvas. Both neighbours compute the identical curve → strokes meet.

**OPT — per-frame bake budget.** `chunkBudget` resets to ~6 at the top of `draw()`. A
fast pan across virgin terrain fills in over several frames instead of stalling one frame
for 40 bakes. Cost is a brief empty tile at the leading edge; far cheaper than a 400 ms
hitch. Tune against the slowest target device.

**PITFALL.** Reset the budget *per frame*. A module-scope counter never reset gives you
exactly six chunks for the page lifetime and then a permanently blank world — and it
reads as a cache bug, not a counter bug.

---

## 02 — Winding river generation

Stateless. Two scalar functions of the along-shore coordinate define the whole
waterfront: where the near bank sits, and how wide the water is.

**Coordinates.** The river runs across the screen, so everything is parameterized on the
rotated axes: along-shore `w = x − y`, depth `d = x + y`. River/shore/bridge functions
take `(w,d)`, never raw tile coords.

```js
/* deterministic functions of the along-shore coordinate w */
function shoreAt(w){                      // depth of first water tile at w
  /* tanh(k·sin) makes near-square swings: long straights broken by SHARP elbows */
  return world.RL + Math.round( 5*Math.sin(w/46 + world.ph1)
                              + 7*Math.tanh(2.5*Math.sin(w/19 + world.ph2)) );
}
function riverWidth(w){
  return 16 + Math.round( 5*Math.sin(w/47 + world.ph3)
                        + 3*Math.sin(w/23 + world.ph1) );
}
ph1: hash2(1,1,SEED)*6.283, ph2: hash2(2,7,SEED)*6.283, ph3: hash2(5,3,SEED)*6.283
```

**Why `tanh(k·sin θ)`.** Pure sines give a lazy uniform wave — every bend the same
radius, unmistakably procedural. `tanh` at `k≈2.5` squares it off: saturated near ±1 most
of the period, fast transitions through zero → long straight reaches punctuated by sharp
elbows. `k` is the only knob that matters: below ~1.5 it degenerates to a sine, above ~4
the elbows go vertical and tile quantization stair-steps them.

**Where the water line comes from** — derived from the district so projects own the
waterfront:

```js
let maxFront = -1e9;
for(const b of blocks) if(b.role !== "ambient")
  maxFront = Math.max(maxFront, b.tx + b.ty + 8);   // district front line
const RL = maxFront + 12;    // 3-tile boardwalk band + meander headroom (±9)
```

- Everything in front of the front line is water or park. Ambient requires depth ≤
  `maxFront` — **nothing spawns between a project tower and the river**. Residual strip
  renders as open Esplanade-style park.
- **Headroom rule:** `RL − maxFront` must exceed meander amplitude, or a deep elbow bites
  into the district and floods a lot. Amplitude ±9, headroom 12. Changing amplitude
  without headroom is a silent flood.
- `RL` tracks the district, so enough projects to need a new block ring makes the
  shoreline recede. Reads as the city reclaiming land — kept deliberately.

**Shoreline overlay.** Diamond tiles quantize the meander into a staircase that reads as
pixel noise at city zoom. Fix is a continuous screen-space pass over the tiles in every
affected chunk: edge beam stroked along `shoreAt(w)`, pilings on a regular `w` interval,
foam dashes offset from the beam. Boardwalk planking replaces raw stone-to-water on the
near bank.

---

## 03 — Bridge generation

Every crossing is a suspension bridge: raised deck sprite on a skewed centerline, pylons
at ⅓ and ⅔ standing on that deck, live per-frame cables. No arch/viaduct branch — built
and cut.

**Siting.** Fixed spacing along `w` with a seed-derived offset
(`boff = floor(hash2(9,4,SEED)*26)`), so crossings are deterministic per seed but not
aligned to the block grid.

**Skew.** v1 drew decks as constant-`w` columns — perpendicular to a *straight* shore,
which the shore no longer is. Every bridge ran the same way; skyline read as a picket
fence. Current:

```
w(d) = w₀ − m·(d − s)      // centerline; s = shoreAt(w₀)
                           // m = smoothed d(shoreAt)/dw at w₀, clamped ±1.3, cached per bridge
```

Deck tile test, pier suppression, pylon/abutment positions, and cable paths all evaluate
against that centerline. Straight reach → square crossing; bend → visibly angled to meet
both banks roughly perpendicular to the local waterline. The ±1.3 clamp stops a bridge at
a sharp elbow from running nearly parallel to the river.

**Deck is elevated, not painted.** Corridor tiles under a span are just water. Each deck
is one baked sprite:

- Slab raised **14 px** above water, following the skewed centerline.
- Approach ramps easing to grade at both banks — deck elevation is a function of `d`, not
  a constant.
- Fascia ribbons on both long edges so the slab shows real thickness in profile.
- Sidewalk + roadway bands with centerline dashes; railing posts both edges.
- Stone piers dropping from the deck underside into the river. `nearBridge(w,d)`
  suppresses shoreline pilings and foam inside the corridor so bank furniture doesn't
  punch through the span.

**Pylons stand on the deck**, lifted by the *local* deck elevation — a ramp pylon sits
lower than a mid-span one. Cable geometry subtracts the same elevation so anchors,
saddles and hangers ride the raised deck instead of floating at water level.

**Depth key `sC − 1.5`**: after near-bank land, before pylons/cables, before far-bank
city. Wrong → the bank paints over the deck, or the deck paints over a foreground tower.

**OPT — cables are live strokes, never baked.** A main cable spans many tiles and many
chunks; baking means an enormous sprite or a curve sliced across cache boundaries.
Instead: per-frame stroke between tower screen positions, dark **2.4 px** underlay plus
bright **1.1 px** overlay, both floored to a minimum device width. A single light-grey
1px cable is invisible over bright water at 0.35–0.5 zoom on a phone.

**Known gaps.** Hanger spacing is parameterized on screen-x along the parabola — slightly
wrong at large `m`; fix by re-parameterizing on arc length if it ever matters. Grid agents
(cars, cyclists) don't cross bridges; deck traffic is a separate stateless crosser layer.

---

## 04 — Async lighting

Two independent things, both load-bearing: **desynchronized in time** (windows don't
blink together, and not on the same clock as everything else) and **deferred in compute**
(lit sprites bake off the critical path).

### A. Desynchronized in time

Naive version bakes two phases with independently hashed window states and flips. Every
window changes every tick; the facade swaps identity 3.5×/sec. Correct is a **stable**
lighting field with a small toggling subset:

```js
/* Lighting is STABLE per window (phase-independent hash); a small twinkle subset
   (~8%) toggles with the phase, so blinks read as scattered twinkling rather than
   a wholesale change of the facade. */
const base = hash2(salt*31 + s,   c*7,     SEED)    > 0.55;   // ~45% lit, never changes
const tw   = hash2(salt*13 + s*5, c*3+9, SEED+77)   > 0.92;   // ~8% participate
const on   = tw ? (base !== !!phase) : base;
```

`salt` is per-building, so no two towers share a field. Sprite parity `(tx+ty)&1` mixes
into phase selection so neighbours never blink in lockstep — same trick as the swap
cabinet doors.

```js
const phase     = Math.floor(tms / CLOCK) & 1;         // 280 ms — everything else
const phaseSlow = Math.floor(tms / (CLOCK*10)) & 1;    // 2.8 s — building windows
```

Occupants come and go; they don't strobe. Building sprites (project + ambient) select
their pre-baked phase with `phaseSlow`. **Zero extra bake cost** — the caches already hold
both phases; the slow clock only changes which is blitted.

Scope consequence: anything baked *into* those sprites rides the slow clock too — antenna
beacons and the project arris stripe pulse at 2.8 s, which for aviation beacons is more
accurate than 3.5 Hz was.

| Fast clock (280 ms) | Slow clock (2.8 s) |
|---|---|
| rider pedal poses, swap-cabinet screens/doors, elevator call light, street + boardwalk lamps, water sparkle, halo pulse | tower + ambient window twinkle, antenna beacons, project arris stripe, anything baked into a building sprite |

**PITFALL — don't add a third clock.** Two uncorrelated rates read as a living city;
three read as noise and the eye starts hunting a pattern that isn't there. New elements
derive from an existing clock (parity, salt, integer multiple).

### B. Deferred in compute

A lit tower sprite is 2 phases × stories × columns of per-window work plus silhouette and
crown. Baking a hundred on the frame the tracker data lands is a hitch on a laptop and a
multi-second freeze on a phone.

- **Bake on demand, share the frame budget.** Tower sprites bake lazily on first blit and
  draw from the same budget discipline as ground chunks. An unbaked tower is skipped for a
  frame, not waited on.
- **Cache key encodes every pixel-affecting input:** `projId | styleIndex | stories |
  logoHash | phase`. Miss one and an edit silently shows the stale building.
- **Halo is lazier still** — silhouette highlight bakes only for the hovered/selected
  tower, in `draw()`, two colors for the pulse.

**PITFALL.** Never implement night lighting as a per-frame filter or composite over the
assembled scene. It breaks bake-once, scales with viewport area instead of sprite count,
and is the easiest way to turn a 60 fps city into a 12 fps one. Lighting belongs in the
bake.

---

## 05 — Tuning knobs

| Symbol | Role | Failure if wrong |
|---|---|---|
| `CH` | chunk edge in tiles | too small: key/draw-call overhead dominates; too large: one bake becomes a frame stall, defeating the budget |
| `BAKE` | fixed chunk bake scale | tying it to `cam.s` invalidates the cache every zoom step |
| `chunkBudget` | bakes per frame (~6) | zero/unreset: permanently blank world; too high: pan stutter returns |
| `RL − maxFront` | boardwalk + meander headroom (12) | less than meander amplitude: elbow floods the district |
| `k` in `tanh(k·sin)` | elbow sharpness (2.5) | low: lazy procedural sine; high: stair-stepped banks |
| `m` clamp | bridge skew limit (±1.3) | unclamped: bridges at sharp elbows run nearly parallel to the river |
| deck lift | 14 px above water | too low: reads as painted tiles; too high: piers dominate, occlusion gets fragile |
| twinkle threshold | `> 0.92` ≈ 8% of windows | higher: facade-wide strobe; zero: dead static skyline |
| `CLOCK*10` | window clock, 2.8 s | faster: strobe; much slower: static image with occasional glitches |

---

## 06 — Invariants & failure modes

- **Ground tile rendering is pure in `(x,y,SEED,phase)`.** Any mutable-state read produces
  chunk borders that disagree, visible only at certain pan positions — miserable to repro.
- **Chunk keys include `SEED` and `RL`.** The shoreline is district-derived and moves.
- **Sprite cache keys encode everything that changes pixels** (stories, style, logo hash,
  phase).
- **The river holds no state.** `shoreAt` / `riverWidth` are closed-form in `w` — no
  sample arrays, no incremental walk. Any chunk must be bakeable in isolation, any time,
  any order.
- **Nothing spawns between the district and the water** (ambient depth ≤ `maxFront`). This
  rule has regressed twice.
- **Painter's order is non-negotiable**, including the bridge stack: near-bank land → deck
  (`sC−1.5`) → pylons → cables → far-bank city.
- **Two clocks, not three.**
- **Lighting is baked, never a per-frame pass.**
- **Deterministic randomness only** — `hash2(x,y,SEED)`, `mulberry32`. No `Math.random()`
  in generation, or the city reshuffles on reload and a project loses its building.
