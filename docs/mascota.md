# Sistema de Mascotas (`/mascota`)

## Overview

El comando `/mascota` permite a cada usuario tener una
**mascota procedural ASCII** única y determinista. Cada mascota se genera a
partir del ID de Discord del usuario, garantizando que siempre se obtenga la
misma criatura.

Las mascotas tienen rarezas (común, raro, épico, legendario), estadísticas
aleatorias, y pueden evolucionar a formas más poderosas.

## Comando

```
/mascota [accion: ver | mostrar]
```

Por defecto (sin argumento) muestra la mascota del usuario.

### Acciones

| Acción      | Descripción                                                           |
| ----------- | --------------------------------------------------------------------- |
| **ver**     | Muestra/créa tu mascota con botones para evolucionar, historial, etc. |
| **mostrar** | Publica tu mascota directamente en el canal.                          |

### Botones

Al usar `/mascota` (acción `ver` por defecto) aparece un mensaje efímero
(solo visible para vos) con estos botones:

| Botón              | Comportamiento                                                                      |
| ------------------ | ----------------------------------------------------------------------------------- |
| **👁 Mostrar**     | Publica la mascota en el canal visible para todos. Se desactiva después de usado.   |
| **⬆ Evolucionar** | Evoluciona la mascota (cuesta 300 puntos). Actualiza el mensaje con la nueva forma. |
| **📜 Historial**   | Muestra el historial completo de evoluciones en un mensaje efímero aparte.          |
| **✖ Cerrar**      | Cierra el mensaje.                                                                  |

El mensaje expira automáticamente a los **5 minutos** y los botones se
deshabilitan.

## Sistema de Puntos

### Cómo ganar puntos

Los puntos se acumulan automáticamente por actividad en Discord:

| Actividad | Puntos por evento |
| --------- | ----------------- |
| Mensaje   | 0.2               |
| Voz (VAD) | 0.1               |

Evolucionar una mascota cuesta **300 puntos**. La mascota se créa automáticamente
la primera vez que usás `/mascota`.

### Seed inicial

Todos los usuarios reciben **200 puntos gratis** al crear su primera
mascota.

### Estructura de puntos

Los puntos se manejan en el userbot (`userbot/activity_db.py`, tabla
`pet_points`):

- `total_earned`: Puntos ganados en total.
- `spent`: Puntos gastados permanentemente.
- `available` = `total_earned - spent`.

## Evolución

### Costo

Evolucionar cuesta **300 puntos** (se descuentan del total).

### Algoritmo

1. Se parte de la `seed` original (derivada del ID de Discord).
2. Para cada nivel de evolución, se genera una nueva seed:
   `new_seed = (original_seed * 6364136223 + level) & 0xFFFFFFFF`
3. Se incrementa `level` hasta que la suma de rareza de las partes de la nueva
   mascota sea **mayor** que la actual.
4. La evolución es **determinista**: mismo usuario + mismo nivel = misma mascota.

## Persistencia

Las mascotas se guardan en `data/pets.json` (JSON plano), separado de la
base de datos SQLite del userbot donde están MMR, pet_points, etc.

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

- `petGenerator`: creación y evolución de mascotas.
- `bot.py`: cada invocación de `/mascota` (acción, usuario, rareza, nivel).
- Logs visibles en `journalctl -u discord-bot -f` o en `bot.log`.

## Testing

```bash
make check  # corre pytest
```

Los tests del sistema de puntos están en `tests/test_activity_db.py`.
