from shazzzbot import unraid

VAR = 'NAME="tower"\nversion="7.3.4"\nmdState="STARTED"\nmdResync="0"\nmdResyncPos="50"\nmdResyncSize="200"\n' \
      'mdResyncAction="check P"\nsbSynced="100"\nsbSynced2="110"\nsbSyncExit="-4"\nsbSyncErrs="0"\n' \
      'shareMoverActive="no"\n'
DISKS = '["parity"]\nstatus="DISK_OK"\ntype="Parity"\ntemp="40"\nsize="100"\n' \
        '["disk1"]\nstatus="DISK_OK"\ntype="Data"\ndevice="sdb"\ntemp="*"\nrotational="0"\n' \
        'fsSize="1000"\nfsFree="20"\n' \
        'numErrors="0"\n' \
        '["parity2"]\nstatus="DISK_NP_DSBL"\ntype="Parity"\n'


def test_parse_and_build():
    p = unraid.parse_ini(DISKS)
    assert p["disk1"]["fsFree"] == "20"
    a = unraid.build(unraid.parse_ini(VAR)[""], p)
    assert a.name == "tower" and a.state == "STARTED"
    assert [d.name for d in a.disks] == ["parity", "disk1"]
    assert a.disks[1].temp is None and a.disks[1].free_kb == 20
    assert [d.rotational for d in a.disks] == [True, False]
    assert a.parity.running and a.parity.paused and a.parity.pct == 25.0


def test_build_disk_rotational_flag():
    disks = {f"disk{i}": {"status": "DISK_OK", "rotational": value}
             for i, value in enumerate(("0", "1", "unknown"))}
    disks["disk3"] = {"status": "DISK_OK"}
    a = unraid.build({}, disks)
    assert [d.rotational for d in a.disks] == [False, True, True, True]


def test_read_missing_dir(tmp_path):
    assert unraid.read(str(tmp_path)) is None


def test_read_ignores_junk_lines(tmp_path):
    (tmp_path / "var.ini").write_text("# comment\n" + VAR)
    (tmp_path / "disks.ini").write_text(DISKS)
    a = unraid.read(str(tmp_path))
    assert a is not None and a.version == "7.3.4" and a.usage == (980, 1000)
