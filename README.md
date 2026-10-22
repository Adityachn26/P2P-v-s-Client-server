# 🌐 P2P vs Client-Server Network Simulator

An interactive Streamlit dashboard that simulates and compares **Peer-to-Peer** and
**Client-Server** architectures using **real TCP sockets and threads**, with a subnet
calculator, animated topology maps, and performance analytics.

## Project structure

```
.
├── app.py                  # Streamlit dashboard (UI, tabs, controls)
├── requirements.txt
└── netsim/
    ├── subnet.py           # Subnet calculator + dynamic IP allocation
    ├── backend.py          # Socket/threading simulations, bandwidth model, benchmark
    └── visualization.py    # NetworkX layouts + Plotly animations and charts
```

## Setup and run

Requires Python 3.10 or newer.

```bash
# 1. Go to the project folder
cd aryan

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Launch the dashboard (opens http://localhost:8501)
streamlit run app.py
```

## Using the dashboard

1. **Sidebar:** enter a base IP and CIDR (e.g. `192.168.1.0` / `24`), the number of clients or peers,
   the file and chunk size, and the uplink speeds.
2. **🧮 Subnet & IPs:** shows the Network ID, broadcast address, usable range, maximum hosts, and the IP given to each node.
3. **🖥️ Client-Server / 🔗 Peer-to-Peer:** click **Run**, then **▶ Play** on the topology map to watch
   packets (◆) travel between nodes. Nodes change from grey to green as they receive the file.
4. **📊 Performance Comparison:** pick client counts and click **Run benchmark** to get charts for response time,
   scalability, overhead, and server bottleneck, each compared with the theoretical minimum.

## How the simulation works

- Each node is a real TCP socket on `127.0.0.1` with a port chosen by the OS. Its subnet IP is a logical label.
- Loopback traffic is nearly instant, so each node's **uplink is throttled** (`LinkLimiter`). This is what
  creates the server bottleneck in Client-Server and lets upload capacity add up in P2P.
- **Client-Server:** the server starts one thread per client, but every thread shares the server's uplink.
- **P2P:** the seeder holds the file at first. Peers download the **rarest chunks first** from the
  **least-busy** peer that has them, then share those chunks onward. A tracker only does discovery and never carries file data.
- Every downloaded file is checked against the original's **SHA-256** hash.

Theoretical lower bounds shown in the charts (Kurose & Ross):

- Client-Server: `D ≥ N·F / u_s`
- P2P: `D ≥ max(F / u_s, N·F / (u_s + N·u_p))`

## Troubleshooting

- **Port 8501 is in use:** run `streamlit run app.py --server.port 8502`
- **Benchmark feels slow:** most of the time goes to the Client-Server runs, which grow linearly with N. Use a smaller file or a faster server uplink.
- **"Too many open files" with 60 peers and small chunks:** run `ulimit -n 4096` before launching.
