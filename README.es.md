# AzuraCast para Home Assistant

[![CI](https://github.com/NaturalDevCR/ha-azuracast/actions/workflows/ci.yml/badge.svg)](https://github.com/NaturalDevCR/ha-azuracast/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

Lleva a Home Assistant todas las estaciones de una instancia de [AzuraCast](https://www.azuracast.com/): lo que suena, los oyentes, los DJ en vivo, las playlists, la cola, saltar canciones y **selectores por tags** que cambian grupos de playlists con una sola acción.

[English](README.md)

## Funciones

- **Un dispositivo por estación** con un reproductor multimedia (lo que suena, carátula, progreso, saltar), sensores (oyentes, canción actual, playlist actual, siguiente canción, locutor en vivo) y sensores binarios (en línea, DJ en vivo).
- **Tiempo real.** Los cambios de canción llegan por el stream de alto rendimiento de lo que suena en AzuraCast. Si está apagado, la integración consulta cada 30 segundos y te indica cómo activarlo.
- **Carátulas a través de Home Assistant.** Home Assistant descarga la carátula y se la entrega a tu navegador con su proxy multimedia, así que funciona con instancias solo accesibles en la red local y con certificados autofirmados, y respeta la opción **Verificar el certificado SSL**.
- **Playlists como interruptores**, con su ID, tipo, orden, peso, programación y tags como atributos.
- Botones para **saltar canción** y **vaciar cola**, además de acciones para saltar, vaciar la cola, activar o desactivar varias playlists a la vez y rebarajar una playlist.
- **Selectores por tags.** Escribe `ha:<selector>=<valor>` en la descripción de una playlist, por ejemplo `ha:ambiente=chill`. Cada estación obtiene un `select` con todos los valores que encuentra. Al elegir un valor se activan las playlists de ese valor, se desactivan las demás del mismo selector y se vacía la cola, así que el cambio se oye cuando termina la canción actual. Las programaciones de AzuraCast siguen decidiendo cuál de ellas suena a cada hora.
- **Eventos** para automatizaciones: `azuracast_song_changed` y `azuracast_selector_applied`.
- Funciona **sin API key** (datos públicos, solo lectura) o con una (gestión). Las estaciones que la key no puede gestionar quedan en solo lectura.

## Requisitos

- Home Assistant 2026.10 o más reciente.
- AzuraCast 0.23 o más reciente.

## Instalación

### HACS

1. En HACS, abre ⋮ → **Custom repositories** y agrega `https://github.com/NaturalDevCR/ha-azuracast` como **Integration**.
2. Descarga **AzuraCast** y reinicia Home Assistant.

### Manual

Copia `custom_components/azuracast` en la carpeta `config/custom_components/` de tu Home Assistant y reinicia.

## Configuración

**Ajustes → Dispositivos y servicios → Agregar integración → AzuraCast**.

| Campo | Descripción |
|---|---|
| URL | Dirección base de la instancia, por ejemplo `https://radio.example.com`. |
| API key | Opcional. Créala en AzuraCast en **Mi cuenta → API Keys**. |
| Verificar el certificado SSL | Desactívalo solo para certificados autofirmados. También se aplica a la descarga de carátulas. |

**Usa un usuario de AzuraCast dedicado a Home Assistant.** Las API keys de AzuraCast tienen todos los permisos del usuario que las creó. Dale a ese usuario permisos solo sobre las estaciones que Home Assistant debe gestionar.

El usuario necesita estos permisos de estación en AzuraCast:

- **Manage Station Media** para los interruptores de playlists, los selectores y las acciones de playlists.
- **Manage Station Broadcasting** para la cola y para saltar canciones: los botones de saltar canción y vaciar la cola, el salto en el reproductor multimedia y las acciones `skip_song` y `clear_queue`.

Una estación con Media pero sin Broadcasting conserva sus interruptores y selectores, pero no tiene controles para saltar canciones ni vaciar la cola, y al elegir un valor de selector se cambian las playlists sin vaciar la cola. Los cambios de permisos se aplican al recargar la integración.

La URL, la API key y la opción de SSL se pueden cambiar después con **Reconfigurar**. Si AzuraCast deja de aceptar la key (por ejemplo, porque se eliminó), Home Assistant te pide una nueva. AzuraCast responde a una key rechazada con "403 Forbidden", igual que a un permiso que falta, así que la integración comprueba la key con el endpoint de la cuenta actual para distinguirlos: una key que sigue funcionando pero perdió sus permisos sobre una estación genera un aviso de reparación.

### Opciones

- **Estaciones a ignorar.** No tendrán dispositivo ni entidades.
- **Mantener la cola al cambiar un selector.** Por defecto se vacía la cola para que el cambio ocurra después de la canción actual.
- **Aceptar valores de selector sin nada programado ahora.** Por defecto se rechaza un valor si ninguna de sus playlists puede sonar en este momento, para que la estación nunca quede en silencio.
- **Intervalo de actualización de playlists** (30–300 s, 60 por defecto).

Las tres primeras opciones son listas de estaciones: las estaciones que no están en la lista, incluidas las que se agreguen después, tienen el comportamiento por defecto.

Para un mejor comportamiento en tiempo real, activa **Use High-Performance Now Playing Updates** en AzuraCast en **Administración → Configuración del sistema**.

## Ejemplo de selectores por tags

Música ambiental con tres ambientes que cambian por bloque horario:

| Playlist | Programación (en AzuraCast) | Descripción |
|---|---|---|
| `tarde-chill` | 12:00–18:00 | `ha:ambiente=chill` |
| `tarde-lounge` | 12:00–18:00 | `ha:ambiente=lounge` |
| `noche-chill` | 18:00–02:00 | `ha:ambiente=chill` |
| `noche-fiesta` | 18:00–02:00 | `ha:ambiente=fiesta` |

La estación obtiene `select.<station>_ambiente` con `chill`, `fiesta` y `lounge`. Un botón de pared puede ejecutar:

```yaml
action: select.select_option
target:
  entity_id: select.ambientes_ambiente
data:
  option: chill
```

A las 18:00, AzuraCast pasa de `tarde-chill` a `noche-chill` por sí solo. Elegir `fiesta` a las 15:00 se rechaza, porque a esa hora no hay ninguna playlist `fiesta` programada. Mientras un cambio espera a que termine la canción actual, el atributo `pending_value` del select contiene el nuevo valor. Úsalo para hacer parpadear el LED de un botón.

### Cómo se comportan los selectores

- Los tags se escriben en la descripción de la playlist como palabras separadas por espacios. Los nombres de selector usan `a-z`, `0-9` y `_`; los valores también admiten `-`. No se distinguen mayúsculas de minúsculas.
- Al elegir un valor se activan todas las playlists de ese valor y se desactivan las playlists de los demás valores del mismo selector. Las playlists sin un tag de ese selector nunca se tocan.
- **Volver a elegir el valor actual nunca vacía la cola ni se rechaza.** Solo vuelve a activar las playlists de ese valor que estuvieran desactivadas (por ejemplo, a mano), para que el ambiente continúe en el siguiente bloque horario.
- Un valor sin nada que pueda sonar en este momento se rechaza, salvo que la estación esté en **Aceptar valores de selector sin nada programado ahora**.
- El select muestra un valor solo cuando exactamente un valor tiene playlists activadas. Si no, no muestra ningún valor y el atributo `reason` indica por qué: `none_enabled` o `mixed`.
- Atributos: `available_now` (valores que pueden sonar ahora), `pending_value`, `playlists` (las playlists de cada valor) y `reason`. `pending_value` se borra cuando empieza una canción del nuevo valor, o después de 15 minutos.
- Una playlist con dos valores del mismo selector queda fuera de ese selector, y un aviso de reparación la menciona.

### Programaciones

Si una playlist "puede sonar ahora" se decide con la misma lógica de programación de AzuraCast, en la zona horaria de la estación:

- Una playlist sin programaciones siempre puede sonar.
- Una franja que cruza la medianoche (por ejemplo 18:00–02:00) pertenece al día en que empieza, para los días de la semana y las fechas de inicio y fin.
- Una programación cuya hora de inicio y de fin son iguales es la franja de "reproducir una vez" de AzuraCast: cuenta como activa durante 15 minutos desde su hora de inicio.

## Entidades

| Entidad | Requiere API key |
|---|---|
| `media_player.<station>` | Solo para saltar |
| `sensor.<station>_listeners`, `_current_song`, `_current_playlist`, `_next_song`, `_live_streamer` | No |
| `binary_sensor.<station>_online`, `_live_dj` | No |
| `switch.<station>_playlist_<name>` | Sí |
| `button.<station>_skip_song`, `_clear_queue` | Sí |
| `select.<station>_<selector>` | Sí |

## Acciones

| Acción | Campos |
|---|---|
| `azuracast.skip_song` | target |
| `azuracast.clear_queue` | target |
| `azuracast.set_playlists_enabled` | target, `playlists` (nombres o IDs), `enabled` |
| `azuracast.reshuffle_playlist` | target, `playlist` (nombre o ID) |

```yaml
action: azuracast.set_playlists_enabled
target:
  device_id: <station device>
data:
  playlists:
    - tarde-chill
    - "42"
  enabled: false
```

- **Destinos.** Elige dispositivos de estación, entidades de AzuraCast de una estación, o áreas y etiquetas que los contengan. Solo se actúa sobre dispositivos de estación: el dispositivo de la instancia de AzuraCast se ignora, y una llamada que no apunta a ninguna estación se rechaza.
- **Estaciones de solo lectura.** Si un destino incluye una estación que la API key no puede gestionar, se rechaza toda la llamada y no se cambia nada.
- **Referencias a playlists.** Un nombre exacto de playlist (distingue mayúsculas de minúsculas) tiene prioridad sobre un ID numérico: una playlist que se llama literalmente `42` gana sobre la playlist con ID 42. Si varias playlists comparten un nombre, la llamada se rechaza; referéncialas por su ID. Todas las referencias se comprueban en cada estación de destino antes de cambiar nada.

## Eventos

- `azuracast_song_changed`: `station_id`, `station_shortcode`, `song`, `playlist`, `tags`.
- `azuracast_selector_applied`: `station_id`, `selector`, `value`, `previous_value`, `cleared_queue`. No se dispara cuando se vuelve a elegir el valor actual.

## Cómo se actualizan los datos

Los datos de lo que suena llegan por el stream; consultar cada 30 s es solo un respaldo. Las playlists y la cola se consultan cada 60 s (configurable) y se actualizan justo después de cada acción. AzuraCast es la fuente de verdad: los cambios hechos en su interfaz web aparecen en la siguiente actualización.

## Limitaciones conocidas

- Las estaciones que no son públicas aparecen solo si la API key puede verlas.
- Las estaciones nuevas requieren recargar la integración. Las playlists y los selectores nuevos aparecen solos.
- Los cambios se oyen al terminar la canción actual, no al instante.
- Los bloques horarios de un mismo valor de selector no deberían solaparse. Todavía no se ha verificado cómo elige AzuraCast entre playlists programadas que se solapan.
- Una playlist con dos valores del mismo selector queda fuera de ese selector, y un aviso de reparación la menciona.

## Solución de problemas

- **Los cambios de canción llegan tarde:** revisa el aviso de reparación sobre las actualizaciones en tiempo real y activa las actualizaciones de alto rendimiento de lo que suena.
- **Una estación no tiene interruptores ni select:** la API key no tiene permisos sobre ella. Revisa el aviso de reparación o el diagnóstico.
- **Se rechaza un valor de selector:** ninguna de sus playlists está programada ahora. Revisa las programaciones en AzuraCast o permítelo en las opciones.
- **Una acción dice que el nombre de una playlist es ambiguo:** varias playlists comparten ese nombre. Usa el ID numérico de la playlist.

Descarga el diagnóstico desde el menú ⋮ de la integración. La URL y la key aparecen ocultas.

## Desinstalación

**Ajustes → Dispositivos y servicios → AzuraCast → ⋮ → Eliminar**, y luego quítala de HACS si la instalaste desde ahí.
