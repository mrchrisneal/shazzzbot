from shazzzbot import jellyfin


def test_parse_counts_by_play_method():
    item = {"Name": "x"}
    sessions = [{"NowPlayingItem": item, "PlayState": {"PlayMethod": "DirectPlay"}},
                {"NowPlayingItem": item, "PlayState": {"PlayMethod": "Transcode"},
                 "TranscodingInfo": {"IsVideoDirect": True, "IsAudioDirect": True}},
                {"NowPlayingItem": item, "PlayState": {"PlayMethod": "Transcode"},
                 "TranscodingInfo": {"IsVideoDirect": False, "IsAudioDirect": True}},
                {"PlayState": {}}]
    assert jellyfin.parse(sessions) == {"direct play": 1, "direct stream": 1, "transcode": 1}


def test_streams_sends_the_token(serve):
    seen = {}

    async def sessions(req):
        seen.update(auth=req.headers["Authorization"], active=req.query["activeWithinSeconds"])
        return {"json": [{"NowPlayingItem": {"Name": "x"}, "PlayState": {"PlayMethod": "DirectPlay"}}]}

    assert serve({"/Sessions": sessions},
                 lambda http, base: jellyfin.streams(http, base + "/", "k")) == {"direct play": 1}
    assert seen == {"auth": 'MediaBrowser Token="k"', "active": "120"}
