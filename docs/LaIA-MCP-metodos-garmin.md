# LaIA MCP — Métodos de Garmin Connect: selección definitiva

Especificación de qué métodos de `python-garminconnect` (v0.3.15) usa el conector LaIA, agrupados en las herramientas con las que un entrenador conversa sobre planificación, análisis y ajuste de entrenos. Cada decisión está verificada contra una cuenta real: estructura de datos comprobada campo a campo, y el ciclo completo de escritura (crear, leer, agendar, desagendar, modificar, borrar) probado de principio a fin con un entreno desechable de resistencia (`RunningWorkout`). Ya no queda ninguna excepción: `upload_strength_workout` también ha pasado la prueba controlada (crear, releer campo a campo, agendar, desagendar y borrar con `StrengthWorkout`). El objetivo de potencia estructurado para los intervalos de rodillo y el fin de paso por botón de vuelta **sí están verificados** contra la cuenta real (ver más abajo). En carrera no se usa ningún objetivo con alerta sonora — el ritmo va en texto legible en el paso, nunca como `PACE_ZONE`.

De 153 métodos públicos de la librería, **44 se usan** (40 de lectura y escritura más los 4 `upload_*_workout`, uno por deporte). El resto queda fuera por un motivo concreto y verificado, no por descarte a ojo.

---

## 1. Las herramientas del MCP

El criterio de agrupación no es la familia técnica de cada método de Garmin, sino **el ritmo al que cambia cada dato** y **para qué tipo de pregunta hace falta**. Cinco herramientas de lectura y un grupo de escritura, más la configuración global y las utilidades de mantenimiento que se documentan al final de esta sección.

La identidad del deportista **no es un parámetro de ninguna tool**. Sale del token OAuth de la sesión (`get_access_token().subject`), resuelta en el servidor antes de ejecutar el cuerpo de la tool, exactamente como ya funciona hoy. Ninguna de las firmas de abajo lleva un identificador de usuario porque el modelo no debe controlarlo ni verlo: si viajara como argumento, una instrucción — malintencionada o simplemente ambigua — podría pedir el `user_id` de otro deportista y el servidor no tendría forma de detectar el engaño. El aislamiento de caché y de sesión de Garmin se hace igualmente por usuario, pero a partir del `subject` del token, nunca de un argumento de la llamada.

**Regla general de fallos parciales:** cada una de las cinco herramientas de lectura agrupa varias llamadas a Garmin. Si una de ellas falla (por ejemplo, el deportista no llevó el reloj esa noche y `get_body_battery` no devuelve nada) mientras las demás funcionan, la tool devuelve los datos que sí obtuvo más un campo `advertencias` listando qué no se pudo traer y por qué. Una tool no debe fallar entera por el fallo de una sola llamada interna, salvo que todas fallen.

### `estado(dias=7, spo2_detalle=False)` — cómo está el deportista hoy y esta semana

Primera llamada de cualquier conversación sobre si puede entrenar fuerte, cómo viene durmiendo, o cómo lleva la semana. Se refresca siempre, sin caché de larga duración.

Envuelve: `get_morning_training_readiness`, `get_body_battery`, `get_sleep_data` (del último día), `get_sleep_daily` (del rango), `get_stats`, `get_training_status`, `get_activities_by_date` (del rango, con carga y zonas de FC ya incluidas en cada actividad).

La tool no devuelve las respuestas crudas de Garmin. Lo que se descarta está declarado por **lista negra, no por lista blanca**: fuera las series punto a punto (sueño minuto a minuto, body battery intradía, SpO2 por época), los metadatos de subida del fichero (`metadataDTO`), el agregado por tramo (`splitSummaries`) y los datos del dueño de la cuenta. Se eligió quitar lo que sobra en vez de enumerar lo que se queda porque una lista blanca sobre 110+ campos por actividad es una forma silenciosa de perder un dato útil el día que Garmin añada uno. El resultado medido llega al mismo sitio: `estado(7)` pasó de 241 KB a 28 KB, `carga(10 días)` de 121 a 57 y `sesion()` de 28 a 21.

**Campos que la curación no puede perder, por venir anidados y ser fáciles de descartar por error al aplanar la respuesta:**
- De `get_training_status`: `mostRecentTrainingStatus.trainingStatusFeedbackPhrase` (la etiqueta PRODUCTIVE/PEAKING/OVERREACHING/DETRAINING) y `mostRecentTrainingStatus.trainingStatus` (su código numérico). Es un dato distinto del Training Readiness y del Endurance Score, y no tiene sustituto en ningún otro método de los seleccionados.
- De cada entrada de `get_sleep_daily`: el campo `spO2` (media de saturación de oxígeno de esa noche), además de `restingHeartRate`, `avgOvernightHrv`, `hrvStatus` y `bodyBatteryChange`, que ya se mencionan en el bloque 2 pero conviene remarcar aquí porque son justo los que se pierden si se aplana la lista solo pensando en sueño.

**Disparador para pedir detalle de SpO2:** implementado por dos vías. **Automática**: si el `spO2` medio de alguna noche del rango baja del 92%, `estado()` pide por su cuenta `get_spo2_data` de cada noche y lo anota en `advertencias` — ahí el mínimo deja de ser un adorno, porque una caída puntual puede no significar nada pero repetida sí. **Manual**: `spo2_detalle=True` lo fuerza aunque ninguna noche baje del umbral, para cuando la pregunta va explícitamente de saturación. Cuesta una llamada por día y por eso no se hace por rutina.

**Descripción para el servidor MCP:**
> Da el estado actual del deportista: preparación para entrenar hoy (`training readiness`), recuperación (sueño, HRV, body battery), estado de entrenamiento (ACWR, fase, reparto de carga) y las actividades de los últimos `dias` con su carga y reparto de intensidad ya calculado. Úsala siempre como primera llamada ante cualquier pregunta sobre si el deportista puede entrenar fuerte hoy, cómo ha dormido, o cómo lleva la semana. De aquí sale el `activity_id` que necesitan `sesion()` y las herramientas de escritura — no pidas el listado de actividades por otra vía.

### `capacidad(extras=None)` — de qué es capaz ahora mismo

Todo lo que cambia en semanas o meses: umbrales, zonas, FTP, VO2max, predicciones de carrera. Se pide una vez por conversación y se reutiliza durante toda ella; no tiene sentido volver a pedirla si ya se pidió hace diez minutos.

Envuelve: `get_lactate_threshold`, `get_cycling_ftp`, `get_heart_rate_zones`, `get_power_zones_for_sport` (una llamada por deporte relevante), `get_max_metrics_range`, `get_endurance_score`, `get_race_predictions`, `get_user_profile`.

Bajo demanda, pasando los nombres en la lista `extras`, solo si la pregunta lo pide explícitamente: `"ftp_progresion"` (`get_functional_threshold_power_range`, últimos 6 meses de carrera y bici), `"records"` (`get_personal_record`), `"hill_score"` (solo con desnivel relevante en la pregunta), `"edad_forma"` (`get_fitnessage_data`), `"peso"` (`get_body_composition`, 90 días) y `"dispositivo"`. Un nombre que no esté en esa lista se rechaza con un error que enumera los válidos, en vez de ignorarse en silencio.

`"dispositivo"` trae `get_devices` resumido a un puñado de flags legibles (`tolerancia_de_carrera`, `datos_solares`, `hrv`, `spo2_nocturno`, `zonas_de_potencia_bici`...) junto al nombre y el firmware del reloj — nunca los más de 250 campos crudos. Y sirve para lo que se pensó: en esa misma llamada, y **solo si el reloj declara soportarlos**, se piden además `get_running_tolerance` y `get_device_solar_data`; nunca a ciegas. No se cachea aparte con caducidad de semanas como se planteó: `capacidad()` entera ya se pide una sola vez por conversación, así que el ahorro sería teórico y la caché una pieza más que mantener.

**Descripción para el servidor MCP:**
> Da la capacidad física actual del deportista: umbrales de FC y ritmo, FTP, zonas de FC y potencia, tendencia de VO2max, endurance score y predicciones de carrera. No cambia de una conversación a otra: llámala una sola vez por conversación y reutiliza el resultado, no vuelvas a pedirla si ya la tienes. Pasa `extras=[...]` solo si la pregunta lo pide: `"peso"`, `"records"`, `"ftp_progresion"`, `"hill_score"`, `"edad_forma"`, o `"dispositivo"` si necesitas saber qué puede hacer el reloj antes de usar algo que dependa del hardware.

### `carga(inicio, fin)` — qué se ha entrenado en un rango

La llamada de la revisión de bloque o de mesociclo: volumen por disciplina, reparto de intensidad, progresión semanal.

Envuelve: `get_activities_by_date` (ya trae carga, zonas de FC, TSS e IF de bici, todo agregable sin llamadas extra), `get_activity` por cada sesión del rango para extraer RPE y feel (cacheado por `activity_id`: una sesión pasada no cambia), `get_weekly_stress`. Aplica los cálculos de la sección 4 (RPE normalizado, feel traducido, sRPE, tiempo suave real) antes de devolver.

**Descripción para el servidor MCP:**
> Analiza un rango de fechas: volumen por disciplina, reparto de intensidad (con la corrección de tiempo suave real, no el reparto crudo de zonas de Garmin), progresión semanal, y sRPE por sesión donde el deportista lo haya registrado. Úsala para revisiones de bloque o de mesociclo, no para preguntas sobre un solo día (para eso usa `estado()`) ni sobre una sola sesión (para eso usa `sesion()`).

### `sesion(activity_id, detalle=False, potencia_por_zona=False)` — qué pasó en una sesión concreta

Nunca se llama sin un `activity_id` previo, que sale siempre de `estado()` o de `plan()`.

Orden de llamada, con dependencia: primero `get_activity`, para saber el tipo de deporte y extraer RPE/feel. Con el tipo ya conocido, en paralelo `get_activity_splits`, `get_activity_gear`, y `get_activity_typed_splits` solo si el deporte es natación (`lap_swimming` o `open_water_swimming`). Con `detalle=True`, además `get_activity_details` (la serie punto a punto, solo cuando la pregunta es de deriva cardiaca o desacople). Con `potencia_por_zona=True` se añade `get_activity_power_in_timezones`, que solo tiene sentido en bici (en cualquier otro deporte se ignora en vez de fallar) y solo si se pregunta específicamente por ese reparto. Y después de saber qué material se usó —el uuid no se conoce hasta tener la respuesta de `get_activity_gear`— se piden sus kilómetros acumulados con `get_gear_stats`, que es lo que permite cruzar una molestia con unas zapatillas gastadas.

**Descripción para el servidor MCP:**
> Da el detalle de una sesión concreta a partir de su `activity_id` (nunca inventes un id ni se lo pidas al usuario como número: sácalo de una llamada previa a `estado()` o `plan()`). Incluye splits, RPE, feel y material usado. Pon `detalle=True` solo si la pregunta es sobre deriva cardiaca, desacople, o necesitas la curva segundo a segundo: por defecto no la pidas, es una respuesta mucho más pesada.

### `plan(inicio, fin, workout_id=None)` — qué hay agendado y con qué construirlo

Junta dos cosas que siempre hacen falta juntas: el calendario y la biblioteca de plantillas, para no crear una plantilla nueva cuando ya existe una parecida.

Envuelve: `get_scheduled_workouts`, `get_workouts`, `get_workout_by_id` (de una plantilla concreta, cuando se va a leer o modificar).

Cada plantilla de la biblioteca lleva además su origen (`workoutProvider`, `consumer`, tal como los devuelve `get_workouts`), para poder identificar plantillas que vienen de una integración o carga masiva anterior. Es información de solo lectura, sin riesgo: mostrar de dónde viene una plantilla no borra ni modifica nada.

**Descripción para el servidor MCP:**
> Da lo que hay agendado en el calendario del deportista en el rango indicado, junto con la biblioteca completa de plantillas de entreno disponibles, cada una con su origen. Llámala siempre antes de crear, modificar, agendar, desagendar o borrar un entreno — nunca escribas sin haber leído el estado actual del calendario y la biblioteca primero.

### Escritura — crear, modificar y agendar

Siempre después de leer con `plan()`, y siempre con confirmación explícita del usuario antes de ejecutar. Cualquier escritura invalida la caché de `plan()` para el deportista de la sesión actual.

- **`crear_entreno(sport, name, steps, agendar_fecha=None)`**:
  `upload_running_workout` / `upload_cycling_workout` /
  `upload_swimming_workout` / `upload_strength_workout`, según `sport`
  (`"running"`, `"cycling"`, `"swimming"`, `"strength"`). Con `agendar_fecha`
  (YYYY-MM-DD) además la deja puesta en el calendario en la misma llamada y
  devuelve el `scheduled_workout_id`, que es lo normal cuando el entreno es
  para un día concreto; sin ella se queda solo en la biblioteca.

  Cada paso es un dict con `kind` (`"warmup"`, `"cooldown"`, `"recovery"`,
  `"interval"`, `"repeat"` o `"strength_set"`) y **termina de una de tres
  formas**: `duration_seconds`, `distance_meters` o `lap_button: true`.

  **Carrera: fin de paso por botón de vuelta, sin objetivo con alerta sonora.**
  En las series de pista y las salidas de grupo el paso termina con
  `lap_button`, que se traduce a `ConditionType.LAP_BUTTON` (clave real
  `"lap.button"`, `conditionTypeId` 1, confirmada contra el catálogo). El
  deportista no quiere el pitido continuo de un objetivo de ritmo con alerta
  (`PACE_ZONE`): se guía por el ritmo medio de vuelta que ya ve en pantalla.
  El ritmo objetivo va como texto en el propio paso, con `text`, que se
  guarda en el campo `description` **del paso** (distinto del `description`
  de la plantilla entera). **Verificado**: ese campo existe, se sube y se
  relee intacto. Lo que **no** se puede verificar por API es si el reloj lo
  muestra en pantalla al arrancar el paso — eso es comportamiento del
  dispositivo y solo se confirma usándolo.

  **Rodillo: potencia estructurada.** Los intervalos de rodillo llevan
  objetivo de potencia con `power_watts` (centro) y `power_margin_watts`
  (margen): 220 W con margen 20 se traduce a `targetValueOne` 200 y
  `targetValueTwo` 240. Se calcula a mano en vez de referenciar la zona que
  el deportista tiene configurada, que es demasiado estrecha para un
  intervalo. **Verificado contra la cuenta real**, nombres de campo
  incluidos.

  **Y hay algo mejor que ensanchar el margen.** El catálogo no solo tiene
  `power.zone` (id 2, potencia instantánea) sino `power.3s` (10), `power.10s`
  (11) y `power.30s` (12): el mismo rango, pero comparado contra la potencia
  **promediada** a esos segundos. Eso ataca en origen el problema que
  motivaba el margen ancho —la oscilación de la lectura instantánea— en vez
  de compensarlo estirando los límites. El conector usa `power.3s` por
  defecto y deja elegir con `power_avg` (`"3s"`, `"10s"`, `"30s"` o
  `"instantanea"`); el margen sigue existiendo y es compatible con ambos.

  **Fuerza.** `strength_set` monta un bloque completo (series × (repeticiones
  + descanso)) con `create_strength_set`: el paso de ejercicio termina por
  `reps`, no por tiempo, y el de descanso es de tipo `rest`. **Verificado con
  la prueba controlada**, repetida con `StrengthWorkout`: se sube, se relee
  campo a campo, se agenda, se desagenda y se borra igual que el resto. La
  duración estimada se sube como 0 y Garmin la acepta sin problema — la
  fuerza se mide en repeticiones, no en tiempo. Ojo al peso, que tiene su
  propia trampa de unidades: ver la sección 4.

- **`modificar_entreno(workout_id, sport=None, name=None, steps=None)`**:
  `update_workout`. **Parte siempre de la estructura real que devuelve
  `get_workout_by_id`** y le aplica encima solo lo que se pase: renombrar es
  `modificar_entreno(id, name="...")`, sin reenviar los pasos. El `workoutId`
  no cambia, así que lo ya agendado no se rompe. Reconstruir la plantilla
  entera desde cero, como se hacía antes, obligaba a reenviarlo todo para
  cambiar una palabra y —peor— borraba en silencio cualquier cosa de la
  plantilla que el esquema de pasos no sepa expresar.

- **`agendar(workout_id, date)`**: `schedule_workout`.

- **`desagendar(scheduled_workout_ids=None, start_date=None, end_date=None,
  exclude_ids=None)`** y **`borrar_entreno(workout_ids=None, source=None)`**:
  `unschedule_workout` y `delete_workout`. Cada una tiene dos modos y **solo
  uno ejecuta**:
  - **Por ids** (`scheduled_workout_ids`, `workout_ids`): actúa, sobre esos
    y solo esos.
  - **Por filtro** (rango de fechas, u `source` para el origen de la
    plantilla): **no toca nada**. Devuelve la lista concreta de lo que
    coincide, con nombre y origen, para enseñársela al usuario. El segundo
    paso se hace pasando los ids de esa respuesta.

**Por qué el filtro no ejecuta.** Existe porque en la práctica se acumulan
plantillas huérfanas de una integración anterior (48 en el caso conocido) y
confirmar una por una para ese volumen es inviable — probablemente por eso se
acumularon sin limpiar. Pero un borrado masivo es irreversible, y en el
diálogo de aprobación lo que se ve es el filtro, no su alcance: "las de
Shape" pueden ser 3 o 38. Partiéndolo en dos, lo que se aprueba es la lista
concreta y no la regla que la genera.

Se implementó **sin el parámetro `confirmar=True`** que se planteó al
principio: la forma misma de la API ya obliga a los dos pasos (por filtro no
se puede borrar, punto), y un booleano de confirmación es justo lo que un
modelo aprende a poner siempre por costumbre, con lo que dejaría de proteger
nada. El filtro sigue teniendo que ser un campo concreto —origen o lista de
ids—, nunca una descripción ambigua tipo "las plantillas viejas"; si la
instrucción del usuario es vaga, la herramienta debe pedir que la concrete en
vez de inferirla.

**Descripción para el servidor MCP (escritura de un solo entreno):**
> Modifica de verdad el calendario o la biblioteca de entrenos del deportista
> en Garmin Connect. Antes de llamarla, confirma explícitamente con el usuario
> qué se va a crear, modificar, agendar, desagendar o borrar — nunca la
> ejecutes solo porque la conversación lo sugiere de forma ambigua. Llama
> siempre a `plan()` justo antes para leer el estado actual del calendario.

### Catálogos de Garmin: por qué al final no hay ninguno cargado en memoria

Se planteó cargar al arrancar el servicio dos catálogos que son iguales para
todo el mundo. Al implementarlo, ninguno de los dos acabó haciendo falta, y
conviene dejar escrito por qué para no volver a proponerlo:

| Catálogo | Qué se pensó | Qué pasó de verdad |
|---|---|---|
| Tipos de entreno (`connectapi("/workout-service/workout/types")`) | Sacar de ahí la clave real de cada condición de fin de paso y de cada tipo de objetivo, para no adivinarlas | Se pidió **una vez, durante el desarrollo**, y de ahí salieron los valores exactos que hoy están como constantes en `workout_builder.py`: `lap.button` es `conditionTypeId` 1, `power.zone` es `workoutTargetTypeId` 2, `power.3s` el 10. No se adivinó nada. Pedirlo en tiempo de ejecución añadiría una llamada de red a cada creación de entreno para releer constantes que no cambian. |
| `get_activity_types` | Traducir el `sportTypeId` que aparece en las respuestas | **Redundante**: cada actividad ya trae su `typeKey` legible inline (`"indoor_cycling"`, `"lap_swimming"`), tanto en `get_activities_by_date` como en `get_activity`. El catálogo traduciría a lo que ya viene puesto. Se llegó a implementar la caché y se retiró al comprobar que nadie la llamaba. |

Además, cargarlos "al arrancar el servicio" no era posible tal cual: pedir
cualquiera de los dos exige una sesión de Garmin autenticada, y al arrancar
el proceso todavía no hay ninguna — las sesiones son por deportista y
aparecen cuando alguien llama. Habría tenido que ser una caché perezosa de
proceso, no una carga de arranque.

### Nota sobre `list_workout_templates`

No es una utilidad aparte ni una tool: es una función interna de
`training_data.py` que pagina `get_workouts` y le añade a cada plantilla su
origen legible. La usa `plan()` para devolver la biblioteca, y el modo por
origen de `borrar_entreno` para resolver qué coincide con un filtro.

El origen merece una nota: `get_workouts` no trae el nombre legible, solo
`workoutProvider` (una clave corta) y `consumer` (un UUID). El nombre que se
enseña —`"prod_athletedata"`, `"Shape Calendar"`, `"Strava"`— vive en
`consumerName` y **solo aparece en `get_workout_by_id`**. Pedir el detalle de
cada plantilla sería una llamada por plantilla; en su lugar se pide una sola
vez por cada combinación distinta de `(workoutProvider, consumer)`, porque
todas las plantillas del mismo proveedor comparten consumer. Las creadas a
mano o por el propio conector caen en la combinación `(None, None)` y salen
con `source: null` — lo que además las hace inalcanzables por el modo de
borrado por origen, que exige un valor concreto.

## 2. Métodos incluidos, por qué

### Preparación y recuperación (dentro de `estado()`)

| Método | Por qué entra |
|---|---|
| `get_morning_training_readiness` | Es el único número que ya fusiona sueño, HRV, carga reciente y estrés en un solo score, con el desglose de qué factor pesa más. Es la lectura del despertar, comparable entre días. |
| `get_body_battery` | El mínimo diario dice si el deportista está recargando entre sesiones o no, más allá de si duerme las horas. |
| `get_sleep_data` (último día) | Trae score, fases y, además, `avgOvernightHrv` y `restingHeartRate` de esa noche, sin necesidad de cruzarlo con otra llamada. |
| `get_sleep_daily` (rango) | Cada día del rango trae sueño, FC en reposo y HRV en una sola llamada — sustituye a pedir HRV y FC en reposo por separado para la vista semanal. |
| `get_stats` | Barato y agregado: FC en reposo, estrés medio, body battery del día, y la media móvil de 7 días de FC en reposo ya calculada por Garmin. |
| `get_training_status` | La llamada que más decisiones cambia: fase de entrenamiento, ACWR, y el reparto de carga mensual entre aeróbico bajo, aeróbico alto y anaeróbico frente al objetivo. Se lee por el deporte principal del dispositivo, no por las tres disciplinas por separado. |
| `get_weekly_stress` | Una llamada trae hasta 52 semanas; distingue una mala racha puntual de una deriva de fondo. |
| `get_spo2_data(cdate)` *(condicional)* | El mínimo de saturación de oxígeno de una noche concreta, más fino que la media que ya trae `get_sleep_daily`. Se pide solo cuando la media de un día baja de un umbral o el usuario pregunta explícitamente por desaturación — una caída puntual puede ser irrelevante, pero si se repite varias noches apunta a algo que merece revisión médica, no solo a "ha dormido peor". No existe versión de rango: para ver si es recurrente hay que pedirlo día a día sobre los días sospechosos, cacheando cada uno (un día pasado no cambia). |

### Capacidad

| Método | Por qué entra |
|---|---|
| `get_lactate_threshold` | FC y ritmo de umbral medidos, la referencia real para prescribir intensidad. |
| `get_cycling_ftp` | Los rodillos de entre semana se prescriben sobre este número. |
| `get_heart_rate_zones` | Las zonas configuradas por perfil deportivo — evita hablar de zonas que el reloj del deportista no muestra. |
| `get_power_zones_for_sport(sport)` | Igual, para potencia, filtrado por deporte para no arrastrar perfiles que no tocan. |
| `get_max_metrics_range` | La tendencia de VO2max en carrera y bici, no solo el valor puntual. Responde a si está mejorando o solo acumulando horas. |
| `get_endurance_score` | Capacidad de sostener esfuerzo prolongado, relevante para distancias medias y largas. |
| `get_race_predictions` | Control de ritmo antes de una competición. |
| `get_user_profile` | Sexo, peso, altura y fecha de nacimiento (la edad se deriva de ahí, no viene calculada). Ningún otro método la trae. |
| `get_functional_threshold_power_range` *(bajo demanda)* | Progresión de FTP, útil en revisión de bloque para ver si un mesociclo de bici sirvió. |
| `get_personal_record` *(bajo demanda)* | Récords guardados; requiere una tabla propia de traducción de `typeId`, porque Garmin no envía la etiqueta. |
| `get_hill_score` *(bajo demanda, solo con desnivel relevante)* | Capacidad en terreno con subidas. |
| `get_fitnessage_data` *(bajo demanda)* | Edad de forma física con su desglose de componentes; se deriva del VO2max pero añade contexto de progreso. |
| `get_devices` *(bajo demanda, `capacidad(extras=["dispositivo"])`)* | Más de 250 campos de capacidad del dispositivo, resumidos a un puñado de flags legibles. Sirve para saber qué puede y qué no puede el reloj de cada deportista antes de intentar leer algo que depende del hardware, como `get_running_tolerance` — que se pide en esa misma llamada solo si el reloj lo declara. |

### Actividades (dentro de `carga()` y `sesion()`)

| Método | Por qué entra |
|---|---|
| `get_activities_by_date` | Trae más de 110 campos por actividad: carga, tiempo en zona de FC (`hrTimeInZone_1` a `_5`), y en bici además potencia media, normalizada, TSS e IF ya calculados. Es la columna vertebral de todo análisis de carga sin necesidad de bajar a detalle por sesión. |
| `get_activity` | El único sitio donde viven `directWorkoutRpe` y `directWorkoutFeel`, el esfuerzo percibido y la sensación que el deportista registra al acabar. |
| `get_activity_splits` | En los entrenos de grupo las series se marcan a mano con el botón de vuelta, así que las vueltas son la única forma de reconstruir qué se hizo de verdad. Cada vuelta trae además potencia, cadencia, tiempo de contacto y temperatura. |
| `get_activity_typed_splits` *(solo natación)* | Largo a largo con SWOLF; sin esto una sesión de nado es una caja negra de metros y pulsaciones. |
| `get_activity_gear` | Qué material se usó en esa sesión concreta, para cruzarlo con molestias o sobrecargas. |
| `get_activity_details` *(bajo demanda, `detalle=True`)* | La serie punto a punto. Solo cuando la pregunta es de deriva cardiaca o desacople; nunca por defecto, porque puede traer hasta 2000 puntos. |
| `get_activity_power_in_timezones` *(bajo demanda, solo bici)* | Reparto de potencia por zona en una sesión concreta; no viene en el listado agregado como sí viene el de FC. |

### Material

| Método | Por qué entra |
|---|---|
| `get_activity_gear` | Ya listado arriba. |
| `get_gear_stats` | Kilómetros acumulados de la zapatilla o bici usada. `sesion()` lo pide siempre que la sesión tenga material asignado, en un segundo paso: el uuid del material no se conoce hasta tener la respuesta de `get_activity_gear`. |

### Entrenamientos estructurados y calendario

| Método | Por qué entra |
|---|---|
| `get_workouts` | Ver la biblioteca antes de crear, para no duplicar plantillas casi idénticas. |
| `get_workout_by_id` | La estructura completa de una plantilla, necesaria para leerla, clonarla o pasársela modificada a `update_workout`. |
| `get_scheduled_workouts` | Lo agendado del mes, cruzado con lo realmente entrenado. |
| `upload_running_workout` / `upload_cycling_workout` / `upload_swimming_workout` | Creación tipada y validada por deporte. El paso que termina por botón de vuelta (`ConditionType.LAP_BUTTON`, clave real `"lap.button"`, confirmada contra el catálogo oficial de Garmin) se construye con estos mismos modelos, sin necesidad de mandar JSON crudo — se usa en las series de carrera y salidas de grupo, nunca en rodillo. Los intervalos de rodillo, en cambio, llevan objetivo de potencia estructurado (`TargetType.POWER_ZONE`) con el rango ensanchado a mano alrededor del vatiaje objetivo, no la zona ya configurada del deportista, para evitar pitidos por la inestabilidad de la lectura de potencia a corto plazo. El objetivo de potencia está verificado contra la cuenta real, igual que el botón de vuelta: se creó una plantilla con ambos, se releyó campo a campo y se borró. En carrera no se usa ningún objetivo con alerta sonora: el ritmo se deja en texto legible en el propio paso. |
| `upload_strength_workout` | Mismo mecanismo, con pasos de series y repeticiones (`create_strength_exercise_step`, `create_strength_set`) en vez de tiempo o distancia. No forma parte del plan semanal actual, pero se mantiene por si entra trabajo de fuerza específica (por ejemplo, para el sóleo). **Verificado con la prueba controlada de escritura**, repetida con `StrengthWorkout`: series, repeticiones (el paso termina por `reps`, no por tiempo), descanso, categoría y nombre del ejercicio se suben y releen intactos, y el ciclo completo de agendar/desagendar/borrar funciona igual que con los otros deportes. Dos cosas que salieron de esa prueba: la duración estimada se sube como 0 y Garmin la acepta sin problema (la fuerza se mide en repeticiones, no en tiempo), y el peso tiene su propia trampa de unidades — ver la sección 4. |
| `update_workout` | Reemplaza la plantilla conservando el `workoutId`, así que lo ya agendado no se rompe. |
| `delete_workout` | Limpieza de biblioteca. |
| `schedule_workout` | Coloca una plantilla en una fecha. |
| `unschedule_workout` | Quita la instancia agendada sin borrar la plantilla. |

### Resto

| Método | Por qué entra |
|---|---|
| `get_body_composition` *(bajo demanda, `capacidad(extras=["peso"])`)* | Peso de los últimos 90 días, relevante para potencia relativa e impacto por zancada. |
| `get_device_last_used` *(automático, solo si hay advertencias)* | Explica por qué falta un dato: si el reloj no se llevó puesto o no ha sincronizado, no hay HRV, y distinguir eso de un HRV realmente malo cambia la lectura entera. `estado()` lo añade solo cuando ya hay algo que explicar, no en cada llamada. |

---

## 3. Métodos que quedan fuera, por qué

**Duplicados exactos de otro método ya seleccionado.** `get_stress_data` y `get_all_day_stress` (mismo endpoint que `get_stats`, dos nombres). `get_user_summary` y `get_stats_and_body` (idénticos a `get_stats`). `get_rhr_day` y `get_rhr_daily` (ya vienen dentro de `get_sleep_daily`/`get_sleep_data`). `get_max_metrics` (el valor del día ya está dentro de `get_training_status`). `get_power_zones` (más ruidoso que `get_power_zones_for_sport`, que da un único dict limpio por deporte). `get_activity_hr_in_timezones` (ya viene en `get_activities_by_date`). `get_activities`, `get_activities_fordate`, `get_last_activity` (mismo esquema que `get_activities_by_date`, sin aportar nada).

**Curvas completas del día que no cambian ninguna decisión de entrenamiento.** `get_heart_rates`, `get_respiration_data`, `get_hydration_data`, pasos, plantas, minutos de intensidad, calorías diarias. `get_spo2_data` ya no está en este grupo: pasó a condicional dentro de `estado()` — ver el bloque de preparación y recuperación en la sección 2.

**Diagnóstico de qué pasó, no de qué hacer.** `get_body_battery_events` (dice qué actividad gastó batería; ya se sabe qué se entrenó por la lista de actividades). `get_activity_weather` (la temperatura ya viene por vuelta en los splits). `get_activity_split_summaries` (agregado calculable desde los splits).

**Uso interno de la librería, no herramientas de conversación.** `count_activities` (solo para comprobar que la sesión de Garmin funciona), `get_gear`, `get_gear_defaults`, `get_gear_activities` (exploración de material, no hace falta como herramienta expuesta: la asignación por defecto ya la gestiona el propio Garmin). `get_activity_types` no está descartado: se usa, pero como configuración de cuenta pedida una vez al arrancar el servicio, no como parte de ninguna de las cinco herramientas — ver la sección "Configuración de cuenta" más arriba.

**Sin datos de entrenamiento reales en la cuenta, confirmado contra la cuenta real, no por descarte de categoría.** Golf, salud femenina, embarazo, nutrición, tensión arterial, retos y badges (y estos últimos, además, con un error de fábrica en la propia librería al paginar con `start=0`).

**Escritura sin valor de coaching o con riesgo innecesario.** `set_activity_name`/`_description`/`_type`, `delete_activity`, todo el bloque de subir/descargar ficheros (`upload_activity`, `import_activity`, `download_activity`, `create_manual_activity`), `push_workout_to_device` (empuja al reloj sin fecha; se pierde al sincronizar), `download_workout`.

**Solo lectura de planes de Garmin Coach, sin poder crearlos ni modificarlos por API.** `get_training_plans`, `get_training_plan_by_id`, `get_adaptive_training_plan_by_id`. Además, tener un plan de Garmin Coach activo en paralelo mandaría al reloj instrucciones que contradicen las del entrenador.

**No existe en esta versión de la librería.** `get_next_scheduled_workout` da error de atributo. Si hace falta "la próxima sesión sin especificar mes", se calcula filtrando `get_scheduled_workouts` del mes actual y el siguiente por fecha.

**Depende del dispositivo, no de la cuenta.** `get_running_tolerance` y `get_device_solar_data` dependen de que el reloj soporte esas funciones. **Implementado**: `capacidad(extras=["dispositivo"])` mira primero los flags del reloj y solo entonces los pide, nunca a ciegas. En el Forerunner 965 de la cuenta de prueba ninguno de los dos está declarado, así que no se piden.

---

## 4. Cálculos que el conector debe aplicar antes de entregar los datos

Ninguno de estos existe ya calculado en la respuesta de Garmin; son transformaciones que tiene que hacer LaIA.

Las trampas de unidades de esta tabla se verificaron por **coherencia entre campos** contra la cuenta real, no por suposición: si `distancia / duración` coincide con la velocidad media, las tres unidades quedan validadas a la vez. Importan más que un dato que falte, porque no fallan — devuelven un número plausible y equivocado, y un modelo usándolo no tiene forma de detectarlo. Comprobadas y correctas sin necesidad de conversión: distancias en metros, duraciones y `hrTimeInZone_*` en segundos (la suma de las cinco zonas nunca supera la duración), zonas de FC en bpm y de potencia en vatios, `weekly_stress` y body battery en 0-100, training effect en 0-5, y `beginTimestamp` como epoch en milisegundos.

| Cálculo | Cómo | Por qué |
|---|---|---|
| **RPE normalizado** | `directWorkoutRpe / 10` | Garmin lo guarda multiplicado por diez. Devolver `null` si el campo no viene, nunca `0`. |
| **Feel traducido** | `directWorkoutFeel` → etiqueta: `0` muy flojo, `25` flojo, `50` normal, `75` fuerte, `100` muy fuerte | Solo llega como número; la etiqueta hace legible la respuesta sin que el entrenador tenga que memorizar la escala. |
| **sRPE (carga por esfuerzo percibido)** | RPE normalizado × minutos de la sesión | Es la única carga fiable en natación, donde la FC en el agua no sirve: en las sesiones de nado revisadas, menos del 5% del tiempo caía en alguna zona de FC. |
| **Tiempo suave real** | `duración − (suma de las 5 zonas de FC) + zona1 + zona2` | La suma de las cinco zonas nunca llega a la duración total: todo lo que queda por debajo del suelo de zona 1 no se registra en ninguna. Sin esta corrección, el porcentaje de trabajo suave sale falseado a la baja. |
| **Edad** | Calculada desde `birthDate` de `get_user_profile` | No viene como campo directo. |
| **Velocidad de umbral en min/km** | `1000 / (speed * 10) / 60` sobre el `speed` de `get_lactate_threshold` | **El `speed` no viene en m/s, sino en m/s ÷ 10** (decámetros por segundo). Verificado contra la cuenta real: 0.36944 son 3,69 m/s ≈ 4:31 min/km; interpretarlo como m/s da 45 min/km, un número absurdo que además no falla, solo miente. Confirmado con la codificación que usa la propia Garmin en los targets de ritmo de un entreno (0.274 ↔ 6:05/km, 0.2545 ↔ 6:33/km). Mismo caso en `userData.lactateThresholdSpeed`. |
| **Traducción de `typeId` de récords personales** | Diccionario propio, construido a mano | `prTypeLabelKey` viene vacío; Garmin no envía la etiqueta de qué tipo de récord es cada uno. |
| **Normalización del identificador de instancia agendada** | Un único nombre interno, por ejemplo `scheduled_workout_id` | El mismo dato se llama `workoutScheduleId` en la respuesta de `schedule_workout` y `id` dentro de cada `calendarItem` de `get_scheduled_workouts`. Sin normalizar, el conector puede buscar la clave equivocada según de qué endpoint venga. |
| **Peso en kg** | `userData.weight / 1000` | **Viene en gramos**: 67000 son 67 kg. Verificado contra la cuenta real. |
| **Ritmo: dos números, no uno** | `duración / distancia` (total) y `1000 / averageSpeed` (en movimiento), devueltos los dos con nombre explícito | **`averageSpeed` se calcula sobre `movingDuration`, no sobre `duration`.** En carrera y bici coinciden y da igual; en natación no, porque los descansos entre series no cuentan como movimiento: 1300 m en 2234 s de reloj con 1225 s nadando son 2:51/100m o 1:34/100m según cuál se use. Devolver solo uno hace que quien lea la respuesta elija sin saber que está eligiendo. |
| **Sueño necesario** | Se deja en minutos, pero con el nombre marcado (`sueno_necesario_min`) | **`sleepNeed` viene en minutos (540 = 9h) mientras que los tiempos por fase de la misma respuesta (`deepTime`, `lightTime`, `remTime`) vienen en segundos.** Dos unidades distintas en el mismo dict. |
| **Tiempo total de sueño** | Ninguna: solo saber qué incluye | `sleepTimeSeconds` **excluye el tiempo despierto**: profundo + ligero + REM cuadra exacto con el total; sumarle el despierto, no. |
| **Predicciones de carrera** | Añadir el min/km que implica cada distancia | `time5K`, `time10K`, `timeHalfMarathon` y `timeMarathon` son **segundos pelados** (`time5K: 1302` son 21:42, a 4:20/km). Sin convertir no dicen nada. |
| **Peso de fuerza en kg** | `weightValue / 1000`, añadido como `weight_kg` junto al original | **Garmin guarda `weightValue` en gramos pero lo etiqueta con unidad "kilogram"**: 20 kg se guardan como `20000.0` con `unitKey: "kilogram"`. Leído tal cual son 20 toneladas. Verificado subiendo y releyendo una plantilla real. |
| **Traducción de `feedbackPhrase`** | Diccionario propio para códigos como `HRV_BALANCED_5`, `ANAEROBIC_SHORTAGE`, `MAINTAINING_2` | Llegan como cadenas con sufijos numéricos sin traducir; son interpretables por contexto pero una traducción evita adivinar. |

---

## 5. Protocolo de llamadas por tipo de conversación

| Conversación | Llamadas, en orden | Total |
|---|---|---|
| "¿Qué hago hoy?" | `estado(7)` → `plan(hoy, +3 días)` | 2 |
| "Planifica la semana" | `estado(14)` → `capacidad()` → `plan(semana)` | 3 |
| "Analiza la sesión de ayer" | `estado(7)` para sacar el `activity_id` → `sesion(activity_id)` | 2 |
| "¿Por qué se me fue la FC en la tirada larga?" | `estado(7)` → `sesion(activity_id, detalle=True)` | 2 |
| "Ponme el entreno del jueves" | `plan(semana)` → `crear_entreno(..., agendar_fecha="jueves")` | 2 |
| "Cambia el entreno del jueves" | `plan(semana, workout_id=...)` → `modificar_entreno(id, steps=[...])` | 2 |
| "Muévelo al viernes" | `plan(semana)` → `desagendar(scheduled_workout_ids=[...])` → `agendar(workout_id, viernes)` | 3 |
| "Limpia las plantillas de Shape" | `plan(mes)` → `borrar_entreno(source="Shape")` *(solo lista)* → confirmar → `borrar_entreno(workout_ids=[...])` | 3 |
| "Revisa el bloque / planifica el mesociclo" | `estado(28)` → `capacidad()` → `carga(8 semanas)` → `plan(mes)` | 4 |
| "¿Cómo voy de forma?" | `capacidad()` → `carga(12 semanas)` | 2 |

Reglas fijas: `capacidad()` no se repite dentro de una misma conversación. `sesion()` nunca se llama sin un `activity_id` obtenido antes. La serie punto a punto (`detalle=True`) solo se pide cuando la pregunta es de deriva o desacople. Ninguna escritura se hace sin haber leído antes `plan()`, y cualquier escritura invalida lo leído para esa fecha: si se encadena otro cambio en el mismo rango, hay que volver a llamar a `plan()`.

Todas estas reglas viven en el docstring de cada tool, no en este documento: son lo único que ve un modelo que use el conector sin haber leído nada más. Este documento explica el porqué; las descripciones de las tools son las que se cumplen.

