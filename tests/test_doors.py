from atlas.config import load_config
from atlas.doors import DatabaseAdapter, diff_requirements, hash_requirement, normalize_id


def test_convention_and_links(toml):
    cfg = load_config(toml())
    ad = DatabaseAdapter(cfg["sources"]["doors"])
    assert ad.modules() == ["SRS_TMS", "SRS_TMS_OLD", "SSS_MISSION"]
    reqs = list(ad.requirements("SRS_TMS"))
    assert len(reqs) == 40
    r = {x.id: x for x in reqs}["SRS-0003"]
    assert r.raw_id == "srs-0003" and r.attributes["status"] in ("Approved", "Draft")
    assert r.parent is None or r.parent.startswith("SRS-")
    links = list(ad.links("SRS_TMS"))
    types = {l.type for l in links}
    assert "satisfies" in types and "references" in types      # table + references in the text
    assert all(l.source == l.source.upper() for l in links)
    ad.close()


def test_normalisation_and_diff():
    assert normalize_id(" srs - 0001 ") == "SRS-0001"
    from atlas.plugins import Requirement
    a = Requirement(id="SRS-1", text="a", module="M")
    b = Requirement(id="SRS-2", text="b", module="M")
    previous = {"SRS-1": hash_requirement(a), "SRS-3": "x"}
    b2 = Requirement(id="SRS-1", text="a changed", module="M")
    d = diff_requirements(previous, [b2, b])
    assert d == {"added": ["SRS-2"], "changed": ["SRS-1"], "removed": ["SRS-3"]}
