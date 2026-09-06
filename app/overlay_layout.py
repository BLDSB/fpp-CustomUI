"""Derive FPP pixel overlay model geometry from an xLights virtual display map.

FPP treats a pixel overlay model as a rectangle: ``height = StringCount *
StrandsPerString`` and ``width = nodes / height``.  A letter-shaped model
configured that way collapses to a 1 x N line, so every spatial effect (text,
scrolls, wipes) renders along a strip instead of across the glyph.

FPP does support arbitrary shapes: ``Orientation: "custom"`` plus a ``data``
grid string, where ``,`` separates columns, ``;`` separates rows, an empty cell
means "no pixel here", and a value is the 1-based node index *within that
model's own channel block*.  fppd builds its channel map as
``(node - 1) * channelsPerNode``, and unmapped cells route to an off-channel —
so a whole-model ``fill`` still lights every real pixel.

The geometry comes from ``/home/fpp/media/config/virtualdisplaymap``, which
xLights' FPP Connect uploads.  Each node is one line of
``x,y,z,startChannel,channelCount,colorOrder,pixelSize`` under a
``# Model: 'Name', N nodes`` header.  Because a node's index is derived from its
channel and its position from its coordinates, upstream xLights complexity
(wiring order, start corner, strand count, string order) is already baked in and
cannot affect the result.
"""

import statistics

# fppd truncates a custom model past this many cells.
MAX_CELLS = 600 * 600


def parse_display_map(text):
    """Parse a virtualdisplaymap into a list of model dicts.

    Returns ``[{"name", "channels_per_node", "nodes": [(x, y, channel), ...]}]``
    in file order.  Malformed lines are skipped rather than raising: the file is
    machine-written, but a truncated upload should not take the page down.
    """
    models = []
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            # "# Model: 'F', 105 nodes" — anything else is a section comment.
            if line.startswith("# Model:"):
                start = line.find("'")
                end = line.rfind("'")
                name = line[start + 1:end] if 0 <= start < end else line[8:].strip()
                current = {"name": name, "channels_per_node": 3, "nodes": []}
                models.append(current)
            continue
        if current is None:
            continue
        parts = line.split(",")
        # The preview-size line ("1651,1343") and any stray text land here.
        if len(parts) < 5:
            continue
        try:
            x, y, _z = int(parts[0]), int(parts[1]), int(parts[2])
            channel, count = int(parts[3]), int(parts[4])
        except ValueError:
            continue
        current["nodes"].append((x, y, channel))
        if count in (1, 3, 4):
            current["channels_per_node"] = count
    return [m for m in models if m["nodes"]]


def _cluster(values):
    """Map each distinct coordinate to a 0-based row/column index.

    Pitch differs per model (the F is 25-26px apart, the C 15-16px), and even
    within a model coordinates jitter by a pixel, so a new index starts only
    where the gap exceeds half the median gap.
    """
    unique = sorted(set(values))
    if len(unique) < 2:
        return {v: 0 for v in unique}
    gaps = [unique[i + 1] - unique[i] for i in range(len(unique) - 1)]
    threshold = max(1.0, statistics.median(gaps) * 0.5)
    index = {unique[0]: 0}
    slot = 0
    for i in range(1, len(unique)):
        if unique[i] - unique[i - 1] > threshold:
            slot += 1
        index[unique[i]] = slot
    return index


def _serialize(grid):
    """Render a 2-D list of node numbers (None = blank) as FPP's data string."""
    return ";".join(
        ",".join("" if cell is None else str(cell) for cell in row) for row in grid
    )


def _grid_result(grid, width, height, node_count, placed, collisions,
                 start_channel, channel_count, channels_per_node):
    return {
        "width": width,
        "height": height,
        "node_count": node_count,
        "placed": placed,
        "collisions": collisions,
        "start_channel": start_channel,
        "channel_count": channel_count,
        "channels_per_node": channels_per_node,
        "data": _serialize(grid),
    }


def derive_grid(model):
    """Derive a single model's grid from its nodes.

    The display map's y axis runs bottom-up, so rows are flipped — without that
    every letter comes out upside down.  Map channels are 0-based; FPP's
    StartChannel is 1-based.
    """
    nodes = model["nodes"]
    cpn = model.get("channels_per_node", 3) or 3
    xs = _cluster([n[0] for n in nodes])
    ys = _cluster([n[1] for n in nodes])
    width = max(xs.values()) + 1
    height = max(ys.values()) + 1

    base = min(n[2] for n in nodes)
    last = max(n[2] for n in nodes)

    grid = [[None] * width for _ in range(height)]
    collisions = 0
    for x, y, channel in nodes:
        row = height - 1 - ys[y]
        col = xs[x]
        if grid[row][col] is not None:
            collisions += 1
        grid[row][col] = (channel - base) // cpn + 1

    placed = sum(1 for row in grid for cell in row if cell is not None)
    return _grid_result(
        grid, width, height, len(nodes), placed, collisions,
        base + 1, (last + cpn) - base, cpn,
    )


def derive_composite_grid(models):
    """Compose every model into one whole-display grid (FPP's "All" model).

    Per-model clustering cannot be reused here: each letter has its own pitch,
    and the composite has to share a single lattice.  The search starts at the
    finest per-model pitch — a shared lattice can be no coarser than the tightest
    letter — and steps down until every node lands without a collision.  The
    largest such pitch wins: it is the grid that matches the sign's real
    resolution, and a finer one only inflates the data string.  Node numbers stay
    relative to the lowest channel of the whole span, so gaps between models are
    simply unused node indices.
    """
    nodes = [n for m in models for n in m["nodes"]]
    if not nodes:
        return None
    cpn = models[0].get("channels_per_node", 3) or 3

    pitches = []
    for m in models:
        cols = _cluster([n[0] for n in m["nodes"]])
        rows = _cluster([n[1] for n in m["nodes"]])
        for index, axis in ((cols, 0), (rows, 1)):
            span = max(index) - min(index)
            steps = max(index.values())
            if steps > 0 and span > 0:
                pitches.append(span / steps)
    top = int(min(pitches)) if pitches else 1
    candidates = [p for p in range(max(top, 1), 0, -1)]

    xs = sorted({n[0] for n in nodes})
    ys = sorted({n[1] for n in nodes})
    x0, y0 = xs[0], ys[0]
    base = min(n[2] for n in nodes)
    last = max(n[2] for n in nodes)

    best = None
    for pitch in candidates:
        cells = {}
        collisions = 0
        for x, y, channel in nodes:
            key = (round((y - y0) / pitch), round((x - x0) / pitch))
            if key in cells:
                collisions += 1
            cells[key] = (channel - base) // cpn + 1
        height = max(r for r, _ in cells) + 1
        width = max(c for _, c in cells) + 1
        if width * height > MAX_CELLS:
            continue
        best = (pitch, cells, width, height, collisions)
        if collisions == 0:
            break

    if best is None:
        return None
    pitch, cells, width, height, collisions = best

    grid = [[None] * width for _ in range(height)]
    for (row, col), node in cells.items():
        # y was clustered top-down here, so flip to match derive_grid.
        grid[height - 1 - row][col] = node

    result = _grid_result(
        grid, width, height, len(nodes), len(cells), collisions,
        base + 1, (last + cpn) - base, cpn,
    )
    result["pitch"] = pitch
    return result


def parse_xlights_custom(text, start_channel, channels_per_node=3):
    """Build a grid from a pasted xLights CustomModel string.

    The escape hatch for a model whose nodes do not sit on a regular lattice.
    xLights uses the same ``,``/``;`` grid with 1-based node numbers, so the
    string is taken as-is; only the channel range has to be supplied.
    """
    rows = [row.split(",") for row in text.replace("|", ";").strip().split(";")]
    grid = []
    highest = 0
    for row in rows:
        out = []
        for cell in row:
            cell = cell.strip()
            if not cell:
                out.append(None)
                continue
            try:
                node = int(cell)
            except ValueError:
                raise ValueError(f"Layout contains a non-numeric cell: {cell!r}")
            if node < 1:
                out.append(None)
                continue
            highest = max(highest, node)
            out.append(node)
        grid.append(out)

    if not grid or highest == 0:
        raise ValueError("Layout is empty — no node numbers found.")

    width = max(len(row) for row in grid)
    for row in grid:
        row.extend([None] * (width - len(row)))
    height = len(grid)
    placed = sum(1 for row in grid for cell in row if cell is not None)

    return _grid_result(
        grid, width, height, placed, placed, 0,
        start_channel, highest * channels_per_node, channels_per_node,
    )


def validate_grid(grid, label="Layout"):
    """Return an error string, or None when the grid is safe to hand to fppd."""
    if grid["width"] < 1 or grid["height"] < 1:
        return f"{label}: grid has no cells."
    if grid["width"] * grid["height"] > MAX_CELLS:
        return (
            f"{label}: {grid['width']}x{grid['height']} exceeds FPP's "
            f"{MAX_CELLS}-cell limit."
        )
    cpn = grid["channels_per_node"] or 3
    capacity = grid["channel_count"] // cpn
    highest = 0
    for row in grid["data"].split(";"):
        for cell in row.split(","):
            if cell:
                highest = max(highest, int(cell))
    if highest > capacity:
        # fppd would write past the end of the model's shared-memory block.
        return (
            f"{label}: node {highest} is outside the model's {capacity}-node "
            f"channel range."
        )
    return None


def grid_mask(data):
    """Rows of "1"/"0" for the UI thumbnail.

    Far smaller over the wire than the data string itself, which for the
    whole-display model runs to tens of kilobytes.
    """
    return [
        "".join("1" if cell else "0" for cell in row.split(","))
        for row in data.split(";")
    ]


def to_fpp_model(name, grid):
    """One model-overlays.json entry for a custom-shaped model."""
    return {
        "Name": name,
        "Type": "Channel",
        "StartChannel": grid["start_channel"],
        "ChannelCount": grid["channel_count"],
        "ChannelCountPerNode": grid["channels_per_node"],
        "StringCount": 1,
        "StrandsPerString": 1,
        "Orientation": "custom",
        "StartCorner": "TL",
        "xLights": False,
        "data": grid["data"],
    }
