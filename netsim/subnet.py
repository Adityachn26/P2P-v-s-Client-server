"""
Subnet calculator and IP address allocator.

Built on Python's standard ``ipaddress`` module so every computation follows
RFC 950 / RFC 3021 semantics (including the special /31 and /32 cases).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SubnetInfo:
    """Everything the dashboard needs to know about one IPv4 subnet."""

    input_address: str
    cidr: int
    network: ipaddress.IPv4Network
    netmask: str
    wildcard_mask: str
    network_id: str
    broadcast: str
    first_usable: str
    last_usable: str
    max_hosts: int
    ip_class: str
    is_private: bool
    host_bits_were_set: bool = field(default=False)

    @property
    def usable_range(self) -> str:
        return f"{self.first_usable} - {self.last_usable}"

    def as_rows(self) -> list[tuple[str, str]]:
        """Key/value pairs for a summary table."""
        return [
            ("Network ID", f"{self.network_id}/{self.cidr}"),
            ("Subnet Mask", self.netmask),
            ("Wildcard Mask", self.wildcard_mask),
            ("Broadcast Address", self.broadcast),
            ("Usable IP Range", self.usable_range),
            ("Max Usable Hosts", f"{self.max_hosts:,}"),
            ("Total Addresses", f"{self.network.num_addresses:,}"),
            ("Address Class", self.ip_class),
            ("Private (RFC 1918)", "Yes" if self.is_private else "No"),
        ]


def _ip_class(addr: ipaddress.IPv4Address) -> str:
    first_octet = int(str(addr).split(".")[0])
    if first_octet < 128:
        return "A"
    if first_octet < 192:
        return "B"
    if first_octet < 224:
        return "C"
    if first_octet < 240:
        return "D (Multicast)"
    return "E (Reserved)"


def calculate_subnet(base_ip: str, cidr: int) -> SubnetInfo:
    """
    Compute subnet properties for ``base_ip/cidr``.

    Raises ``ValueError`` with a readable message for bad input.
    """
    base_ip = base_ip.strip()
    if not 0 <= int(cidr) <= 32:
        raise ValueError("CIDR prefix must be between /0 and /32.")

    try:
        address = ipaddress.IPv4Address(base_ip)
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"'{base_ip}' is not a valid IPv4 address.") from exc

    # strict=False lets users type a host address (e.g. 192.168.1.37/24);
    # we normalise it to the network and flag that host bits were present.
    network = ipaddress.IPv4Network(f"{address}/{cidr}", strict=False)
    host_bits_set = network.network_address != address

    if network.prefixlen == 32:
        # Single-host route: the one address is the only "usable" one.
        first = last = network.network_address
        max_hosts = 1
    elif network.prefixlen == 31:
        # RFC 3021 point-to-point link: both addresses usable, no broadcast.
        first, last = network.network_address, network.broadcast_address
        max_hosts = 2
    else:
        first = network.network_address + 1
        last = network.broadcast_address - 1
        max_hosts = network.num_addresses - 2

    return SubnetInfo(
        input_address=base_ip,
        cidr=network.prefixlen,
        network=network,
        netmask=str(network.netmask),
        wildcard_mask=str(network.hostmask),
        network_id=str(network.network_address),
        broadcast=str(network.broadcast_address) if network.prefixlen < 31 else "N/A",
        first_usable=str(first),
        last_usable=str(last),
        max_hosts=max_hosts,
        ip_class=_ip_class(network.network_address),
        is_private=network.is_private,
        host_bits_were_set=host_bits_set,
    )


def allocate_ips(info: SubnetInfo, count: int) -> list[str]:
    """
    Hand out ``count`` sequential usable host addresses from the subnet,
    like a tiny DHCP pool starting at the first usable address.
    """
    if count > info.max_hosts:
        raise ValueError(
            f"Subnet {info.network_id}/{info.cidr} has only {info.max_hosts} "
            f"usable hosts but {count} nodes were requested."
        )
    start = ipaddress.IPv4Address(info.first_usable)
    return [str(start + i) for i in range(count)]
