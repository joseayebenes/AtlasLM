"""Texto que representa a un requisito de cara a la busqueda semantica (RF-072).

Un embedding no se calcula sobre el requisito, sino sobre **una representacion textual** de
el. Que entra en esa representacion es una decision de producto: el identificador y el
titulo aportan senal de que trata el objeto, el cuerpo aporta el contenido, y algunos
atributos del proyecto (criticidad, subsistema, estado) pueden ser decisivos para que una
busqueda conceptual acierte.

Por eso el conjunto de atributos es configurable, y por eso hace falta el
``hash_texto``: si ese conjunto cambia, el requisito no ha cambiado -su hash de contenido
sigue siendo el mismo- pero su texto de embedding si, y el indice se quedaria obsoleto en
silencio. Es el riesgo R-005 trasladado a los embeddings.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence


def construir_texto(requisito: dict[str, object], atributos: Sequence[str] = ()) -> str:
    """Construye el texto a embeder a partir de un requisito de la copia local.

    Recibe el diccionario que devuelve ``SqliteRepository.get_requirement`` en lugar de un
    ``RequirementRecord`` porque el servicio de embeddings trabaja sobre la copia local, no
    sobre DOORS: para entonces, DOORS ya no interviene.

    Cada parte va etiquetada ("Identificador:", "Titulo:") en lugar de concatenada a secas,
    porque ayuda al modelo a distinguir un titulo de un cuerpo de texto y evita que un
    identificador suelto se confunda con contenido.
    """
    partes: list[str] = []
    identificador = str(requisito.get("identifier") or "")
    if identificador:
        partes.append(f"Identificador: {identificador}")
    titulo = str(requisito.get("heading") or "")
    if titulo:
        partes.append(f"Titulo: {titulo}")
    cuerpo = str(requisito.get("text") or "")
    if cuerpo:
        partes.append(f"Texto: {cuerpo}")

    valores = requisito.get("attributes") or {}
    if isinstance(valores, dict):
        for nombre in atributos:
            valor = valores.get(nombre)
            if valor:
                partes.append(f"{nombre}: {valor}")

    return "\n".join(partes)


def hash_texto(texto: str) -> str:
    """SHA-256 del texto de embedding.

    Es distinto del hash de contenido del requisito y ambos se guardan (RF-073). El de
    contenido dice si el requisito cambio; este dice si cambio lo que se embebio, que
    tambien varia al cambiar el perfil de atributos configurado.
    """
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()
