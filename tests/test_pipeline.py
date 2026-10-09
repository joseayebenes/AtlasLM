import pytest
from atlas.pipeline import FatalError, Ledger, Runner, Step


def _paso(nombre, unidades, huellas, run, **kw):
    return Step(name=nombre, units=lambda ctx: list(unidades), fingerprint=lambda ctx, u: huellas[u],
                run=run, **kw)


def test_incremental_omite_lo_hecho_y_reprocesa_cambios(tmp_path):
    hechas = []
    huellas = {"a": "1", "b": "1"}
    paso = _paso("p", ["a", "b"], huellas, lambda ctx, u: hechas.append(u) or {"u": u})
    r = Runner(Ledger(tmp_path / "db"), [paso], workers=1)
    assert r.run().processed == {"p": 2}
    rep = r.run()
    assert rep.skipped == {"p": 2} and rep.processed == {}
    huellas["b"] = "2"                                  # cambia una unidad
    rep = r.run()
    assert rep.processed == {"p": 1} and hechas == ["a", "b", "b"]


def test_reanuda_tras_un_corte_a_mitad(tmp_path):
    hechas, caer = [], {"on": True}

    def run(ctx, u):
        if u == "b" and caer["on"]:
            raise KeyboardInterrupt            # simula Ctrl+C / kill a mitad de la unidad b
        hechas.append(u)

    paso = _paso("p", ["a", "b", "c"], {"a": "1", "b": "1", "c": "1"}, run, parallel=False)
    ledger = Ledger(tmp_path / "db")
    with pytest.raises(KeyboardInterrupt):
        Runner(ledger, [paso], workers=1).run()
    assert ledger.get("p", "a")["status"] == "ok" and ledger.get("p", "b")["status"] == "en_curso"
    assert not (tmp_path / "db").with_suffix(".lock").exists()   # el bloqueo se libera
    caer["on"] = False
    rep = Runner(ledger, [paso], workers=1).run()
    assert rep.processed == {"p": 2} and rep.skipped == {"p": 1} and hechas == ["a", "b", "c"]


def test_errores_cuarentena_y_fatal(tmp_path):
    def run(ctx, u):
        if u == "mala":
            raise ValueError("no puedo")
        if u == "fatal":
            raise FatalError("sin conexión")
        return 1

    ledger = Ledger(tmp_path / "db")
    paso = _paso("p", ["ok", "mala"], {"ok": "1", "mala": "1"}, run)
    r = Runner(ledger, [paso], max_attempts=2)
    assert r.run().errors == {"p": 1}
    assert r.run().quarantined == {"p": 1}                # segundo fallo: cuarentena
    assert r.run().quarantined == {"p": 1} and ledger.get("p", "mala")["status"] == "cuarentena"
    assert Runner(ledger, [paso], max_attempts=2, retry_quarantined=True).run().quarantined == {"p": 1}
    paso2 = _paso("q", ["fatal", "despues"], {"fatal": "1", "despues": "1"}, run, parallel=False)
    rep = Runner(ledger, [paso2]).run()
    assert rep.fatal and ledger.get("q", "despues") is None       # no se llegó a tocar


def test_poda_de_unidades_desaparecidas(tmp_path):
    podadas = []
    unidades = ["a", "b"]
    paso = Step(name="p", units=lambda ctx: list(unidades), fingerprint=lambda ctx, u: "1",
                run=lambda ctx, u: 1, prune=lambda ctx, u: podadas.append(u))
    ledger = Ledger(tmp_path / "db")
    Runner(ledger, [paso]).run()
    unidades.remove("a")
    rep = Runner(ledger, [paso]).run()
    assert rep.pruned == {"p": 1} and podadas == ["a"] and ledger.get("p", "a") is None


def test_orden_por_dependencias_y_ciclos(tmp_path):
    orden = []
    pasos = [_paso("c", ["x"], {"x": "1"}, lambda ctx, u: orden.append("c"), depends_on=["b"]),
             _paso("a", ["x"], {"x": "1"}, lambda ctx, u: orden.append("a")),
             _paso("b", ["x"], {"x": "1"}, lambda ctx, u: orden.append("b"), depends_on=["a"])]
    Runner(Ledger(tmp_path / "db"), pasos).run()
    assert orden == ["a", "b", "c"]
    with pytest.raises(ValueError, match="circular"):
        Runner(Ledger(tmp_path / "db2"), [_paso("a", [], {}, None, depends_on=["b"]),
                                           _paso("b", [], {}, None, depends_on=["a"])])
