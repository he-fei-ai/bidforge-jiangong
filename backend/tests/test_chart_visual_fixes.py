import json
from PIL import Image
from app.services.ai.mermaid_renderer import render_mermaid_to_bytes


def test_gantt_duration_header_not_truncated():
    code = json.dumps({
        "title": "plan",
        "totalDays": 230,
        "tasks": [
            {"id": 1, "name": "t1", "start": 1, "end": 10},
            {"id": 5, "name": "t5", "start": 171, "end": 230},
        ],
    })
    bio = render_mermaid_to_bytes(code, "gantt", skip_http=True, allow_pil=True)
    assert bio is not None
    bio.seek(0)
    img = Image.open(bio)
    w, h = img.size
    # Width must stay bounded (header fix must not blow up canvas)
    assert w < 6000 and h > 300


def test_timeline_first_last_card_not_clipped():
    code = json.dumps({
        "title": "milestones",
        "milestones": [
            {"name": "start", "date": "day1"},
            {"name": "mid", "date": "day50"},
            {"name": "end", "date": "day100"},
        ],
    })
    bio = render_mermaid_to_bytes(code, "timeline", skip_http=True, allow_pil=True)
    assert bio is not None
    bio.seek(0)
    img = Image.open(bio)
    w, h = img.size
    # 3 cards at 2.5x: total ~ 960*2.5 + margins; must be reasonable
    assert 2000 < w < 4000
