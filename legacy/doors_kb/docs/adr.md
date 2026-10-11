# Decisiones de arquitectura (ADR)

Cada decision registra **que** se decidio, **por que** y que consecuencias tiene. Las ocho primeras
provienen de la seccion 14 de la especificacion; la novena se acordo al arrancar esta implementacion.

---

## ADR-001 — Usar DOORS Automation + DXL

**Decision.** Acceder a DOORS Classic mediante la interfaz Automation (COM/OLE) `DOORS.Application`,
ejecutando scripts DXL generados desde Python.

**Motivacion.** Es la interfaz disponible para DOORS Classic de escritorio y permite ejecutar consultas
dentro de una sesion controlada. No existe una API REST equivalente en esta version del producto.

**Consecuencias.** El proceso Python debe correr en Windows, en la misma maquina que DOORS, con el mismo
usuario. Queda descartada la ejecucion desde WSL, contenedores de desarrollo o Remote SSH.

---

## ADR-002 — Crear una sesion Automation propia

**Decision.** Crear la sesion con `Dispatch("DOORS.Application")` en lugar de conectarse a una ventana ya
abierta con `GetActiveObject`.

**Motivacion.** Durante el desarrollo se comprobo que una ventana de DOORS abierta manualmente **no**
aparece de forma fiable como `DOORS.Application` en la Running Object Table de Windows. Ademas, la sesion
creada por COM no comparte el modulo abierto en la ventana manual, lo que producia errores `NO_MODULE`.

**Consecuencias.** El usuario debe autenticarse en la ventana que abre Python (no vale su sesion previa) y
el modulo se abre explicitamente por `fullName`. La espera del login necesita un timeout propio.

---

## ADR-003 — Solo lectura, sin tool de DXL arbitrario

**Decision.** Los modulos se abren con `read(...)` y el servidor MCP **no** expone ninguna herramienta que
permita al agente ejecutar DXL libre.

**Motivacion.** Reduce a cero el riesgo de que un agente modifique o borre la base de requisitos. DXL es un
lenguaje completo: una tool generica seria equivalente a dar acceso de escritura total.

**Consecuencias.** Cada capacidad nueva exige una tool especifica y revisada. Todo valor que Python inserta
en un script DXL se escapa antes de concatenarse.

---

## ADR-004 — SQLite como replica local

**Decision.** Mantener una copia de consulta de los requisitos en SQLite.

**Motivacion.** Es sencillo, portable, transaccional y suficiente para construir FTS y RAG sin
infraestructura adicional. Desacopla las busquedas frecuentes del agente de la latencia y del coste DXL.

**Consecuencias.** La base local es un **derivado reconstruible**; DOORS sigue siendo la fuente de verdad.
Aparece el problema de la frescura: el sistema debe poder informar de cuando se sincronizo por ultima vez.

---

## ADR-005 — Hash de contenido por requisito

**Decision.** Calcular un SHA-256 sobre una representacion canonica de los campos sincronizados.

**Motivacion.** Permite clasificar cada requisito como `inserted`, `updated` o `unchanged` sin comparar
campo a campo, y sera la senal que evite recalcular embeddings de objetos que no han cambiado.

**Consecuencias.** El hash depende del conjunto de atributos elegido: cambiar ese conjunto invalida todos
los hashes del modulo (riesgo R-005). Conviene fijar un perfil de atributos estable por modulo antes de
generar embeddings.

---

## ADR-006 — Paginacion por cursor, nunca por offset

**Decision.** Recorrer los modulos con un cursor basado en el ultimo Absolute Number visitado y
`next(Object)`, en lugar de saltar N objetos desde el principio.

**Motivacion.** La primera implementacion usaba offset: cada pagina volvia a recorrer todos los objetos
anteriores, con coste cuadratico creciente que acababa agotando el watchdog interno de DXL
(*DXL Execution Timeout*) en modulos grandes.

**Consecuencias.** El coste por pagina es aproximadamente constante. A cambio, el recorrido es
estrictamente secuencial: no se puede saltar a una pagina arbitraria sin recorrer las anteriores.

---

## ADR-007 — FTS antes que embeddings

**Decision.** Implementar SQLite FTS5 antes de la busqueda semantica.

**Motivacion.** Da una linea base lexical rapida para identificadores, terminos exactos y vocabulario
tecnico, que es donde la busqueda vectorial suele rendir peor. Con esa linea base se puede medir que
aporta realmente la semantica y separar problemas de datos de problemas vectoriales.

**Consecuencias.** El hito de embeddings se retrasa deliberadamente. Ambos son hitos posteriores a esta
entrega (H4 y H5).

---

## ADR-008 — Separar cliente, sincronizacion, repositorio y MCP

**Decision.** Cuatro capas con fronteras explicitas: acceso a DOORS, servicio de sincronizacion,
persistencia y servidores MCP.

**Motivacion.** Mejora la testabilidad y permite sustituir la fuente o el indice en el futuro. En concreto,
que la sincronizacion dependa de un **protocolo** `RequirementsSource` y no de COM es lo que hace posible
probar toda su logica sin abrir DOORS (RNF-013).

**Consecuencias.** Hay que mantener la fuente falsa fiel a la semantica real de DOORS: si divergen, los
tests dejan de demostrar lo que dicen demostrar.

---

## ADR-009 — Embeddings mediante API compatible con OpenAI

**Decision.** Cuando se aborde el hito H5, los embeddings se generaran llamando a una API de un tercero que
implementa el **protocolo de OpenAI**, configurada mediante `base_url`, `api_key` y nombre de modelo.

**Motivacion.** Evita cargar un modelo pesado en la maquina de DOORS y permite cambiar de proveedor sin
tocar el codigo, porque el contrato HTTP es el mismo.

**Consecuencias.** La maquina que genere embeddings necesitara salida de red hacia ese endpoint, lo que
puede estar restringido en entornos de gestion de requisitos; la generacion podria tener que ejecutarse en
una maquina distinta de la que habla con DOORS. Cada embedding almacenara el modelo con el que se genero,
junto al hash de contenido, para poder detectar y rehacer indices obsoletos (RF-073).

**Estado.** Decidida, **no implementada** en esta entrega (alcance H0-H3).

---

## ADR-010 — Tabla FTS5 propia, no de contenido externo

**Decision.** El indice lexical es una tabla FTS5 autonoma alimentada desde el repositorio,
con tokenizador `unicode61 remove_diacritics 2` y **sin** *stemming* `porter`.

**Motivacion.** Las columnas de una FTS5 con `content=` deben corresponderse con columnas de
la tabla de contenido, y aqui los atributos del proyecto viven normalizados en
`requirement_attributes` (RF-053): no existe esa correspondencia. Sobre el tokenizador: los
requisitos estan escritos en espanol y se escriben indistintamente con y sin tildes, asi que
eliminar diacriticos es imprescindible; el *stemmer* `porter` es de ingles y degradaria
justo lo que ADR-007 quiere preservar, los identificadores y el vocabulario tecnico exacto.

**Consecuencias.** El texto se duplica en disco. A cambio, la actualizacion del indice es
explicita y comprobable, y se mantiene desde la clasificacion por hash que ya produce la
sincronizacion, sin una pasada extra. El indice es reconstruible (`--rebuild-index`), lo que
permite cambiar de tokenizador mas adelante sin volver a consultar DOORS.

---

## ADR-011 — Busqueda vectorial por fuerza bruta con numpy

**Decision.** La similitud se calcula multiplicando una matriz de embeddings por el vector de
consulta, con numpy, declarado en el extra `[embeddings]` y no en el nucleo.

**Motivacion.** No hay extension vectorial nativa disponible (`sqlite-vec` no lo esta), y con
modulos de cientos a decenas de miles de objetos un producto escalar resuelve en
milisegundos. Evita una dependencia nativa y mantiene la copia local reconstruible y
portable. En Python puro la misma operacion tarda segundos por consulta, asi que numpy se
exige de verdad: si falta, la busqueda semantica falla diciendo como instalarlo, en lugar de
degradarse a algo lento sin explicar por que.

**Consecuencias.** El nucleo (sincronizacion, FTS, servidor directo) sigue sin dependencias
mas alla del SDK de MCP. Habra que revisar la estrategia si algun modulo se acerca al orden
de 10^5 requisitos.

---

## ADR-012 — El display set nunca se usa al sincronizar

**Decision.** RF-022 (respetar la vista visible del modulo) es una opcion **de consulta**. El
servicio de sincronizacion la fija a "modulo completo" y no la expone.

**Motivacion.** Si el recorrido de sincronizacion respetara un filtro de vista, los objetos
ocultos por ese filtro no apareceran, y `mark_missing_as_deleted` los marcaria como
eliminados sin que hayan desaparecido de DOORS. Seria un borrado logico masivo provocado por
un ajuste de interfaz.

**Consecuencias.** Un agente puede pedir la vista visible en `list_requirements` o
`search_requirements`, pero no hay forma de sincronizar solo una parte filtrada de un modulo.
Hay dos tests que fijan la regla: uno comprueba que el sincronizador nunca pide la vista, y
otro reproduce el fallo que evita.

---

## ADR-013 — Dos hashes por embedding

**Decision.** Cada embedding guarda el hash de contenido del requisito **y** el hash del
texto que se embebio.

**Motivacion.** El primero cumple RF-073 y dice si el requisito cambio. Pero el texto de
embedding incluye un perfil configurable de atributos (RF-072), asi que puede cambiar sin que
el requisito cambie: al anadir un atributo al perfil, todos los `content_hash` siguen
iguales y el indice se quedaria obsoleto en silencio. Es el riesgo R-005 trasladado a los
embeddings.

**Consecuencias.** Cambiar el perfil regenera todos los embeddings del modulo, lo que con un
proveedor de pago tiene coste: conviene fijar el perfil antes de indexar un repositorio
grande. La clave primaria incluye ademas el modelo, de modo que dos modelos pueden convivir y
se puede migrar de proveedor sin quedarse sin busqueda semantica mientras se reindexa (R-006).

---

## ADR-014 — Los scripts DXL no construyen JSON

**Decision.** Los scripts DXL emiten cada valor precedido de su longitud
(`15:Absolute Number`) y es Python quien monta el JSON. En el DXL generado no queda **ni una
sola secuencia de escape**.

**Motivacion.** La primera ejecucion contra un DOORS real fallo con
`DxlExecutionError: DOORS devolvio una respuesta que no es JSON`. La causa: el script cerraba
cada valor con el literal DXL `"\""`, que no producia una comilla sino una barra invertida
seguida de comilla, de modo que la cadena JSON nunca cerraba.

El backslash concreto era lo de menos. El problema de fondo era que habia **tres lenguajes
de escapado encadenados** -Python escapa para generar el DXL, DXL escapa para construir la
cadena, y esa cadena tiene que ser JSON valido- y bastaba equivocarse en cualquiera de los
tres para corromper la respuesta. Parchear ese cierre habria dejado el mismo fallo latente en
`jsonEscape`, que usa las mismas secuencias para escapar comillas **dentro** de los valores:
habria vuelto a romperse en cuanto un requisito contuviera una comilla, que es lo normal.

Es la tercera vez que este proyecto tropieza con escapado anidado, despues del `\n` literal
del preambulo (RNF-008) y del doble escapado del JSON de error.

**Consecuencias.**

* Un valor puede contener comillas, barras invertidas, saltos de linea o tabuladores sin
  ningun tratamiento especial: el lector no busca delimitadores, cuenta caracteres.
* Una respuesta cortada se detecta con precision -la longitud declarada no cuadra- en lugar
  de manifestarse como un error de sintaxis indistinguible de una respuesta mal construida.
* El orden de los campos pasa a ser el contrato entre `dxl.py` y `client.py`: los nombres no
  viajan. A cambio la respuesta ocupa menos y no hay nada que escapar.
* Sigue existiendo escapado de **entrada** (`escape_dxl_string`): lo que Python inserta en un
  script -una ruta de modulo, un termino de busqueda- tiene que seguir siendo un literal
  cerrado para que un agente no pueda inyectar codigo DXL (RF-041, ADR-003).
* Hay un test que falla si alguien vuelve a introducir una barra invertida en el DXL generado.

---

## ADR-015 — Cada llamada a DOORS lleva su propio testigo

**Decision.** Cada script incluye un identificador unico de la llamada y la respuesta tiene
que devolverlo; si no coincide, se rechaza.

**Motivacion.** Al ejecutar la lectura de atributos contra un DOORS real, Python recibio
`ok`: el resultado de la sonda de sesion de la llamada **anterior**. Cuando un script DXL
falla, `oleSetResult` no llega a ejecutarse y la propiedad `result` de DOORS conserva el
valor previo. No hay nada en esa respuesta que indique que es vieja.

Es un fallo especialmente peligroso porque no se manifiesta como un error: en una
sincronizacion paginada, una pagina que falla devolveria los requisitos de la pagina
anterior, el sincronizador los daria por visitados y marcaria como eliminados objetos que
nunca llego a leer, que es justo lo que RF-061 existe para impedir.

**Consecuencias.** Un script que falla produce ahora un error explicito que remite a la
ventana *DXL output* de DOORS, donde esta el mensaje del interprete. El testigo se inserta
sin escapar en el script, asi que se valida que sea alfanumerico antes de generarlo.


---

## ADR-016 — Toda conversion de numero a texto pasa por un unico sitio

**Decision.** El DXL generado convierte numeros a texto solo a traves de `aTexto(int)`, que
pone el **numero delante** (`return v ""`), y la longitud de un campo se guarda en una
variable antes de convertirla.

**Motivacion.** La tercera ejecucion contra DOORS real fallo con
`<Line:4> incorrect arguments for (=)` en `string n = length(s) ""`. La primera conclusion
fue que DXL exige la cadena delante, y se cambio a `"" v`. **Era incorrecta**: el
autodiagnostico probo las dos formas contra la instalacion real y el resultado fue el
contrario — `42 ""` funciona y `"" 42` no.

La causa real del fallo original no era la direccion sino el analisis sintactico:
`length(s) ""` se interpreta como `length(s "")`, porque la concatenacion se mete dentro de
los parentesis de la llamada. La expresion devuelve entonces un entero y la asignacion a una
cadena falla. Con una variable intermedia no hay ambiguedad.

**Consecuencias.** Hay un solo punto que corregir si otra instalacion se comporta de otro
modo, y dos tests: uno fija la direccion confirmada y otro impide volver a concatenar la
llamada a `length` del tiron.

**Leccion.** Un unico sintoma admitia dos explicaciones y se eligio la equivocada sin poder
comprobarla. Es exactamente el caso que ADR-017 existe para evitar: probar las dos
alternativas cuesta una linea y elimina la conjetura.

---

## ADR-017 — Un comando que prueba las primitivas de DXL una por una

**Decision.** `doors-selftest` ejecuta un script minimo e independiente por cada primitiva
de DXL que usa el proyecto y muestra cuales funcionan contra la instalacion real.

**Motivacion.** La capa DXL no se puede probar fuera de Windows, y tres ejecuciones
seguidas contra DOORS real fallaron por tres suposiciones distintas sobre el lenguaje
-`at.size` fuera de una enumeracion, el escapado de comillas y la direccion de la
concatenacion-. Cada una costaba una sincronizacion completa, una captura de la ventana
*DXL output* y una correccion, y solo revelaba el siguiente fallo al arreglar el anterior.
Comprobarlas todas a la vez convierte esa cadena de descubrimientos en una sola ejecucion.

**Consecuencias.** Las pruebas no usan las funciones auxiliares del protocolo a proposito:
si una estuviera rota, todas fallarian y el diagnostico no distinguiria la causa. Cada
prueba explica ademas que capacidad del proyecto queda afectada si falla, para que la tabla
diga no solo que algo no va, sino que consecuencias tiene. Cuando el proyecto empiece a usar
una primitiva nueva, hay que anadir su prueba: un test lo recuerda.
