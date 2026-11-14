from shazzzbot import netdata


def test_parse_keeps_raised_alarms_critical_first():
    data = {"alarms": {
        "a": {"status": "WARNING", "chart": "disk_space./var/lib/netdata", "name": "disk_space_usage",
              "value_string": "96.4%"},
        "b": {"status": "CRITICAL", "chart": "system.ram", "name": "ram_in_use", "value_string": "97%"},
        "c": {"status": "CLEAR", "chart": "system.cpu", "name": "cpu", "value_string": "1%"},
    }}
    assert netdata.parse(data) == [(2, "ram ram_in_use 97%"), (1, "/var/lib/netdata disk_space_usage 96.4%")]


def test_alarms_fetches_the_api(serve):
    async def alarms(_):
        return {"json": {"alarms": {"a": {"status": "CRITICAL", "chart": "system.ram", "name": "ram"}}}}

    assert serve({"/api/v1/alarms": alarms}, lambda http, base: netdata.alarms(http, base)) == [(2, "ram ram")]


def test_parse_drops_ignored_alarms():
    data = {"alarms": {"a": {"status": "WARNING", "chart": "disk_space./var/lib/netdata", "name": "disk_space_usage",
                             "value_string": "96.4%"},
                       "b": {"status": "CRITICAL", "chart": "system.ram", "name": "ram_in_use", "value_string": "97%"}}}
    assert netdata.parse(data, ["/var/lib/netdata disk_space_usage"]) == [(2, "ram ram_in_use 97%")]
    assert netdata.parse(data, ["* disk_space_*"]) == [(2, "ram ram_in_use 97%")]
    assert len(netdata.parse(data, ["nothing"])) == 2
