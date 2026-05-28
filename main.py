#!/usr/bin/env python3
"""
Cliente BLE + Interfaz Gráfica Raspberry Pi


Arquitectura:
  - Un hilo asyncio ejecuta las dos tareas BLE (ESP32 + Smartphone) en paralelo.
  - La GUI corre en el hilo principal de Qt.
  - Los datos se intercambian mediante DataStore (lock + deques).
  - Qt Signals llevan los cambios de estado de conexión al hilo Qt.
"""

import sys
import asyncio
import struct
import json
import time
import csv
import threading
import numpy as np
from collections import deque
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner, BleakClient, BleakError

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QCheckBox, QLabel, QPushButton, QGroupBox, QGridLayout,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QFont
import pyqtgraph as pg


# ═══════════════════════════════════════════════════════════════════
# Configuración
# ═══════════════════════════════════════════════════════════════════

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULT_CONFIG: dict = {
    "esp32_name":               "ESP32",
    "esp32_mac":                "",         # dejar vacío para buscar por nombre
    "smartphone_name":          "",         # nombre BLE del smartphone
    "smartphone_service_uuid":  "",         # UUID de servicio del smartphone
    "smartphone_char_uuid":     "",         # UUID de característica del smartphone
    "window_ms":                2000,       # tamaño de la ventana deslizante (ms)
}

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()

CONFIG = load_config()



ACCEL_SVC_UUID = "44332211-4433-2211-4433-221144332211"
ACCEL_CHR_UUID = "88776655-8877-6655-8877-665588776655"
TEMP_SVC_UUID  = "00001809-0000-1000-8000-00805f9b34fb"
TEMP_CHR_UUID  = "00002a6e-0000-1000-8000-00805f9b34fb"



# DataStore – almacén de datos thread-safe para acelerómetro, temperatura y smartphone.

WINDOW_MS   = int(CONFIG.get("window_ms", 2000))
MAX_SAMPLES = max(WINDOW_MS, 2000)   # al menos 2 s de datos


class DataStore:
    """Buffer compartido entre el hilo asyncio-BLE y el hilo Qt-GUI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # Acelerómetro (ventana deslizante)
        self._accel_t:  deque = deque(maxlen=MAX_SAMPLES)
        self._accel_ax: deque = deque(maxlen=MAX_SAMPLES)
        self._accel_ay: deque = deque(maxlen=MAX_SAMPLES)
        self._accel_az: deque = deque(maxlen=MAX_SAMPLES)

        # Temperatura
        self._temp_val: float | None = None
        self._temp_ts:  int   | None = None

        # Smartphone
        self._phone_val = None
        self._phone_ts: int | None = None

        # CSV logging
        self._csv_file   = None
        self._csv_writer = None
        self._logging    = False

    # ── Escritura ──────────────────────────────────────────────────

    def add_accel(self, ts_ms: int, ax: float, ay: float, az: float) -> None:
        with self._lock:
            self._accel_t.append(ts_ms)
            self._accel_ax.append(ax)
            self._accel_ay.append(ay)
            self._accel_az.append(az)
            if self._logging and self._csv_writer:
                self._csv_writer.writerow(
                    [ts_ms, "accel", f"{ax:.5f}", f"{ay:.5f}", f"{az:.5f}", "", ""]
                )

    def add_temp(self, ts_ms: int, temp: float) -> None:
        with self._lock:
            self._temp_val = temp
            self._temp_ts  = ts_ms
            if self._logging and self._csv_writer:
                self._csv_writer.writerow(
                    [ts_ms, "temp", "", "", "", f"{temp:.2f}", ""]
                )

    def add_phone(self, ts_ms: int, value) -> None:
        with self._lock:
            self._phone_val = value
            self._phone_ts  = ts_ms
            if self._logging and self._csv_writer:
                self._csv_writer.writerow(
                    [ts_ms, "phone", "", "", "", "", str(value)]
                )

    # ── Lectura ────────────────────────────────────────────────────

    def get_accel_snapshot(self):
        with self._lock:
            return (
                list(self._accel_t),
                list(self._accel_ax),
                list(self._accel_ay),
                list(self._accel_az),
            )

    def get_temp(self):
        with self._lock:
            return self._temp_val, self._temp_ts

    def get_phone(self):
        with self._lock:
            return self._phone_val, self._phone_ts

    # ── Logging CSV ────────────────────────────────────────────────

    def start_log(self, filename: str) -> None:
        with self._lock:
            f = open(filename, "w", newline="", encoding="utf-8")
            self._csv_file   = f
            self._csv_writer = csv.writer(f)
            self._csv_writer.writerow(
                ["timestamp_ms", "source", "ax", "ay", "az", "temperatura", "valor_celular"]
            )
            self._logging = True

    def stop_log(self) -> None:
        with self._lock:
            self._logging = False
            if self._csv_file:
                self._csv_file.close()
                self._csv_file   = None
                self._csv_writer = None


STORE = DataStore()


# ═══════════════════════════════════════════════════════════════════
# Señales Qt emitidas desde el hilo asyncio
# ═══════════════════════════════════════════════════════════════════

class BLESignals(QObject):
    esp32_status  = pyqtSignal(str)
    phone_status  = pyqtSignal(str)

BLE_SIGNALS = BLESignals()


# ═══════════════════════════════════════════════════════════════════
# Callbacks de notificación BLE
# ═══════════════════════════════════════════════════════════════════

def _accel_notify(sender, data: bytearray) -> None:
    """
    Formato acelerómetro: 3 × float32 little-endian = 12 bytes.
        [ax(4B)][ay(4B)][az(4B)]
    El ESP32 envía una muestra cada 1000 ms (1 Hz por decisión del firmware).
    """
    if len(data) < 12:
        return
    ax, ay, az = struct.unpack_from("<fff", data, 0)
    ts = int(time.time() * 1000)
    STORE.add_accel(ts, ax, ay, az)


def _temp_notify(sender, data: bytearray) -> None:
    """
    Formato temperatura: int16 little-endian en centésimas de °C.
        [temp_centi(2B)]  →  temp = valor / 100.0
    Estándar Bluetooth SIG 0x2A6E.
    """
    if len(data) < 2:
        return
    raw  = struct.unpack_from("<h", data, 0)[0]
    temp = raw / 100.0
    ts   = int(time.time() * 1000)
    STORE.add_temp(ts, temp)


def _phone_notify(sender, data: bytearray) -> None:
    """
    Intenta decodificar UTF-8; si falla, intenta float32; si falla, hex.
    """
    ts = int(time.time() * 1000)
    try:
        value = data.decode("utf-8").strip()
    except Exception:
        try:
            value = round(struct.unpack_from("<f", data, 0)[0], 4)
        except Exception:
            value = data.hex()
    STORE.add_phone(ts, value)


# ═══════════════════════════════════════════════════════════════════
# Tareas asyncio de conexión BLE
# ═══════════════════════════════════════════════════════════════════

async def _connect_esp32() -> None:
    """Escanea → conecta → suscribe al ESP32; reconecta si se desconecta."""
    while True:
        BLE_SIGNALS.esp32_status.emit("Escaneando ESP32…")
        try:
            device = None

            # 1. Buscar por MAC si está configurada
            if CONFIG.get("esp32_mac"):
                device = await BleakScanner.find_device_by_address(
                    CONFIG["esp32_mac"], timeout=10.0
                )

            # 2. Buscar por nombre de anuncio
            if device is None:
                target_name = CONFIG.get("esp32_name", "ESP32")
                devices = await BleakScanner.discover(timeout=6.0)
                for d in devices:
                    if target_name in (d.name or ""):
                        device = d
                        break

            if device is None:
                BLE_SIGNALS.esp32_status.emit("ESP32 no encontrado, reintentando…")
                await asyncio.sleep(5)
                continue

            BLE_SIGNALS.esp32_status.emit(f"Conectando → {device.address}")
            async with BleakClient(device, timeout=15.0) as client:
                BLE_SIGNALS.esp32_status.emit(f"✓ Conectado: {device.address}")

                # Suscribir acelerómetro
                try:
                    await client.start_notify(ACCEL_CHR_UUID, _accel_notify)
                except Exception as e:
                    BLE_SIGNALS.esp32_status.emit(f"[WARN] accel notify: {e}")

                # Suscribir temperatura
                try:
                    await client.start_notify(TEMP_CHR_UUID, _temp_notify)
                except Exception as e:
                    BLE_SIGNALS.esp32_status.emit(f"[WARN] temp notify: {e}")

                # Mantener la conexión activa
                while client.is_connected:
                    await asyncio.sleep(1)

        except BleakError as e:
            BLE_SIGNALS.esp32_status.emit(f"BLE error: {e}")
        except Exception as e:
            BLE_SIGNALS.esp32_status.emit(f"Error: {e}")

        await asyncio.sleep(3)


async def _connect_smartphone() -> None:
    """Escanea → conecta → suscribe al smartphone; reconecta si se desconecta."""
    svc_uuid = CONFIG.get("smartphone_service_uuid", "").strip()
    chr_uuid = CONFIG.get("smartphone_char_uuid",    "").strip()
    name     = CONFIG.get("smartphone_name",         "").strip()

    if not svc_uuid or not chr_uuid:
        BLE_SIGNALS.phone_status.emit("Sin config – edita config.json")
        return

    while True:
        BLE_SIGNALS.phone_status.emit("Escaneando Smartphone…")
        try:
            device = None
            devices = await BleakScanner.discover(timeout=6.0)
            for d in devices:
                # Buscar por nombre
                if name and name in (d.name or ""):
                    device = d
                    break
                # Buscar por UUID de servicio anunciado
                adv_uuids = [str(u).lower() for u in (d.metadata.get("uuids") or [])]
                if svc_uuid.lower() in adv_uuids:
                    device = d
                    break

            if device is None:
                BLE_SIGNALS.phone_status.emit("Smartphone no encontrado, reintentando…")
                await asyncio.sleep(5)
                continue

            BLE_SIGNALS.phone_status.emit(f"Conectando → {device.address}")
            async with BleakClient(device, timeout=15.0) as client:
                BLE_SIGNALS.phone_status.emit(f"✓ Conectado: {device.address}")
                await client.start_notify(chr_uuid, _phone_notify)
                while client.is_connected:
                    await asyncio.sleep(1)

        except BleakError as e:
            BLE_SIGNALS.phone_status.emit(f"BLE error: {e}")
        except Exception as e:
            BLE_SIGNALS.phone_status.emit(f"Error: {e}")

        await asyncio.sleep(3)


def _run_ble_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Función de hilo: ejecuta ambas tareas BLE en el loop asyncio dado."""
    asyncio.set_event_loop(loop)
    loop.run_until_complete(
        asyncio.gather(_connect_esp32(), _connect_smartphone())
    )


# ═══════════════════════════════════════════════════════════════════
# Interfaz Gráfica (PyQt5 + pyqtgraph)
# ═══════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    """Ventana principal del dashboard IoT."""

    # Colores para los ejes del acelerómetro
    COLOR_AX = "#e74c3c"   # rojo
    COLOR_AY = "#2ecc71"   # verde
    COLOR_AZ = "#3498db"   # azul

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("IoT BLE Dashboard – ESP32 + Smartphone")
        self.setMinimumSize(1150, 680)
        self._build_ui()
        self._connect_ble_signals()

        # Timer de refresco de GUI: 50 ms ≈ 20 fps
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(50)

    # ── Construcción de la UI ──────────────────────────────────────

    def _build_ui(self) -> None:
        root_widget = QWidget()
        self.setCentralWidget(root_widget)
        root_layout = QHBoxLayout(root_widget)
        root_layout.setSpacing(8)
        root_layout.setContentsMargins(8, 8, 8, 8)

        # ─ Panel izquierdo (controles) ─────────────────────────────
        left = QWidget()
        left.setFixedWidth(230)
        left_vbox = QVBoxLayout(left)
        left_vbox.setAlignment(Qt.AlignTop)
        left_vbox.setSpacing(10)

        # Selección de sensores
        sel_box = QGroupBox("Sensores a visualizar")
        sel_vbox = QVBoxLayout(sel_box)
        self.cb_ax    = QCheckBox("Acelerómetro – Eje X"); self.cb_ax.setChecked(True)
        self.cb_ay    = QCheckBox("Acelerómetro – Eje Y"); self.cb_ay.setChecked(True)
        self.cb_az    = QCheckBox("Acelerómetro – Eje Z"); self.cb_az.setChecked(True)
        self.cb_temp  = QCheckBox("Temperatura (ESP32)");  self.cb_temp.setChecked(True)
        self.cb_phone = QCheckBox("Datos Smartphone");     self.cb_phone.setChecked(True)
        for cb in (self.cb_ax, self.cb_ay, self.cb_az, self.cb_temp, self.cb_phone):
            sel_vbox.addWidget(cb)
        left_vbox.addWidget(sel_box)

        # Estado de conexiones
        conn_box = QGroupBox("Estado BLE")
        conn_grid = QGridLayout(conn_box)
        conn_grid.setColumnStretch(1, 1)
        conn_grid.addWidget(QLabel("<b>ESP32:</b>"), 0, 0, Qt.AlignTop)
        self.lbl_esp32 = QLabel("—")
        self.lbl_esp32.setWordWrap(True)
        conn_grid.addWidget(self.lbl_esp32, 0, 1)
        conn_grid.addWidget(QLabel("<b>Phone:</b>"), 1, 0, Qt.AlignTop)
        self.lbl_phone_conn = QLabel("—")
        self.lbl_phone_conn.setWordWrap(True)
        conn_grid.addWidget(self.lbl_phone_conn, 1, 1)
        left_vbox.addWidget(conn_box)

        # Temperatura
        temp_box = QGroupBox("Temperatura (ESP32)")
        temp_vbox = QVBoxLayout(temp_box)
        self.lbl_temp_val = QLabel("— °C")
        self.lbl_temp_val.setFont(QFont("Monospace", 20, QFont.Bold))
        self.lbl_temp_val.setAlignment(Qt.AlignCenter)
        self.lbl_temp_ts = QLabel("")
        self.lbl_temp_ts.setAlignment(Qt.AlignCenter)
        temp_vbox.addWidget(self.lbl_temp_val)
        temp_vbox.addWidget(self.lbl_temp_ts)
        left_vbox.addWidget(temp_box)

        # Smartphone
        phone_box = QGroupBox("Datos Smartphone")
        phone_vbox = QVBoxLayout(phone_box)
        self.lbl_phone_val = QLabel("—")
        self.lbl_phone_val.setFont(QFont("Monospace", 14))
        self.lbl_phone_val.setAlignment(Qt.AlignCenter)
        self.lbl_phone_val.setWordWrap(True)
        self.lbl_phone_ts = QLabel("")
        self.lbl_phone_ts.setAlignment(Qt.AlignCenter)
        phone_vbox.addWidget(self.lbl_phone_val)
        phone_vbox.addWidget(self.lbl_phone_ts)
        left_vbox.addWidget(phone_box)

        # Botón de logging CSV
        self.btn_log = QPushButton("▶  Iniciar registro CSV")
        self.btn_log.setCheckable(True)
        self.btn_log.setMinimumHeight(36)
        self.btn_log.clicked.connect(self._toggle_log)
        left_vbox.addWidget(self.btn_log)

        left_vbox.addStretch()
        root_layout.addWidget(left)

        # ─ Panel derecho (gráfico + estadísticas) ──────────────────
        right = QWidget()
        right_vbox = QVBoxLayout(right)
        right_vbox.setSpacing(6)
        root_layout.addWidget(right, stretch=1)

        # Gráfico de acelerómetro
        pg.setConfigOptions(antialias=True, background="#1e1e2e", foreground="#cdd6f4")
        self.plot = pg.PlotWidget()
        self.plot.setTitle(
            f"Acelerómetro – ventana deslizante ({WINDOW_MS} ms)",
            color="#cdd6f4", size="11pt"
        )
        self.plot.setLabel("left",   "Aceleración", units="g")
        self.plot.setLabel("bottom", "Tiempo relativo", units="ms")
        self.plot.addLegend(offset=(10, 10))
        self.plot.showGrid(x=True, y=True, alpha=0.25)

        self.curve_ax = self.plot.plot(
            pen=pg.mkPen(self.COLOR_AX, width=1.5), name="Ax"
        )
        self.curve_ay = self.plot.plot(
            pen=pg.mkPen(self.COLOR_AY, width=1.5), name="Ay"
        )
        self.curve_az = self.plot.plot(
            pen=pg.mkPen(self.COLOR_AZ, width=1.5), name="Az"
        )
        right_vbox.addWidget(self.plot, stretch=3)

        # Panel de estadísticas
        stats_box = QGroupBox("Estadísticas (últimas 1000 muestras del acelerómetro)")
        stats_grid = QGridLayout(stats_box)
        stats_grid.setSpacing(4)

        bold = QFont(); bold.setBold(True)
        for col, header in enumerate(["", "Eje X", "Eje Y", "Eje Z"]):
            lbl = QLabel(header); lbl.setFont(bold)
            stats_grid.addWidget(lbl, 0, col)

        self._stat_lbls: dict = {}
        for row, row_name in enumerate(["RMS", "Peak positivo", "Pico a pico"], start=1):
            stats_grid.addWidget(QLabel(row_name), row, 0)
            for col, axis in enumerate(["ax", "ay", "az"], start=1):
                lbl = QLabel("—")
                lbl.setFont(QFont("Monospace", 9))
                stats_grid.addWidget(lbl, row, col)
                self._stat_lbls[(row_name, axis)] = lbl

        right_vbox.addWidget(stats_box, stretch=1)

    # ── Señales BLE ────────────────────────────────────────────────

    def _connect_ble_signals(self) -> None:
        BLE_SIGNALS.esp32_status.connect(self.lbl_esp32.setText)
        BLE_SIGNALS.phone_status.connect(self.lbl_phone_conn.setText)

    # ── Refresco periódico ─────────────────────────────────────────

    def _refresh(self) -> None:
        """Se llama cada 50 ms desde el QTimer para actualizar la GUI."""
        ts_arr, ax_arr, ay_arr, az_arr = STORE.get_accel_snapshot()

        # ── Gráfico acelerómetro ──────────────────────────────────
        if ts_arr:
            t0    = ts_arr[0]
            t_rel = [t - t0 for t in ts_arr]
            self.curve_ax.setData(t_rel, ax_arr) if self.cb_ax.isChecked() \
                else self.curve_ax.setData([], [])
            self.curve_ay.setData(t_rel, ay_arr) if self.cb_ay.isChecked() \
                else self.curve_ay.setData([], [])
            self.curve_az.setData(t_rel, az_arr) if self.cb_az.isChecked() \
                else self.curve_az.setData([], [])

        # ── Estadísticas ──────────────────────────────────────────
        n = min(1000, len(ax_arr))
        if n > 0:
            axes = {
                "ax": np.array(ax_arr[-n:], dtype=np.float32),
                "ay": np.array(ay_arr[-n:], dtype=np.float32),
                "az": np.array(az_arr[-n:], dtype=np.float32),
            }
            for axis, data in axes.items():
                rms  = float(np.sqrt(np.mean(data ** 2)))
                peak = float(np.max(data))
                pp   = float(np.max(data) - np.min(data))
                self._stat_lbls[("RMS",           axis)].setText(f"{rms:8.3f} g")
                self._stat_lbls[("Peak positivo", axis)].setText(f"{peak:8.3f} g")
                self._stat_lbls[("Pico a pico",   axis)].setText(f"{pp:8.3f} g")

        # ── Temperatura ───────────────────────────────────────────
        temp_val, temp_ts = STORE.get_temp()
        if self.cb_temp.isChecked():
            if temp_val is not None:
                self.lbl_temp_val.setText(f"{temp_val:.2f} °C")
                self.lbl_temp_ts.setText(
                    "ts: " + datetime.fromtimestamp(temp_ts / 1000).strftime("%H:%M:%S")
                )
            # else: mantiene el último valor mostrado
        else:
            self.lbl_temp_val.setText("(desactivado)")
            self.lbl_temp_ts.setText("")

        # ── Datos smartphone ──────────────────────────────────────
        phone_val, phone_ts = STORE.get_phone()
        if self.cb_phone.isChecked():
            if phone_val is not None:
                self.lbl_phone_val.setText(str(phone_val))
                self.lbl_phone_ts.setText(
                    "ts: " + datetime.fromtimestamp(phone_ts / 1000).strftime("%H:%M:%S")
                )
        else:
            self.lbl_phone_val.setText("(desactivado)")
            self.lbl_phone_ts.setText("")

    # ── Logging CSV ────────────────────────────────────────────────

    def _toggle_log(self, checked: bool) -> None:
        if checked:
            fname = f"datos_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            STORE.start_log(fname)
            self.btn_log.setText(f"⏹  Detener registro  ({fname})")
        else:
            STORE.stop_log()
            self.btn_log.setText("▶  Iniciar registro CSV")

    def closeEvent(self, event) -> None:
        STORE.stop_log()
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════

def main() -> None:
    # Hilo daemon con el loop asyncio (BLE)
    ble_loop = asyncio.new_event_loop()
    ble_thread = threading.Thread(
        target=_run_ble_loop, args=(ble_loop,), daemon=True
    )
    ble_thread.start()

    # GUI en el hilo principal
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
