# README.md

Este es un sistema que conecta **tres dispositivos por BLE simultáneamente**: un **ESP32** (servidor) y un **dispositivo móvil** (servidor) que publican datos, y una **Raspberry Pi** (cliente) que se conecta a ambos a la vez, los grafica en tiempo real y los registra en CSV.

---

## 1. Arquitectura del sistema

- El **ESP32** corre ESP-IDF + NimBLE. Expone 2 servicios GATT (acelerómetro y temperatura) y notifica los datos.
- El **smartphone** actúa como periférico BLE con un servicio/característica configurables (ver [`raspberry/config.json`](raspberry/config.json)).
- La **Raspberry Pi** ([`raspberry/main.py`](raspberry/main.py)) abre dos conexiones BLE en paralelo (un hilo `asyncio` con `bleak`), comparte los datos con la GUI vía un `DataStore` protegido por lock, y los muestra/registra con PyQt5 + pyqtgraph.

---

## 2. UUIDs de servicios y características

### ESP32

| Elemento | UUID | Tipo | Propiedades |
|---|---|---|---|
| Servicio Acelerómetro | `44332211-4433-2211-4433-221144332211` | Custom (128-bit) | — |
| Característica Acelerómetro | `88776655-8877-6655-8877-665588776655` | Custom (128-bit) | READ + NOTIFY |
| Servicio Temperatura | `00001809-0000-1000-8000-00805f9b34fb` | SIG `0x1809` (Health Thermometer) | — |
| Característica Temperatura | `00002a6e-0000-1000-8000-00805f9b34fb` | SIG `0x2A6E` (Temperature) | READ + NOTIFY |

> El CCCD (descriptor para activar notificaciones) lo crea NimBLE automáticamente para cada característica.

### Dispositivo móvil

Los UUIDs del dispositivo móvil (en el experimento una Tablet con HarmonyOS), son **configurables** desde [`raspberry/config.json`](raspberry/config.json). Valores utilizados para el experimento:

| Elemento | UUID (ejemplo) | Propiedades |
|---|---|---|
| Servicio | `12345678-1234-5678-1234-56789abcdef0` | — |
| Característica | `12345678-1234-5678-1234-56789abcdef1` | NOTIFY |

> El cliente decodifica el dato del smartphone como texto UTF-8. Si no es imprimible, lo muestra en hexadecimal.

---

## 3. Formato binario de los paquetes BLE del ESP32

### Acelerómetro

- Cada notificación = **batch de 20 muestras** consecutivas (1000 Hz → 50 notificaciones/s, cada 20 ms). Se envía en little-endian.

### Temperatura

- `int16` **little-endian** en centésimas de °C (estándar Bluetooth SIG `0x2A6E`).
- Decodificación: `temp_°C = valor / 100.0`. Se notifica cada 15 segundos.

---

## 4. Dirección MAC y nombre de advertising del ESP32

| Parámetro | Valor |
|---|---|
| Nombre de anuncio BLE | `ESP32` (nombre completo en el paquete de advertising principal, descubrible por escáneres pasivos) |
| Tipo de dirección | Pública (`BLE_ADDR_PUBLIC`) — es la MAC de fábrica del chip |
| Dirección MAC | `c0:49:ef:08:ce:80`

---

## 5. App del dispositivo móvil — configuración y ejecución

El dispositivo móvil debe actuar como **periférico/servidor BLE**, anunciando el servicio y exponiendo su característica con NOTIFY. Hubo errores al probar con dispositivos que llevan sistemas operativos iOS y MacOS mediante la aplicación LightBlue. Sin embargo, con una tablet HarmonyOS (similar a Android) mediante la aplicación *nRF Connect* se pudo concretar una conexión estable.

Configura en la app un servicio con una característica **NOTIFY** usando los UUIDs definidos en `config.json`, y publícalos en el advertising:

```jsonc
"smartphone_service_uuid": "12345678-1234-5678-1234-56789abcdef0",
"smartphone_char_uuid":    "12345678-1234-5678-1234-56789abcdef1"
```

---

## 6. Firmware ESP32 — compilación y flasheo

Requiere **ESP-IDF** (target `esp32`, stack **NimBLE** ya habilitado en [`sdkconfig`](sdkconfig)).

```bash
# Desde la raíz del proyecto, con el entorno de ESP-IDF activado
idf.py set-target esp32        # solo la primera vez
idf.py build                   # compila el firmware
idf.py -p /dev/ttyUSB0 flash monitor   # flashea y abre el monitor serie
```

> Ajusta el puerto (`/dev/ttyUSB0`, `/dev/ttyACM0`, `COMx`, etc.) según tu sistema.
> En el monitor verás `Nimble comenzado` / `Advertisment comenzado` y muestras de referencia de los sensores.

---

## 7. Cliente Raspberry Pi — ejecución

Requiere **Python 3** con BlueZ (Linux). Dependencias en [`raspberry/requirements.txt`](raspberry/requirements.txt).

```bash
cd raspberry
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

1. **(Opcional)** Descubre dispositivos y sus UUIDs anunciados para rellenar `config.json`:
   ```bash
   python3 scan.py
   ```
2. **Edita [`config.json`](raspberry/config.json)**: nombre/MAC del ESP32 y UUIDs del smartphone.
3. **Ejecuta la aplicación**:
   ```bash
   python3 main.py
   ```

La GUI permite seleccionar qué sensores graficar, muestra el estado de ambas conexiones BLE, las estadísticas del acelerómetro (RMS, peak, pico a pico) y permite registrar todo en CSV.

---

# Decisiones de diseño

## Generales

### Uso de IA
- Para ciertas ocasiones se hizo uso de los servicios de IA de Claude. Esto se hizo con cuidado para no dejar de aprender en el proceso, es decir, se usó como una herramienta en vez de un reemplazo al estudiante. En particular, su uso fue para:
    - Consultar sobre zonas del código que nos complicaron de sobremanera.
    - Documentar el código existente.

## Servidor en ESP32

### Frecuencia de muestreo del acelerómetro
- La frecuencia de muestreo del acelerómetro es de 1000 Hz. Para no saturar la consola, sólo se imprime una muestra de referencia por segundo (las 1000 se siguen generando).
- Para enviar las 1000 muestras/s por BLE sin saturar el stack, se agrupan en **batches de 20 muestras** y se envía una notificación cada 20 ms (50 batches/s). Cada notificación pesa 240 B (20 × 12 B), dentro del MTU típico de NimBLE (256 B).

### Formato binario de los paquetes enviados
- **Acelerómetro**: cada notificación contiene 20 muestras consecutivas de `3 × float32` little-endian (`[ax][ay][az]` × 20 = 240 B).
- **Temperatura**: `int16` little-endian en centésimas de °C (estándar Bluetooth SIG 0x2A6E).


## Cliente en Raspberry Pi

### Reconstrucción de timestamps del acelerómetro
- Cada notificación trae 20 muestras sin timestamp individual. El cliente asigna timestamps espaciados 1 ms hacia atrás desde la llegada del batch.
