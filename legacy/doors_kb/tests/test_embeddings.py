"""Embeddings: proveedor, texto y generacion incremental (RF-071 a RF-074, hito H5).

Todo se prueba sin red: el transporte HTTP se inyecta, igual que el worker COM se prueba
inyectando la inicializacion de COM.
"""

import json

import pytest

from doors_kb.config import Settings
from doors_kb.db import SqliteRepository
from doors_kb.db.repository import desempaquetar_vector, empaquetar_vector
from doors_kb.embeddings import (
    EmbeddingService,
    FakeEmbeddingProvider,
    OpenAICompatibleProvider,
    construir_texto,
)
from doors_kb.errors import EmbeddingError
from doors_kb.sync import SyncService

MODULO = "/Demo/Reqs"


# ---------------------------------------------------------------------------------------
# Texto de embedding (RF-072)
# ---------------------------------------------------------------------------------------


def test_el_texto_incluye_identificador_titulo_y_cuerpo():
    texto = construir_texto(
        {"identifier": "REQ-2", "heading": "Timeout TCP", "text": "Se cierra tras 30 s.",
         "attributes": {"Estado": "Aprobado"}}
    )

    assert "Identificador: REQ-2" in texto
    assert "Titulo: Timeout TCP" in texto
    assert "Texto: Se cierra tras 30 s." in texto
    assert "Estado" not in texto  # sin perfil configurado, los atributos no entran


def test_solo_entran_los_atributos_configurados():
    """RF-072: el perfil es una decision del proyecto, no una lista fija."""
    requisito = {"identifier": "REQ-2", "heading": "T", "text": "C",
                 "attributes": {"Estado": "Aprobado", "Criticidad": "SIL-2"}}

    texto = construir_texto(requisito, ["Criticidad"])

    assert "Criticidad: SIL-2" in texto
    assert "Estado" not in texto


# ---------------------------------------------------------------------------------------
# Proveedor compatible con OpenAI (RF-071, ADR-009)
# ---------------------------------------------------------------------------------------


def _transporte(respuestas):
    """Transporte falso que devuelve respuestas preparadas y registra las peticiones."""
    peticiones = []

    def transporte(url, cuerpo, cabeceras, timeout):
        peticiones.append({"url": url, "cuerpo": json.loads(cuerpo), "cabeceras": cabeceras})
        return respuestas.pop(0)

    transporte.peticiones = peticiones
    return transporte


def _ok(vectores):
    datos = {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectores)]}
    return (200, json.dumps(datos).encode())


def test_la_peticion_sigue_el_protocolo_de_openai():
    transporte = _transporte([_ok([[1.0, 0.0], [0.0, 1.0]])])
    proveedor = OpenAICompatibleProvider(
        "https://proveedor/v1", "sk-secreta", "modelo-x", transporte=transporte
    )

    vectores = proveedor.embed(["uno", "dos"])

    assert vectores == [[1.0, 0.0], [0.0, 1.0]]
    peticion = transporte.peticiones[0]
    assert peticion["url"] == "https://proveedor/v1/embeddings"
    assert peticion["cuerpo"] == {"model": "modelo-x", "input": ["uno", "dos"]}
    assert peticion["cabeceras"]["Authorization"] == "Bearer sk-secreta"


def test_los_textos_se_trocean_en_lotes():
    """Un modulo entero en una sola peticion supera cualquier limite del proveedor."""
    transporte = _transporte([_ok([[1.0]] * 2), _ok([[1.0]] * 2), _ok([[1.0]])])
    proveedor = OpenAICompatibleProvider(
        "https://p/v1", "k", "m", batch_size=2, transporte=transporte
    )

    vectores = proveedor.embed([f"texto {i}" for i in range(5)])

    assert len(vectores) == 5
    assert [len(p["cuerpo"]["input"]) for p in transporte.peticiones] == [2, 2, 1]


def test_los_vectores_se_ordenan_por_el_indice_de_la_respuesta():
    """El protocolo no garantiza el orden, y un desajuste asociaria vectores equivocados."""
    desordenada = {"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]}
    transporte = _transporte([(200, json.dumps(desordenada).encode())])
    proveedor = OpenAICompatibleProvider("https://p/v1", "k", "m", transporte=transporte)

    assert proveedor.embed(["a", "b"]) == [[1.0], [2.0]]


def test_una_respuesta_incompleta_es_un_error():
    """Faltar un vector no puede resolverse 'a ojo': se detiene."""
    incompleta = {"data": [{"index": 0, "embedding": [1.0]}]}
    transporte = _transporte([(200, json.dumps(incompleta).encode())])
    proveedor = OpenAICompatibleProvider("https://p/v1", "k", "m", transporte=transporte)

    with pytest.raises(EmbeddingError, match="incompleta"):
        proveedor.embed(["a", "b"])


@pytest.mark.parametrize("codigo", [429, 500, 503])
def test_los_errores_transitorios_se_reintentan(codigo):
    transporte = _transporte([(codigo, b"ocupado"), _ok([[1.0]])])
    proveedor = OpenAICompatibleProvider(
        "https://p/v1", "k", "m", transporte=transporte, dormir=lambda _s: None
    )

    assert proveedor.embed(["a"]) == [[1.0]]
    assert len(transporte.peticiones) == 2


def test_una_clave_invalida_no_se_reintenta():
    """Un 401 no mejora repitiendo la llamada, y el mensaje del servicio ayuda a arreglarlo."""
    transporte = _transporte([(401, b'{"error":"invalid api key"}')])
    proveedor = OpenAICompatibleProvider(
        "https://p/v1", "k", "m", transporte=transporte, dormir=lambda _s: None
    )

    with pytest.raises(EmbeddingError, match="invalid api key"):
        proveedor.embed(["a"])
    assert len(transporte.peticiones) == 1


def test_falta_de_configuracion_falla_al_construir():
    with pytest.raises(EmbeddingError, match="EMBEDDINGS_BASE_URL"):
        OpenAICompatibleProvider("", "k", "m")
    with pytest.raises(EmbeddingError, match="EMBEDDINGS_MODEL"):
        OpenAICompatibleProvider("https://p/v1", "k", "")


def test_el_proveedor_falso_es_determinista_entre_procesos():
    """Con hash() de Python los vectores cambiarian en cada arranque y no serian comparables."""
    a = FakeEmbeddingProvider().embed(["la conexion se cierra"])[0]
    b = FakeEmbeddingProvider().embed(["la conexion se cierra"])[0]

    assert a == b


# ---------------------------------------------------------------------------------------
# Serializacion de vectores
# ---------------------------------------------------------------------------------------


def test_los_vectores_se_guardan_en_formato_fijo():
    """float32 little-endian explicito: el fichero SQLite puede moverse entre maquinas."""
    vector = [0.5, -1.5, 2.0]

    recuperado = desempaquetar_vector(empaquetar_vector(vector))

    assert recuperado == vector
    assert len(empaquetar_vector(vector)) == 12  # 3 x 4 bytes


# ---------------------------------------------------------------------------------------
# Generacion incremental (RF-073, RF-074)
# ---------------------------------------------------------------------------------------


class ProveedorQueCuenta(FakeEmbeddingProvider):
    """Proveedor falso que registra cuantos textos se le han pedido."""

    def __init__(self) -> None:
        super().__init__()
        self.textos_pedidos: list[str] = []

    def embed(self, textos):
        self.textos_pedidos.extend(textos)
        return super().embed(textos)


@pytest.fixture
def entorno(fuente, atributos):
    with SqliteRepository(":memory:") as repo:
        ajustes = Settings(sync_attributes=atributos)
        SyncService(fuente, repo, ajustes).sync_module(MODULO)
        proveedor = ProveedorQueCuenta()
        yield EmbeddingService(proveedor, repo, ajustes), proveedor, repo, fuente


def test_la_primera_pasada_embebe_todos_los_requisitos(entorno):
    servicio, proveedor, repo, _ = entorno

    stats = servicio.update_index(MODULO)

    assert (stats["candidates"], stats["generated"], stats["skipped"]) == (3, 3, 0)
    assert len(proveedor.textos_pedidos) == 3
    assert repo.count_embeddings(MODULO, proveedor.model) == 3


def test_la_segunda_pasada_no_pide_ni_un_embedding(entorno):
    """RF-074: lo que no ha cambiado no se recalcula. Con un proveedor de pago, es la factura."""
    servicio, proveedor, _, _ = entorno
    servicio.update_index(MODULO)
    proveedor.textos_pedidos.clear()

    stats = servicio.update_index(MODULO)

    assert (stats["generated"], stats["skipped"]) == (0, 3)
    assert proveedor.textos_pedidos == []


def test_solo_se_reembebe_el_requisito_modificado(entorno):
    servicio, proveedor, repo, fuente = entorno
    servicio.update_index(MODULO)
    proveedor.textos_pedidos.clear()

    fuente.modificar(2, text="La conexion se cierra tras 60 segundos.")
    SyncService(fuente, repo, servicio.settings).sync_module(MODULO)
    stats = servicio.update_index(MODULO)

    assert (stats["generated"], stats["skipped"]) == (1, 2)
    assert len(proveedor.textos_pedidos) == 1
    assert "60 segundos" in proveedor.textos_pedidos[0]


def test_cambiar_el_perfil_de_atributos_si_regenera(entorno):
    """El caso que el hash de contenido no ve: el requisito no cambio, el texto embebido si."""
    servicio, proveedor, _, _ = entorno
    servicio.update_index(MODULO, attributes=())
    proveedor.textos_pedidos.clear()

    stats = servicio.update_index(MODULO, attributes=("Estado",))

    assert stats["generated"] == 3
    assert "Estado: Aprobado" in proveedor.textos_pedidos[0]


def test_un_requisito_borrado_pierde_su_embedding(entorno):
    """Igual que en el indice lexical: no se responde con lo que ya no esta en DOORS."""
    servicio, proveedor, repo, fuente = entorno
    servicio.update_index(MODULO)

    fuente.borrar(2)
    SyncService(fuente, repo, servicio.settings).sync_module(MODULO)
    stats = servicio.update_index(MODULO)

    assert stats["removed"] == 1
    assert repo.count_embeddings(MODULO, proveedor.model) == 2


def test_cada_embedding_guarda_modelo_y_hashes(entorno):
    """RF-073: sin esos datos no se puede saber que hay que rehacer."""
    servicio, proveedor, repo, _ = entorno
    servicio.update_index(MODULO)

    fila = repo.conn.execute(
        "SELECT model, dim, content_hash, embedding_text_hash FROM requirement_embeddings"
    ).fetchone()

    assert fila["model"] == proveedor.model
    assert fila["dim"] == proveedor.dim
    assert fila["content_hash"] and fila["embedding_text_hash"]
    assert fila["content_hash"] != fila["embedding_text_hash"]


def test_dos_modelos_pueden_convivir(entorno):
    """Permite migrar de proveedor sin quedarse sin busqueda semantica mientras se reindexa."""
    servicio, proveedor, repo, _ = entorno
    servicio.update_index(MODULO)

    otro = FakeEmbeddingProvider(model="otro-modelo", dim=32)
    EmbeddingService(otro, repo, servicio.settings).update_index(MODULO)

    assert repo.count_embeddings(MODULO, proveedor.model) == 3
    assert repo.count_embeddings(MODULO, "otro-modelo") == 3


def test_un_fallo_del_proveedor_queda_registrado(entorno):
    """Igual que sync_runs: una generacion fallida deja rastro."""
    servicio, _, repo, _ = entorno

    class ProveedorRoto(FakeEmbeddingProvider):
        def embed(self, textos):
            raise EmbeddingError("el servicio no responde")

    servicio.provider = ProveedorRoto()
    with pytest.raises(EmbeddingError):
        servicio.update_index(MODULO)

    registro = repo.last_embedding_run(MODULO)
    assert registro["status"] == "failed"
    assert "no responde" in registro["error"]


def test_un_fallo_a_mitad_no_deja_embeddings_sueltos(entorno):
    """La escritura va en transaccion: o se guardan todos los de la pasada, o ninguno."""
    servicio, proveedor, repo, _ = entorno

    class ProveedorQueDevuelveDeMenos(FakeEmbeddingProvider):
        def embed(self, textos):
            return super().embed(textos)[:-1]

    servicio.provider = ProveedorQueDevuelveDeMenos()
    with pytest.raises(EmbeddingError, match="no se puede asociar"):
        servicio.update_index(MODULO)

    assert repo.count_embeddings(MODULO, proveedor.model) == 0


def test_un_atributo_del_perfil_que_no_esta_sincronizado_se_avisa(entorno, caplog):
    """No es un error, pero tampoco puede pasar desapercibido.

    El texto de embedding solo puede usar atributos que la sincronizacion haya traido a la
    copia local. Pedir uno que no esta no falla: simplemente no aporta nada, y sin aviso
    seria un ajuste que parece aplicado y no lo esta.
    """
    servicio, _, _, _ = entorno

    with caplog.at_level("WARNING"):
        servicio.update_index(MODULO, attributes=("Criticidad",))

    assert "no estan en la copia local" in caplog.text
    assert "Criticidad" in caplog.text
