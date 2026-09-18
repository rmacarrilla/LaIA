# LaIA MCP — Métodos de Garmin Connect: selección definitiva

Especificación de qué métodos de `python-garminconnect` (v0.3.15) usa el conector LaIA, agrupados en las herramientas con las que un entrenador conversa sobre planificación, análisis y ajuste de entrenos. Cada decisión está verificada contra una cuenta real: estructura de datos comprobada campo a campo, y el ciclo completo de escritura (crear, leer, agendar, desagendar, modificar, borrar) probado de principio a fin con un entreno desechable de resistencia (`RunningWorkout`). Hay dos excepciones señaladas explícitamente, ambas en la sección de entrenamientos estructurados: `upload_strength_workout`, incluido por completitud pero sin pasar esa misma prueba; y el objetivo de potencia estructurado para los intervalos de rodillo (`TargetType.POWER_ZONE`, con el rango ensanchado a mano alrededor del vatiaje objetivo), necesario y sin verificar todavía contra la cuenta real. En carrera no se usa ningún objetivo con alerta sonora — el ritmo va en texto legible en el paso, nunca como `PACE_ZONE`.

De 153 métodos públicos de la librería, **33 se usan**. El resto queda fuera por un motivo concreto y verificado, no por descarte a ojo.

---

## 1. Las herramientas del MCP

El criterio de agrupación no es la familia técnica de cada método de Garmin, sino **el ritmo al que cambia cada dato** y **para qué tipo de pregunta hace falta**. Cinco herramientas de lectura y un grupo de escritura, más la configuración global y las utilidades de mantenimiento que se documentan al final de esta sección.

La identidad del deportista **no es un parámetro de ninguna tool**. Sale del token OAuth de la sesión (`get_access_token().subject`), resuelta en el servidor antes de ejecutar el cuerpo de la tool, exactamente como ya funciona hoy. Ninguna de las firmas de abajo lleva un identificador de usuario porque el modelo no debe controlarlo ni verlo: si viajara como argumento, una instrucción — malintencionada o simplemente ambigua — podría pedir el `user_id` de otro deportista y el servidor no tendría forma de detectar el engaño. El aislamiento de caché y de sesión de Garmin se hace igualmente por usuario, pero a partir del `subject` del token, nunca de un argumento de la llamada.

**Regla general de fallos parciales:** cada una de las cinco herramientas de lectura agrupa varias llamadas a Garmin. Si una de ellas falla (por ejemplo, el deportista no llevó el reloj esa noche y `get_body_battery` no devuelve nada) mientras las demás funcionan, la tool devuelve los datos que sí obtuvo más un campo `advertencias` listando qué no se pudo traer y por qué. Una tool no debe fallar entera por el fallo de una sola llamada interna, salvo que todas fallen.

### `estado(dias=7)` — cómo está el deportista hoy y esta semana

Primera llamada de cualquier conversación sobre si puede entrenar fuerte, cómo viene durmiendo, o cómo lleva la semana. Se refresca siempre, sin caché de larga duración.

Envuelve: `get_morning_training_readiness`, `get_body_battery`, `get_sleep_data` (del último día), `get_sleep_daily` (del rango), `get_stats`, `get_training_status`, `get_activities_by_date` (del rango, con carga y zonas de FC ya incluidas en cada actividad).

La tool no devuelve el JSON crudo de `get_activities_by_date` (110+ campos por actividad): de cada actividad solo pasan a la respuesta los campos que las herramientas 2 a 6 de este documento usan (carga, zonas de FC, RPE si ya está cacheado, tipo de deporte, duración, distancia). El resto se descarta antes de responder.

**Campos que la curación no puede perder, por venir anidados y ser fáciles de descartar por error al aplanar la respuesta:**
- De `get_training_status`: `mostRecentTrainingStatus.trainingStatusFeedbackPhrase` (la etiqueta PRODUCTIVE/PEAKING/OVERREACHING/DETRAINING) y `mostRecentTrainingStatus.trainingStatus` (su código numérico). Es un dato distinto del Training Readiness y del Endurance Score, y no tiene sustituto en ningún otro método de los seleccionados.
- De cada entrada de `get_sleep_daily`: el campo `spO2` (media de saturación de oxígeno de esa noche), además de `restingHeartRate`, `avgOvernightHrv`, `hrvStatus` y `bodyBatteryChange`, que ya se mencionan en el bloque 2 pero conviene remarcar aquí porque son justo los que se pierden si se aplana la lista solo pensando en sueño.

**Disparador para pedir detalle de SpO2:** si el `spO2` medio de algún día de `get_sleep_daily` baja de un umbral (por ejemplo, 92%) o el usuario pregunta explícitamente por saturación de oxígeno o desaturación nocturna, `estado()` llama además a `get_spo2_data(cdate)` para ese día concreto — ver la entrada correspondiente en el bloque 2, ahora condicional en vez de descartada.

**Descripción para el servidor MCP:**
> Da el estado actual del deportista: preparación para entrenar hoy (`training readiness`), recuperación (sueño, HRV, body battery), estado de entrenamiento (ACWR, fase, reparto de carga) y las actividades de los últimos `dias` con su carga y reparto de intensidad ya calculado. Úsala siempre como primera llamada ante cualquier pregunta sobre si el deportista puede entrenar fuerte hoy, cómo ha dormido, o cómo lleva la semana. De aquí sale el `activity_id` que necesitan `sesion()` y las herramientas de escritura — no pidas el listado de actividades por otra vía.

### `capacidad(incluir_dispositivo=False)` — de qué es capaz ahora mismo

Todo lo que cambia en semanas o meses: umbrales, zonas, FTP, VO2max, predicciones de carrera. Se pide una vez por conversación y se reutiliza durante toda ella; no tiene sentido volver a pedirla si ya se pidió hace diez minutos.

Envuelve: `get_lactate_threshold`, `get_cycling_ftp`, `get_heart_rate_zones`, `get_power_zones_for_sport` (una llamada por deporte relevante), `get_max_metrics_range`, `get_endurance_score`, `get_race_predictions`, `get_user_profile`.

Bajo demanda dentro de esta misma herramienta, solo si la pregunta lo pide explícitamente: `get_functional_threshold_power_range` (progresión de FTP), `get_personal_record` (récords), `get_hill_score` (solo con desnivel relevante en la pregunta), `get_fitnessage_data` (solo si se pregunta por la "edad de forma física").

Con `incluir_dispositivo=True`, además `get_devices`, filtrado a un puñado de campos derivados (por ejemplo `running_tolerance_capable`, `solar_capable`) antes de devolverlo — nunca los más de 250 campos crudos. Se usa antes de decidir si tiene sentido llamar a `get_running_tolerance` para ese deportista en concreto. Este dato cambia solo si el deportista cambia de reloj, así que conviene cachearlo por separado con una caducidad mucho más larga (semanas) que el resto de `capacidad()`, para que pedir el flag no fuerce refrescar todo lo demás.

**Descripción para el servidor MCP:**
> Da la capacidad física actual del deportista: umbrales de FC y ritmo, FTP, zonas de FC y potencia, tendencia de VO2max, endurance score y predicciones de carrera. No cambia de una conversación a otra: llámala una sola vez por conversación y reutiliza el resultado, no vuelvas a pedirla si ya la tienes. Pasa `incluir_dispositivo=True` solo si necesitas saber qué puede hacer el reloj del deportista antes de usar una función que depende del hardware.

### `carga(inicio, fin)` — qué se ha entrenado en un rango

La llamada de la revisión de bloque o de mesociclo: volumen por disciplina, reparto de intensidad, progresión semanal.

Envuelve: `get_activities_by_date` (ya trae carga, zonas de FC, TSS e IF de bici, todo agregable sin llamadas extra), `get_activity` por cada sesión del rango para extraer RPE y feel (cacheado por `activity_id`: una sesión pasada no cambia), `get_weekly_stress`. Aplica los cálculos de la sección 4 (RPE normalizado, feel traducido, sRPE, tiempo suave real) antes de devolver.

**Descripción para el servidor MCP:**
> Analiza un rango de fechas: volumen por disciplina, reparto de intensidad (con la corrección de tiempo suave real, no el reparto crudo de zonas de Garmin), progresión semanal, y sRPE por sesión donde el deportista lo haya registrado. Úsala para revisiones de bloque o de mesociclo, no para preguntas sobre un solo día (para eso usa `estado()`) ni sobre una sola sesión (para eso usa `sesion()`).

### `sesion(activity_id, detalle=False)` — qué pasó en una sesión concreta

Nunca se llama sin un `activity_id` previo, que sale siempre de `estado()` o de `plan()`.

Orden de llamada, con dependencia: primero `get_activity`, para saber el tipo de deporte y extraer RPE/feel. Con el tipo ya conocido, en paralelo `get_activity_splits`, `get_activity_gear`, y `get_activity_typed_splits` solo si el deporte es natación (`lap_swimming` o `open_water_swimming`). Con `detalle=True`, además `get_activity_details` (la serie punto a punto, solo cuando la pregunta es de deriva cardiaca o desacople). Con `get_activity_power_in_timezones` solo si el deporte es de bici y se pregunta específicamente por el reparto de potencia.

**Descripción para el servidor MCP:**
> Da el detalle de una sesión concreta a partir de su `activity_id` (nunca inventes un id ni se lo pidas al usuario como número: sácalo de una llamada previa a `estado()` o `plan()`). Incluye splits, RPE, feel y material usado. Pon `detalle=True` solo si la pregunta es sobre deriva cardiaca, desacople, o necesitas la curva segundo a segundo: por defecto no la pidas, es una respuesta mucho más pesada.

### `plan(inicio, fin)` — qué hay agendado y con qué construirlo

Junta dos cosas que siempre hacen falta juntas: el calendario y la biblioteca de plantillas, para no crear una plantilla nueva cuando ya existe una parecida.

Envuelve: `get_scheduled_workouts`, `get_workouts`, `get_workout_by_id` (de una plantilla concreta, cuando se va a leer o modificar).

Cada plantilla de la biblioteca lleva además su origen (`workoutProvider`, `consumer`, tal como los devuelve `get_workouts`), para poder identificar plantillas que vienen de una integración o carga masiva anterior. Es información de solo lectura, sin riesgo: mostrar de dónde viene una plantilla no borra ni modifica nada.

**Descripción para el servidor MCP:**
> Da lo que hay agendado en el calendario del deportista en el rango indicado, junto con la biblioteca completa de plantillas de entreno disponibles, cada una con su origen. Llámala siempre antes de crear, modificar, agendar, desagendar o borrar un entreno — nunca escribas sin haber leído el estado actual del calendario y la biblioteca primero.

### Escritura — crear, modificar y agendar

Siempre después de leer con `plan()`, y siempre con confirmación explícita del usuario antes de ejecutar. Cualquier escritura invalida la caché de `plan()` para el deportista de la sesión actual.

- **`crear_entreno(deporte, nombre, pasos, agendar_fecha=None)`**: `upload_running_workout` / `upload_cycling_workout` / `upload_swimming_workout` / `upload_strength_workout`, según el deporte. Cada paso de resistencia se construye con el modelo tipado de la librería (`ExecutableStep`). Los pasos de fuerza usan los helpers propios de la librería (`create_strength_exercise_step`, `create_strength_rest_step`, `create_strength_set`), con series y repeticiones en vez de tiempo o distancia. **Fuerza no tiene día fijo en el plan semanal actual, pero se mantiene como opción**, útil por ejemplo para trabajo específico de fortalecimiento mientras dure la sobrecarga de sóleo. A diferencia de los otros tres deportes, `upload_strength_workout` no ha pasado por la prueba controlada de escritura: esa prueba se hizo con `RunningWorkout`. Antes de usarlo en producción, hay que repetirla cambiando `RunningWorkout` por `StrengthWorkout`.

  **Carrera: fin de paso por botón de vuelta, sin objetivo con alerta sonora.** En las series de pista y las salidas de grupo, el paso termina con `ConditionType.LAP_BUTTON` (clave real `"lap.button"`, confirmada). El deportista no quiere el pitido continuo de un objetivo de ritmo con alerta (`PACE_ZONE`): se guía por el ritmo medio de vuelta que ya ve en pantalla. Lo único que hace falta es que el ritmo objetivo quede legible en el propio paso —el texto que el reloj muestra al empezar el intervalo, cuando suena el pitido de cambio de paso— para saber a qué ritmo correr sin depender de una alerta. **Sin verificar**: hay que confirmar si `ExecutableStep` admite un campo de texto por paso (no el `description` de la plantilla entera, que ya sabemos que es libre, sino uno específico del paso) que Garmin muestre en el reloj al iniciar ese paso. Si no existe ese campo, la alternativa es dejar el nombre del paso lo bastante descriptivo (por ejemplo, "Intervalo 1 — 4:30/km") y confiar en que el reloj lo enseñe al arrancarlo.

  **Rodillo: potencia estructurada, con margen ensanchado a mano, no la zona configurada en Garmin.** Los intervalos de rodillo sí llevan `TargetType.POWER_ZONE`, con `targetValueOne` (mínimo) y `targetValueTwo` (máximo). La zona de potencia que ya tiene el deportista configurada en `get_power_zones_for_sport` es demasiado estrecha para esto: la lectura de potencia a 3-10 segundos oscila, y un margen ajustado provoca pitidos constantes aunque la media del intervalo esté bien. El rango se calcula como el vatiaje objetivo en el centro, ensanchado por un margen que decide el entrenador para ese intervalo (por ejemplo, objetivo 220 W con margen de 20 W → rango 200-240 W), no como referencia a una zona ya guardada. **Sin verificar contra la cuenta real**: falta confirmar con la prueba controlada de escritura que `targetValueOne`/`targetValueTwo` son los nombres correctos y que Garmin acepta un rango calculado a mano en vez de una zona configurada.

  Este mismo mecanismo de potencia estructurada no se ha pedido para las salidas de bici en carretera (domingo): por ahora se trata como carrera, con el objetivo en texto, salvo que en algún momento se pida explícitamente lo contrario.
- **`modificar_entreno(workout_id, cambios)`**: `update_workout`, mandando siempre la estructura completa devuelta por `get_workout_by_id` con el cambio aplicado. El `workoutId` no cambia, así que lo ya agendado no se rompe.
- **`agendar(workout_id, fecha)`**: `schedule_workout`.
- **`desagendar(scheduled_workout_id)`** (uno) **o `desagendar_en_bloque(filtro)`** (varios): `unschedule_workout` repetido sobre el conjunto que resulte del filtro.
- **`borrar_entreno(workout_id)`** (uno) **o `borrar_entreno_en_bloque(filtro)`** (varios): `delete_workout` repetido sobre el conjunto que resulte del filtro.

**Patrón obligatorio de dos pasos para las variantes en bloque.** Existen porque en la práctica se han acumulado plantillas huérfanas de una integración o carga anterior (48 en el caso conocido), y pedir confirmación una por una para ese volumen es inviable — es probablemente la razón por la que se acumularon sin limpiar. Pero un borrado o desagendado masivo sigue siendo una acción irreversible, así que nunca se ejecuta en una sola llamada a partir de una instrucción de conversación:
1. Primero se resuelve el filtro (por `workoutProvider`, `consumer`, o una lista explícita de ids) contra `plan()` y se devuelve la lista exacta de plantillas o instancias que coinciden, con su nombre y origen, sin tocar nada todavía.
2. Solo una segunda llamada explícita, referida a ese mismo conjunto ya mostrado (por ejemplo, pasando los ids exactos de la respuesta anterior, no el filtro de nuevo), ejecuta el borrado o el desagendado.

El filtro tiene que ser un campo concreto (origen, o una lista de ids), nunca una descripción ambigua tipo "las plantillas viejas". Si la instrucción del usuario es ambigua, la herramienta de escritura no debe inferir el filtro por su cuenta: se le pide al usuario que lo concrete antes de pasar al paso 1.

**Descripción para el servidor MCP (las cinco herramientas de un solo entreno comparten esta base, adaptando el verbo):**
> Modifica de verdad el calendario o la biblioteca de entrenos del deportista en Garmin Connect. Antes de llamarla, confirma explícitamente con el usuario qué se va a crear, modificar, agendar, desagendar o borrar — nunca la ejecutes solo porque la conversación lo sugiere de forma ambigua. Llama siempre a `plan()` justo antes para leer el estado actual del calendario.

**Descripción para el servidor MCP (variantes en bloque):**
> Desagenda o borra varias plantillas o instancias a la vez, identificadas por un filtro explícito (origen o lista de ids), nunca por una descripción vaga. Se usa en dos pasos: primero pide sin `confirmar=True` para ver el listado exacto de lo que coincide con el filtro, sin tocar nada; solo cuando el usuario haya confirmado esa lista concreta, repite la llamada con `confirmar=True` y los mismos ids para ejecutar. No la uses nunca en un solo paso a partir de una instrucción ambigua como "borra las plantillas viejas".

### Configuración global — se carga una vez al arrancar el servicio, no depende del deportista

Dos catálogos de Garmin que no cambian según quién pregunta, así que no hace falta guardarlos por usuario ni en una tabla de base de datos: basta con cargarlos una vez en memoria cuando arranca el proceso (un valor cacheado a nivel de servicio, no de deportista) y que las herramientas lean ese valor ya cargado.

| Método | Cuándo se pide | Para qué lo usan las herramientas |
|---|---|---|
| `get_activity_types` | Una vez, al arrancar el servicio | Traducir el `sportTypeId` o `typeKey` que aparece en las respuestas de `estado()`, `carga()` y `sesion()`, sin tener que llamarlo cada vez. |
| Catálogo de tipos de entreno (`connectapi("/workout-service/workout/types")`) | Una vez, al arrancar el servicio | De ahí sale la clave de texto real de cada condición de fin de paso (la del botón de vuelta es `conditionTypeKey: "lap.button"`, confirmada contra la cuenta real) y de cada tipo de objetivo. Sin este catálogo cargado, `crear_entreno` no puede construir el paso de botón de vuelta sin adivinar la clave. |

Ninguno de los dos está descartado: los dos se usan, solo que no forman parte del cuerpo de ninguna de las cinco herramientas de conversación ni dependen de qué deportista pregunta, así que no necesitan tabla ni caché por usuario — con un singleton en memoria del propio servicio basta.

### Nota sobre `list_workout_templates`

`list_workout_templates` ya no es una utilidad aparte: la lectura (filtrar por `workoutProvider` o `consumer`) está integrada en `plan()`, como se describe más arriba, porque es información de solo lectura sin riesgo. La parte de esta lógica que sí requería una salvaguarda — el borrado o desagendado en bloque a partir de ese filtro — está resuelta en `desagendar_en_bloque` y `borrar_entreno_en_bloque`, con el patrón obligatorio de dos pasos descrito en la sección de escritura. No queda ninguna pieza de esta lógica fuera del MCP ni sin la confirmación explícita que exige cualquier acción irreversible.

---

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
| `get_devices` *(bajo demanda, `capacidad(incluir_dispositivo=True)`)* | Más de 250 campos de capacidad del dispositivo, filtrados a un puñado de campos derivados. Sirve para saber qué puede y qué no puede el reloj concreto de cada deportista antes de intentar leer algo que depende del hardware, como `get_running_tolerance`. |

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
| `get_gear_stats` *(bajo demanda)* | Kilómetros acumulados de una zapatilla o bici concreta, para cruzar con sobrecargas. |

### Entrenamientos estructurados y calendario

| Método | Por qué entra |
|---|---|
| `get_workouts` | Ver la biblioteca antes de crear, para no duplicar plantillas casi idénticas. |
| `get_workout_by_id` | La estructura completa de una plantilla, necesaria para leerla, clonarla o pasársela modificada a `update_workout`. |
| `get_scheduled_workouts` | Lo agendado del mes, cruzado con lo realmente entrenado. |
| `upload_running_workout` / `upload_cycling_workout` / `upload_swimming_workout` | Creación tipada y validada por deporte. El paso que termina por botón de vuelta (`ConditionType.LAP_BUTTON`, clave real `"lap.button"`, confirmada contra el catálogo oficial de Garmin) se construye con estos mismos modelos, sin necesidad de mandar JSON crudo — se usa en las series de carrera y salidas de grupo, nunca en rodillo. Los intervalos de rodillo, en cambio, llevan objetivo de potencia estructurado (`TargetType.POWER_ZONE`) con el rango ensanchado a mano alrededor del vatiaje objetivo, no la zona ya configurada del deportista, para evitar pitidos por la inestabilidad de la lectura de potencia a corto plazo. **El objetivo de potencia no está verificado contra la cuenta real**, a diferencia del botón de vuelta: falta confirmar los nombres de campo con una prueba controlada. En carrera no se usa ningún objetivo con alerta sonora: el ritmo se deja en texto legible en el propio paso. |
| `upload_strength_workout` | Mismo mecanismo, con pasos de series y repeticiones (`create_strength_exercise_step`, `create_strength_set`) en vez de tiempo o distancia. No forma parte del plan semanal actual, pero se mantiene por si entra trabajo de fuerza específica (por ejemplo, para el sóleo). **Sin verificar con la prueba controlada de escritura** — hay que repetirla con `StrengthWorkout` antes de usarlo en producción. |
| `update_workout` | Reemplaza la plantilla conservando el `workoutId`, así que lo ya agendado no se rompe. |
| `delete_workout` | Limpieza de biblioteca. |
| `schedule_workout` | Coloca una plantilla en una fecha. |
| `unschedule_workout` | Quita la instancia agendada sin borrar la plantilla. |

### Resto

| Método | Por qué entra |
|---|---|
| `get_body_composition` *(bajo demanda, consulta semanal)* | Peso a lo largo del tiempo, relevante para potencia relativa e impacto por zancada. |
| `get_device_last_used` *(bajo demanda, diagnóstico)* | Explica por qué falta un dato de un día concreto: si el reloj no se llevó por la noche, no hay HRV, y conviene distinguir eso de un HRV realmente malo. |

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

**Depende del dispositivo, no de la cuenta.** `get_running_tolerance` y `get_device_solar_data` dependen de que el reloj del deportista soporte esas funciones. Se llaman condicionalmente según lo que declare `capacidad(incluir_dispositivo=True)` (ver sección 1), nunca a ciegas.

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
| **Traducción de `feedbackPhrase`** | Diccionario propio para códigos como `HRV_BALANCED_5`, `ANAEROBIC_SHORTAGE`, `MAINTAINING_2` | Llegan como cadenas con sufijos numéricos sin traducir; son interpretables por contexto pero una traducción evita adivinar. |

---

## 5. Protocolo de llamadas por tipo de conversación

| Conversación | Llamadas, en orden | Total |
|---|---|---|
| "¿Qué hago hoy?" | `estado(7)` → `plan(hoy, +3 días)` | 2 |
| "Planifica la semana" | `estado(14)` → `capacidad()` → `plan(semana)` | 3 |
| "Analiza la sesión de ayer" | `estado(7)` para sacar el `activity_id` → `sesion(activity_id)` | 2 |
| "¿Por qué se me fue la FC en la tirada larga?" | `estado(7)` → `sesion(activity_id, detalle=True)` | 2 |
| "Cambia el entreno del jueves" | `plan(semana)` → `desagendar` → `modificar_entreno` o `crear_entreno` → `agendar` | 4 |
| "Revisa el bloque / planifica el mesociclo" | `estado(28)` → `capacidad()` → `carga(8 semanas)` → `plan(mes)` | 4 |
| "¿Cómo voy de forma?" | `capacidad()` → `carga(12 semanas)` | 2 |

Reglas fijas: `capacidad()` no se repite dentro de una misma conversación. `sesion()` nunca se llama sin un `activity_id` obtenido antes. La serie punto a punto (`detalle=True`) solo se pide cuando la pregunta es de deriva o desacople. Ninguna escritura se hace sin haber leído antes `plan()`, y cualquier escritura invalida la caché de `plan()` para esa fecha.

