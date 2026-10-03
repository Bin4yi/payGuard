# PayGuard promo — design spec

**Concept:** "Soft surfaces, hard guarantees." Calm, tactile neumorphic objects
(the things you hold: approvals, grants, receipts) float under frosted glass
panels (the things you see through: the agent, the diagrams, the app). The
softness makes the one hard element, the gateway's refusal, land.

## Palette (light canvas, one accent)

| Role | Hex |
|---|---|
| Base canvas (neumorphic) | `#E6EBF2` |
| Shadow dark / light | `#C3CBD8` / `#FFFFFF` |
| Ink | `#0E1528` |
| Muted ink | `#46526A` |
| Accent (indigo) | `#4F46E5` |
| Accent support (teal, glows only) | `#0891B2` |
| Refusal (red) | `#D92D20` |
| Paid (green) | `#047857` |

Glass: `rgba(255,255,255,.55)` + `backdrop-filter: blur(28px) saturate(170%)`,
2px `rgba(255,255,255,.9)` border, long soft shadow.

## Type

- Montserrat 900 / 700 / 400 for headlines and body (embedded).
- JetBrains Mono 400 / 700 for technical labels, field names, hashes (embedded).
- Headlines 96–150px, body 34–42px, labels 22–26px.

## Do

- One focal object per beat, anchored off-centre; supporting labels on edges.
- Ambient motion on every decorative (orb drift, slow breathing).
- Red only for refusals and attacks. Green only for "paid exactly once".

## Don't

- No dark scenes, no neon, no gradient text except the single title lockup.
- No claims beyond what the app does (see BRIEF.md notes).
