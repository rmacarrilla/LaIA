# LaIA MCP — Métodos de Garmin Connect: selección definitiva

Especificación de qué métodos de `python-garminconnect` (v0.3.15) usa el conector LaIA, agrupados en las herramientas con las que un entrenador conversa sobre planificación, análisis y ajuste de entrenos. Cada decisión está verificada contra una cuenta real: estructura de datos comprobada campo a campo, y el ciclo completo de escritura (crear, leer, agendar, desagendar, modificar, borrar) probado de principio a fin con un entreno desechable.

De 153 métodos públicos de la librería, **31 se usan**. El resto queda fuera por un motivo concreto y verificado, no por descarte a ojo.

---

## 1. Las herramientas del MCP

El criterio de agrupación no es la familia técnica de cada método de Garmin, sino **el ritmo al que cambia cada dato** y **para qué tipo de pregunta hace falta**. Cinco herramientas de lectura y un grupo de escritura.

### `estado(dias=7)` — cómo está el deportista hoy y esta semana

Primera llamada de cualquier conversación sobre si puede entrenar fuerte, cómo viene durmiendo, o cómo lleva la semana. Se refresca siempre, sin caché de larga duración.

Envuelve: `get_morning_training_readiness`, `get_body_battery`, `get_sleep_data` (del último día), `get_sleep_daily` (del rango), `get_stats`, `get_training_status`, `get_activities_by_date` (del rango, con carga y zonas de FC ya incluidas en cada actividad).

### `capacidad()` — de qué es capaz ahora mismo

Todo lo que cambia en semanas o meses: umbrales, zonas, FTP, VO2max, predicciones de carrera. Se pide una vez por conversación y se reutiliza durante toda ella; no tiene sentido volver a pedirla si ya se pidió hace diez minutos.

Envuelve: `get_lactate_threshold`, `get_cycling_ftp`, `get_heart_rate_zones`, `get_power_zones_for_sport` (una llamada por deporte relevante), `get_max_metrics_range`, `get_endurance_score`, `get_race_predictions`, `get_user_profile`.

Bajo demanda dentro de esta misma herramienta, solo si la pregunta lo pide explícitamente: `get_functional_threshold_power_range` (progresión de FTP), `get_personal_record` (récords), `get_hill_score` (solo con desnivel relevante en la pregunta), `get_fitnessage_data` (solo si se pregunta por la "edad de forma física").

### `carga(inicio, fin)` — qué se ha entrenado en un rango

La llamada de la revisión de bloque o de mesociclo: volumen por disciplina, reparto de intensidad, progresión semanal.

Envuelve: `get_activities_by_date` (ya trae carga, zonas de FC, TSS e IF de bici, todo agregable sin llamadas extra), `get_activity` por cada sesión del rango para extraer RPE y feel (cacheado por `activity_id`: una sesión pasada no cambia), `get_weekly_stress`.

### `sesion(activity_id, detalle=False)` — qué pasó en una sesión concreta

Nunca se llama sin un `activity_id` previo, que sale siempre de `estado()` o de `plan()`.

Envuelve: `get_activity`, `get_activity_splits`, `get_activity_typed_splits` (solo si la sesión es de natación), `get_activity_gear`. Con `detalle=True`: además `get_activity_details` (la serie punto a punto, solo cuando la pregunta es de deriva cardiaca o desacople). Con `get_activity_power_in_timezones` solo si la sesión es de bici y se pregunta específicamente por el reparto de potencia.

### `plan(inicio, fin)` — qué hay agendado y con qué construirlo

Junta dos cosas que siempre hacen falta juntas: el calendario y la biblioteca de plantillas, para no crear una plantilla nueva cuando ya existe una parecida.

Envuelve: `get_scheduled_workouts`, `get_workouts`, `get_workout_by_id` (de una plantilla concreta, cuando se va a leer o modificar).

### Escritura — crear, modificar y agendar

Siempre después de leer con `plan()`, y siempre con tu confirmación antes de ejecutar.

- **`crear_entreno`**: `upload_running_workout` / `upload_cycling_workout` / `upload_swimming_workout`, según el deporte. Cada paso se construye con el modelo tipado de la librería (`ExecutableStep`), incluyendo el paso que termina por botón de vuelta cuando la sesión es de grupo.
- **`modificar_entreno`**: `update_workout`, mandando siempre la estructura completa devuelta por `get_workout_by_id` con el cambio aplicado. El `workoutId` no cambia, así que lo ya agendado no se rompe.
- **`agendar`**: `schedule_workout`.
- **`desagendar`**: `unschedule_workout`.
- **`borrar_entreno`**: `delete_workout`.

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
| `get_devices` *(una vez, al conectar la cuenta, no en cada conversación)* | Más de 250 campos de capacidad del dispositivo. Sirve para saber qué puede y qué no puede el reloj concreto de cada deportista — por ejemplo, si `runningToleranceCapable` es `false`, no tiene sentido intentar leer `get_running_tolerance`. |

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
| `upload_running_workout` / `upload_cycling_workout` / `upload_swimming_workout` | Creación tipada y validada por deporte. El paso que termina por botón de vuelta (`ConditionType.LAP_BUTTON`, clave real `"lap.button"`, confirmada contra el catálogo oficial de Garmin) se construye con estos mismos modelos, sin necesidad de mandar JSON crudo. |
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

**Curvas completas del día que no cambian ninguna decisión de entrenamiento.** `get_heart_rates`, `get_spo2_data`, `get_respiration_data`, `get_hydration_data`, pasos, plantas, minutos de intensidad, calorías diarias.

**Diagnóstico de qué pasó, no de qué hacer.** `get_body_battery_events` (dice qué actividad gastó batería; ya se sabe qué se entrenó por la lista de actividades). `get_activity_weather` (la temperatura ya viene por vuelta en los splits). `get_activity_split_summaries` (agregado calculable desde los splits).

**Uso interno de la librería, no herramientas de conversación.** `get_activity_types` (catálogo estático para traducir `sportTypeId`), `count_activities` (solo para comprobar que la sesión de Garmin funciona), `get_gear`, `get_gear_defaults`, `get_gear_activities` (exploración de material, no hace falta como herramienta expuesta: la asignación por defecto ya la gestiona el propio Garmin).

**Sin datos de entrenamiento reales en la cuenta, confirmado contra la cuenta real, no por descarte de categoría.** Golf, salud femenina, embarazo, nutrición, tensión arterial, retos y badges (y estos últimos, además, con un error de fábrica en la propia librería al paginar con `start=0`).

**Escritura sin valor de coaching o con riesgo innecesario.** `set_activity_name`/`_description`/`_type`, `delete_activity`, todo el bloque de subir/descargar ficheros (`upload_activity`, `import_activity`, `download_activity`, `create_manual_activity`), `push_workout_to_device` (empuja al reloj sin fecha; se pierde al sincronizar), `download_workout`.

**Solo lectura de planes de Garmin Coach, sin poder crearlos ni modificarlos por API.** `get_training_plans`, `get_training_plan_by_id`, `get_adaptive_training_plan_by_id`. Además, tener un plan de Garmin Coach activo en paralelo mandaría al reloj instrucciones que contradicen las del entrenador.

**No existe en esta versión de la librería.** `get_next_scheduled_workout` da error de atributo. Si hace falta "la próxima sesión sin especificar mes", se calcula filtrando `get_scheduled_workouts` del mes actual y el siguiente por fecha.

**Depende del dispositivo, no de la cuenta.** `get_running_tolerance` y `get_device_solar_data` dependen de que el reloj del deportista soporte esas funciones (confirmable con `get_devices`); se llaman condicionalmente según lo que ese dispositivo declare, nunca a ciegas.

---

## 4. Cálculos que el conector debe aplicar antes de entregar los datos

Ninguno de estos existe ya calculado en la respuesta de Garmin; son transformaciones que tiene que hacer LaIA.

| Cálculo | Cómo | Por qué |
|---|---|---|
| **RPE normalizado** | `directWorkoutRpe / 10` | Garmin lo guarda multiplicado por diez. Devolver `null` si el campo no viene, nunca `0`. |
| **Feel traducido** | `directWorkoutFeel` → etiqueta: `0` muy flojo, `25` flojo, `50` normal, `75` fuerte, `100` muy fuerte | Solo llega como número; la etiqueta hace legible la respuesta sin que el entrenador tenga que memorizar la escala. |
| **sRPE (carga por esfuerzo percibido)** | RPE normalizado × minutos de la sesión | Es la única carga fiable en natación, donde la FC en el agua no sirve: en las sesiones de nado revisadas, menos del 5% del tiempo caía en alguna zona de FC. |
| **Tiempo suave real** | `duración − (suma de las 5 zonas de FC) + zona1 + zona2` | La suma de las cinco zonas nunca llega a la duración total: todo lo que queda por debajo del suelo de zona 1 no se registra en ninguna. Sin esta corrección, el porcentaje de trabajo suave sale falseado a la baja. |
| **Edad** | Calculada desde `birthDate` de `get_user_profile` | No viene como campo directo. |
| **Velocidad de umbral en min/km** | Conversión desde `speed` (m/s) de `get_lactate_threshold` | Garmin la da en metros por segundo. |
| **Traducción de `typeId` de récords personales** | Diccionario propio, construido a mano | `prTypeLabelKey` viene vacío; Garmin no envía la etiqueta de qué tipo de récord es cada uno. |
| **Normalización del identificador de instancia agendada** | Un único nombre interno, por ejemplo `scheduled_workout_id` | El mismo dato se llama `workoutScheduleId` en la respuesta de `schedule_workout` y `id` dentro de cada `calendarItem` de `get_scheduled_workouts`. Sin normalizar, el conector puede buscar la clave equivocada según de qué endpoint venga. |
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

