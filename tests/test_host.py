from shazzzbot.host import _PAGE, _TICK, Host

CID = "a" * 64
PROCS = {
    "1": ("init", "0::/"),
    "100": ("plex", f"0::/docker/{CID}"),
    "101": ("plex worker", f"0::/docker/{CID}"),
    "200": ("qemu-system-x86", "0::/machine.slice/machine-qemu\\x2d1\\x2dWin\\x2d11.scope/libvirt/vcpu0"),
    "300": ("kworker/0:1", "0::/"),
    "350": ("mdunraidd0", "0::/"),
    "360": ("init", "0::/lxc.payload.pihole/init.scope"),
    "400": ("qemu-system-x86", "0::/machine/qemu-2-Harbor.libvirt-qemu/emulator"),
}


def _stat(pid, comm, ticks, pages):
    f = ["S"] + ["0"] * 21
    f[11], f[19], f[21] = str(ticks), "7", str(pages)
    return f"{pid} ({comm}) " + " ".join(f)


def _tree(root, cpu, rx, ticks):
    (root / "proc/1/net").mkdir(parents=True, exist_ok=True)
    line = " ".join(map(str, cpu)) + " 0 0\n"
    (root / "proc/stat").write_text(f"cpu {line}cpu0 {line}cpu1 {line}intr 1\n")
    (root / "proc/diskstats").write_text("8 0 sda 0 0 100 0 0 0 200 0 0 500 0\n8 1 sda1 0 0 9 0 0 0 9 0 0 9 0\n")
    (root / "proc/1/net/route").write_text("Iface Destination\nbr0 00000000 x\n")
    (root / "proc/1/net/dev").write_text(f"h1\nh2\n  br0: {rx} 0 0 0 0 0 0 0 {rx} 0\n  lo: 9 0 0 0 0 0 0 0 9 0\n")
    (root / "proc/meminfo").write_text("MemTotal: 2048 kB\nMemAvailable: 1024 kB\n")
    (root / "proc/loadavg").write_text("1.0 2.0 3.0 1/1 1\n")
    (root / "proc/uptime").write_text("90061.5 0\n")
    for i, (pid, (comm, cg)) in enumerate(PROCS.items()):
        d = root / "proc" / pid
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(_stat(pid, comm, ticks * i * i, 2**20 // _PAGE * i * i))
        (d / "cgroup").write_text(cg + "\n")


def test_two_polls(tmp_path):
    _tree(tmp_path, [0] * 8, 0, 0)
    h = Host(str(tmp_path))
    first = h.poll()
    assert first.uptime_s == 90061 and first.ram_used_mb == 1.0 and first.cpu_pct == 0.0
    _tree(tmp_path, [10, 0, 10, 60, 20, 0, 0, 0], 1000, _TICK)
    s = h.poll({CID: "plex"})
    assert round(s.cpu_pct) == 20 and round(s.iowait_pct) == 20
    assert [round(c) for c in s.cpu_cores] == [20, 20]
    assert s.load == (1.0, 2.0, 3.0) and s.net_rx_mbps > 0
    assert [n for n, _ in s.top_ram] == [
        "VM: Harbor", "LXC: pihole", "*Parity/array I/O", "kworker", "VM: Win-11", "plex", "init"]
    assert dict(s.top_ram)["plex"] == 5.0
    assert [n for n, _ in s.top_cpu][:4] == ["VM: Harbor", "LXC: pihole", "*Parity/array I/O", "kworker"]


def test_edges_new_devices_vanished_procs_and_sensors(tmp_path):
    _tree(tmp_path, [0] * 8, 0, 0)
    (tmp_path / "proc/1/net/route").write_text("Iface Destination\nbr0 0000A8C0 x\n")
    (tmp_path / "proc/999").mkdir()
    (tmp_path / "proc/100/cgroup").unlink()
    for hw, name, temps in (("hwmon0", "coretemp", {"1": "41000", "2": "55000"}), ("hwmon1", "nvme", {"1": "70000"}),
                             ("hwmon2", "k10temp", {"1": "bad"})):
        d = tmp_path / "sys/class/hwmon" / hw
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        for i, v in temps.items():
            (d / f"temp{i}_input").write_text(v)
    h = Host(str(tmp_path))
    assert h.poll().temp_c == 55.0
    (tmp_path / "proc/diskstats").write_text("8 0 sda 0 0 100 0 0 0 200 0 0 500 0\n8 16 sdb 0 0 1 0 0 0 1 0 0 1 0\n")
    (tmp_path / "proc/500").mkdir()
    (tmp_path / "proc/500/stat").write_text(_stat("500", "newcomer", 5, 1))
    (tmp_path / "sys/class/net/eth0").mkdir(parents=True)
    (tmp_path / "sys/class/net/eth0/speed").write_text("2500\n")
    s = h.poll()
    assert s.net_speed_mbps == 2500.0 and list(s.disk_io) == ["sda"]
    assert "newcomer" in dict(s.top_ram) and "plex" in dict(s.top_ram)
