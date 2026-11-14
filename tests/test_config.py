import logging

import pytest

from shazzzbot import config, schema
from shazzzbot.services import Service

BASE = 'config_format = 1\nguild_id = 1\noutputs = [{channel = 2}]\n'


def test_config_defaults_come_from_the_schema_and_containers_are_independent():
    first = config.Config("secret", 1, [config.Output(2)])
    second = config.Config("secret", 1, [config.Output(2)])
    for name in ("interval", "edit_every", "image_every", "timezone", "order"):
        assert getattr(first, name) == schema.default("config", name)
    for name, (table, key) in config.PATHS.items():
        if name != "services":
            assert getattr(first, name) == schema.default(table, key)
    first.order.clear()
    first.service_columns.append("extra")
    first.restart_groups["changed"] = ["*"]
    assert second.order == schema.default("config", "order")
    assert second.service_columns == schema.default("services", "columns")
    assert second.restart_groups == schema.default("restarts", "groups")
    assert config.Preview().port == schema.default("preview", "port")
    assert config.Output(2).render == schema.default("outputs", "render")


def test_parser_reports_schema_key_suggestions(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="titel.*did you mean.*title"):
        load(tmp_path, monkeypatch, BASE + '[system]\ntitel = "server"\n')
    with pytest.raises(SystemExit, match="tasks_shown.*did you mean.*tasks"):
        load(tmp_path, monkeypatch, BASE + 'tasks_shown = 3\n')
    with pytest.raises(SystemExit, match="chanel.*did you mean.*channel"):
        load(tmp_path, monkeypatch, 'config_format = 1\nguild_id = 1\n[[outputs]]\nchanel = 2\n')


def test_valid_parser_model_round_trips_through_schema(tmp_path, monkeypatch):
    cfg = load(tmp_path, monkeypatch, 'config_format = 1\nguild_id = 1\n[[outputs]]\nchannel = 2\n'
               '[outputs.render]\nsystem = "text"\n')
    model = config.model(cfg)
    schema.validate("config", model, "config.toml")
    assert model["outputs"][0]["channel"] == 2 and model["outputs"][0]["render"] == {"system": "text"}
    assert not {"token", "relays", "jellyfin_key"} & set(model)


@pytest.mark.parametrize("value", [0, -1, True, "2", 2.0])
def test_output_channels_reject_non_ids(value):
    with pytest.raises(SystemExit, match="channel"):
        config._check(config.Config("t", 1, [config.Output(value)]), "config.toml")


def test_node_command_domain_checks_preserve_allowed_targets_and_safe_text():
    cfg = config.Config("t", 1, [config.Output(2)])
    cfg.node_updates = {"bot": {"hostname": "server.example", "command": "./ship.sh target"}}
    config._check(cfg, "config.toml")
    cfg.node_updates["unknown"] = cfg.node_updates["bot"]
    with pytest.raises(SystemExit, match="configured RELAY_URL"):
        config._check(cfg, "config.toml")
    del cfg.node_updates["unknown"]
    for command in ("echo ```", "echo\ntext", "echo " + chr(127)):
        cfg.node_updates["bot"]["command"] = command
        with pytest.raises(SystemExit, match="one ASCII line without code fences"):
            config._check(cfg, "config.toml")


def test_task_rename_type_errors_are_reported_by_the_schema(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match=r"rename.*expected a table"):
        load(tmp_path, monkeypatch, BASE + "[tasks]\nrename = 1\n")


def forge_block(extra="", kind="forgejo"):
    return ('[[forges]]\n' + f'type = "{kind}"\nurl = "https://forge.example"\n'
            'token_env = "TEST_FORGE_TOKEN"\nrepos = ["owner/repo"]\n' + extra)


def test_forges_load_tokens_by_environment_reference_and_default_features(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    cfg = load(tmp_path, monkeypatch, BASE + forge_block())
    assert cfg.forges[0].show == ("ci", "runs", "runners", "pushes", "issues")
    assert cfg.forges[0].name == "Forgejo" and cfg.forges[0].latest == 5
    assert cfg.forges[0].pinned == () and cfg.forges[0].filter == {}
    assert "fixture-forge-token" not in repr(cfg.forges)
    text = BASE + ('[[forges]]\ntype = "github"\ntoken_env = "TEST_FORGE_TOKEN"\nrepos = ["owner/repo"]\n')
    cfg = load(tmp_path, monkeypatch, text)
    assert cfg.forges[0].url == "https://api.github.com" and cfg.forges[0].latest == 5
    assert cfg.forges[0].pinned == () and cfg.forges[0].filter == {}
    assert cfg.forges[0].show == ("ci", "runs", "pushes", "issues")
    assert load(tmp_path, monkeypatch, BASE).forges == []
    cfg = load(tmp_path, monkeypatch, BASE + forge_block('show = ["issues", "ci"]\n'))
    assert cfg.forges[0].show == ("ci", "issues") and "forges" in config.SECTIONS
    cfg = load(tmp_path, monkeypatch, BASE + forge_block('name = "Home Forge"\n'))
    assert cfg.forges[0].name == "Home Forge"


@pytest.mark.parametrize("field,value", [("type", '"other"'), ("type", "1"), ("url", "1"),
                                         ("url", '"ftp://forge.example"'), ("url", '"https://u:' + 'pass@localforge"'),
                                         ("url", '"https://forge.example?token=x"'), ("url", '"https://forge.example#x"'),
                                         ("url", '"https://forge.example:0"'), ("url", '"https://forge.example:nope"'),
                                         ("url", '"https://forge.example:"'), ("url", '"https://forge..example"'),
                                         ("url", '"https://forge.example/../api"'), ("url", '"https://forge.example/%61"'),
                                         ("url", '"https://forge.example/space here"'), ("url", '"https://"'),
                                         ("token_env", "1"), ("token_env", '"NOT A NAME"'),
                                         ("name", "1"), ("name", '\"\"'), ("name", '\"   \"'),
                                         ("name", '\"' + "n" * 65 + '\"'), ("name", '\"bad\\nname\"'),
                                         ("repos", '"owner/repo"'), ("repos", "[]"), ("repos", "[1]"),
                                         ("repos", "[" + ",".join(f'"o/{i}"' for i in range(11)) + "]"),
                                         ("repos", '["../repo"]'), ("repos", '["owner/.."]'),
                                         ("repos", '["owner/repo", "OWNER/REPO"]'),
                                         ("show", '"ci"'), ("show", "[]"), ("show", "[1]"),
                                         ("show", '["ci", "ci"]'), ("show", '["unknown"]')])
def test_forge_settings_fail_closed(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    lines = forge_block().splitlines()
    lines = [line for line in lines if not line.startswith(field + " =")]
    text = BASE + "\n".join(lines) + f"\n{field} = {value}\n"
    with pytest.raises(SystemExit, match="forges entry"):
        load(tmp_path, monkeypatch, text)


@pytest.mark.parametrize("kind", ["forgejo", "github"])
def test_forges_accept_ten_repositories_at_the_maximum_owner_and_name_lengths(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    repos = ["o" * 100 + "/" + "r" * 99 + str(index) for index in range(10)]
    values = ", ".join(f'"{repo}"' for repo in repos)
    block = forge_block(kind=kind).replace('repos = ["owner/repo"]', f"repos = [{values}]")
    cfg = load(tmp_path, monkeypatch, BASE + block)
    assert cfg.forges[0].repos == tuple(repos)
    assert len(cfg.forges[0].repos) == 10 and all(len(repo) == 201 for repo in cfg.forges[0].repos)


@pytest.mark.parametrize("latest", range(1, 6))
def test_forges_accept_each_latest_display_limit(tmp_path, monkeypatch, latest):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    cfg = load(tmp_path, monkeypatch, BASE + forge_block(f"latest = {latest}\n"))
    assert cfg.forges[0].latest == latest


@pytest.mark.parametrize("value", ["0", "6", "true", "false", "1.0", '\"5\"'])
def test_forges_reject_invalid_latest_display_limits(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    with pytest.raises(SystemExit, match="latest"):
        load(tmp_path, monkeypatch, BASE + forge_block(f"latest = {value}\n"))


@pytest.mark.parametrize("kind", ["forgejo", "github"])
def test_forge_pins_and_filters_use_tracked_spelling_without_reordering_pins(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    extra = ('latest = 2\npinned = ["OWNER/second", "OWNER/FIRST"]\n'
             'filter = {ci = ["OWNER/THIRD", "OWNER/second"], runs = ["OWNER/THIRD"], '
             'pushes = ["OWNER/FIRST"], issues = ["OWNER/second"]}\n')
    block = forge_block(extra, kind).replace('repos = ["owner/repo"]',
        'repos = ["Owner/first", "owner/SECOND", "owner/third"]')
    settings = load(tmp_path, monkeypatch, BASE + block).forges[0]
    assert settings.repos == ("Owner/first", "owner/SECOND", "owner/third")
    assert settings.pinned == ("owner/SECOND", "Owner/first")
    assert all(isinstance(repos, tuple) for repos in settings.filter.values())
    assert {kind: set(repos) for kind, repos in settings.filter.items()} == {
        "ci": {"owner/third", "owner/SECOND"}, "runs": {"owner/third"},
        "pushes": {"Owner/first"}, "issues": {"owner/SECOND"}}


def test_forge_empty_preferences_allow_all_and_filters_can_exclude_a_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    empty = load(tmp_path, monkeypatch, BASE + forge_block('pinned = []\nfilter = {}\n')).forges[0]
    assert empty.pinned == () and empty.filter == {}
    block = forge_block('show = ["ci"]\nlatest = 1\npinned = ["owner/repo"]\n'
                        'filter = {ci = ["OWNER/OTHER"]}\n').replace(
                            'repos = ["owner/repo"]', 'repos = ["owner/repo", "owner/other"]')
    settings = load(tmp_path, monkeypatch, BASE + block).forges[0]
    assert settings.pinned == ("owner/repo",) and settings.filter == {"ci": ("owner/other",)}
    assert empty.filter == {}


@pytest.mark.parametrize("value", ['"owner/repo"', "1", "true", "{}", "[1]", "[true]", '[" "]',
                                  '["other/repo"]', '[" owner/repo"]', '["owner/repo "]',
                                  '["owner/repo", "OWNER/REPO"]', '[["owner/repo"]]'])
def test_forge_pins_reject_bad_types_unknown_repos_and_case_duplicates(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    with pytest.raises(SystemExit, match="pinned"):
        load(tmp_path, monkeypatch, BASE + forge_block(f"pinned = {value}\n"))


@pytest.mark.parametrize("filter_value", ["{}", '{ci = ["owner/other"]}'])
def test_forge_pins_cannot_exceed_latest_even_when_a_filter_excludes_one(tmp_path, monkeypatch, filter_value):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    block = forge_block('latest = 1\npinned = ["owner/repo", "owner/other"]\n'
                        f'filter = {filter_value}\n').replace(
                            'repos = ["owner/repo"]', 'repos = ["owner/repo", "owner/other"]')
    with pytest.raises(SystemExit, match="pinned"):
        load(tmp_path, monkeypatch, BASE + block)


@pytest.mark.parametrize("value", ["[]", "1", "true", '"ci"', '["ci"]', '{unknown = ["owner/repo"]}',
                                  '{CI = ["owner/repo"]}', '{runners = ["owner/repo"]}',
                                  '{ci = "owner/repo"}', "{ci = 1}", "{ci = true}", "{ci = {}}", "{ci = []}",
                                  "{ci = [1]}", "{ci = [false]}", '{ci = [["owner/repo"]]}',
                                  '{ci = ["owner/repo", "OWNER/REPO"]}', '{ci = ["other/repo"]}',
                                  '{ci = [" owner/repo"]}', '{ci = ["owner/repo "]}'])
def test_forge_filters_reject_bad_types_sections_and_repo_lists(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    with pytest.raises(SystemExit, match="filter"):
        load(tmp_path, monkeypatch, BASE + forge_block(f"filter = {value}\n"))


@pytest.mark.parametrize("section", ["runs", "pushes", "issues"])
def test_forge_filters_cannot_enable_a_section_absent_from_show(tmp_path, monkeypatch, section):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    block = forge_block(f'show = ["ci"]\nfilter = {{{section} = ["owner/repo"]}}\n')
    with pytest.raises(SystemExit, match="filter"):
        load(tmp_path, monkeypatch, BASE + block)


@pytest.mark.parametrize("owner_length,name_length", [(101, 100), (100, 101)])
def test_forges_reject_repository_components_above_the_length_limit(tmp_path, monkeypatch, owner_length, name_length):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    repo = "o" * owner_length + "/" + "r" * name_length
    block = forge_block().replace('repos = ["owner/repo"]', f'repos = ["{repo}"]')
    with pytest.raises(SystemExit, match=r"repos.*expected string.*owner/name"):
        load(tmp_path, monkeypatch, BASE + block)


@pytest.mark.parametrize("text", ['forges = "bad"\n', 'forges = [1]\n', forge_block() * 4,
                                  forge_block() * 2, forge_block('token = "must-not-go-here"\n'),
                                  forge_block(kind="github") + 'show = ["runners"]\n'])
def test_forge_blocks_and_permissions_are_strict(tmp_path, monkeypatch, text):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    with pytest.raises(SystemExit, match="forge"):
        load(tmp_path, monkeypatch, BASE + text)


def test_forge_token_must_exist_and_forgejo_url_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_FORGE_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="token_env TEST_FORGE_TOKEN"):
        load(tmp_path, monkeypatch, BASE + forge_block())
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    without_url = "\n".join(line for line in forge_block().splitlines() if not line.startswith("url ="))
    with pytest.raises(SystemExit, match="url must"):
        load(tmp_path, monkeypatch, BASE + without_url)


def test_forge_url_prefixes_ipv4_and_ipv6_are_validated():
    from shazzzbot import forges

    assert forges._url("http://127.0.0.1:3100/git/") == "http://127.0.0.1:3100/git"
    assert forges._url("https://[::1]/") == "https://[::1]"
    for value in ("x" * 2049, "https://" + "a" * 254, "https://999.1.1.1", "https://-bad", chr(0xE9)):
        with pytest.raises(ValueError):
            forges._url(value)


def test_ups_is_optional_and_accepts_a_nut_target(tmp_path, monkeypatch):
    assert load(tmp_path, monkeypatch, BASE).ups == ""
    assert load(tmp_path, monkeypatch, BASE + '[ups]\ntarget = ""\n').ups == ""
    assert load(tmp_path, monkeypatch, BASE + '[ups]\ntarget = "unit@nut"\n').ups == "unit@nut"


@pytest.mark.parametrize("value", ['true', '1', '[]', '"unit"', '"unit@host:0"', '"unit@host:65536"',
                                   '"unit:password@host"', '"unit@host/path"', '"unit@host\\nLOGIN"'])
def test_ups_configuration_fails_closed(tmp_path, monkeypatch, value):
    with pytest.raises(SystemExit, match=r"ups.*(expected string|must be name@host)"):
        load(tmp_path, monkeypatch, BASE + f"[ups]\ntarget = {value}\n")


@pytest.mark.parametrize("extra", ['[restarts]\ngroups = "bad"\n', '[restarts.groups]\n"" = ["web*"]\n',
                                  '[restarts.groups]\n" " = ["web*"]\n', '[restarts.groups]\napps = []\n',
                                  '[restarts.groups]\napps = "web*"\n', '[restarts.groups]\napps = [1]\n',
                                  '[restarts.groups]\napps = [""]\n', '[restarts.groups]\napps = [" "]\n'])
def test_restart_groups_fail_closed(tmp_path, monkeypatch, extra):
    with pytest.raises(SystemExit, match="groups"):
        load(tmp_path, monkeypatch, BASE + extra)


def test_restart_groups_default_to_empty_and_load_container_globs(tmp_path, monkeypatch):
    assert load(tmp_path, monkeypatch, BASE).restart_groups == {}
    cfg = load(tmp_path, monkeypatch, BASE + '[restarts.groups]\nmedia = ["jellyfin*", "reader"]\nweb = ["web-*"]\n')
    assert cfg.restart_groups == {"media": ["jellyfin*", "reader"], "web": ["web-*"]}
    cfg.restart_groups = {1: ["web*"]}
    with pytest.raises(SystemExit, match="restarts.groups"):
        config._check(cfg, "config.toml")


def load(tmp_path, monkeypatch, text, token="t"):
    p = tmp_path / "config.toml"
    p.write_text(text)
    if token:
        monkeypatch.setenv("DISCORD_TOKEN", token)
    else:
        monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    return config.load(str(p))


def test_load_merges_thresholds_and_services(tmp_path, monkeypatch):
    c = load(tmp_path, monkeypatch,
             BASE + '[thresholds]\ntemp = [60, 75]\n[[services.checks]]\nname = "A"\ncontainer = "a"\n')
    assert c.token == "t" and c.guild_id == 1 and c.thresholds["temp"] == (60.0, 75.0)
    assert c.thresholds["resource"] == config.DEFAULT_THRESHOLDS["resource"] and c.services == [Service("A", "a")]


def test_ignored_services_are_dropped(tmp_path, monkeypatch):
    svc = '[[services.checks]]\nname = "{}"\ncontainer = "c"\n'
    c = load(tmp_path, monkeypatch,
             BASE + '[services]\nignore = ["Archive*"]\n' + svc.format("ArchiveBox") + svc.format("Jellyfin"))
    assert [s.name for s in c.services] == ["Jellyfin"]


def test_token_is_required(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="DISCORD_TOKEN"):
        load(tmp_path, monkeypatch, BASE, token="")


def test_keys_trapped_under_a_table_are_named(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match=r"\['shown'\] sit under \[tasks.rename\]"):
        load(tmp_path, monkeypatch, BASE + '[tasks.rename]\n"x" = "X"\nshown = 3\n')
    c = load(tmp_path, monkeypatch, BASE + '[tasks.rename]\nignore = "Cleanup"\nshown = "Shown"\n')
    assert c.tasks_rename == {"ignore": "Cleanup", "shown": "Shown"}


def test_unknown_and_missing_keys_are_named(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="unknown setting.*nope"):
        load(tmp_path, monkeypatch, BASE + 'nope = 1\n')
    with pytest.raises(SystemExit, match="unknown setting.*token"):
        load(tmp_path, monkeypatch, BASE + 'token = "in the file"\n')
    with pytest.raises(SystemExit, match="missing required.*outputs"):
        load(tmp_path, monkeypatch, 'config_format = 1\nguild_id = 1\n')
    with pytest.raises(SystemExit, match="missing required.*guild_id"):
        load(tmp_path, monkeypatch, 'config_format = 1\noutputs = [{channel = 2}]\n')


@pytest.mark.parametrize("extra, msg", [
    ("audit_channel_id = -4\n", "audit_channel_id"),
    ("interval = 2\n", r"interval: value must"),
    ("image_every = 59\n", r"image_every: value must"),
    ("[history]\ndays = 8\n", r"history\]\.days: (value must|expected integer)"),
    ("commands_channel_id = -1\n", "commands_channel_id"),
    ("edit_every = 4\n", r"edit_every: value must"),
    ("[history]\ndays = true\n", r"days: (value must|expected integer)"),
    ('[restarts]\nallow = "jellyfin"\n', r"restarts\]\.allow"),
    ("[services]\nignore = [1]\n", r"services\]\.ignore"),
    ('[tasks]\nignore = "x"\n', r"tasks\]\.ignore"),
    ('timezone = "Mars/Base"\n', "timezone must be"),
    ("timezone = 5\n", r"timezone: expected string"),
    ("[system]\ntitle = 5\n", r"title: expected string"),
    ('[services]\ncolumns = ["Local", "Public"]\n', r"columns.*expected a list of exactly 3 items"),
    ('[services]\ncolumns = ["Local", "", "Public"]\n', r"columns.*expected (list|string)"),
    ('[jellyfin]\nurl = "http://jf"\n', "jellyfin.url needs JELLYFIN_KEY"),
    ('jellyfin_key = "k"\n', "unknown setting"),
    ('unraid_api_url = "http://x"\n', "unknown setting"),
    ("[perms.owner]\nusers = [1]\n", r"perms: unknown setting.*owner"),
    ('[perms.view]\nusers = ["me"]\n', "perms.view"),
    ("[perms.view]\ngroups = [1]\n", "perms.view"),
    ("[thresholds]\ncpu = [1, 2]\n", r"unknown setting.*cpu"),
    ("[thresholds]\ntemp = [90, 80]\n", r"temp: expected threshold"),
    ("[thresholds]\ntemp = [90]\n", r"temp: expected threshold"),
    ("[tasks]\nshown = 0\n", r"shown: value must"),
    ('order = ["disks", "disks"]\n', r"order: items must be unique"),
])
def test_bad_values_are_explained(tmp_path, monkeypatch, extra, msg):
    with pytest.raises(SystemExit, match=msg):
        load(tmp_path, monkeypatch, BASE + extra)


def test_missing_or_broken_file_is_explained(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    (tmp_path / "dir.toml").mkdir()
    for name in ("absent.toml", "dir.toml"):
        with pytest.raises(SystemExit, match="is missing"):
            config.load(str(tmp_path / name))
    with pytest.raises(SystemExit, match="line 2"):
        load(tmp_path, monkeypatch, 'config_format = 1\nguild_id = = 1\n')


def test_zero_ids_are_rejected_and_audit_zero_means_none(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match=r"guild_id: value must"):
        load(tmp_path, monkeypatch, 'config_format = 1\nguild_id = 0\noutputs = [{channel = 2}]\n')
    assert load(tmp_path, monkeypatch, BASE + "audit_channel_id = 0\n").audit_channel_id == 0


def test_the_example_config_loads_once_ids_are_filled(tmp_path, monkeypatch):
    with open("config.example.toml") as f:
        text = f.read()
    with pytest.raises(SystemExit, match="channel"):
        load(tmp_path, monkeypatch, text)
    with pytest.raises(SystemExit, match="guild_id"):
        load(tmp_path, monkeypatch, text.replace("channel = 0", "channel = 2"))
    c = load(tmp_path, monkeypatch,
             text.replace("guild_id = 0", "guild_id = 1").replace("channel = 0", "channel = 2"))
    assert c.restart_allow == [] and c.services == []


FP = "AB:" * 31 + "CD"


@pytest.mark.parametrize(("env", "msg"), [
    ({"RELAY_URL": "https://r:8443"}, "go together"),
    ({"RELAY_URL": "http://r:8443", "RELAY_KEY": "k", "RELAY_FINGERPRINT": FP}, "https://"),
    ({"RELAY_URL": "https://r:8443", "RELAY_KEY": "k", "RELAY_FINGERPRINT": "abc"}, "64 hex"),
])
def test_relay_env_is_all_or_nothing_and_checked(tmp_path, monkeypatch, env, msg):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match=msg):
        load(tmp_path, monkeypatch, BASE)


def test_relay_env_is_read_and_the_fingerprint_normalized(tmp_path, monkeypatch):
    assert load(tmp_path, monkeypatch, BASE).relays == []
    for sfx in ("_10", "", "_2"):
        for k, v in {"RELAY_URL": f"https://r{sfx}:8443", "RELAY_KEY": "k", "RELAY_FINGERPRINT": FP}.items():
            monkeypatch.setenv(k + sfx, v)
    monkeypatch.setenv("RELAY_URLX", "ignored")
    monkeypatch.setenv("RELAY_KEY_3", " ")
    got = load(tmp_path, monkeypatch, BASE).relays
    assert [r.url for r in got] == ["https://r:8443", "https://r_2:8443", "https://r_10:8443"]
    assert got[0] == ("https://r:8443", "k", "ab" * 31 + "cd")
    monkeypatch.setenv("RELAY_KEY_4", "k")
    with pytest.raises(SystemExit, match="RELAY_URL_4, RELAY_KEY_4 and RELAY_FINGERPRINT_4 go together"):
        load(tmp_path, monkeypatch, BASE)
    with pytest.raises(SystemExit, match="unknown"):
        load(tmp_path, monkeypatch, BASE + 'relays = "x"\n')


def test_public_host_needs_a_relay_and_a_string(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="needs a relay"):
        load(tmp_path, monkeypatch, BASE + '[services]\npublic_host = "x.example.com"\n')
    with pytest.raises(SystemExit, match=r"public_host: expected string"):
        load(tmp_path, monkeypatch, BASE + '[services]\npublic_host = 5\n')


def test_jellyfin_key_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("JELLYFIN_KEY", " k ")
    assert load(tmp_path, monkeypatch, BASE + '[jellyfin]\nurl = "http://jf"\n').jellyfin_key == "k"


def test_blowup_user_id_defaults_to_disabled_and_accepts_ids(tmp_path, monkeypatch):
    assert load(tmp_path, monkeypatch, BASE).blowup_user_id == 0
    assert load(tmp_path, monkeypatch, BASE + "blowup_user_id = 0\n").blowup_user_id == 0
    assert load(tmp_path, monkeypatch, BASE + "blowup_user_id = 42\n").blowup_user_id == 42


@pytest.mark.parametrize("value", ["-1", "true", "false", "0.0", "1.5", '\"42\"'])
def test_blowup_user_id_rejects_invalid_ids(tmp_path, monkeypatch, value):
    with pytest.raises(SystemExit, match=r"blowup_user_id: (expected integer|value must)"):
        load(tmp_path, monkeypatch, BASE + f"blowup_user_id = {value}\n")


def test_services_section_defaults_order_and_external_tcp_entries(tmp_path, monkeypatch):
    text = ('config_format = 1\nguild_id = 1\n[[outputs]]\nchannel = 2\nrender = {services = "text"}\n'
            '[[services.checks]]\nname = "SSH"\ntcp = "host:22"\n'
            'vpn_tcp = "vpn:22"\npublic_tcp = "public:22"\nevery = 120\n')
    cfg = load(tmp_path, monkeypatch, text)
    assert "external" not in cfg.order and "external" not in config.SECTIONS
    assert cfg.outputs[0].render["services"] == "text"
    assert cfg.services == [Service("SSH", tcp="host:22", vpn_tcp="vpn:22", public_tcp="public:22", every=120)]
    cfg = load(tmp_path, monkeypatch, BASE + 'order = ["services", "system"]\n')
    assert cfg.order == ["services", "system"] and cfg.outputs[0].render == {}


@pytest.mark.parametrize("text", [
    BASE + 'order = ["external", "services"]\n',
    'config_format = 1\nguild_id = 1\n[[outputs]]\nchannel = 2\nsections = ["external"]\n',
    'config_format = 1\nguild_id = 1\n[[outputs]]\nchannel = 2\nrender = {external = "image"}\n'])
def test_external_section_config_explains_the_merge(tmp_path, monkeypatch, text):
    with pytest.raises(SystemExit, match="'external' is merged into 'services'"):
        load(tmp_path, monkeypatch, text)


@pytest.mark.parametrize("every", ["0", "59", "true", "60.0"])
def test_external_service_intervals_must_be_whole_seconds_at_least_sixty(tmp_path, monkeypatch, every):
    with pytest.raises(SystemExit, match=r"every.*(expected integer|value must|whole seconds)"):
        load(tmp_path, monkeypatch, BASE + f'[[services.checks]]\nname = "SSH"\ntcp = "host:22"\nevery = {every}\n')


def test_outputs_inherit_global_defaults_without_legacy_keys(tmp_path, monkeypatch, caplog):
    text = ('config_format = 1\nguild_id = 1\nimage_every = 120\nedit_every = 30\n'
            '[[outputs]]\nchannel = 2\n')
    with caplog.at_level(logging.WARNING, "shazzzbot"):
        cfg = load(tmp_path, monkeypatch, text)
    assert cfg.outputs == [config.Output(2, image_every=120, edit_every=30)]
    assert not hasattr(cfg, "panel_channel_id") and not caplog.records


def test_outputs_have_independent_formats_sections_and_cadences(tmp_path, monkeypatch):
    text = ('config_format = 1\nguild_id = 1\norder = ["history", "system"]\n'
            '[[outputs]]\nchannel = 2\nsections = ["system", "history"]\n'
            'render = {system = "embed", history = "text"}\n'
            '[[outputs]]\nchannel = 3\nimage_every = 1800\nedit_every = 90\n'
            '[outputs.render]\ndisks = "image"\n')
    cfg = load(tmp_path, monkeypatch, text)
    assert cfg.outputs == [config.Output(2, {"system": "embed", "history": "text"}, ["history", "system"], 60, 60),
                           config.Output(3, {"disks": "image"}, image_every=1800, edit_every=90)]


def test_top_level_render_is_inherited_and_output_overrides_win(tmp_path, monkeypatch):
    text = ('config_format = 1\nguild_id = 1\n[render]\nsystem = "text"\nservices = "embed"\n'
            '[[outputs]]\nchannel = 2\n[outputs.render]\nsystem = "image"\n[[outputs]]\nchannel = 3\n')
    cfg = load(tmp_path, monkeypatch, text)
    assert cfg.render == {"system": "text", "services": "embed"}
    assert [o.render for o in cfg.outputs] == [{"system": "image"}, {}]
    with pytest.raises(SystemExit, match="render"):
        load(tmp_path, monkeypatch,
             'config_format = 1\nguild_id = 1\n[render]\nsystem = "png"\n[[outputs]]\nchannel = 2\n')


@pytest.mark.parametrize("obsolete,key", [
    ('panel_channel_id = 2\n', "panel_channel_id"),
    ('[mirror_channels]\ntext = 3\n', "mirror_channels"),
    ('[mirror_image_every]\ntext = 120\n', "mirror_image_every"),
    ('tasks_file = "/obsolete/tasks.json"\n', "tasks_file"),
    ('tasks_file = 1\n', "tasks_file"),
])
def test_obsolete_settings_are_unknown(tmp_path, monkeypatch, obsolete, key):
    with pytest.raises(SystemExit, match="unknown setting.*" + key):
        load(tmp_path, monkeypatch, BASE + obsolete)


@pytest.mark.parametrize("outputs,msg", [('outputs = []\n', "at least one channel"),
                                        ('outputs = 1\n', "at least one channel"),
                                        ('outputs = [1]\n', "must be a channel block")])
def test_outputs_must_be_nonempty_channel_blocks(tmp_path, monkeypatch, outputs, msg):
    with pytest.raises(SystemExit, match=msg):
        load(tmp_path, monkeypatch, 'config_format = 1\nguild_id = 1\n' + outputs)


@pytest.mark.parametrize("entry,msg", [
    ('', "missing required setting channel"),
    ('channel = 0\n', "channel: value must"),
    ('channel = -1\n', "channel: value must"),
    ('channel = true\n', "channel: expected integer"),
    ('channel = "2"\n', "channel: expected integer"),
    ('channel = 2.0\n', "channel: expected integer"),
    ('channel = 2\nunknown = 1\n', "unknown setting"),
    ('channel = 2\nrender = []\n', r"render: expected a table"),
    ('channel = 2\nrender = {weather = "text"}\n', r"(render: unknown setting|sections.*choose from)"),
    ('channel = 2\nrender = {system = "pdf"}\n', r"render.system: (choose from|expected string)"),
    ('channel = 2\nrender = {system = 1}\n', r"render.system: (choose from|expected string)"),
    ('channel = 2\nsections = []\n', r"sections.*expected a list of at least 1 item"),
    ('channel = 2\nsections = "system"\n', r"sections.*expected (list|string)"),
    ('channel = 2\nsections = ["weather"]\n', r"(render: unknown setting|sections.*choose from)"),
    ('channel = 2\nsections = ["system", "system"]\n', r"sections: items must be unique"),
    ('channel = 2\nsections = [["system"]]\n', r"sections.*expected (list|string)"),
    ('channel = 2\nsections = [1]\n', r"sections.*expected (list|string)"),
    ('channel = 2\nimage_every = 59\n', r"image_every: (expected integer|value must)"),
    ('channel = 2\nimage_every = true\n', r"image_every: (expected integer|value must)"),
    ('channel = 2\nedit_every = 4\n', r"edit_every: (expected integer|value must)"),
    ('channel = 2\nedit_every = 5.0\n', r"edit_every: (expected integer|value must)"),
])
def test_bad_output_values_fail_closed(tmp_path, monkeypatch, entry, msg):
    with pytest.raises(SystemExit, match=msg):
        load(tmp_path, monkeypatch, 'config_format = 1\nguild_id = 1\n[[outputs]]\n' + entry)


def test_duplicate_output_channels_and_nested_order_are_rejected(tmp_path, monkeypatch):
    with pytest.raises(SystemExit, match="duplicate channel 2"):
        load(tmp_path, monkeypatch, 'config_format = 1\nguild_id = 1\n[[outputs]]\nchannel = 2\n'
             '[[outputs]]\nchannel = 2\n')
    with pytest.raises(SystemExit, match=r"order.*expected string"):
        load(tmp_path, monkeypatch, 'config_format = 1\nguild_id = 1\norder = [["system"]]\n[[outputs]]\nchannel = 2\n')
    with pytest.raises(SystemExit, match="mirror_channels"):
        load(tmp_path, monkeypatch, BASE + 'mirror_channels = 1\n')


def test_helper_configuration_uses_an_environment_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_HELPER_KEY", "d" * 32)
    assert load(tmp_path, monkeypatch, BASE).helper is None
    cfg = load(tmp_path, monkeypatch, BASE + '[helper]\nsocket = "/run/shazzzbot-helper/helper.sock"\n'
               'key_env = "TEST_HELPER_KEY"\n')
    assert cfg.helper.socket == "/run/shazzzbot-helper/helper.sock" and cfg.helper.key_env == "TEST_HELPER_KEY"
    assert "d" * 32 not in repr(cfg.helper)


@pytest.mark.parametrize("value", ['1', '{}', '{socket="/run/x"}', '{socket="/run/x",key_env="KEY",key="secret"}',
                                  '{socket=1,key_env="KEY"}', '{socket="relative",key_env="KEY"}',
                                  '{socket="/run/../x",key_env="KEY"}', '{socket="/run//x",key_env="KEY"}',
                                  '{socket="/run/./x",key_env="KEY"}', '{socket="/run/x/",key_env="KEY"}',
                                  '{socket="/run/x\\n",key_env="KEY"}', '{socket="/run/\\u0000x",key_env="KEY"}',
                                  '{socket="/' + 'x' * 107 + '",key_env="KEY"}',
                                  '{socket="/run/x",key_env=1}', '{socket="/run/x",key_env=""}',
                                  '{socket="/run/x",key_env="BAD NAME"}'])
def test_invalid_helper_configuration_fails_closed(tmp_path, monkeypatch, value):
    with pytest.raises(SystemExit, match="helper"):
        load(tmp_path, monkeypatch, BASE + f'helper = {value}\n')


@pytest.mark.parametrize("key", [None, "", "short", "x" * 4097, "!" * 32])
def test_helper_key_must_exist_and_be_valid_without_exposing_it(tmp_path, monkeypatch, key):
    monkeypatch.delenv("TEST_HELPER_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("TEST_HELPER_KEY", key)
    with pytest.raises(SystemExit, match="valid helper key") as caught:
        load(tmp_path, monkeypatch, BASE + '[helper]\nsocket="/run/x"\nkey_env="TEST_HELPER_KEY"\n')
    if key:
        assert key not in str(caught.value)


def test_disk_busy_threshold_is_one_warning_only_number(tmp_path, monkeypatch):
    import math

    assert load(tmp_path, monkeypatch, BASE).thresholds["disk_busy"] == (95, math.inf)
    for value in (0, 92.5, 100):
        cfg = load(tmp_path, monkeypatch, BASE + f'[thresholds]\ndisk_busy = {value}\n')
        assert cfg.thresholds["disk_busy"] == (value, math.inf)


@pytest.mark.parametrize("value", ['[95]', '[100, 95]', 'true', '"95"'])
def test_disk_busy_threshold_rejects_invalid_values(tmp_path, monkeypatch, value):
    with pytest.raises(SystemExit, match="disk_busy"):
        load(tmp_path, monkeypatch, BASE + f'[thresholds]\ndisk_busy = {value}\n')


def test_any_threshold_accepts_warning_only_or_a_pair(tmp_path, monkeypatch):
    import math

    cfg = load(tmp_path, monkeypatch, BASE + '[thresholds]\ndisk_busy = [95, 100]\nresource = 90\n')
    assert cfg.thresholds["disk_busy"] == (95, 100)
    assert cfg.thresholds["resource"] == (90, math.inf)


def test_one_forge_can_opt_in_to_a_system_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", "fixture-" + "forge-token")
    cfg = load(tmp_path, monkeypatch, BASE + forge_block('system_row = true\n'))
    assert cfg.forges[0].system_row is True
    assert load(tmp_path, monkeypatch, BASE + forge_block()).forges[0].system_row is False
    second = forge_block('system_row = true\n').replace('https://forge.example', 'https://other.example')
    for text in (forge_block('system_row = true\n') + second, forge_block('system_row = 1\n'),
                 forge_block('system_row = "true"\n')):
        with pytest.raises(SystemExit, match="system_row"):
            load(tmp_path, monkeypatch, BASE + text)


def test_preview_is_off_unless_enabled_and_checks_its_settings(tmp_path, monkeypatch):
    assert load(tmp_path, monkeypatch, BASE).preview is None
    assert load(tmp_path, monkeypatch, BASE + '[preview]\nport = 9000\n').preview is None
    cfg = load(tmp_path, monkeypatch, BASE + '[preview]\nenabled = true\n')
    assert cfg.preview == config.Preview()
    cfg = load(tmp_path, monkeypatch, BASE + '[preview]\nenabled = true\nbind = "127.0.0.1"\nport = 9000\n'
               'user = "me"\npassword_hash = "scrypt$x"\n')
    assert cfg.preview == config.Preview("127.0.0.1", 9000, "me", "scrypt$x")


@pytest.mark.parametrize("body,match", [("preview = 1\n", r"preview.*expected a table"),
                                        ("[preview]\nother = 1\n", r"preview.*unknown setting.*other"),
                                        ("[preview]\nenabled = 1\n", r"enabled: expected boolean"),
                                        ('[preview]\nbind = "host.example"\n', r"bind.*IP"),
                                        ("[preview]\nbind = 1\n", "IP"),
                                        ("[preview]\nport = 80\n", "1024"),
                                        ("[preview]\nport = true\n", r"port: expected integer"),
                                        ('[preview]\nuser = "me"\n', "go together"),
                                        ('[preview]\npassword_hash = "h"\n', "go together"),
                                        ('[preview]\nuser = "a:b"\npassword_hash = "h"\n', r"user: expected string"),
                                        ('[preview]\nuser = 1\npassword_hash = "h"\n', r"user: expected string")])
def test_preview_rejects_bad_settings(tmp_path, monkeypatch, body, match):
    with pytest.raises(SystemExit, match=match):
        load(tmp_path, monkeypatch, BASE + body)


def test_nested_is_the_inverse_of_flatten_and_skips_absent_settings():
    flat = {"guild_id": 1, "title": "T", "restart_allow": ["a*"]}
    assert config.nested(flat) == {"guild_id": 1, "system": {"title": "T"}, "restarts": {"allow": ["a*"]}}
    assert config.flatten(config.nested(flat)) == flat
