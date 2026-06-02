# `/sugerencias` como sistema con feedback — diseño

**Issue:** [#23](https://github.com/dilelu94/VaPls-Discord-Bot/issues/23) — `/sugerencias` no da feedback de qué sugerencias ya existen.
**Fecha:** 2026-06-01

## Contexto

El comando `/sugerencias` ya existe (`suggestionsCommand.py`): toma una idea libre,
la categoriza con Gemini Flash-Lite, la agrupa con ideas similares y la persiste en
un JSON (`config.SUGGESTIONS_PATH`). El cuerpo original del issue (feedback al
matchear) ya está casi cubierto. El alcance real es el comentario del colaborador:

> "Esto hay que modelarlo como un sistema en serio, tiene que tener su propio modelo
> y si podemos usar posthog para backend mejor, se tienen que registrar las
> sugerencias, y poder también pedir sugerencias por otro comando, tienen que
> enviarse solo después de haber sido categorizadas por gemini."

**Hallazgo de arquitectura:** en este repo PostHog es write-only (captura de eventos
+ logs OTel). Leer datos de vuelta requeriría su Query API (personal API key +
HogQL), una integración aparte y pesada. La fuente de verdad sigue siendo el JSON
local.

## Decisiones (acordadas con el usuario)

1. **Backend:** JSON local como fuente de verdad **+** un evento PostHog por cada
   sugerencia categorizada (para que queden "registradas" en analytics). No se usa
   la Query API de PostHog.
2. **Comando ver:** público, ephemeral, muestra el top de grupos ordenados por
   cantidad de sugerencias.
3. **Sin categoría:** si Gemini falla, **no se persiste nada**; se le pide al usuario
   reintentar. Se elimina el bucket `unprocessed`.

## Cambios

### A. Modelo de datos propio
Dataclasses en `suggestionsCommand.py`:
- `Submission(user_id, user_name, text, at)`
- `Group(id, title, summary, created_at, updated_at, submissions: list[Submission])`
  con propiedad `size` y `to_dict`/`from_dict`.
- `SuggestionStore`: encapsula el load/save atómico del JSON (lógica actual).

El JSON on-disk se mantiene compatible con el formato actual; deja de escribirse el
campo `unprocessed`.

### B. Feedback de matcheo (issue original)
Formato pedido en el issue, contando las **pre-existentes** (= `size - 1`):
`✅ Sumé tu idea al grupo 'mejor sistema de votación' (3 sugerencias similares ya estaban ahí)`.

### C. Registro en PostHog
En cada submit categorizado: `analytics.capture("suggestion_submitted", ...)` con
`{action, group_id, group_title, group_size}`. Mockeable en el boundary en tests.

### D. Comando ver
`/sugerencias-ver` (público, ephemeral): lista grupos ordenados por cantidad de
submissions desc (top ~10): `**título** (N) — resumen`. Vacío → mensaje amable.
Emite `analytics.capture("suggestions_viewed")`.

### E. Solo persistir si categorizó
Flujo de submit:
1. Validar/truncar idea.
2. `_classify()` con Gemini.
3. Si falla → no persiste; responde "Gemini no disponible, probá de nuevo".
4. Matchea → append, `action="matched"`.
5. Nueva → crea grupo, `action="created"`.
6. En 4/5: persistir JSON + evento PostHog.
7. Responder con el formato del issue (matched muestra `size - 1`).

## Tests (behavioral)
- Matched → respuesta con conteo de pre-existentes correcto.
- Created → grupo nuevo.
- Gemini falla → NO persiste + usuario recibe aviso de reintento (reescribe los 2
  tests actuales que esperaban guardado).
- `/sugerencias-ver` con varios grupos → orden por conteo; vacío → mensaje.
- PostHog: se captura evento en submit categorizado (mock en boundary).

## Fuera de alcance (YAGNI)
- Query API de PostHog.
- Filtros/búsqueda en el comando ver.
- Re-categorización en background.
