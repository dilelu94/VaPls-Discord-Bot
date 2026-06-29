# Sistema de Mascotas (`/mascota`)

## Overview

El comando `/mascota` permite a cada usuario generar, evolucionar y mostrar una
**mascota procedural ASCII** única y determinista. Cada mascota se genera a
partir del ID de Discord del usuario, garantizando que siempre se obtenga la
misma criatura.

Las mascotas tienen rarezas (común, raro, épico, legendario), estadísticas
aleatorias, y pueden evolucionar a formas más poderosas.

## Comando

```
/mascota [accion: ver | evolucionar | revertir | historial]
```

Por defecto (sin argumento) muestra la mascota del usuario.

### Acciones

| Acción          | Descripción                                                             |
| --------------- | ----------------------------------------------------------------------- |
| **ver**         | Muestra tu mascota actual. Si no tenés una, se crea automáticamente.    |
| **evolucionar** | Evoluciona tu mascota a una forma más rara (si es posible).             |
| **revertir**    | Revierte la mascota a su forma anterior (libera los puntos reservados). |
| **historial**   | Muestra el historial completo de todas las evoluciones de tu mascota.   |

### Botones

Al usar `/mascota` (ver, evolucionar o revertir) aparece un mensaje efímero
(solo visible para vos) con dos botones:

| Botón          | Comportamiento                                                                    |
| -------------- | --------------------------------------------------------------------------------- |
| **👁 Mostrar** | Publica la mascota en el canal visible para todos. Se desactiva después de usado. |
| **✖ Cerrar**  | Cierra el mensaje.                                                                |

El mensaje expira automáticamente a los **5 minutos** y los botones se
deshabilitan.

## Sistema de Puntos

Evolucionar una mascota cuesta **300 puntos de mascota** (pet points).

### Cómo ganar puntos

Los puntos se acumulan automáticamente por actividad en Discord:

| Actividad | Puntos por evento |
| --------- | ----------------- |
| Mensaje   | 0.2               |
| Voz (VAD) | 0.1               |

(aprox. ~500 puntos = más de 1 semana de actividad moderada)

### Seed inicial

Usuarios con **MMR > 1500** reciben **500 puntos gratis** la primera vez que
consultan su mascota.

### Estructura de puntos

Los puntos se manejan en el userbot (`userbot/activity_db.py`, tabla
`pet_points`):

- `total_earned`: Puntos ganados en total.
- `spent`: Puntos gastados permanentemente (solo la primera evolución).
- `reserved`: Puntos reservados para evoluciones (se liberan al revertir).
- `available` = `total_earned - spent - reserved`.

## Evolución

### Primera evolución

La primera evolución **gasta** 300 puntos permanentemente (es el costo de
"generar" la mascota sobre la que se evoluciona).

### Evoluciones siguientes

Las evoluciones posteriores **reservan** 300 puntos. Al revertir, los puntos
vuelven a estar disponibles.

### Algoritmo

1. Se parte de la `seed` original (derivada del ID de Discord).
2. Para cada nivel de evolución, se genera una nueva seed:
   `new_seed = (original_seed * 6364136223 + level) & 0xFFFFFFFF`
3. Se incrementa `level` hasta que la suma de rareza de las partes de la nueva
   mascota sea **mayor** que la actual.
4. La evolución es **determinista**: mismo usuario + mismo nivel = misma mascota.

### Reversibilidad

- La reversión libera los 300 puntos reservados.
- La primera forma (nivel 0, seed original) no se puede revertir más.

## Renderizado de imágenes

El bot puede generar imágenes PNG del ASCII de la mascota usando un renderizador
JavaScript con node-canvas:

### Dependencias

- **Node.js** v18+ (instalado en el servidor para bgutil-pot-provider).
- `npm install canvas gifencoder` en `pet-renderer/`.

### Arquitectura

```
bot.py (Python)
  └─ subprocess → node pet-renderer/render-cli.js [--gif]
                    ├─ petRenderer.js   (canvas → PNG/GIF)
                    └─ asciiAnimator.js (frames animados)
  └─ Discord AttachmentBuilder → canal
```

El CLI recibe el JSON del pet por stdin y escribe el buffer de imagen a stdout.

### Archivos relevantes

| Archivo                         | Rol                                                       |
| ------------------------------- | --------------------------------------------------------- |
| `petGenerator.py`               | Generación procedural en Python (seed, partes, rareza).   |
| `pet-renderer/render-cli.js`    | Entry point CLI para subprocess.                          |
| `pet-renderer/petRenderer.js`   | Renderiza ASCII a PNG/GIF con node-canvas.                |
| `pet-renderer/asciiAnimator.js` | Genera frames animados (blink, breathe, float).           |
| `userbot/activity_db.py`        | Tabla `pet_points` + funciones de earn/reserve/spend.     |
| `userbot/bot.py`                | Endpoints relay HTTP para pet-points.                     |
| `bot.py`                        | Comando `/mascota` + helpers de comunicación con userbot. |

## Logging

Todas las acciones del sistema de mascotas se registran con `log.info()`:

- `petGenerator`: creación, evolución y reversión de mascotas.
- `bot.py`: cada invocación de `/mascota` (acción, usuario, rareza, nivel).
- Logs visibles en `journalctl -u discord-bot -f` o en `bot.log`.

## Testing

```bash
make check  # corre pytest
```

Los tests del sistema de puntos están en `tests/test_activity_db.py`.
