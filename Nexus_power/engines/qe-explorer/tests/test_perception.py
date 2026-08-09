"""U2 — pixel perception: G1 fingerprint, G3 signatures, G2/G3 synthesis. Pure."""
from __future__ import annotations

from io import BytesIO

from app.fingerprint import state_fingerprint
from app.perception import (
    average_hash,
    bbox_bucket,
    perceptual_hash_png,
    synthesize_vision_controls,
    synthesize_vision_outcomes,
    vision_control_signature,
)


# ── G1: perceptual hash mixed into the fingerprint only when DOM is sparse ────────

def test_perceptual_hash_is_ignored_on_a_rich_dom_page():
    controls = [{"role": "button", "name": "A"}, {"role": "button", "name": "B"},
                {"role": "button", "name": "C"}]
    a = state_fingerprint("http://x/app", controls)
    b = state_fingerprint("http://x/app", controls, perceptual_hash="deadbeef")
    assert a == b                       # rich DOM → phash never fragments state


def test_perceptual_hash_distinguishes_canvas_screens():
    # 0 controls, same URL — a canvas app. Different phash → different state.
    s1 = state_fingerprint("http://x/app", [], perceptual_hash="aaaa")
    s2 = state_fingerprint("http://x/app", [], perceptual_hash="bbbb")
    assert s1 != s2
    assert s1 == state_fingerprint("http://x/app", [], perceptual_hash="aaaa")


def test_no_phash_is_byte_compatible_with_before():
    controls = [{"role": "button", "name": "A"}]
    assert (state_fingerprint("http://x", controls)
            == state_fingerprint("http://x", controls, perceptual_hash=""))


# ── G1: average hash ─────────────────────────────────────────────────────────────

def test_average_hash_is_deterministic_and_discriminates():
    assert average_hash([[0, 0], [0, 0]]) == average_hash([[0, 0], [0, 0]])
    assert average_hash([[0, 0], [0, 0]]) != average_hash([[255, 0], [0, 0]])
    assert average_hash([]) == ""


def test_perceptual_hash_png_degrades_honestly():
    assert perceptual_hash_png(b"") == ""
    assert perceptual_hash_png(b"not-a-png") == ""      # bad bytes → "", never crash


def test_perceptual_hash_png_on_real_images_if_pillow():
    try:
        from PIL import Image
    except Exception:
        import pytest
        pytest.skip("Pillow not installed")
    b1 = BytesIO(); Image.new("L", (32, 32), 0).save(b1, format="PNG")
    img = Image.new("L", (32, 32), 0)
    for x in range(16):
        for y in range(32):
            img.putpixel((x, y), 255)
    b2 = BytesIO(); img.save(b2, format="PNG")
    h1, h2 = perceptual_hash_png(b1.getvalue()), perceptual_hash_png(b2.getvalue())
    assert h1 and h2 and h1 != h2


# ── G3: jitter-tolerant identity ─────────────────────────────────────────────────

def test_bbox_bucket_is_jitter_tolerant():
    assert bbox_bucket([0.50, 0.50, 0.10, 0.05]) == bbox_bucket([0.505, 0.498, 0.10, 0.05])
    assert bbox_bucket([0.10, 0.10, 0.05, 0.05]) != bbox_bucket([0.80, 0.80, 0.05, 0.05])


def test_vision_signature_stable_under_label_and_bbox_wobble():
    a = vision_control_signature("Continue", "button", [0.5, 0.5, 0.1, 0.05])
    b = vision_control_signature("continue ", "button", [0.505, 0.498, 0.1, 0.05])
    assert a == b
    assert a != vision_control_signature("Start Over", "button", [0.5, 0.5, 0.1, 0.05])


# ── G2/G3: synthesis into walk-consumable shapes ─────────────────────────────────

def test_synthesize_vision_controls_shape():
    perceived = [{"label": "Continue", "role": "button",
                  "bbox": [500, 600, 100, 40], "click_x": 550, "click_y": 620}]
    r = synthesize_vision_controls(perceived, page_w=1000, page_h=1200)[0]
    assert r["name"] == "Continue" and r["qec"]["capture_mode"] == "vision"
    assert r["qec"]["click_x"] == 550 and r["qec"]["click_y"] == 620
    assert r["signature"].startswith("vis:")
    assert r["qec"]["bbox"] == [0.5, 0.5, 0.1, round(40 / 1200, 4)]


def test_synthesize_vision_outcomes_shape():
    got = synthesize_vision_outcomes(
        [{"label": "Monthly Premium", "text": "$42.10"}, {"label": "", "text": ""}])
    assert got == [{"label": "Monthly Premium", "selector": "", "text": "$42.10",
                    "source": "vision"}]


# ── U1: route opaque surfaces (enterable frame vs vision) ────────────────────────

def test_route_opaque_surfaces():
    from app.perception import route_opaque_surfaces
    surfaces = [
        {"kind": "cross_origin_iframe", "label": "js.stripe.com"},
        {"kind": "canvas", "label": "chart region"},
        {"kind": "closed_shadow", "label": "x-widget"},
        {"kind": "unknown", "label": "?"},
        "junk",
    ]
    routed = route_opaque_surfaces(surfaces)
    assert [s["label"] for s in routed["enter_frames"]] == ["js.stripe.com"]
    assert {s["label"] for s in routed["vision"]} == {"chart region", "x-widget"}


# ── U2: escalation decision ──────────────────────────────────────────────────────

def test_should_perceive_escalates_only_on_opaque_surface_and_sparse_dom():
    from app.perception import should_perceive
    canvas = [{"kind": "canvas", "label": "chart"}]
    frame = [{"kind": "cross_origin_iframe", "label": "stripe"}]
    sparse = [{"qec": {"name_confidence": "none"}}]
    rich = [{"qec": {"name_confidence": "high"}} for _ in range(5)]
    assert should_perceive(sparse, canvas) is True    # opaque canvas + sparse DOM
    assert should_perceive(rich, canvas) is False     # DOM explains the page → skip
    assert should_perceive(sparse, frame) is False    # frame → entry, not vision
    assert should_perceive(sparse, []) is False       # no opaque surface
