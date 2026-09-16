# Editable scene / asset organization

The scene is constructed from procedural Three.js geometries through React Three Fiber.
Drei supplies rounded boxes and lines. There is no flattened room model and no raster
image pretending to be 3D. No character model, texture, font, or HDRI is fetched at runtime.

| Scene object name                                 | Source / independently editable parts                                      |
| ------------------------------------------------- | -------------------------------------------------------------------------- |
| `room-architecture`                               | Floor, architectural bays, walls, warm strips, ceiling, foreground rail    |
| `stadium-backdrop`                                | Field plane, yard lines, goalpost, tier rows, floodlights, instanced crowd |
| `station-gpt`, `station-claude`, `station-gemini` | Position and rotation in `scene/theme.ts`; shared assembly                 |
| `{id}-console-shell`                              | Rounded housing, flat screen fascia and desk/control top                   |
| `robot-{id}`                                      | Independent robot root; shell, face surface, ears, torso, joints and hands |
| `{id}-body-rig`, `{id}-head-pivot`                | Body lean/breathing and expressive head movement                           |
| `{id}-left-arm-pivot`, `{id}-right-arm-pivot`     | Arm gestures, with separate forearms and hands                             |
| `claude-tablet`                                   | Separate held analyst prop                                                 |
| `{id}-dynamic-screen`                             | Regenerated canvas texture driven from supplied state                      |
| `{id}-ticket-dock`, `{id}-ticket-card`            | Holder and movable card; text and lock mark rendered locally               |
| `central-scoreboard`                              | Housing, header, independent rank rows and temporary overlay               |
| `leaderboard-row-{id}`                            | Stable competitor identity with animated rank position                     |

The approved image remains at `public/reference/approved-arena.png` for visual comparison.
It is not requested by the running arena. This reconstruction preserves the room's color
families, left/rear/right station geometry, character silhouettes, football floor and
stadium/film-room identity. Procedural robots are intentionally simpler than the concept
render's sculpted hands and panels; later geometry refinements can retain the named pivots.

Future GLB assets belong under `public/models/` and local textures under `public/textures/`.
Keep robots, stations and room separate; preserve or adapt these named rig pivots. Geometry
may be replaced inside these components without changing the external state interface.
Imported artwork should bring its license/source notes into this document.

Dynamic display surfaces use CanvasTexture in sRGB, local Arial/system font drawing,
no mipmaps, and linear filtering. Current labels, money, odds, line, confidence, risk,
live values and ticket status are painted from state; they are not baked into static
textures. Materials and accents are separate meshes. Crowd uses instancing to reduce
draw calls. Canvas caps device pixel ratio at 1.5; the sun uses one 2048 shadow map.

Current limitations: procedural facial expressions and simple arm pivots, no skeletal
animation clips, no voice/audio, no object picking/editor UI, no official team branding,
and no imported stadium photograph. Mobile preserves the full master layout with a wider
camera; close-up presets and the HTML state readout improve small-screen data inspection.
