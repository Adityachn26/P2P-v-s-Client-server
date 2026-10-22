"""
Streamlit dashboard: P2P vs Client-Server network architecture simulator.

Run with:   streamlit run app.py
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from netsim import visualization as viz
from netsim.backend import run_benchmark, run_client_server, run_p2p, theoretical_times
from netsim.subnet import allocate_ips, calculate_subnet

MB = 1024 * 1024
KB = 1024

st.set_page_config(page_title="P2P vs Client-Server Simulator", page_icon="🌐", layout="wide")


# --------------------------------------------------------------------------- #
# Sidebar: network + simulation configuration
# --------------------------------------------------------------------------- #
st.sidebar.title("⚙️ Configuration")

st.sidebar.subheader("Subnet")
base_ip = st.sidebar.text_input("Base IP address", "192.168.1.0")
cidr = st.sidebar.number_input("CIDR prefix ( / )", min_value=8, max_value=30, value=24, step=1)

try:
    subnet = calculate_subnet(base_ip, int(cidr))
except ValueError as err:
    st.sidebar.error(str(err))
    st.error(f"Invalid subnet configuration: {err}")
    st.stop()

# One address is reserved for the server / seeder.
max_nodes = max(1, min(60, subnet.max_hosts - 1))
if subnet.max_hosts < 2:
    st.error("This subnet has fewer than 2 usable hosts; pick a larger network (smaller CIDR).")
    st.stop()

st.sidebar.subheader("Topology")
n_nodes = st.sidebar.slider("Clients / peers", 1, max_nodes, min(8, max_nodes),
                            help=f"Capped by the subnet ({subnet.max_hosts} usable hosts) and at 60 for speed.")

st.sidebar.subheader("Workload")
file_kb = st.sidebar.select_slider("File size", [128, 256, 512, 1024, 2048], value=512,
                                   format_func=lambda v: f"{v} KB" if v < 1024 else f"{v // 1024} MB")
chunk_kb = st.sidebar.select_slider("Chunk size", [8, 16, 32, 64, 128], value=32,
                                    format_func=lambda v: f"{v} KB")
server_rate = st.sidebar.slider("Server / seeder uplink (MB/s)", 1.0, 20.0, 4.0, 0.5)
peer_rate = st.sidebar.slider("Peer uplink (MB/s)", 0.25, 5.0, 1.0, 0.25,
                              help="Upload capacity each P2P peer contributes back to the swarm.")

with st.sidebar.expander("Advanced"):
    processing_ms = st.number_input("Server processing delay per request (ms)", 0.0, 200.0, 5.0, 1.0)
    parallel = st.slider("Parallel chunk downloads per peer", 1, 6, 3)
    seed = st.number_input("Random seed", 0, 10_000, 42)

ips = allocate_ips(subnet, max_nodes + 1)
file_size = file_kb * KB
chunk_size = chunk_kb * KB
params = dict(n=n_nodes, file=file_size, chunk=chunk_size, srv=server_rate, peer=peer_rate,
              proc=processing_ms, par=parallel, seed=seed, net=str(subnet.network))


# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.title("🌐 Peer-to-Peer vs Client-Server Network Simulator")
st.caption(
    "Real TCP sockets + threads on localhost, with throttled per-node uplinks. "
    f"Subnet **{subnet.network_id}/{subnet.cidr}** · {n_nodes} clients/peers · "
    f"{file_kb} KB file in {chunk_kb} KB chunks."
)

tab_subnet, tab_cs, tab_p2p, tab_cmp, tab_about = st.tabs(
    ["🧮 Subnet & IPs", "🖥️ Client-Server", "🔗 Peer-to-Peer", "📊 Performance Comparison", "📘 How it works"]
)


# --------------------------------------------------------------------------- #
# Tab 1: subnet calculator + IP allocation
# --------------------------------------------------------------------------- #
with tab_subnet:
    if subnet.host_bits_were_set:
        st.info(f"`{subnet.input_address}` has host bits set; normalised to network "
                f"`{subnet.network_id}/{subnet.cidr}`.")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Network ID", f"{subnet.network_id}/{subnet.cidr}")
    c2.metric("Broadcast", subnet.broadcast)
    c3.metric("Usable hosts", f"{subnet.max_hosts:,}")
    c4.metric("Subnet mask", subnet.netmask)
    st.markdown(f"**Usable IP range:** `{subnet.usable_range}`")

    left, right = st.columns([1, 1])
    with left:
        st.subheader("Subnet details")
        st.dataframe(pd.DataFrame(subnet.as_rows(), columns=["Property", "Value"]),
                     hide_index=True, width="stretch")
    with right:
        st.subheader("Dynamic IP assignment")
        assigned = ips[: n_nodes + 1]
        table = pd.DataFrame({
            "Node": ["Server / Seeder"] + [f"Client / Peer {i}" for i in range(1, n_nodes + 1)],
            "Assigned IP": assigned,
            "Role (C/S)": ["Server"] + ["Client"] * n_nodes,
            "Role (P2P)": ["Seeder"] + ["Peer"] * n_nodes,
        })
        st.dataframe(table, hide_index=True, width="stretch", height=360)
        st.caption(f"{len(assigned)} of {subnet.max_hosts:,} usable addresses allocated "
                   f"({100 * len(assigned) / subnet.max_hosts:.1f}% pool utilisation).")


# --------------------------------------------------------------------------- #
# Tabs 2 & 3: single-run simulations
# --------------------------------------------------------------------------- #
def render_run(mode: str) -> None:
    key = f"result_{mode}"
    run_col, info_col = st.columns([1, 3])
    with run_col:
        clicked = st.button(f"▶ Run {mode} simulation", key=f"btn_{mode}", type="primary", width="stretch")
    with info_col:
        if mode == "Client-Server":
            st.markdown("A **central server thread pool** serves the whole file to every client. "
                        "All client threads share the server's single uplink.")
        else:
            st.markdown("A **seeder** starts with the file. Peers fetch chunks **rarest-first** from "
                        "whoever has them and immediately re-share them with others.")

    if clicked:
        with st.spinner(f"Running {mode} over real sockets..."):
            if mode == "Client-Server":
                result = run_client_server(n_nodes, ips, file_size, chunk_size, server_rate * MB,
                                           processing_ms, int(seed))
            else:
                result = run_p2p(n_nodes, ips, file_size, chunk_size, server_rate * MB, peer_rate * MB,
                                 parallel, int(seed))
        st.session_state[key] = (result, params)

    if key not in st.session_state:
        st.info("Press the button to launch the simulation.")
        return

    result, used = st.session_state[key]
    if used != params:
        st.warning("Settings changed since this run; showing the previous result. Run again to refresh.")

    d_cs, d_p2p = theoretical_times(result.n_clients or 1, result.file_size,
                                    used["srv"] * MB, used["peer"] * MB)
    theory = d_cs if mode == "Client-Server" else d_p2p
    m = st.columns(6)
    m[0].metric("Avg response", f"{result.avg_time:.2f}s")
    m[1].metric("All done after", f"{result.max_time:.2f}s",
                delta=f"{result.max_time - theory:+.2f}s vs theory", delta_color="off")
    m[2].metric("Origin load", f"{result.origin_load_pct:.0f}%")
    m[3].metric("TCP connections", f"{result.connections:,}")
    m[4].metric("Control msgs", f"{result.control_messages:,}")
    m[5].metric("Integrity (SHA-256)",
                "✅ all valid" if result.all_verified else f"⚠️ {sum(result.verified.values())}/{len(result.verified)}")

    st.plotly_chart(viz.topology_animation(result), width="stretch", key=f"topo_{mode}")
    a, b = st.columns([2, 1])
    a.plotly_chart(viz.completion_bar(result), width="stretch", key=f"bar_{mode}")
    b.plotly_chart(viz.upload_share_pie(result), width="stretch", key=f"pie_{mode}")
    st.plotly_chart(viz.timeline_chart(result), width="stretch", key=f"tl_{mode}")

    with st.expander(f"Packet / transfer log ({len(result.events)} transfers)"):
        ip_of = {n.label: n.ip for n in result.nodes}
        log_df = pd.DataFrame([{
            "Start (s)": round(e.start, 4), "End (s)": round(e.end, 4),
            "Source": f"{e.src} ({ip_of[e.src]})", "Destination": f"{e.dst} ({ip_of[e.dst]})",
            "Chunk": e.chunk, "Bytes": e.nbytes,
        } for e in result.events])
        st.dataframe(log_df, hide_index=True, width="stretch", height=300)


with tab_cs:
    render_run("Client-Server")

with tab_p2p:
    render_run("P2P")


# --------------------------------------------------------------------------- #
# Tab 4: benchmark both architectures across N
# --------------------------------------------------------------------------- #
with tab_cmp:
    st.markdown("Runs **both** architectures for each client count with identical settings and "
                "compares measured results against the theoretical lower bounds.")
    choices = [n for n in (1, 2, 4, 6, 8, 12, 16, 20, 25, 30, 40, 50, 60) if n <= max_nodes]
    default = [n for n in (2, 4, 8, 12, 16, 20) if n <= max_nodes] or choices[:1]
    counts = sorted(st.multiselect("Client counts to benchmark", choices, default=default))

    est = sum(theoretical_times(n, file_size, server_rate * MB, peer_rate * MB)[0] * 1.05 + 0.3 for n in counts)
    st.caption(f"Estimated run time ≈ {est:.0f}s (dominated by the Client-Server runs).")

    if st.button("🚀 Run benchmark", type="primary", disabled=not counts):
        bar = st.progress(0.0, text="Starting...")
        rows = run_benchmark(counts, ips, file_size, chunk_size, server_rate * MB, peer_rate * MB,
                             processing_ms, parallel, int(seed),
                             progress=lambda f, msg: bar.progress(f, text=msg))
        st.session_state["bench"] = (pd.DataFrame(rows), params)

    if "bench" in st.session_state:
        df, used = st.session_state["bench"]
        if {k: v for k, v in used.items() if k != "n"} != {k: v for k, v in params.items() if k != "n"}:
            st.warning("Settings changed since this benchmark; re-run to refresh.")

        biggest = df["Clients"].max()
        cs_t = df[(df.Architecture == "Client-Server") & (df.Clients == biggest)]["Max Response (s)"].iloc[0]
        p2p_t = df[(df.Architecture == "P2P") & (df.Clients == biggest)]["Max Response (s)"].iloc[0]
        k = st.columns(3)
        k[0].metric(f"C/S time @ {biggest} clients", f"{cs_t:.2f}s")
        k[1].metric(f"P2P time @ {biggest} peers", f"{p2p_t:.2f}s")
        k[2].metric("P2P speed-up", f"{cs_t / max(p2p_t, 1e-9):.1f}×")

        r1a, r1b = st.columns(2)
        r1a.plotly_chart(viz.response_time_chart(df), width="stretch")
        r1b.plotly_chart(viz.scalability_chart(df), width="stretch")
        r2a, r2b = st.columns(2)
        r2a.plotly_chart(viz.overhead_chart(df), width="stretch")
        r2b.plotly_chart(viz.origin_load_chart(df), width="stretch")

        st.subheader("Raw results")
        st.dataframe(df.round(3), hide_index=True, width="stretch")
        st.download_button("⬇ Download CSV", df.to_csv(index=False), "benchmark_results.csv", "text/csv")

    st.divider()
    st.plotly_chart(viz.theory_curve(200, file_size, server_rate * MB, peer_rate * MB), width="stretch")


# --------------------------------------------------------------------------- #
# Tab 5: explanation
# --------------------------------------------------------------------------- #
with tab_about:
    st.markdown(r"""
### What is actually happening?

| Layer | Implementation |
|---|---|
| **Addressing** | `ipaddress` computes the subnet; each node receives a logical IP from the usable range. |
| **Transport** | Every node is a real `socket.socket` (TCP) bound to `127.0.0.1` on an OS-assigned port. |
| **Concurrency** | One `threading.Thread` per connection on every listener; client/peer downloads run on their own threads. |
| **Bandwidth** | A `LinkLimiter` per node models a FIFO uplink of fixed capacity. Concurrent flows share it. |
| **Protocol** | Length-prefixed frames: `[header_len][payload_len][JSON header][payload]`. |
| **Integrity** | Each client/peer verifies the reassembled file against the origin's SHA-256. |

### Client-Server
All *N* clients connect to one server. The server spawns a handler thread per client, but every handler
pushes data through **the same uplink**, so the total time grows linearly: $D_{cs} \ge \dfrac{N \cdot F}{u_s}$.

### Peer-to-Peer
The file is split into chunks. A lightweight **tracker** (discovery only — it never carries file data) tells
peers who has which chunk. Peers pick the **rarest** chunk first and download it from the **least-busy** holder,
then announce it so others can fetch it from them. Upload capacity grows with the swarm:
$D_{p2p} \ge \max\left(\dfrac{F}{u_s}, \dfrac{N \cdot F}{u_s + N \cdot u_p}\right)$.

### Trade-offs you can see in the charts
* **Response time:** C/S grows linearly; P2P flattens towards $F/u_p$.
* **Scalability:** P2P aggregate throughput rises with N; C/S is pinned at $u_s$.
* **Overhead:** P2P needs many more connections and control messages (tracker lookups, HAVE announcements,
  per-chunk requests) — the price of decentralisation.
* **Bottleneck:** In C/S the server uploads 100% of bytes; in P2P the seeder's share drops as the swarm grows.
""")
