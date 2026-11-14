"""Host metrics from a read-only bind of the host's /proc and /sys. Rates come from deltas between polls."""
import glob
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from heapq import nlargest

_WHOLE_DISK = re.compile(r"^(sd[a-z]+|nvme\d+n\d+)$")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
# libvirt: /machine/qemu-1-Name.libvirt-qemu (cgroupfs) or /machine.slice/machine-qemu\x2d1\x2dName.scope (systemd).
_VM_SCOPE = re.compile(r"qemu-\d+-(.+?)\.(?:scope|libvirt-qemu)")
_LXC = re.compile(r"/lxc\.payload\.([^/\s]+)")
_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})")
_TICK = os.sysconf("SC_CLK_TCK")
_PAGE = os.sysconf("SC_PAGE_SIZE")
TOP_N = 10
Instance = tuple[str, str]
# Friendlier names for host threads, marked with * as overrides. Keys ignore trailing digits (mdunraidd0).
PROC_NAMES = {"mdunraidd": "Parity/array I/O", "mdrecoveryd": "Parity/array I/O", "shfs": "User shares",
              "containerd-shim": "Docker shims"}


@dataclass
class Snapshot:
    uptime_s: int = 0
    cpu_pct: float = 0.0
    cpu_cores: list[float] = field(default_factory=list)
    iowait_pct: float = 0.0
    ram_used_mb: float = 0.0
    ram_total_mb: float = 0.0
    load: tuple[float, float, float] = (0.0, 0.0, 0.0)
    disk_read_mbs: float = 0.0
    disk_write_mbs: float = 0.0
    disk_util_pct: float = 0.0
    net_rx_mbps: float = 0.0
    net_tx_mbps: float = 0.0
    net_speed_mbps: float | None = None
    temp_c: float | None = None
    apps_cpu: dict[str, float] | None = None
    apps_ram: dict[str, float] | None = None
    app_instances: dict[str, frozenset[Instance]] = field(default_factory=dict)
    grouped_apps: frozenset[str] = frozenset()
    top_cpu: list[tuple[str, float]] = field(default_factory=list)
    top_ram: list[tuple[str, float]] = field(default_factory=list)
    # device (sdb, nvme0n1) -> (read MB/s, write MB/s, busy %)
    disk_io: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    procs: frozenset[str] = frozenset()  # command names of host processes outside containers and VMs

    @property
    def ram_pct(self) -> float:
        return 100 * self.ram_used_mb / self.ram_total_mb if self.ram_total_mb else 0.0


# Paths are the fixed host mount roots, not input. reviewed 2026-11
# bearer:disable python_lang_path_traversal
class Host:
    def __init__(self, root: str = "/host"):
        self.proc = os.path.join(root, "proc")
        self.sys = os.path.join(root, "sys")
        self._prev: dict | None = None
        self._owners: dict[tuple[str, str], str] = {}

    def _read(self, *parts: str) -> str:
        with open(os.path.join(*parts)) as f:
            return f.read()

    def _raw(self) -> dict:
        # The aggregate "cpu" line, then one "cpuN" line per core.
        cpu = [[int(x) for x in ln.split()[1:9]] for ln in self._read(self.proc, "stat").splitlines()
               if ln.startswith("cpu")]
        disks = {}
        for line in self._read(self.proc, "diskstats").splitlines():
            p = line.split()
            if _WHOLE_DISK.match(p[2]):
                disks[p[2]] = (int(p[5]), int(p[9]), int(p[12]))
        # /proc/net is per-namespace; pid 1 is the host's, the container's own would read its veth.
        iface = self._default_iface()
        net = (0, 0)
        for line in self._read(self.proc, "1", "net", "dev").splitlines()[2:]:
            name, _, rest = line.partition(":")
            if name.strip() == iface:
                f = rest.split()
                net = (int(f[0]), int(f[8]))
        return {"t": time.monotonic(), "cpu": cpu, "disks": disks, "net": net, "iface": iface, "procs": self._procs()}

    def _default_iface(self) -> str:
        for line in self._read(self.proc, "1", "net", "route").splitlines()[1:]:
            f = line.split()
            if f[1] == "00000000":
                return f[0]
        return "eth0"

    def _owner(self, pid: str, comm: str) -> str:
        """Unraid leaves host daemons in the root cgroup, so only containers and VMs are grouped by cgroup."""
        try:
            cg = self._read(self.proc, pid, "cgroup")
        except OSError:
            return comm
        if m := _CONTAINER_ID.search(cg):
            return "ct:" + m.group(0)
        cg = _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), cg)
        if m := _VM_SCOPE.search(cg):
            return "vm:" + m.group(1)
        if m := _LXC.search(cg):
            return "lxc:" + m.group(1)
        name = PROC_NAMES.get(comm.rstrip("0123456789"))
        return "*" + name if name else comm

    def _procs(self) -> dict[tuple[str, str], tuple[str, int, int]]:
        """(pid, starttime) -> (owner, cpu ticks, rss bytes). Owners are cached per process lifetime."""
        out = {}
        for pid in os.listdir(self.proc):
            if not pid.isdigit():
                continue
            try:
                head, _, rest = self._read(self.proc, pid, "stat").rpartition(")")
            except OSError:
                continue
            f = rest.split()
            key = (pid, f[19])
            owner = self._owners.get(key)
            if owner is None:
                owner = self._owners[key] = self._owner(pid, head.partition("(")[2].split("/")[0])
            out[key] = (owner, int(f[11]) + int(f[12]), int(f[21]) * _PAGE)
        self._owners = {k: v for k, v in self._owners.items() if k in out}
        return out

    def _temp(self) -> float | None:
        best = None
        for hw in glob.glob(os.path.join(self.sys, "class", "hwmon", "hwmon*")):
            try:
                if self._read(hw, "name").strip() not in ("coretemp", "k10temp", "zenpower"):
                    continue
                for t in glob.glob(os.path.join(hw, "temp*_input")):
                    c = int(self._read(t)) / 1000
                    best = c if best is None else max(best, c)
            except (OSError, ValueError):
                continue
        return best

    def poll(self, names: dict[str, str] | None = None) -> Snapshot:
        """names maps container id -> name; unknown ids show as a short id."""
        names = names or {}
        now, prev = self._raw(), self._prev
        self._prev = now
        mem = dict(line.split(":", 1) for line in self._read(self.proc, "meminfo").splitlines())
        total = int(mem["MemTotal"].split()[0]) / 1024
        avail = int(mem["MemAvailable"].split()[0]) / 1024
        load = self._read(self.proc, "loadavg").split()
        s = Snapshot(
            uptime_s=int(float(self._read(self.proc, "uptime").split()[0])),
            ram_used_mb=total - avail,
            ram_total_mb=total,
            load=(float(load[0]), float(load[1]), float(load[2])),
            temp_c=self._temp(),
            procs=frozenset(o for o, _, _ in now["procs"].values() if not o.startswith(("ct:", "vm:", "lxc:"))),
        )
        if prev is None:
            return s
        dt = now["t"] - prev["t"] or 1e-9
        s.cpu_pct, s.iowait_pct = _busy(now["cpu"][0], prev["cpu"][0])
        s.cpu_cores = [_busy(n, p)[0] for n, p in zip(now["cpu"][1:], prev["cpu"][1:])]
        for dev, (r, w, ticks) in now["disks"].items():
            if dev in prev["disks"]:
                pr, pw, pt = prev["disks"][dev]
                s.disk_io[dev] = ((r - pr) * 512 / dt / 2**20, (w - pw) * 512 / dt / 2**20,
                                  min(100.0, 100.0 * (ticks - pt) / (dt * 1000)))
        io = s.disk_io.values()
        s.disk_read_mbs, s.disk_write_mbs = sum(v[0] for v in io), sum(v[1] for v in io)
        s.disk_util_pct = max((v[2] for v in io), default=0.0)
        s.net_rx_mbps = (now["net"][0] - prev["net"][0]) * 8 / dt / 1e6
        s.net_tx_mbps = (now["net"][1] - prev["net"][1]) * 8 / dt / 1e6
        try:
            speed = int(self._read(self.sys, "class", "net", now["iface"], "speed"))
            s.net_speed_mbps = float(speed) if speed > 0 else None
        except (OSError, ValueError):
            pass
        cpu: Counter[str] = Counter()
        ram: Counter[str] = Counter()
        instances: dict[str, set[Instance]] = {}
        grouped = set()
        for key, (owner, ticks, rss) in now["procs"].items():
            label = _label(owner, names)
            ram[label] += rss
            cpu[label] += ticks - prev["procs"][key][1] if key in prev["procs"] else 0
            if owner.startswith(("ct:", "vm:", "lxc:", "*")):
                grouped.add(label)
            else:
                instances.setdefault(label, set()).add(key)
        scale = 100 / (dt * _TICK * (os.cpu_count() or 1))
        s.apps_cpu = {name: value * scale for name, value in cpu.items()}
        s.apps_ram = {name: value / 2**20 for name, value in ram.items()}
        s.top_cpu = nlargest(TOP_N, s.apps_cpu.items(), key=lambda item: item[1])
        s.top_ram = nlargest(TOP_N, s.apps_ram.items(), key=lambda item: item[1])
        s.grouped_apps = frozenset(grouped)
        s.app_instances = {name: frozenset(keys) for name, keys in instances.items() if name not in grouped}
        return s


def _busy(now: list[int], prev: list[int]) -> tuple[float, float]:
    """(busy %, iowait %) between two /proc/stat cpu lines."""
    dc = [a - b for a, b in zip(now, prev)]
    total = sum(dc) or 1
    return 100.0 * (total - dc[3] - dc[4]) / total, 100.0 * dc[4] / total


def _label(owner: str, names: dict[str, str]) -> str:
    kind, _, rest = owner.partition(":")
    if kind == "ct":
        return names.get(rest, rest[:12])
    return f"VM: {rest}" if kind == "vm" else f"LXC: {rest}" if kind == "lxc" else owner
