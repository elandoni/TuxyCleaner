import ctypes, os, shutil, tempfile, sys, json, subprocess, shlex
from pathlib import Path
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QMessageBox, QCheckBox
)
from PySide6.QtCore import Qt, QThread, Signal
import psutil

APP_NAME = "TuxyCleaner"
APP_TAGLINE = "Limpieza simple, rápida y confiable"

# --- rutas útiles ---
TEMP_DIR = Path(tempfile.gettempdir())
LOCAL = Path(os.getenv("LOCALAPPDATA") or "")
ROAM  = Path(os.getenv("APPDATA") or "")

LOGDIR = (LOCAL / "TuxyCleaner" / "logs")
LOGDIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = LOCAL / "TuxyCleaner" / "settings.json"
SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "excludes": [],         # rutas a excluir
    "max_size_mb": 2048,   # no tocar archivos más grandes (seguridad)
}

# --- helpers ---

def load_settings():
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

SETTINGS = load_settings()


def human(nbytes: int) -> str:
    for unit in ("B","KB","MB","GB","TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def logline(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (LOGDIR / (datetime.now().strftime("%Y-%m-%d") + ".log")).open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}")


def candidate_files(temp_dir: Path, max_size_mb: int):
    max_bytes = max_size_mb * 1024 * 1024
    for p in temp_dir.rglob("*"):
        try:
            if p.is_file():
                sz = p.stat().st_size
                if sz <= max_bytes:
                    yield p, sz
        except:
            pass


def browser_cache_dirs():
    dirs = [
        LOCAL / "Google/Chrome/User Data/Default/Cache",
        LOCAL / "Microsoft/Edge/User Data/Default/Cache",
        LOCAL / "BraveSoftware/Brave-Browser/User Data/Default/Cache",
    ]
    ff_root = ROAM / "Mozilla/Firefox/Profiles"
    if ff_root.exists():
        for prof in ff_root.glob("*"):
            dirs.append(prof / "cache2")
    return [d for d in dirs if d]


def empty_recycle_bin():
    SHERB_NOCONFIRMATION = 0x1
    SHERB_NOPROGRESSUI   = 0x2
    SHERB_NOSOUND        = 0x4
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)


# --- worker en hilo ---
class CleanerWorker(QThread):
    progress = Signal(int)           # 0..100
    status   = Signal(str)
    done     = Signal(dict)          # métricas completas

    def __init__(self, include_browsers: bool, parent=None):
        super().__init__(parent)
        self.include_browsers = include_browsers

    def _delete_file_safe(self, p: Path) -> tuple[int, int]:
        """(detected_bytes, deleted_bytes)"""
        try:
            size = p.stat().st_size if p.exists() else 0
            detected = size
            # chequear permiso de escritura
            can_write = os.access(p, os.W_OK)
            if can_write:
                try:
                    p.unlink(missing_ok=True)
                    return detected, size  # detectado y borrado
                except Exception:
                    return detected, 0     # detectado pero falló borrar
            else:
                return detected, 0
        except Exception:
            return 0, 0

    def _clean_temp(self):
        detected = deleted = count = 0
        files = list(candidate_files(TEMP_DIR, SETTINGS.get("max_size_mb", 2048)))
        n = max(len(files), 1)
        self.status.emit("Eliminando temporales…")
        for i, (f, _) in enumerate(files, start=1):
            d, r = self._delete_file_safe(f)
            detected += d; deleted += r
            count += 1
            if i % 50 == 0 or i == n:
                self.progress.emit(int(i*60/n))  # hasta 60%
        # carpetas vacías
        for d in list(TEMP_DIR.rglob("*"))[::-1]:
            try:
                if d.is_dir():
                    d.rmdir()
            except:
                pass
        return {"detected": detected, "deleted": deleted, "count": count}

    def _clean_browsers(self):
        detected = deleted = count = 0
        dirs = browser_cache_dirs()
        total_entries = sum(1 for d in dirs if d.exists()) or 1
        processed = 0
        for d in dirs:
            processed += 1
            if not d.exists():
                continue
            self.status.emit(f"Limpiando caché: {d}")
            for p in d.rglob("*"):
                if p.is_file():
                    dbytes, rbytes = self._delete_file_safe(p)
                    detected += dbytes; deleted += rbytes; count += 1
            self.progress.emit(60 + int(processed * 30 / total_entries))  # 60..90%
        return {"detected": detected, "deleted": deleted, "count": count}

    def run(self):
        metrics = {"temp": {"detected": 0, "deleted": 0, "count": 0},
                   "browsers": {"detected": 0, "deleted": 0, "count": 0},
                   "recycle": {"done": False}}

        mtemp = self._clean_temp(); metrics["temp"] = mtemp
        if self.include_browsers:
            mbro = self._clean_browsers(); metrics["browsers"] = mbro
        self.status.emit("Vaciando Papelera…")
        try:
            empty_recycle_bin(); metrics["recycle"]["done"] = True
        except:
            metrics["recycle"]["done"] = False
        self.progress.emit(100)
        self.done.emit(metrics)


# --- interfaz ---
class TuxyCleaner(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.label_title = QLabel(f"<h2>{APP_NAME}</h2>")
        self.label_sub   = QLabel(APP_TAGLINE)
        self.label_sub.setStyleSheet("color:#7b8ea3;")
        self.label_info  = QLabel("Calculando…")
        self.label_info.setAlignment(Qt.AlignCenter)

        # toggles
        toggles = QHBoxLayout()
        self.chk_browsers = QCheckBox("Incluir caché de navegadores")
        toggles.addWidget(self.chk_browsers)

        self.progress    = QProgressBar(); self.progress.setValue(0)
        self.btn_clean   = QPushButton("Limpiar ahora")
        self.btn_clean.setEnabled(False)

        self.btn_schedule = QPushButton("Programar limpieza semanal")
        self.btn_schedule.setEnabled(True)

        for w in (self.label_title, self.label_sub, self.label_info):
            layout.addWidget(w)
        layout.addLayout(toggles)
        layout.addWidget(self.progress)
        layout.addWidget(self.btn_clean)
        layout.addWidget(self.btn_schedule)

        self.btn_clean.clicked.connect(self.start_clean)
        self.btn_schedule.clicked.connect(self.create_schtask)
        self.refresh_estimate()

        # estilo
        self.setStyleSheet(
            """
            QWidget { background:#0f172a; color:#e2e8f0; }
            QProgressBar { background:#1e293b; border:1px solid #334155; height:12px; }
            QProgressBar::chunk { background-color:#38bdf8; }
            QPushButton { background:#0284c7; border:none; padding:10px; border-radius:8px; }
            QPushButton:disabled { background:#334155; color:#94a3b8; }
            QPushButton:hover:!disabled { background:#0ea5e9; }
            QCheckBox { spacing:8px; }
            """
        )

    def refresh_estimate(self):
        # mostramos sólo estimación de temporales detectables
        total_temp = sum(sz for _, sz in candidate_files(TEMP_DIR, SETTINGS.get("max_size_mb", 2048)))
        try:
            home_drive = Path.home().anchor or str(Path.home())
            disk = psutil.disk_usage(home_drive)
            disk_info = f"Espacio total: {human(disk.total)} — Libre: {human(disk.free)}"
        except Exception:
            disk_info = ""
        self.label_info.setText(
            f"Temporales detectados (posibles): <b>{human(total_temp)}</b><br>{disk_info}"
        )
        self.btn_clean.setEnabled(True)

    def start_clean(self):
        include_browsers = self.chk_browsers.isChecked()
        reply = QMessageBox.question(self, "Confirmar",
                                     "¿Borrar temporales" + (" y caché de navegadores" if include_browsers else "") + " y vaciar Papelera?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self.btn_clean.setEnabled(False)
        self.progress.setValue(0)
        self.label_info.setText("Preparando…")

        self.worker = CleanerWorker(include_browsers=include_browsers)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.status.connect(lambda t: self.label_info.setText(t))
        self.worker.done.connect(self.finish_clean)
        self.worker.start()

    def finish_clean(self, metrics: dict):
        temp_d = metrics["temp"]["detected"]; temp_r = metrics["temp"]["deleted"]
        bro_d  = metrics["browsers"]["detected"]; bro_r  = metrics["browsers"]["deleted"]
        total_d = temp_d + bro_d
        total_r = temp_r + bro_r
        msg = (f"Detectados: {human(total_d)} — Eliminados: <b>{human(total_r)}</b>"
   f"(Temporales: {human(temp_r)} de {human(temp_d)} | Navegadores: {human(bro_r)} de {human(bro_d)})\n"
   f"Papelera: {'vaciada' if metrics['recycle']['done'] else 'no disponible'}")
        self.label_info.setText(msg.replace("\n", "<br>"))
        logline(msg)
        QMessageBox.information(self, "TuxyCleaner", msg)
        self.refresh_estimate()
        self.btn_clean.setEnabled(True)

    def create_schtask(self):
        try:
            exe = Path(sys.argv[0])
            if exe.suffix.lower() != ".exe":
                py = sys.executable
                tr = f'"{py}" "{Path(__file__).resolve()}"'
            else:
                tr = f'"{exe.resolve()}"'
            cmd = f'schtasks /Create /TN "TuxyCleaner Weekly" /SC WEEKLY /D SUN /ST 12:00 /RL HIGHEST /TR {tr} /F'
            subprocess.run(shlex.split(cmd), check=False)
            QMessageBox.information(self, "Programado", "Tarea semanal creada (Dom 12:00)")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"No se pudo crear la tarea: {e}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = TuxyCleaner()
    w.show()
    sys.exit(app.exec())
import ctypes, os, shutil, tempfile, sys, json, subprocess, shlex
from pathlib import Path
from datetime import datetime

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QProgressBar, QMessageBox, QCheckBox
)
from PySide6.QtCore import Qt, QThread, Signal
import psutil

APP_NAME = "TuxyCleaner"
APP_TAGLINE = "Limpieza simple, rápida y confiable"

# --- rutas útiles ---
TEMP_DIR = Path(tempfile.gettempdir())
LOCAL = Path(os.getenv("LOCALAPPDATA") or "")
ROAM  = Path(os.getenv("APPDATA") or "")

LOGDIR = (LOCAL / "TuxyCleaner" / "logs")
LOGDIR.mkdir(parents=True, exist_ok=True)
SETTINGS_PATH = LOCAL / "TuxyCleaner" / "settings.json"
SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "excludes": [],         # rutas a excluir
    "max_size_mb": 2048,   # no tocar archivos más grandes (seguridad)
}

# --- helpers ---

def load_settings():
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

SETTINGS = load_settings()


def human(nbytes: int) -> str:
    for unit in ("B","KB","MB","GB","TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def logline(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (LOGDIR / (datetime.now().strftime("%Y-%m-%d") + ".log")).open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def candidate_files(temp_dir: Path, max_size_mb: int):
    max_bytes = max_size_mb * 1024 * 1024
    for p in temp_dir.rglob("*"):
        try:
            if p.is_file():
                sz = p.stat().st_size
                if sz <= max_bytes:
                    yield p, sz
        except:
            pass


def browser_cache_dirs():
    dirs = [
        LOCAL / "Google/Chrome/User Data/Default/Cache",
        LOCAL / "Microsoft/Edge/User Data/Default/Cache",
        LOCAL / "BraveSoftware/Brave-Browser/User Data/Default/Cache",
    ]
    ff_root = ROAM / "Mozilla/Firefox/Profiles"
    if ff_root.exists():
        for prof in ff_root.glob("*"):
            dirs.append(prof / "cache2")
    return [d for d in dirs if d]


def empty_recycle_bin():
    SHERB_NOCONFIRMATION = 0x1
    SHERB_NOPROGRESSUI   = 0x2
    SHERB_NOSOUND        = 0x4
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)


# --- worker en hilo ---
class CleanerWorker(QThread):
    progress = Signal(int)           # 0..100
    status   = Signal(str)
    done     = Signal(int, int)      # freed_temp, freed_browsers

    def __init__(self, preview: bool, include_browsers: bool, parent=None):
        super().__init__(parent)
        self.preview = preview
        self.include_browsers = include_browsers

    def _clean_temp(self):
        freed = 0
        files = list(candidate_files(TEMP_DIR, SETTINGS.get("max_size_mb", 2048)))
        n = max(len(files), 1)
        self.status.emit("Analizando temporales…" if self.preview else "Eliminando temporales…")
        for i, (f, size) in enumerate(files, start=1):
            try:
                if self.preview:
                    # no borrar, solo contabilizar
                    freed += size
                else:
                    f.unlink(missing_ok=True)
                    freed += size
            except:
                pass
            if i % 50 == 0 or i == n:
                self.progress.emit(int(i*60/n))  # hasta 60%
        # carpetas vacías solo si no es preview
        if not self.preview:
            for d in list(TEMP_DIR.rglob("*"))[::-1]:
                try:
                    if d.is_dir():
                        d.rmdir()
                except:
                    pass
        return freed

    def _clean_browsers(self):
        freed = 0
        dirs = browser_cache_dirs()
        total_entries = sum(1 for d in dirs if d.exists()) or 1
        processed = 0
        for d in dirs:
            processed += 1
            if not d.exists():
                continue
            self.status.emit(f"{'Analizando' if self.preview else 'Limpiando'} caché: {d}")
            for p in d.rglob("*"):
                try:
                    if p.is_file():
                        sz = p.stat().st_size
                        if self.preview:
                            freed += sz
                        else:
                            p.unlink(missing_ok=True)
                            freed += sz
                except:
                    pass
            self.progress.emit(60 + int(processed * 30 / total_entries))  # 60..90%
        return freed

    def run(self):
        freed_temp = self._clean_temp()
        freed_browsers = 0
        if self.include_browsers:
            freed_browsers = self._clean_browsers()
        self.status.emit("Vaciando Papelera…" if not self.preview else "Papelera (omitida en vista previa)…")
        if not self.preview:
            try:
                empty_recycle_bin()
            except:
                pass
        self.progress.emit(100)
        self.done.emit(freed_temp, freed_browsers)


# --- interfaz ---
class TuxyCleaner(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.label_title = QLabel(f"<h2>{APP_NAME}</h2>")
        self.label_sub   = QLabel(APP_TAGLINE)
        self.label_sub.setStyleSheet("color:#7b8ea3;")
        self.label_info  = QLabel("Calculando…")
        self.label_info.setAlignment(Qt.AlignCenter)

        # toggles
        toggles = QHBoxLayout()
        self.chk_preview = QCheckBox("Vista previa (no borra)")
        self.chk_browsers = QCheckBox("Incluir caché de navegadores")
        toggles.addWidget(self.chk_preview)
        toggles.addWidget(self.chk_browsers)

        self.progress    = QProgressBar(); self.progress.setValue(0)
        self.btn_clean   = QPushButton("Limpiar ahora")
        self.btn_clean.setEnabled(False)

        self.btn_schedule = QPushButton("Programar limpieza semanal")
        self.btn_schedule.setEnabled(True)

        for w in (self.label_title, self.label_sub, self.label_info):
            layout.addWidget(w)
        layout.addLayout(toggles)
        layout.addWidget(self.progress)
        layout.addWidget(self.btn_clean)
        layout.addWidget(self.btn_schedule)

        self.btn_clean.clicked.connect(self.start_clean)
        self.btn_schedule.clicked.connect(self.create_schtask)
        self.refresh_estimate()

        # estilo
        self.setStyleSheet(
            """
            QWidget { background:#0f172a; color:#e2e8f0; }
            QProgressBar { background:#1e293b; border:1px solid #334155; height:12px; }
            QProgressBar::chunk { background-color:#38bdf8; }
            QPushButton { background:#0284c7; border:none; padding:10px; border-radius:8px; }
            QPushButton:disabled { background:#334155; color:#94a3b8; }
            QPushButton:hover:!disabled { background:#0ea5e9; }
            QCheckBox { spacing:8px; }
            """
        )

    def refresh_estimate(self):
        total_temp = sum(sz for _, sz in candidate_files(TEMP_DIR, SETTINGS.get("max_size_mb", 2048)))
        try:
            home_drive = Path.home().anchor or str(Path.home())
            disk = psutil.disk_usage(home_drive)
            disk_info = f"Espacio total: {human(disk.total)} — Libre: {human(disk.free)}"
        except Exception:
            disk_info = ""
        self.label_info.setText(
            f"Temporales detectados: <b>{human(total_temp)}</b><br>{disk_info}"
        )
        self.btn_clean.setEnabled(True)

    def start_clean(self):
        preview = self.chk_preview.isChecked()
        include_browsers = self.chk_browsers.isChecked()
        action = "Analizar" if preview else "Borrar"
        msg = f"¿{action} temporales" + (" y caché de navegadores" if include_browsers else "") + ("? (Papelera omitida en vista previa)" if preview else " y vaciar Papelera?")
        reply = QMessageBox.question(self, "Confirmar", msg, QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self.btn_clean.setEnabled(False)
        self.progress.setValue(0)
        self.label_info.setText("Preparando…")

        self.worker = CleanerWorker(preview=preview, include_browsers=include_browsers)
        self.worker.progress.connect(self.progress.setValue)
        self.worker.status.connect(lambda t: self.label_info.setText(t))
        self.worker.done.connect(self.finish_clean)
        self.worker.start()

    def finish_clean(self, freed_temp: int, freed_browsers: int):
        total = freed_temp + freed_browsers
        preview = self.chk_preview.isChecked()
        if preview:
            text = f"Vista previa completa. Podrías liberar: {human(total)} (Temp: {human(freed_temp)}, Navegadores: {human(freed_browsers)})"
        else:
            text = f"¡Listo! Se liberaron {human(total)} (Temp: {human(freed_temp)}, Navegadores: {human(freed_browsers)})"
        self.label_info.setText(text)
        logline(text)
        QMessageBox.information(self, "TuxyCleaner", text)
        self.refresh_estimate()
        self.btn_clean.setEnabled(True)

    def create_schtask(self):
        # programa una tarea semanal el domingo 12:00 ejecutando la app con --silent-preview (a futuro)
        try:
            exe = Path(sys.argv[0])
            # si estamos corriendo como script, usa python + ruta script; si es exe, usa exe
            if exe.suffix.lower() != ".exe":
                # ruta a python y script
                py = sys.executable
                tr = f'"{py}" "{Path(__file__).resolve()}" --silent'
            else:
                tr = f'"{exe.resolve()}" --silent'
            cmd = f'schtasks /Create /TN "TuxyCleaner Weekly" /SC WEEKLY /D SUN /ST 12:00 /RL HIGHEST /TR {tr} /F'
            subprocess.run(shlex.split(cmd), check=False)
            QMessageBox.information(self, "Programado", "Tarea semanal creada (Dom 12:00)")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"No se pudo crear la tarea: {e}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = TuxyCleaner()
    w.show()
    sys.exit(app.exec())
