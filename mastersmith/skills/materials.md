---
name: materials
---

# Materials: what each finish is, and what to write in the plan

A real hard-surface object is never one material. Give every part a `finish`, and every area of a part that is a
different material a `zone`. The assembler turns the finish into a surface: a CC0 set for its structure, the
planned colour, wear on the edges, dirt in the cavities. Numbers here are what the plan should carry.

| finish | when | color | metal | roughness | notes |
|---|---|---|---|---|---|
| `metal` (bare steel, dark) | barrels, bolts, muzzle devices, pins, springs, rails on military weapons | #1e1e1e–#3a3a3a (parkerised / blued) | true | 0.45–0.6 | dark metal gets the parkerised set: matte, fine grain, worn edges lighter and smoother |
| `metal` (bare steel, light) | machined parts, stainless, tools, exposed slides | #6a6a6a–#9a9a9a | true | 0.3–0.4 | brushed set; the brushing runs along the part's length |
| `metal` + "aluminium"/"anodised" in `what` | handguards, receivers, rail mounts, optics bodies | #2a2a2a–#555555 | true | 0.35–0.45 | anodised look: brushed aluminium set, slightly satin |
| `painted` | vehicle panels, powder-coated furniture, coloured pods | the paint's colour | false | 0.35–0.55 | never metal true, even on a steel body: paint is a dielectric |
| `polymer` | stocks, grips, magazines, handguard shells, trigger guards | #3a3a3a–#6a6a6a (grey/black) or the picture's colour | false | 0.55–0.7 | moulded grain; satin, never shiny |
| `rubber` | butt pads, grip panels, tyres, cable boots | #141414–#262626 | false | 0.85–0.95 | dead matte, stippled; does not polish on edges |
| `glass` | lenses, windows, canopies | the tint | false | 0.05–0.15 | a zone, not projected, no wear |
| `wood` / `fabric` | furniture on old rifles, slings, seats | the picture's colour | false | 0.6–0.8 | no CC0 set yet: the mesher's texture is kept |

## Rules

1. **Metal true only for bare metal.** A painted truck panel is `painted`; a black polymer receiver is `polymer`.
   Metal true on a coated part reads as chrome or graphite.
2. **Zones carry the second material.** A polymer stock with a rubber butt pad: the stock is `polymer`, the pad a
   zone with `finish: rubber`. A scope: the body `metal` (aluminium), the lens a `glass` zone. Bare-steel controls
   (bolt handle, selector, pins) on a polymer body are `metal` zones.
3. **Colours come from the picture** (`ms plan` samples them per part); the finish decides how they render. Do not
   lighten a black steel part to make it "read": the material pass lifts metals to a real reflectance itself.
4. **Weapons**: barrel, muzzle device, bolt, charging handle, selector, trigger, sights, rails = `metal` (dark);
   receiver/stock/grip/handguard/magazine = `polymer` on modern rifles, `metal` (dark) on stamped-steel ones,
   `wood` on old ones; grip panels and pads = `rubber`; optics glass = `glass`.
5. **Vehicles**: body shell, doors, hood, bumpers = `painted` (one colour), trim and grilles = `metal`, tyres =
   `rubber` zones of the wheel, windows = `glass` zones, seats = `fabric`.
6. **Check the six views**: steel must look dark and reflective with lighter worn edges; rubber flat; polymer
   satin with visible grain; paint smooth with dirt in the seams. One graphite-looking material everywhere is a
   fail, and so is a metal that looks like chrome.
