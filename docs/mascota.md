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
| **mostrar** | Publica tu mascota directamente en el canal (con imagen animada GIF). |

### Botones

Al usar `/mascota` (acción `ver` por defecto) aparece un mensaje efímero
(solo visible para vos) que incluye la imagen de tu mascota y estos botones:

| Botón              | Comportamiento                                                                      |
| ------------------ | ----------------------------------------------------------------------------------- |
| **👁 Mostrar**     | Publica el GIF animado de la mascota en el canal visible para todos. Se desactiva.  |
| **⬆ Evolucionar** | Evoluciona la mascota (cuesta 300 puntos). Actualiza el mensaje con la nueva forma. |
| **⬇ Revertir**    | Revierte la mascota a su forma anterior (recupera los 300 puntos).                  |
| **📜 Historial**   | Muestra el historial completo de evoluciones en un mensaje efímero aparte.          |
| **✖ Cerrar**      | Cierra el mensaje.                                                                  |

El mensaje expira automáticamente a los **60 segundos** y los botones se
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

### Decaimiento por inactividad

Si un usuario no tiene actividad en `last_activity_at` por más de 24 horas,
pierde 10 puntos de mascota por día de inactividad en adelante. El
decaimiento se calcula al momento de earn contra `last_activity_at`.

### Estructura de puntos

Los puntos se manejan en el userbot (`userbot/activity_db.py`, tabla
`pet_points`):

- `total_earned`: Puntos ganados en total.
- `spent`: Puntos gastados permanentemente.
- `reserved`: Puntos reservados (por evolución en curso).
- `available` = `total_earned - spent - reserved`.

## Evolución

### Costo

Evolucionar cuesta **300 puntos** (se reservan, no se gastan).
Revertir libera la reserva.

### Algoritmo

1. Se parte de la `seed` original (derivada del ID de Discord).
2. Para cada nivel de evolución, se genera una nueva semilla derivada:
   `new_seed = (original_seed * 6364136223 + level) & 0xFFFFFFFF`
3. La evolución es **conservativa**: el cuerpo y estructura general de la mascota se mantienen. Solo se selecciona **una parte al azar** (cabeza, base, ojos, etc.) y se la reemplaza por otra del mismo tipo pero de rareza estrictamente superior. Además, aumentan un poco los stats base linealmente.
4. La evolución es **determinista**: mismo usuario + mismo nivel = misma evolución exacta.

## Persistencia

Las mascotas se guardan en `data/pets.json` (JSON plano), separado de la
base de datos SQLite del userbot donde están MMR, pet_points, etc.

## Renderizado de imágenes

El bot genera imágenes animadas (GIF) o estáticas (PNG) a partir del texto ASCII de la mascota usando un renderizador JavaScript con `node-canvas`. El **accesorio** se renderiza de forma estática por encima del nombre, mientras que el cuerpo base se centra y se anima automáticamente (respiración, parpadeo, flotación).

### Dependencias

- **Node.js** v18+ (instalado en el servidor para bgutil-pot-provider).
- `npm install canvas gifencoder` en `pet-renderer/`.

### Arquitectura

```
bot.py (Python)
  └─ subprocess → node pet-renderer/render-cli.js [--gif]
                    ├─ petRenderer.js   (canvas → PNG/GIF)
                    └─ asciiAnimator.js (frames animados)
  └─ Discord File → canal
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
