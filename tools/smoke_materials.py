import json
import os
import sys

os.environ.setdefault("LUMEN_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import materials as m  # noqa: E402

print("key1", m.material_key("https://v3-web.douyinvod.com/xxx/video/tos/cn/?video_id=123&sign=abc"))
print("key2", m.material_key("https://img1.a.com/x/y.jpg"))
print("key3", m.material_key("https://img2.a.com/x/y.jpg"))
print("key4", m.material_key("/api/materials/rand/abc"))
print("ext", m._ext_of("https://a.com/x.mp4?sig=1"), "|", m._ext_of("https://a.com/api"))
print("clip", m.is_clip_id("shaofu"), m.is_clip_id("a/b"))
print("media_url", m._is_media_url("https://a.com/x.mp4", "video"), m._is_media_url("https://a.com/x.jpg", "video"))

print("parse_plain_json", m.parse_items(json.dumps(["https://a.com/1.mp4", "https://a.com/2.jpg"]), {"kind": "video"}))
print(
    "parse_path",
    m.parse_items(
        json.dumps(
            {"data": {"list": [{"play_url": "https://a.com/1.mp4", "cover": "https://a.com/c.jpg", "title": "t"}]}}
        ),
        {"kind": "video", "itemsPath": "data.list", "urlField": "play_url"},
    ),
)
print("parse_text", m.parse_items("http://a.com/1.mp4\nhttp://a.com/2.mp4", {"kind": "video"}))
print("parse_auto", m.parse_items(json.dumps({"rows": [{"url": "https://a.com/1.mp4", "name": "n"}]}), {"kind": "video"}))
print("parse_watermark", m.parse_items(json.dumps([{"play_url": "https://a.com/1.mp4", "play_url_wm": "https://a.com/wm.mp4"}]), {"kind": "video"}))
print("playlist_master", m.parse_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nv.m3u8\n#EXT-X-ENDLIST", "https://a.com/p.m3u8"))
print("playlist_media", m.parse_playlist("#EXTM3U\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:1\nseg1.ts\n#EXT-X-ENDLIST", "https://a.com/p.m3u8"))
print("playlist_enc", end=" ")
try:
    m.parse_playlist("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"k\"\n#EXTINF:1\ns.ts\n", "https://a.com/p.m3u8")
except ValueError as err:
    print("raise:", err)
print("base36", m._base36(1700000000000))
print("extra", m._with_extra_params("https://a.com/x?a=1", {"extraParams": '{"b":2}'}))
print("extra_bad", end=" ")
try:
    m._with_extra_params("ftp://a.com/x", {})
except ValueError as err:
    print("raise:", err)
print("snapshot", m.read_material_snapshot("nope"))
print("headers", m.material_headers({"headers": '{"Referer":"https://x/"}', "apiKey": "k", "apiKeyPrefix": "Bearer "})["Authorization"])
print("snapshot_src", m.material_source_snapshot({"id": "s", "name": "n", "lines": ["https://a/1"], "url": "https://a/2", "apiKey": "secret"}))
