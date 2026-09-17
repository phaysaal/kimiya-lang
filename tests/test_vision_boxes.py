"""Locator box conventions: a family's native box form becomes image pixels
before caching and before the capture-origin mapping."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kimiya import vision  # noqa: E402

# Gemini: [ymin, xmin, ymax, xmax] on a 0..1000 grid → pixels [x0, y0, x1, y1]
px = vision.image_pixels((356, 180, 378, 500), "google", 1312, 1105)
assert [round(v) for v in px] == [236, 393, 656, 418], px
# everyone else: pixels as given
assert vision.image_pixels((390, 396, 652, 411), "openai", 1312, 1105) == (390, 396, 652, 411)
assert vision.image_pixels((390, 396, 652, 411), "", 1312, 1105) == (390, 396, 652, 411)
# no size known → left untouched rather than guessed
assert vision.image_pixels((356, 180, 378, 500), "google", 0, 0) == (356, 180, 378, 500)
# and the mapping through the capture origin still applies afterwards
hit = vision._to_pixels(px, {"x": 2161, "y": 175})
assert hit["x"] == 2161 + round((236 + 656) / 2) and hit["h"] in (24, 25), hit
print("vision boxes: ok")
