import io
import weakref

import pytest
from PIL import Image

from shazzzbot import panel, renderbudget, view
from tests.test_forges import AT, maximum_forge_sections


class Allocations:
    def __init__(self, monkeypatch):
        self.records = []
        self.live = {}
        self.closed = set()
        self.peak = 0
        original_new = Image.Image._new
        original_close = Image.Image.close

        def allocated(source, core):
            image = original_new(source, core)
            token = len(self.records)
            reference = weakref.ref(image, lambda _reference: self.live.pop(token, None))
            self.records.append((image.mode, image.size, reference))
            self.live[token] = 4 * image.width * image.height
            self.peak = max(self.peak, sum(self.live.values()))
            image._allocation_token = token
            return image

        def closed(image):
            token = getattr(image, "_allocation_token", None)
            self.live.pop(token, None)
            self.closed.add(token)
            return original_close(image)

        monkeypatch.setattr(Image.Image, "_new", allocated)
        monkeypatch.setattr(Image.Image, "close", closed)


def test_maximum_forge_renders_real_png_with_released_bands_inside_the_budget(monkeypatch):
    section = maximum_forge_sections.__wrapped__(monkeypatch)[0]
    monkeypatch.setattr(view, "placeholder", lambda *_args, **_kwargs: pytest.fail("valid forge needs real image"))
    allocations = Allocations(monkeypatch)
    png = view.image(section, now=AT)
    rendered = [(mode, size) for mode, size, _reference in allocations.records]
    width, height = 1993, 3730
    assert view.K == renderbudget.SCALE == 2
    assert ("RGB", (width, height)) in rendered
    assert ("RGB", (width * view.K, height * view.K)) not in rendered
    assert view._image_bytes(width, height) < renderbudget.RENDER_BUDGET
    assert allocations.peak + 8 * width * height < renderbudget.RENDER_BUDGET
    bands = [(token, size, reference) for token, (mode, size, reference) in enumerate(allocations.records)
             if mode == "RGB" and size[0] == width * view.K]
    assert len(bands) > 1
    assert all(size[1] <= 256 * view.K for _token, size, _reference in bands)
    assert all(token in allocations.closed or reference() is None for token, _size, reference in bands)
    with Image.open(io.BytesIO(png)) as result:
        assert result.format == "PNG" and result.size == (width, height)
        assert result.getpixel((0, 0)) == view.BG


def test_normal_two_times_render_has_identical_pixels_across_band_boundaries(monkeypatch):
    section = panel.Section("bands", "Band boundaries", [
        panel.Part([(panel.FINE, (f"First row {index}",)) for index in range(3)], heading="First"),
        panel.Part([(panel.WARNING, (f"Second row {index}",)) for index in range(3)], heading="Second"),
    ], note="A note across the card")
    allocations = Allocations(monkeypatch)
    monkeypatch.setattr(view, "_band_height", lambda _width, height: height)
    single = view.image(section, now=AT, min_width=800)
    assert ("RGB", (1600, 836)) in [(mode, size) for mode, size, _reference in allocations.records]
    monkeypatch.setattr(view, "_band_height", lambda _width, height: min(37, height))
    multiple = view.image(section, now=AT, min_width=800)
    with Image.open(io.BytesIO(single)) as expected, Image.open(io.BytesIO(multiple)) as actual:
        assert view.K == 2 and actual.size == expected.size == (800, 418)
        assert actual.tobytes() == expected.tobytes()
