# Grading — fill open ground with smooth terrain for IngeTrazo

Fills the open ground between edges — a path, a terrace, a ramp, a sunken plaza — with **smooth, organic terrain** that meets every edge exactly, like ground laid out by hand. The edges may lie at different heights.

[Deutsch → LIESMICH.md](LIESMICH.md)

![Grading](screenshot.webp)

## Installation
1. In IngeTrazo: **Extensions ▸ Open plugins folder** (Windows: `%APPDATA%\ingetrazo\plugins\`, Linux: `~/.local/share/ingetrazo/plugins/`).
2. Copy `grading_tool.py` into that folder.
3. Restart IngeTrazo → **Extensions ▸ Grading ▸** *Fill from Edges… · Edit Grading…* (also on the right-click menu).

Requires IngeTrazo ≥ 0.5.6 (extension API 2) · tested with IngeTrazo 0.6.0. A sample model is in [`examples/grading_examples.igz`](examples/grading_examples.igz).

## How to use
Select the edges around the open ground (loose edges and/or groups of edges) and run **Fill from Edges…**.

- The **largest closed loop** (seen from above) is the outer contour. Closed loops inside it are inner contours: **Holes** (a platform or a bed the ground stops at) or **Fixed lines** (the ground runs on past them).
- **Open edge chains** inside are fixed lines (a swale, a ridge); selected **guide points** (Tape Measure) are spot heights.
- **Smoothness** — 100 % lets the ground run out level from every edge and rounds the top and toe of each slope; 0 % gives straight slopes with a crease at the edges.
- **Divisions / Mesh size** set the density of the triangle mesh; the dialog shows the triangle count and the **steepest slope** (° and 1 : n).
- **Live Preview** shows the terrain in the model while the dialog stays open (the viewport can still be orbited); **OK** keeps the current preview without recalculating.
- The result is **one group** that remembers its edges and settings — select it and run **Edit Grading…** to change it. One undo step per run.

## Units
The dialog works in the **units of the open document** — set in IngeTrazo under **Window ▸ Units…** (Ctrl+U) or **Window ▸ Preferences… ▸ Units**:
- Length fields show and take the document's length unit and form: m, cm, mm, in, ft, feet-and-inches (`9'10"`) or fractional feet-and-inches (`9'10 1/8"`).
- Type with or without a unit: `25`, `2,5 cm`, `1'2"`, `3/4"`. A number without a unit is in the document's unit (inches in a feet-and-inches document); a value typed in another unit is converted.
- Angles are in the document's angle unit (degrees, radians or gon), areas in its area unit; per-cent fields and counts have no unit. The decimal separator follows the document — `.` and `,` are both accepted when typing.
- Changing the units only changes how values are shown and typed — the model stays exactly as it is. Open the dialog again after changing them.
- On IngeTrazo builds before 0.5.8 the fields are plain numbers in metres.
- Heights, offsets and distances are lengths in the document's unit; **Smoothness** is in per cent.

## Changelog
- **1.2** — **document units**: triangle size and the sizes in the dialog show and take the units of Preferences ▸ Units, numbers with its decimal separator. Tested with IngeTrazo 0.6.0.
- **1.1** — own toolbar **Grading** with one icon per command (Fill from Edges… · Edit Grading…). It starts on a row of its own under the built-in toolbars; move, float or hide it like those (right-click on a toolbar). Icons drawn in IngeTrazo's own style, they follow the light/dark theme.
- **1.0** — first release.

## Licence
GPL-3.0-or-later · © 2026 Pesi (pesi3d.de) · [Impressum](https://pesi3d.de)
