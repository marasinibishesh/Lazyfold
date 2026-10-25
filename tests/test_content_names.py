"""Names suggested from what's inside: documents, slides, spreadsheets and pictures."""

import zipfile

import pytest

from lazyfold import automation, engine, extract, service
from lazyfold.llm import ModelAdvice, NameAdvice
from lazyfold.models import FileRecord
from lazyfold.rules import Decision

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _pptx(path, slides):
    with zipfile.ZipFile(path, "w") as z:
        for i, lines in enumerate(slides, 1):
            paras = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in lines)
            z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:a="{A}" xmlns:p="x"><a:txBody>{paras}</a:txBody></p:sld>')


def _rec(name, text="", sniffed=None, **meta):
    r = FileRecord(rel_path=name, name=name, ext="." + name.rsplit(".", 1)[-1].lower(), size=10, mtime_ns=1,
                   mtime_iso="2026-10-01T00:00:00", sniffed=sniffed)
    r.text, r.meta = text, meta
    return r


# --------------------------------------------------------------------------- extraction

def test_pptx_text_in_slide_order(tmp_path):
    p = tmp_path / "Presentation1.pptx"
    _pptx(p, [["Contoso Coffee", "Q3 Sales Review"]] + [[f"slide {i}"] for i in range(2, 12)])
    text, meta = extract.extract_pptx(p)
    assert text.startswith("Contoso Coffee\nQ3 Sales Review\nslide 2") and "slide 10" in text
    assert text.index("slide 2") < text.index("slide 10")  # numeric, not alphabetical, order
    assert meta["slides"] == 11


def test_xlsx_sheet_names_and_cells(tmp_path):
    p = tmp_path / "Book1.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{S}"><sheets><sheet name="Budget 2027"/></sheets></workbook>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{S}"><si><t>Household budget</t></si><si><t>Rent</t></si></sst>')
    text, meta = extract.extract_xlsx(p)
    assert text == "Sheets: Budget 2027\nHousehold budget\nRent" and meta["sheets"] == ["Budget 2027"]


def test_office_xml_with_a_dtd_is_refused(tmp_path):
    p = tmp_path / "evil.pptx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("ppt/slides/slide1.xml", '<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
    assert extract.extract(p, "pptx", ".pptx")[0] == ""


def test_image_preview_is_a_small_jpeg(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    p = tmp_path / "big.png"
    PIL.new("RGB", (3000, 2000), (10, 120, 200)).save(p)
    import base64
    import io
    data = base64.b64decode(extract.image_preview(p))
    with PIL.open(io.BytesIO(data)) as im:
        assert im.format == "JPEG" and max(im.size) <= extract.PREVIEW_PX


# --------------------------------------------------------------------------- name policy

def test_generic_names_still_need_words_from_the_file():
    r = _rec("document (3).pdf", "Receipt from Northwind Coffee Roasters")
    assert engine.accept_name("Receipt - Northwind Coffee", r, any_name=False)[0] == "Receipt - Northwind Coffee"
    assert engine.accept_name("Invoice - Globex", r, any_name=False) == (None, "ignored model name not found in the file's text")


def test_meaningful_names_are_only_improved_with_content_naming_on():
    r = _rec("invoice_10234.pdf", "Invoice 10234 Acme Cloud Services Ltd total due")
    assert engine.accept_name("Invoice - Acme Cloud Services", r, any_name=False) == (None, None)  # old behaviour
    name, why = engine.accept_name("Invoice - Acme Cloud Services", r, any_name=True)
    assert name == "Invoice - Acme Cloud Services" and why.startswith("Gemma suggested a clearer name")


def test_no_rename_when_the_name_already_says_it():
    assert engine.says_the_same("Logo final version 2", "logo-final-v2")
    assert engine.says_the_same("Q3 Sales Review", "Q3_sales_review_final")
    assert not engine.says_the_same("Contoso Coffee Q3 Sales Review", "Presentation1")
    r = _rec("Q3_sales_review_final.pptx", "Q3 Sales Review")
    assert engine.accept_name("Q3 Sales Review", r, any_name=True) == (None, None)


def test_picture_names_keep_the_camera_date():
    r = _rec("IMG_20260914_183022.jpg", sniffed="jpeg")
    assert engine.accept_name("Beach at sunset", r, any_name=True, seen=True)[0] == "Beach at sunset - 2026-09-14"
    r = _rec("Screenshot 2026-10-12 101522.png", sniffed="png")
    assert engine.accept_name("Wi-Fi settings", r, any_name=True, seen=True)[0] == "Wi-Fi settings - 2026-10-12"


# --------------------------------------------------------------------------- classifier

class FakeClient:
    def __init__(self, name="Contoso Coffee Q3 Sales Review"):
        self.name, self.calls = name, []

    def advise(self, rec):
        self.calls.append(("advise", rec.name, None))
        return ModelAdvice("documents", 0.9, self.name, "read it", 0.1)

    def suggest_name(self, rec, image_b64=None):
        self.calls.append(("name", rec.name, image_b64))
        return NameAdvice(self.name, "looked at it", 0.1)


def test_slides_are_named_but_keep_their_folder(tmp_path):
    c = FakeClient()
    hc = engine.HybridClassifier(c, root=tmp_path, content_names=True)
    d = hc(_rec("Presentation1.pptx", "Contoso Coffee\nQ3 Sales Review", sniffed="pptx"))
    assert d.category == "presentations" and d.new_stem == "Contoso Coffee Q3 Sales Review"
    assert any(r.startswith("Gemma") for r in d.reasons)  # so the auto-tidy treats it as model-influenced


def test_pictures_are_sent_as_a_preview(tmp_path, monkeypatch):
    c = FakeClient("Mountain lake at sunset")
    hc = engine.HybridClassifier(c, root=tmp_path, content_names=True)
    monkeypatch.setattr(hc, "_preview", lambda path: "QUJD")
    d = hc(_rec("IMG_0042.jpg", sniffed="jpeg"))
    assert c.calls == [("name", "IMG_0042.jpg", "QUJD")] and d.new_stem == "Mountain lake at sunset"


def test_picture_cap_and_missing_preview(tmp_path, monkeypatch):
    c = FakeClient("Mountain lake")
    hc = engine.HybridClassifier(c, root=tmp_path, content_names=True)
    monkeypatch.setattr(hc, "_preview", lambda path: "")
    assert hc(_rec("IMG_1.jpg", sniffed="jpeg")).new_stem is None and not c.calls  # unreadable picture: no call
    monkeypatch.setattr(hc, "_preview", lambda path: "QUJD")
    hc.pictures = engine.MAX_PICTURES
    assert hc(_rec("IMG_2.jpg", sniffed="jpeg")).new_stem is None and not c.calls


def test_content_naming_off_asks_nothing_for_slides_or_pictures(tmp_path):
    c = FakeClient()
    hc = engine.HybridClassifier(c, root=tmp_path, content_names=False)
    assert hc(_rec("Presentation1.pptx", "Contoso Coffee", sniffed="pptx")).new_stem is None
    assert hc(_rec("IMG_0042.jpg", sniffed="jpeg")).new_stem is None
    assert not c.calls


def test_automatic_tidy_never_names_by_content(tmp_path, monkeypatch):
    seen = {}

    def fake_make_plan(root, **kw):
        seen.update(kw)
        raise ValueError("stop here")
    monkeypatch.setattr(service, "make_plan", fake_make_plan)
    automation.auto_run(tmp_path, notify_fn=lambda *a: True)
    assert seen.get("content_names") is False


def test_switching_a_suggestion_off_keeps_the_current_name(tmp_path):
    """The review sends the original stem back for a suggestion clicked off."""
    from lazyfold.models import Plan, Proposal
    p = Proposal(rel_path="Presentation1.pptx", action="move", category="presentations",
                 new_name="Contoso Coffee Q3 Sales Review.pptx",
                 dest_rel="Presentations/Contoso Coffee Q3 Sales Review.pptx", confidence=0.9, reasons=[],
                 engine="rules+model", fingerprint={"size": 1, "mtime_ns": 1, "head_sha": "x"}, ext=".pptx")
    plan = Plan(plan_id="p1", root=str(tmp_path), created="2026-10-25T10:00:00", engine="rules+model", proposals=[p])
    out = service.apply_edits(plan, {"Presentation1.pptx"}, {"Presentation1.pptx": "Presentation1"}, {})
    moved = [x for x in out.proposals if x.rel_path == "Presentation1.pptx"][0]
    assert moved.new_name == "Presentation1.pptx" and moved.dest_rel.endswith("Presentation1.pptx")


# --------------------------------------------------------------------------- learned rules vs TabPFN

def test_leave_it_rules_step_aside_when_tabpfn_is_on(monkeypatch):
    from lazyfold import learning, planner
    rule = learning.Rule(key="ext:.png", outcome=learning.KEEP, support=3, contradictions=0)
    monkeypatch.setattr(learning, "_cached_rules", lambda: {"ext:.png": rule})
    rec = _rec("Screenshot 1.png", sniffed="png")
    base = Decision("screenshots", 0.95, ["name looks like a screenshot"])
    assert learning.apply_learned(rec, base).category is None  # rules alone: leave it
    kept = learning.apply_learned(rec, base, keep=False)
    assert kept.category == "screenshots" and not any(r.startswith("learned:") for r in kept.reasons)
    monkeypatch.setattr("lazyfold.predict.available", lambda: (True, "ok"))
    assert planner.PlanOptions.from_settings({"use_predictor": True}).learned_keep is False
    assert planner.PlanOptions.from_settings({"use_predictor": False}).learned_keep is True
    monkeypatch.setattr("lazyfold.predict.available", lambda: (False, "not installed"))
    assert planner.PlanOptions.from_settings({"use_predictor": True}).learned_keep is True  # standard edition


def test_folder_choices_still_become_rules_with_tabpfn_on(monkeypatch):
    from lazyfold import learning
    rule = learning.Rule(key="ext:.csv", outcome="finance", support=2, contradictions=0)
    monkeypatch.setattr(learning, "_cached_rules", lambda: {"ext:.csv": rule})
    d = learning.apply_learned(_rec("export.csv"), Decision("spreadsheets", 0.9, []), keep=False)
    assert d.category == "finance"
