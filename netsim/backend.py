"""
Network architecture simulations built on real TCP sockets and threads.

Every simulated node owns a real socket bound to 127.0.0.1 on an OS-assigned
port. The subnet IP shown in the UI (e.g. 192.168.1.5) is the node's
*logical* address; a small address book maps it to its loopback endpoint.

Loopback is effectively infinitely fast, so each node's upload link is
throttled by a ``LinkLimiter``. That is what makes the comparison realistic:

* Client-Server: one server uplink is shared by every client  -> bottleneck.
* Peer-to-Peer:  every peer adds its own uplink to the swarm   -> scales.

Wire protocol (length-prefixed frames)::

    +-----------------+------------------+--------------+-------------+
    | header_len (4B) | payload_len (4B) | JSON header  | raw payload |
    +-----------------+------------------+--------------+-------------+
"""

from __future__ import annotations

import hashlib
import json
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

LOOPBACK = "127.0.0.1"
FRAME_PREFIX = struct.Struct("!II")
IO_BLOCK = 16 * 1024  # bytes sent per throttled write; lets flows interleave fairly
SOCKET_TIMEOUT = 30.0  # seconds before a stuck transfer is abandoned


# --------------------------------------------------------------------------- #
# Bandwidth model
# --------------------------------------------------------------------------- #
class LinkLimiter:
    """
    A FIFO link of fixed capacity shared by every flow that uses it.

    Each call reserves the next free slot on the link, so N concurrent
    senders on one limiter each get roughly 1/N of the capacity.
    """

    def __init__(self, rate_bytes_per_sec: float):
        self.rate = float(rate_bytes_per_sec)
        self._lock = threading.Lock()
        self._next_free = 0.0

    def transmit(self, nbytes: int) -> None:
        with self._lock:
            now = time.perf_counter()
            start = max(now, self._next_free)
            self._next_free = start + nbytes / self.rate
            finish = self._next_free
        delay = finish - time.perf_counter()
        if delay > 0:
            time.sleep(delay)


# --------------------------------------------------------------------------- #
# Metrics / event log
# --------------------------------------------------------------------------- #
@dataclass
class TransferEvent:
    """One data transfer between two nodes (used for animation and stats)."""

    src: str
    dst: str
    start: float
    end: float
    nbytes: int
    chunk: int | None = None


class NetworkLog:
    """Thread-safe collector of transfers and protocol overhead counters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.t0 = time.perf_counter()
        self.events: list[TransferEvent] = []
        self.control_messages = 0
        self.control_bytes = 0
        self.data_bytes = 0
        self.connections = 0
        self.uploaded_by: dict[str, int] = {}

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def control(self, nbytes: int, count: int = 1) -> None:
        with self._lock:
            self.control_messages += count
            self.control_bytes += nbytes

    def connection(self) -> None:
        with self._lock:
            self.connections += 1

    def transfer(self, event: TransferEvent) -> None:
        with self._lock:
            self.events.append(event)
            self.data_bytes += event.nbytes
            self.uploaded_by[event.src] = self.uploaded_by.get(event.src, 0) + event.nbytes


# --------------------------------------------------------------------------- #
# Framing helpers
# --------------------------------------------------------------------------- #
def send_msg(
    sock: socket.socket,
    header: dict,
    payload: bytes = b"",
    limiter: LinkLimiter | None = None,
) -> int:
    """Send one frame; the payload is throttled through ``limiter``. Returns header bytes."""
    hdr = json.dumps(header, separators=(",", ":")).encode()
    sock.sendall(FRAME_PREFIX.pack(len(hdr), len(payload)) + hdr)
    for i in range(0, len(payload), IO_BLOCK):
        block = payload[i : i + IO_BLOCK]
        if limiter is not None:
            limiter.transmit(len(block))
        sock.sendall(block)
    return FRAME_PREFIX.size + len(hdr)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(min(n - len(buf), 256 * 1024))
        if not part:
            raise ConnectionError("peer closed connection mid-frame")
        buf.extend(part)
    return bytes(buf)


def recv_msg(sock: socket.socket) -> tuple[dict, bytes, int]:
    """Receive one frame. Returns (header, payload, header_wire_bytes)."""
    hdr_len, payload_len = FRAME_PREFIX.unpack(_recv_exact(sock, FRAME_PREFIX.size))
    header = json.loads(_recv_exact(sock, hdr_len))
    payload = _recv_exact(sock, payload_len) if payload_len else b""
    return header, payload, FRAME_PREFIX.size + hdr_len


class ListeningNode:
    """Base class: a TCP listener that spawns one handler thread per connection."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((LOOPBACK, 0))  # port 0 -> OS picks a free port
        self._sock.listen(512)
        self._sock.settimeout(0.2)  # lets the accept loop notice shutdown
        self.port = self._sock.getsockname()[1]
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running.set()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=2)
        self._sock.close()

    def _accept_loop(self) -> None:
        while self._running.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            conn.settimeout(SOCKET_TIMEOUT)
            threading.Thread(target=self._safe_handle, args=(conn,), daemon=True).start()

    def _safe_handle(self, conn: socket.socket) -> None:
        try:
            with conn:
                self.handle(conn)
        except (OSError, ConnectionError, ValueError):
            pass  # a client vanished; the simulation records it as incomplete

    def handle(self, conn: socket.socket) -> None:  # pragma: no cover - abstract
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass
class NodeInfo:
    label: str  # e.g. "Server", "Client 3", "Peer 7", "Seeder"
    ip: str  # logical subnet IP
    role: str  # "server" | "client" | "seeder" | "peer"
    port: int = 0  # real loopback port


@dataclass
class SimulationResult:
    mode: str  # "Client-Server" | "P2P"
    nodes: list[NodeInfo]
    completion: dict[str, float]  # client label -> seconds to full file
    verified: dict[str, bool]  # client label -> SHA-256 matched
    events: list[TransferEvent]
    file_size: int
    chunk_size: int
    wall_time: float
    control_messages: int
    control_bytes: int
    data_bytes: int
    connections: int
    uploaded_by: dict[str, int]
    origin_label: str  # node that holds the original file
    params: dict = field(default_factory=dict)

    @property
    def n_clients(self) -> int:
        return len(self.completion)

    @property
    def avg_time(self) -> float:
        return sum(self.completion.values()) / max(1, len(self.completion))

    @property
    def max_time(self) -> float:
        return max(self.completion.values(), default=0.0)

    @property
    def origin_load_pct(self) -> float:
        """Share of all delivered bytes that came from the server/seeder."""
        return 100.0 * self.uploaded_by.get(self.origin_label, 0) / max(1, self.data_bytes)

    @property
    def overhead_pct(self) -> float:
        total = self.control_bytes + self.data_bytes
        return 100.0 * self.control_bytes / max(1, total)

    @property
    def all_verified(self) -> bool:
        return bool(self.verified) and all(self.verified.values())


def make_file(size: int, seed: int) -> bytes:
    """Deterministic pseudo-random 'file' so every run is reproducible."""
    return random.Random(seed).randbytes(size)


def split_chunks(data: bytes, chunk_size: int) -> list[bytes]:
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


# --------------------------------------------------------------------------- #
# Client-Server architecture
# --------------------------------------------------------------------------- #
class FileServer(ListeningNode):
    """
    Central server. Each client connection is handled on its own thread, but
    all threads push data through the *same* uplink limiter, so the server's
    bandwidth is divided among every concurrent client.
    """

    def __init__(self, info: NodeInfo, data: bytes, chunk_size: int,
                 uplink: LinkLimiter, log: NetworkLog, processing_ms: float):
        super().__init__()
        self.info = info
        self.chunks = split_chunks(data, chunk_size)
        self.digest = hashlib.sha256(data).hexdigest()
        self.uplink = uplink
        self.log = log
        self.processing_s = processing_ms / 1000.0
        info.port = self.port

    def handle(self, conn: socket.socket) -> None:
        self.log.connection()
        request, _, hdr_bytes = recv_msg(conn)
        self.log.control(hdr_bytes)
        if request.get("type") != "GET_FILE":
            self.log.control(send_msg(conn, {"type": "ERROR", "reason": "bad request"}))
            return

        client = request["from"]
        time.sleep(self.processing_s)  # simulated request parsing / disk lookup
        self.log.control(send_msg(conn, {
            "type": "FILE_META", "chunks": len(self.chunks), "sha256": self.digest,
        }))
        for idx, chunk in enumerate(self.chunks):
            start = self.log.now()
            self.log.control(send_msg(conn, {"type": "CHUNK", "idx": idx}, chunk, self.uplink))
            self.log.transfer(TransferEvent(self.info.label, client, start, self.log.now(), len(chunk), idx))
        self.log.control(send_msg(conn, {"type": "DONE"}))


def _cs_client(info: NodeInfo, server_port: int, log: NetworkLog,
               completion: dict, verified: dict, start_barrier: threading.Barrier) -> None:
    start_barrier.wait()  # all clients fire their request at the same instant
    t_start = log.now()
    try:
        with socket.create_connection((LOOPBACK, server_port), timeout=SOCKET_TIMEOUT) as sock:
            log.control(send_msg(sock, {"type": "GET_FILE", "from": info.label, "src_ip": info.ip}))
            meta, _, hdr = recv_msg(sock)
            log.control(hdr)
            parts: list[bytes] = [b""] * meta["chunks"]
            while True:
                header, payload, hdr = recv_msg(sock)
                log.control(hdr)
                if header["type"] == "DONE":
                    break
                parts[header["idx"]] = payload
        completion[info.label] = log.now() - t_start
        verified[info.label] = hashlib.sha256(b"".join(parts)).hexdigest() == meta["sha256"]
    except (OSError, ConnectionError, ValueError, KeyError):
        verified[info.label] = False


def run_client_server(
    n_clients: int,
    ips: list[str],
    file_size: int = 512 * 1024,
    chunk_size: int = 32 * 1024,
    server_rate: float = 4 * 1024 * 1024,
    processing_ms: float = 5.0,
    seed: int = 42,
) -> SimulationResult:
    """
    Run one Client-Server simulation: ``n_clients`` threads simultaneously
    request the same file from one server. ``ips[0]`` is the server.
    """
    if len(ips) < n_clients + 1:
        raise ValueError("Not enough IP addresses for server + clients.")

    data = make_file(file_size, seed)
    log = NetworkLog()
    server_info = NodeInfo("Server", ips[0], "server")
    server = FileServer(server_info, data, chunk_size, LinkLimiter(server_rate), log, processing_ms)
    clients = [NodeInfo(f"Client {i + 1}", ips[i + 1], "client") for i in range(n_clients)]

    completion: dict[str, float] = {}
    verified: dict[str, bool] = {}
    barrier = threading.Barrier(n_clients)
    server.start()
    t_wall = time.perf_counter()
    try:
        threads = [
            threading.Thread(target=_cs_client, args=(c, server.port, log, completion, verified, barrier), daemon=True)
            for c in clients
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=SOCKET_TIMEOUT * 2)
    finally:
        server.stop()

    return SimulationResult(
        mode="Client-Server",
        nodes=[server_info, *clients],
        completion=completion,
        verified=verified,
        events=sorted(log.events, key=lambda e: e.start),
        file_size=file_size,
        chunk_size=chunk_size,
        wall_time=time.perf_counter() - t_wall,
        control_messages=log.control_messages,
        control_bytes=log.control_bytes,
        data_bytes=log.data_bytes,
        connections=log.connections,
        uploaded_by=dict(log.uploaded_by),
        origin_label=server_info.label,
        params={"server_rate": server_rate, "processing_ms": processing_ms},
    )


# --------------------------------------------------------------------------- #
# Peer-to-Peer architecture
# --------------------------------------------------------------------------- #
class Tracker:
    """
    Peer discovery only (like a BitTorrent tracker / DHT): it knows *who has
    which chunk* and where peers listen. It never carries file data, so it is
    not a data bottleneck. Each lookup/announce is counted as control overhead.
    """

    MSG_BYTES = 64  # approximate size of a HAVE / lookup message on the wire

    def __init__(self, log: NetworkLog) -> None:
        self._lock = threading.Lock()
        self.log = log
        self.peers: dict[str, "Peer"] = {}
        self.have: dict[str, set[int]] = {}

    def register(self, peer: "Peer") -> None:
        with self._lock:
            self.peers[peer.info.label] = peer
            self.have[peer.info.label] = set(peer.chunks)
        self.log.control(self.MSG_BYTES)

    def announce(self, label: str, idx: int) -> None:
        with self._lock:
            self.have[label].add(idx)
        self.log.control(self.MSG_BYTES)

    def snapshot(self) -> dict[str, set[int]]:
        self.log.control(self.MSG_BYTES)
        with self._lock:
            return {k: set(v) for k, v in self.have.items()}


class Peer(ListeningNode):
    """
    A P2P node: simultaneously a server (uploads chunks it owns through its own
    uplink) and a client (downloads missing chunks from other peers).
    """

    def __init__(self, info: NodeInfo, n_chunks: int, uplink: LinkLimiter,
                 log: NetworkLog, tracker: Tracker, digest: str,
                 initial: dict[int, bytes] | None = None, parallel: int = 3,
                 rng: random.Random | None = None):
        super().__init__()
        self.info = info
        info.port = self.port
        self.n_chunks = n_chunks
        self.uplink = uplink
        self.log = log
        self.tracker = tracker
        self.digest = digest
        self.chunks: dict[int, bytes] = dict(initial or {})
        self.parallel = parallel
        self.rng = rng or random.Random()
        self._lock = threading.Lock()
        self._pending: set[int] = set()
        self.active_uploads = 0
        self.done = threading.Event()
        self.t_start = 0.0
        self.completion: float | None = None
        self.verified = False
        if len(self.chunks) == n_chunks:
            self.done.set()

    # ---- server side ---------------------------------------------------- #
    def handle(self, conn: socket.socket) -> None:
        self.log.connection()
        request, _, hdr = recv_msg(conn)
        self.log.control(hdr)
        idx = request.get("idx")
        with self._lock:
            chunk = self.chunks.get(idx)
            if chunk is not None:
                self.active_uploads += 1
        if chunk is None:
            self.log.control(send_msg(conn, {"type": "NOT_HAVE", "idx": idx}))
            return
        try:
            start = self.log.now()
            self.log.control(send_msg(conn, {"type": "CHUNK", "idx": idx}, chunk, self.uplink))
            self.log.transfer(TransferEvent(self.info.label, request["from"], start, self.log.now(), len(chunk), idx))
        finally:
            with self._lock:
                self.active_uploads -= 1

    # ---- client side ---------------------------------------------------- #
    def start_download(self, barrier: threading.Barrier) -> list[threading.Thread]:
        def worker() -> None:
            barrier.wait()
            if not self.t_start:
                self.t_start = self.log.now()
            self._download_loop()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.parallel)]
        for t in threads:
            t.start()
        return threads

    def _pick(self) -> tuple[int, "Peer"] | None:
        """Rarest-first chunk selection, least-loaded holder selection."""
        have = self.tracker.snapshot()
        with self._lock:
            wanted = [i for i in range(self.n_chunks) if i not in self.chunks and i not in self._pending]
        if not wanted:
            return None

        holders = {i: [p for p, s in have.items() if i in s and p != self.info.label] for i in wanted}
        available = [i for i in wanted if holders[i]]
        if not available:
            return None
        rarest = min(len(holders[i]) for i in available)
        idx = self.rng.choice([i for i in available if len(holders[i]) == rarest])

        candidates = [self.tracker.peers[p] for p in holders[idx]]
        least = min(c.active_uploads for c in candidates)
        source = self.rng.choice([c for c in candidates if c.active_uploads == least])
        with self._lock:
            if idx in self.chunks or idx in self._pending:
                return None
            self._pending.add(idx)
        return idx, source

    def _download_loop(self) -> None:
        deadline = time.perf_counter() + SOCKET_TIMEOUT * 4
        while not self.done.is_set() and time.perf_counter() < deadline:
            choice = self._pick()
            if choice is None:
                time.sleep(0.005)  # everything wanted is in flight or not yet seeded
                continue
            idx, source = choice
            ok = False
            try:
                with socket.create_connection((LOOPBACK, source.port), timeout=SOCKET_TIMEOUT) as sock:
                    self.log.control(send_msg(sock, {"type": "GET_CHUNK", "idx": idx, "from": self.info.label}))
                    header, payload, hdr = recv_msg(sock)
                    self.log.control(hdr)
                    if header["type"] == "CHUNK":
                        with self._lock:
                            self.chunks[idx] = payload
                        self.tracker.announce(self.info.label, idx)
                        ok = True
            except (OSError, ConnectionError, ValueError):
                pass
            finally:
                with self._lock:
                    self._pending.discard(idx)
            if ok:
                self._check_complete()

    def _check_complete(self) -> None:
        with self._lock:
            if len(self.chunks) < self.n_chunks or self.done.is_set():
                return
            data = b"".join(self.chunks[i] for i in range(self.n_chunks))
            self.completion = self.log.now() - self.t_start
            self.verified = hashlib.sha256(data).hexdigest() == self.digest
            self.done.set()


def run_p2p(
    n_peers: int,
    ips: list[str],
    file_size: int = 512 * 1024,
    chunk_size: int = 32 * 1024,
    seeder_rate: float = 4 * 1024 * 1024,
    peer_rate: float = 1 * 1024 * 1024,
    parallel: int = 3,
    seed: int = 42,
) -> SimulationResult:
    """
    Run one P2P simulation: an initial seeder holds the file (same uplink as
    the Client-Server server, for a fair comparison) and ``n_peers`` peers
    swarm to download it, re-sharing every chunk they receive.
    ``ips[0]`` is the seeder.
    """
    if len(ips) < n_peers + 1:
        raise ValueError("Not enough IP addresses for seeder + peers.")

    data = make_file(file_size, seed)
    chunks = split_chunks(data, chunk_size)
    digest = hashlib.sha256(data).hexdigest()
    log = NetworkLog()
    tracker = Tracker(log)
    rng = random.Random(seed)

    seeder = Peer(NodeInfo("Seeder", ips[0], "seeder"), len(chunks), LinkLimiter(seeder_rate),
                  log, tracker, digest, initial=dict(enumerate(chunks)))
    peers = [
        Peer(NodeInfo(f"Peer {i + 1}", ips[i + 1], "peer"), len(chunks), LinkLimiter(peer_rate),
             log, tracker, digest, parallel=parallel, rng=random.Random(rng.random()))
        for i in range(n_peers)
    ]
    everyone = [seeder, *peers]
    for p in everyone:
        tracker.register(p)
        p.start()

    barrier = threading.Barrier(n_peers * parallel)
    t_wall = time.perf_counter()
    try:
        workers = [t for p in peers for t in p.start_download(barrier)]
        for t in workers:
            t.join(timeout=SOCKET_TIMEOUT * 5)
    finally:
        for p in everyone:
            p.stop()

    return SimulationResult(
        mode="P2P",
        nodes=[p.info for p in everyone],
        completion={p.info.label: p.completion for p in peers if p.completion is not None},
        verified={p.info.label: p.verified for p in peers},
        events=sorted(log.events, key=lambda e: e.start),
        file_size=file_size,
        chunk_size=chunk_size,
        wall_time=time.perf_counter() - t_wall,
        control_messages=log.control_messages,
        control_bytes=log.control_bytes,
        data_bytes=log.data_bytes,
        connections=log.connections,
        uploaded_by=dict(log.uploaded_by),
        origin_label=seeder.info.label,
        params={"seeder_rate": seeder_rate, "peer_rate": peer_rate, "parallel": parallel},
    )


# --------------------------------------------------------------------------- #
# Theory + benchmarking
# --------------------------------------------------------------------------- #
def theoretical_times(n: int, file_size: int, server_rate: float, peer_rate: float) -> tuple[float, float]:
    """
    Minimum distribution time (Kurose & Ross, ignoring client download caps):

        D_cs  >= N*F / u_s
        D_p2p >= max( F / u_s,  N*F / (u_s + N*u_p) )
    """
    d_cs = n * file_size / server_rate
    d_p2p = max(file_size / server_rate, n * file_size / (server_rate + n * peer_rate))
    return d_cs, d_p2p


def run_benchmark(
    client_counts: list[int],
    ips: list[str],
    file_size: int,
    chunk_size: int,
    server_rate: float,
    peer_rate: float,
    processing_ms: float = 5.0,
    parallel: int = 3,
    seed: int = 42,
    progress: Callable[[float, str], None] | None = None,
) -> list[dict]:
    """Run both architectures for each N and return one metrics row per run."""
    rows: list[dict] = []
    total = len(client_counts) * 2
    step = 0
    for n in client_counts:
        d_cs, d_p2p = theoretical_times(n, file_size, server_rate, peer_rate)
        for mode in ("Client-Server", "P2P"):
            if progress:
                progress(step / total, f"Running {mode} with {n} clients...")
            if mode == "Client-Server":
                r = run_client_server(n, ips, file_size, chunk_size, server_rate, processing_ms, seed)
                theory = d_cs
            else:
                r = run_p2p(n, ips, file_size, chunk_size, server_rate, peer_rate, parallel, seed)
                theory = d_p2p
            rows.append({
                "Architecture": mode,
                "Clients": n,
                "Avg Response (s)": r.avg_time,
                "Max Response (s)": r.max_time,
                "Theoretical Min (s)": theory,
                "Throughput (MB/s)": (n * file_size / (1024 * 1024)) / max(r.max_time, 1e-9),
                "Origin Load (%)": r.origin_load_pct,
                "Connections": r.connections,
                "Control Msgs": r.control_messages,
                "Control Msgs / Client": r.control_messages / n,
                "Control Bytes": r.control_bytes,
                "Overhead (%)": r.overhead_pct,
                "Completed": f"{len(r.completion)}/{n}",
                "Verified": r.all_verified,
            })
            step += 1
    if progress:
        progress(1.0, "Benchmark complete.")
    return rows


__all__ = [
    "LinkLimiter", "NetworkLog", "TransferEvent", "NodeInfo", "SimulationResult",
    "run_client_server", "run_p2p", "run_benchmark", "theoretical_times",
]
