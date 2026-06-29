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
/mascota [accion: ver | evolucionar | historial]
```

Por defecto (sin argumento) muestra la mascota del usuario.

### Acciones

| Acción          | Descripción                                                           |
| --------------- | --------------------------------------------------------------------- |
| **ver**         | Muestra tu mascota actual. Si no tenés una, se crea automáticamente.  |
| **evolucionar** | Evoluciona tu mascota a una forma más rara (si es posible).           |
| **historial**   | Muestra el historial completo de todas las evoluciones de tu mascota. |

### Botones

Al usar `/mascota` aparece un mensaje efímero
(solo visible para vos) con dos botones:

| Botón          | Comportamiento                                                                    |
| -------------- | --------------------------------------------------------------------------------- |
| **👁 Mostrar** | Publica la mascota en el canal visible para todos. Se desactiva después de usado. |
| **✖ Cerrar**  | Cierra el mensaje.                                                                |

El mensaje expira automáticamente a los **5 minutos** y los botones se
deshabilitan.

## Sistema de Puntos

### Cómo ganar puntos

Los puntos se acumulan automáticamente por actividad en Discord:

| Actividad | Puntos por evento |
| --------- | ----------------- |
| Mensaje   | 0.2               |
| Voz (VAD) | 0.1               |

Evolucionar una mascota cuesta **300 puntos** (la primera evolución es gratis, costo 0).

### Seed inicial

Todos los usuarios reciben **200 puntos gratis** la primera vez que consultan
su mascota (sin condición de MMR).

### Estructura de puntos

Los puntos se manejan en el userbot (`userbot/activity_db.py`, tabla
`pet_points`):

- `total_earned`: Puntos ganados en total.
- `spent`: Puntos gastados permanentemente.
- `available` = `total_earned - spent`.

## Evolución

### Primera evolución

La primera evolución **gasta** 0 puntos (generación gratis). Es el paso que
"crea" la mascota sobre la que se evoluciona.

### Evoluciones siguientes

Las evoluciones posteriores **cuestan** 300 puntos (se descuentan del total).

### Algoritmo

1. Se parte de la `seed` original (derivada del ID de Discord).
2. Para cada nivel de evolución, se genera una nueva seed:
   `new_seed = (original_seed * 6364136223 + level) & 0xFFFFFFFF`
3. Se incrementa `level` hasta que la suma de rareza de las partes de la nueva
   mascota sea **mayor** que la actual.
4. La evolución es **determinista**: mismo usuario + mismo nivel = misma mascota.

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
