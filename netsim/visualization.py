"""
Plotly / NetworkX visualizations: animated topology maps and comparison charts.

The topology animation replays the real transfer log recorded by the backend.
Each frame is a snapshot at time t:
  * edges carrying data at t are highlighted,
  * a "packet" marker sits on each active edge, interpolated between source
    and destination by how far that transfer has progressed,
  * node fill shows how much of the file each node has received so far.
"""

from __future__ import annotations

import math

import networkx as nx
import pandas as pd
import plotly.graph_objects as go

from .backend import SimulationResult

COLORS = {
    "Client-Server": "#2563eb",  # blue
    "P2P": "#ea580c",  # orange
    "server": "#7c3aed",
    "seeder": "#7c3aed",
    "edge_idle": "rgba(148,163,184,0.35)",
}
ROLE_SYMBOL = {"server": "square", "seeder": "star", "client": "circle", "peer": "circle"}


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #
def build_graph(result: SimulationResult) -> tuple[nx.Graph, dict[str, tuple[float, float]]]:
    """Return the topology graph plus x/y positions for each node."""
    g = nx.Graph()
    for node in result.nodes:
        g.add_node(node.label, ip=node.ip, role=node.role)

    origin = result.origin_label
    others = [n.label for n in result.nodes if n.label != origin]

    if result.mode == "Client-Server":
        # Star topology: the server is the hub, every link goes through it.
        g.add_edges_from((origin, c) for c in others)
        pos = nx.circular_layout(others) if len(others) > 1 else {others[0]: (1.0, 0.0)}
        pos = {k: tuple(v) for k, v in pos.items()}
        pos[origin] = (0.0, 0.0)
    else:
        # Partial mesh: draw every peer pair that actually exchanged data.
        g.add_edges_from({tuple(sorted((e.src, e.dst))) for e in result.events})
        pos = {k: tuple(v) for k, v in nx.circular_layout([origin, *others]).items()}
    return g, pos


def _edge_xy(edges, pos) -> tuple[list, list]:
    xs, ys = [], []
    for a, b in edges:
        xs += [pos[a][0], pos[b][0], None]
        ys += [pos[a][1], pos[b][1], None]
    return xs, ys


def _progress_at(result: SimulationResult, t: float) -> dict[str, float]:
    """Fraction (0..1) of the file each node holds at time t."""
    received: dict[str, float] = {n.label: 0.0 for n in result.nodes}
    for e in result.events:
        if e.end <= t:
            received[e.dst] += e.nbytes
        elif e.start < t:
            received[e.dst] += e.nbytes * (t - e.start) / max(e.end - e.start, 1e-9)
    frac = {k: min(1.0, v / result.file_size) for k, v in received.items()}
    frac[result.origin_label] = 1.0
    return frac


def _node_trace(result: SimulationResult, pos, progress: dict[str, float]) -> go.Scatter:
    labels = [n.label for n in result.nodes]
    colors, sizes, hover, text = [], [], [], []
    for n in result.nodes:
        p = progress[n.label]
        is_origin = n.label == result.origin_label
        colors.append(p)
        sizes.append(34 if is_origin else 22)
        done = result.completion.get(n.label)
        hover.append(
            f"<b>{n.label}</b><br>IP: {n.ip}<br>Role: {n.role}<br>"
            f"Socket: 127.0.0.1:{n.port}<br>Received: {p * 100:.0f}%"
            + (f"<br>Finished in {done:.2f}s" if done is not None else "")
        )
        short = "S" if is_origin else n.label.split()[-1]
        text.append(short)
    return go.Scatter(
        x=[pos[l][0] for l in labels],
        y=[pos[l][1] for l in labels],
        mode="markers+text",
        text=text,
        textposition="middle center",
        textfont=dict(color="white", size=10),
        hovertext=hover,
        hoverinfo="text",
        marker=dict(
            size=sizes,
            color=colors,
            cmin=0,
            cmax=1,
            colorscale=[[0, "#94a3b8"], [0.5, "#38bdf8"], [1, "#16a34a"]],
            symbol=[ROLE_SYMBOL[n.role] for n in result.nodes],
            line=dict(width=2, color="white"),
            colorbar=dict(title="File<br>received", tickformat=".0%", len=0.6, thickness=12),
        ),
        name="Nodes",
        showlegend=False,
    )


def _frame_traces(result: SimulationResult, pos, t: float, packet_color: str):
    active = [e for e in result.events if e.start <= t < e.end]
    ex, ey = _edge_xy([(e.src, e.dst) for e in active], pos)
    active_edges = go.Scatter(
        x=ex, y=ey, mode="lines", hoverinfo="skip", showlegend=False,
        line=dict(color=packet_color, width=2.5),
    )
    px, py, ph = [], [], []
    for e in active:
        f = (t - e.start) / max(e.end - e.start, 1e-9)
        (x0, y0), (x1, y1) = pos[e.src], pos[e.dst]
        px.append(x0 + f * (x1 - x0))
        py.append(y0 + f * (y1 - y0))
        ph.append(f"{e.src} → {e.dst}<br>chunk #{e.chunk} ({e.nbytes // 1024} KB)")
    packets = go.Scatter(
        x=px, y=py, mode="markers", hovertext=ph, hoverinfo="text", showlegend=False,
        marker=dict(size=9, color=packet_color, symbol="diamond", line=dict(width=1, color="white")),
    )
    return active_edges, packets, _node_trace(result, pos, _progress_at(result, t)), len(active)


def topology_animation(result: SimulationResult, n_frames: int = 60, frame_ms: int = 120) -> go.Figure:
    """Animated topology map replaying the recorded packet transfers."""
    g, pos = build_graph(result)
    color = COLORS[result.mode]
    t_end = max((e.end for e in result.events), default=1.0)
    times = [t_end * i / (n_frames - 1) for i in range(n_frames)]

    ix, iy = _edge_xy(g.edges(), pos)
    idle = go.Scatter(x=ix, y=iy, mode="lines", hoverinfo="skip", showlegend=False,
                      line=dict(color=COLORS["edge_idle"], width=1))

    first = _frame_traces(result, pos, 0.0, color)
    fig = go.Figure(data=[idle, *first[:3]])

    frames = []
    for i, t in enumerate(times):
        edges, packets, nodes, n_active = _frame_traces(result, pos, t, color)
        frames.append(go.Frame(
            data=[edges, packets, nodes],
            traces=[1, 2, 3],  # trace 0 (idle edges) is static
            name=str(i),
            layout=go.Layout(title_text=f"{result.mode} · t = {t:.2f}s · {n_active} packets in flight"),
        ))
    fig.frames = frames

    slider_steps = [
        dict(method="animate", label=f"{t:.2f}s",
             args=[[str(i)], dict(mode="immediate", frame=dict(duration=0, redraw=True), transition=dict(duration=0))])
        for i, t in enumerate(times)
    ]
    fig.update_layout(
        title_text=f"{result.mode} · press ▶ to replay traffic",
        height=560,
        margin=dict(l=10, r=10, t=50, b=10),
        xaxis=dict(visible=False, range=[-1.3, 1.3]),
        yaxis=dict(visible=False, range=[-1.3, 1.3], scaleanchor="x"),
        plot_bgcolor="rgba(0,0,0,0)",
        hovermode="closest",
        updatemenus=[dict(
            type="buttons", direction="left", x=0.0, y=-0.02, xanchor="left", yanchor="top",
            pad=dict(r=10, t=10), showactive=False,
            buttons=[
                dict(label="▶ Play", method="animate",
                     args=[None, dict(frame=dict(duration=frame_ms, redraw=True),
                                      transition=dict(duration=0), fromcurrent=True, mode="immediate")]),
                dict(label="⏸ Pause", method="animate",
                     args=[[None], dict(frame=dict(duration=0, redraw=False), mode="immediate")]),
            ],
        )],
        sliders=[dict(
            active=0, steps=slider_steps, x=0.25, len=0.75, y=-0.02, yanchor="top",
            currentvalue=dict(prefix="Time: ", visible=True), pad=dict(t=10),
        )],
    )
    return fig


# --------------------------------------------------------------------------- #
# Per-run charts
# --------------------------------------------------------------------------- #
def completion_bar(result: SimulationResult) -> go.Figure:
    items = sorted(result.completion.items(), key=lambda kv: kv[1])
    fig = go.Figure(go.Bar(
        x=[k for k, _ in items], y=[v for _, v in items],
        marker_color=COLORS[result.mode],
        hovertemplate="%{x}: %{y:.3f}s<extra></extra>",
    ))
    fig.update_layout(title="Download completion time per node", yaxis_title="seconds",
                      height=320, margin=dict(l=10, r=10, t=40, b=10))
    return fig


def upload_share_pie(result: SimulationResult) -> go.Figure:
    """Who did the uploading? Server-only in C/S, spread out in P2P."""
    origin = result.origin_label
    origin_bytes = result.uploaded_by.get(origin, 0)
    peer_bytes = sum(v for k, v in result.uploaded_by.items() if k != origin)
    fig = go.Figure(go.Pie(
        labels=[origin, "Other peers" if result.mode == "P2P" else "Clients"],
        values=[origin_bytes, peer_bytes],
        hole=0.55,
        marker=dict(colors=[COLORS["server"], COLORS[result.mode]]),
        sort=False,
    ))
    fig.update_layout(title="Upload work distribution", height=320,
                      margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h"))
    return fig


def timeline_chart(result: SimulationResult, max_rows: int = 40) -> go.Figure:
    """Gantt-style view of every chunk transfer, grouped by receiver."""
    rows = [n.label for n in result.nodes if n.label != result.origin_label][:max_rows]
    wanted = set(rows)
    fig = go.Figure()
    # One trace per source type keeps the chart fast even with hundreds of transfers.
    for from_origin, color in ((True, COLORS["server"]), (False, COLORS[result.mode])):
        ev = [e for e in result.events if e.dst in wanted and (e.src == result.origin_label) == from_origin]
        if not ev:
            continue
        fig.add_trace(go.Bar(
            base=[e.start for e in ev], x=[e.end - e.start for e in ev], y=[e.dst for e in ev],
            orientation="h", marker_color=color, showlegend=False,
            customdata=[[e.src, e.chunk, e.end] for e in ev],
            hovertemplate="%{customdata[0]} → %{y}<br>chunk #%{customdata[1]}"
                          "<br>%{base:.3f}s → %{customdata[2]:.3f}s<extra></extra>",
        ))
    fig.update_layout(
        title="Transfer timeline (purple = from origin, colored = from peers)",
        barmode="overlay", xaxis_title="seconds", height=max(300, 18 * len(rows) + 80),
        margin=dict(l=10, r=10, t=40, b=10), yaxis=dict(autorange="reversed"),
    )
    return fig


# --------------------------------------------------------------------------- #
# Comparison charts
# --------------------------------------------------------------------------- #
def _line_by_arch(df: pd.DataFrame, y: str, title: str, y_title: str,
                  theory_col: str | None = None) -> go.Figure:
    fig = go.Figure()
    for arch in ("Client-Server", "P2P"):
        d = df[df["Architecture"] == arch].sort_values("Clients")
        fig.add_trace(go.Scatter(
            x=d["Clients"], y=d[y], mode="lines+markers", name=f"{arch} (measured)",
            line=dict(color=COLORS[arch], width=3), marker=dict(size=8),
        ))
        if theory_col:
            fig.add_trace(go.Scatter(
                x=d["Clients"], y=d[theory_col], mode="lines", name=f"{arch} (theory)",
                line=dict(color=COLORS[arch], width=1.5, dash="dash"),
            ))
    fig.update_layout(title=title, xaxis_title="Number of clients / peers", yaxis_title=y_title,
                      height=380, margin=dict(l=10, r=10, t=50, b=10),
                      legend=dict(orientation="h", y=-0.25), hovermode="x unified")
    return fig


def response_time_chart(df: pd.DataFrame) -> go.Figure:
    return _line_by_arch(df, "Max Response (s)", "Response Time vs Number of Clients",
                         "Time until all clients finish (s)", theory_col="Theoretical Min (s)")


def scalability_chart(df: pd.DataFrame) -> go.Figure:
    return _line_by_arch(df, "Throughput (MB/s)", "Scalability: Aggregate Delivery Throughput",
                         "Total MB delivered / second")


def origin_load_chart(df: pd.DataFrame) -> go.Figure:
    return _line_by_arch(df, "Origin Load (%)", "Central Bottleneck: Share of Bytes Sent by Server/Seeder",
                         "% of all data uploaded by origin")


def overhead_chart(df: pd.DataFrame) -> go.Figure:
    """Grouped bars: connections and control messages per architecture."""
    fig = go.Figure()
    for arch in ("Client-Server", "P2P"):
        d = df[df["Architecture"] == arch].sort_values("Clients")
        fig.add_trace(go.Bar(x=d["Clients"].astype(str), y=d["Control Msgs / Client"],
                             name=arch, marker_color=COLORS[arch],
                             customdata=d[["Connections", "Overhead (%)"]],
                             hovertemplate="%{y:.1f} msgs/client<br>%{customdata[0]} TCP connections"
                                           "<br>%{customdata[1]:.2f}% byte overhead<extra>" + arch + "</extra>"))
    fig.update_layout(title="Protocol Overhead: Control Messages per Client", barmode="group",
                      xaxis_title="Number of clients / peers", yaxis_title="control messages per client",
                      height=380, margin=dict(l=10, r=10, t=50, b=10), legend=dict(orientation="h", y=-0.25))
    return fig


def theory_curve(max_n: int, file_size: int, server_rate: float, peer_rate: float) -> go.Figure:
    """Smooth theoretical distribution-time curves for a large range of N."""
    from .backend import theoretical_times

    ns = list(range(1, max_n + 1))
    cs, p2p = zip(*(theoretical_times(n, file_size, server_rate, peer_rate) for n in ns))
    fig = go.Figure([
        go.Scatter(x=ns, y=cs, name="Client-Server  N·F/uₛ", line=dict(color=COLORS["Client-Server"], width=3)),
        go.Scatter(x=ns, y=p2p, name="P2P  max(F/uₛ, N·F/(uₛ+N·uₚ))", line=dict(color=COLORS["P2P"], width=3)),
    ])
    asymptote = file_size / peer_rate
    if math.isfinite(asymptote):
        fig.add_hline(y=asymptote, line_dash="dot", line_color="gray",
                      annotation_text=f"P2P limit F/uₚ = {asymptote:.2f}s")
    fig.update_layout(title="Theoretical Minimum Distribution Time (Kurose & Ross)",
                      xaxis_title="N", yaxis_title="seconds", height=380,
                      margin=dict(l=10, r=10, t=50, b=10), legend=dict(orientation="h", y=-0.25))
    return fig
