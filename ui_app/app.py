#!/usr/bin/env python3
"""
ui_app/app.py -- PySide6 GUI for the TD-H9 HID flasher.
ui_app/app.py -- оконное приложение PySide6 для прошивки TD-H9 в HID-режиме.

Builds on the logic in the sibling h9_flash.py module (package unpacking,
sector-CRC scan, sector erase/write/verify) and wraps it in a desktop window:
  - pick a .fw package,
  - optional backup / write-only,
  - optional slow full shifted-CRC verification (--verify equivalent),
  - optional force rewrite ignoring current CRC (--nocheck equivalent),
  - background thread so the UI stays responsive, with progress bar and log.

Строится на логике соседнего модуля h9_flash.py (распаковка .fw, CRC-скан
секторов, стирание/запись/проверка секторов) и оборачивает её в окно:
  - выбор пакета .fw,
  - опциональный бэкап / только запись,
  - опциональная медленная полная проверка по сдвинутым CRC (аналог --verify),
  - опциональная принудительная перезапись (аналог --nocheck),
  - фоновый поток (UI не блокируется), прогресс и лог.
"""

import os
import sys
import threading

# Ensure the project root (parent of ui_app/) is on sys.path so we can import
# h9_flash, and expose the DLL directory for hidapi.dll on Windows.
# When frozen by PyInstaller, the script runs from sys._MEIPASS; h9_flash.py is
# bundled there via --add-data, so we add that path as well.
# Добавляем корень проекта (родитель ui_app/) в sys.path, чтобы импортировать
# h9_flash, и открываем для hidapi.dll папку со скриптом на Windows. При сборке
# PyInstaller скрипт запускается из sys._MEIPASS; h9_flash.py попадает туда
# через --add-data, поэтому добавляем и этот путь.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_APP_DIR)
_SEARCH_DIRS = [_ROOT, _APP_DIR]
if getattr(sys, "frozen", False):
    # PyInstaller: data files unpack to sys._MEIPASS (one-file) or lie next to
    # the executable (one-dir). Add both so hidapi.dll and h9_flash resolve.
    # PyInstaller: данные распаковываются в sys._MEIPASS (one-file) либо лежат
    # рядом с exe (one-dir). Добавляем оба пути, чтобы найти hidapi.dll и h9_flash.
    _MEIPASS = getattr(sys, "_MEIPASS", None)
    if _MEIPASS:
        _SEARCH_DIRS.insert(0, _MEIPASS)
    _EXEDIR = os.path.dirname(sys.executable)
    if _EXEDIR and _EXEDIR not in _SEARCH_DIRS:
        _SEARCH_DIRS.append(_EXEDIR)
for _d in _SEARCH_DIRS:
    if _d and _d not in sys.path:
        sys.path.insert(0, _d)
if sys.platform.startswith("win"):
    try:
        for _d in _SEARCH_DIRS:
            if _d and os.path.isdir(_d):
                os.add_dll_directory(_d)
    except Exception:
        pass

from PySide6.QtCore import Qt, Signal, Slot, QObject, QThread
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QLineEdit, QFileDialog, QCheckBox, QProgressBar,
    QPlainTextEdit, QMessageBox,
)

import hid  # pyhidapi, needs hidapi.dll in PATH / _ROOT

import h9_flash as core  # reuses unpack/scan/write logic / переиспользуем логику
from h9_flash import (
    APP_START, IMAGE_SIZE, SECTOR_SIZE,
    H9HidRadio, choose_device, decode_package, expected_crcs,
    scan_radio, write_sector, known_app_matches, hashlib_sha256,
    STOCK_PACKAGE_SHA, STOCK_IMAGE_SHA,
)


class WorkerSignals(QObject):
    """Qt signals emitted from the worker thread. / Сигналы рабочего потока."""
    log = Signal(str)                # plain log line / строка лога
    progress = Signal(int, int)      # done, total / готово, всего
    finished = Signal(int, str)      # exit code, message / код, сообщение


class FlashWorker(QThread):
    """Runs the flashing workflow in a background thread.

    Выполняет прошивку в фоновом потоке."""

    def __init__(self, fw_path, backup_path, do_verify, do_nocheck, parent=None):
        super().__init__(parent)
        self.fw_path = fw_path
        self.backup_path = backup_path
        self.do_verify = do_verify
        self.do_nocheck = do_nocheck
        self.signals = WorkerSignals()

    def log(self, msg):
        """Emit a log line. / Отправляет строку лога."""
        self.signals.log.emit(msg)

    def run(self):
        try:
            code, msg = self.flash_work()
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            code, msg = 2, "Unexpected error / Непредвиденная ошибка:\n%s\n%s" % (tb, e)
        self.signals.finished.emit(code, msg)

    def flash_work(self):
        """The actual flashing steps; returns (exit_code, message).

        Фактические шаги прошивки; возвращает (код_выхода, сообщение)."""
        log = self.log

        # 1) Read and unpack the .fw package / Читаем и распаковываем .fw
        log("Reading package: %s" % self.fw_path)
        if not os.path.isfile(self.fw_path):
            return 1, "Firmware file not found / Файл прошивки не найден: %s" % self.fw_path
        with open(self.fw_path, "rb") as f:
            pkg = f.read()
        try:
            image = decode_package(pkg)
        except ValueError as e:
            return 1, "Package unpack error / Ошибка распаковки пакета: %s" % e

        pkg_kind = "official v1.0.38 / официальный v1.0.38" if hashlib_sha256(pkg) == STOCK_PACKAGE_SHA else "custom build / кастомная сборка"
        img_kind = "stock v1.0.38" if hashlib_sha256(image) == STOCK_IMAGE_SHA else "build"
        log("  Package: %s (%s)" % (hashlib_sha256(pkg), pkg_kind))
        log("  Image:   %s (%s)" % (hashlib_sha256(image), img_kind))
        if len(image) != IMAGE_SIZE:
            return 1, "Wrong image size %d (expected %d) / Неверный размер %d (ожидалось %d)" % (len(image), IMAGE_SIZE, len(image), IMAGE_SIZE)

        # 2) Open the radio in HID mode / Открываем рацию в HID-режиме
        log("Opening TD-H9 HID (PTT+2)... / Открываю TD-H9 HID (PTT+2)...")
        dev = choose_device()
        radio = H9HidRadio(dev)
        try:
            radio.initialize()
        except Exception:
            dev.close()
            raise
        log("  Radio connected, geometry valid. / Рация подключена, геометрия валидна.")

        try:
            # 3) Full sector-CRC scan (no writes) / Полный CRC-скан секторов (без записи)
            total_sectors = (IMAGE_SIZE - APP_START) // SECTOR_SIZE
            log("Scanning sector CRCs... / Сканирую CRC секторов...")
            cur = []
            for relative in range(0, IMAGE_SIZE - APP_START, SECTOR_SIZE * 16):
                count = min(16, (IMAGE_SIZE - APP_START - relative) // SECTOR_SIZE)
                cur += radio.crcs(relative, count)
                self.signals.progress.emit(len(cur), total_sectors)
            self.signals.progress.emit(total_sectors, total_sectors)
            tgt_crcs = expected_crcs(image)

            matches = all(cur[i] == tgt_crcs[i] for i in range(total_sectors))

            # 4) Optional backup / Опциональный бэкап
            if self.backup_path:
                if matches:
                    with open(self.backup_path, "wb") as f:
                        f.write(image)
                    log("  Backup saved: %s / Бэкап сохранён: %s" % (self.backup_path, self.backup_path))
                else:
                    log("  WARNING: radio != target, backup skipped."
                        " / ВНИМАНИЕ: рация != цели, бэкап пропущен.")

            # 5) Optional slow shifted-CRC verification / Опциональная медленная проверка
            if self.do_verify and not self.do_nocheck:
                log("Running full shifted-CRC verification (slow)... / Полная проверка по сдвинутым CRC...")
                if not known_app_matches(radio, cur, image):
                    return 3, ("Verification failed: radio does not match target. "
                               "Проверка не пройдена: рация не совпадает с целью.")
                log("  Shifted-CRC verification passed. / Проверка по сдвинутым CRC пройдена.")

            # 6) Decide which sectors to write / Определяем секторы для записи
            if self.do_nocheck:
                changed = [APP_START + i * SECTOR_SIZE for i in range(total_sectors)]
                log("  --nocheck: rewriting ALL app sectors / перезаписываю все секторы.")
            else:
                changed = []
                for i in range(total_sectors):
                    physical = APP_START + i * SECTOR_SIZE
                    if cur[i] != tgt_crcs[i]:
                        changed.append(physical)
                if not changed:
                    return 0, ("Radio already matches the target image. Nothing to write."
                               "Рация уже совпадает с целью. Нечего писать.")
            log("  Sectors to rewrite: %d / Секторов к перезаписи: %d" % (len(changed), len(changed)))

            # 7) Write sectors high-to-low / Запись секторов от старших к младшим
            for idx, physical in enumerate(changed, 1):
                log("  [%d/%d] sector 0x%05X..." % (idx, len(changed), physical))
                write_sector(radio, physical, image)
                self.signals.progress.emit(idx, len(changed))
                log("      OK")

            # 8) Final verification / Финальная проверка
            log("Final verification of all sectors... / Финальная проверка всех секторов...")
            after = scan_radio(radio, on_progress=lambda d, t: self.signals.progress.emit(d, t))
            bad = 0
            for i in range(total_sectors):
                if after[i] != tgt_crcs[i]:
                    bad += 1
                    log("  MISMATCH / НЕ СОВПАДАЕТ: 0x%X" % (APP_START + i * SECTOR_SIZE))
            if bad:
                return 2, "Verification failed: %d sector(s) mismatch / Проверка не прошла: %d секторов" % (bad, bad)
            return 0, ("Done: all sectors written and verified. Готово: все секторы записаны и проверены. "
                       "Disconnect USB and power-cycle the radio. Отключите USB и перезапустите рацию.")
        finally:
            dev.close()


class MainWindow(QWidget):
    """Main flasher window. / Главное окно флешера."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("TD-H9 HID Flasher / Прошивка TD-H9")
        self.resize(560, 520)
        self._build_ui()
        self._worker = None

    # --- UI construction / Построение интерфейса ------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)

        # HID-mode instructions / Текстовка входа в HID-режим
        hint = QLabel(
            "1. Turn the radio OFF. \n"
            "2. Hold BOTH PTT and the [2] key. \n"
            "3. While holding, power the radio ON (this opens the HID updater). \n"
            "4. Connect the USB-C data cable to the PC. \n"
            "5. Click 'Detect device' — you should see 4C4A:4155. \n\n"
            "1. Выключите рацию. \n"
            "2. Зажмите ОДНОВРЕМЕННО PTT и кнопку [2]. \n"
            "3. Удерживая кнопки, включите рацию (откроется HID-режим). \n"
            "4. Подключите USB-C кабель (с данными) к ПК. \n"
            "5. Нажмите 'Поиск устройства' — должна появиться запись 4C4A:4155."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#555; padding:4px;")
        root.addWidget(hint)

        # Firmware file row / Строка выбора прошивки
        form = QFormLayout()
        self.fw_edit = QLineEdit()
        self.fw_edit.setPlaceholderText("/ path/to/firmware.fw / путь к прошивке .fw")
        browse_fw = QPushButton("Browse / Обзор")
        browse_fw.clicked.connect(self._browse_fw)
        h = QHBoxLayout()
        h.addWidget(self.fw_edit, 1)
        h.addWidget(browse_fw)
        form.addRow("Firmware / Прошивка:", h)

        # Options / Настройки
        options = QGroupBox("Options / Настройки")
        ov = QVBoxLayout(options)
        self.backup_edit = QLineEdit()
        self.backup_edit.setPlaceholderText("/ path/to/backup.bin / путь к бэкапу (необязательно)")
        browse_backup = QPushButton("Browse / Обзор")
        browse_backup.clicked.connect(self._browse_backup)
        bh = QHBoxLayout()
        bh.addWidget(self.backup_edit, 1)
        bh.addWidget(browse_backup)
        self.verify_check = QCheckBox("Slow full shifted-CRC verification (--verify) / Полная проверка по сдвинутым CRC")
        self.nocheck_check = QCheckBox("Force rewrite, skip CRC match (--nocheck) / Принудительная перезапись, пропустить CRC")
        ov.addLayout(bh)
        ov.addWidget(self.verify_check)
        ov.addWidget(self.nocheck_check)
        form.addRow("Backup / Бэкап:", bh)

        root.addLayout(form)
        root.addWidget(options)

        # Progress + log / Прогресс и лог
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Log / Журнал:")
        root.addWidget(self.log_view, 1)

        # Log toolbar: copy (log is selectable/copyable by default via Ctrl+C)
        # and clear buttons. / Панель лога: кнопки копировать и очистить
        # (лог по умолчанию копируется выделением + Ctrl+C).
        log_btns = QHBoxLayout()
        self.btn_copy = QPushButton("Copy log / Копировать лог")
        self.btn_copy.clicked.connect(self._copy_log)
        self.btn_clear = QPushButton("Clear / Очистить")
        self.btn_clear.clicked.connect(self._clear_log)
        log_btns.addWidget(self.btn_copy)
        log_btns.addWidget(self.btn_clear)
        log_btns.addStretch(1)
        root.addLayout(log_btns)

        # Buttons / Кнопки
        btns = QHBoxLayout()
        self.btn_flash = QPushButton("Flash / Прошить")
        self.btn_flash.clicked.connect(self.start_flash)
        self.btn_stop = QPushButton("Stop / Стоп")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_worker)
        self.btn_list = QPushButton("Detect device / Поиск устройства")
        self.btn_list.clicked.connect(self.detect_device)
        btns.addWidget(self.btn_list)
        btns.addStretch(1)
        btns.addWidget(self.btn_flash)
        btns.addWidget(self.btn_stop)
        root.addLayout(btns)

    # --- UI actions / Действия интерфейса --------------------------------------
    def _browse_fw(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Select firmware / Выберите прошивку", "",
                                            "FW package (*.fw);;All files (*.*)")
        if fn:
            self.fw_edit.setText(fn)

    def _browse_backup(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Save backup / Сохранить бэкап", "backup.bin",
                                            "Binary (*.bin);;All files (*.*)")
        if fn:
            self.backup_edit.setText(fn)

    def detect_device(self):
        """Try to enumerate installed JieLi HID devices. / Пробуем найти JieLi HID."""
        try:
            devs = [d for d in hid.enumerate() if d['vendor_id'] == core.CHIP_VID]
            if not devs:
                self.log_view.appendPlainText("No JieLi (4C4A) device found. / Устройство JieLi (4C4A) не найдено.")
                return
            for d in devs:
                pid = d['product_id']
                match = "  <-- TD-H9 flash mode / режим прошивки" if pid == core.CHIP_PID else ""
                self.log_view.appendPlainText("VID:0x%04X PID:0x%04X  %s %s%s"
                                              % (d['vendor_id'], pid, d.get('manufacturer_string', ''),
                                                 d.get('product_string', ''), match))
        except Exception as e:
            self.log_view.appendPlainText("hid error / ошибка hid: %s" % e)

    def _copy_log(self):
        """Copy the whole log to the system clipboard. / Копирует весь лог в буфер обмена."""
        text = self.log_view.toPlainText()
        QApplication.clipboard().setText(text)
        # Brief feedback / краткое подтверждение
        self.log_view.appendPlainText("Log copied to clipboard. / Лог скопирован в буфер обмена.")

    def _clear_log(self):
        """Clear the log view. / Очищает лог."""
        self.log_view.clear()

    # --- Worker control / Управление потоком -------------------------------------
    def start_flash(self):
        if self._worker is not None and self._worker.isRunning():
            return
        fw = self.fw_edit.text().strip()
        if not fw:
            QMessageBox.warning(self, "No firmware", "Please select a .fw file. / Выберите файл .fw.")
            return
        backup = self.backup_edit.text().strip() or None
        verify = self.verify_check.isChecked()
        nocheck = self.nocheck_check.isChecked()

        self._worker = FlashWorker(fw, backup, verify, nocheck)
        self._worker.signals.log.connect(self.log_view.appendPlainText)
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.finished.connect(self._on_finished)
        self.set_busy(True)
        self._worker.start()

    def stop_worker(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.terminate()
            self.log_view.appendPlainText("Stop requested. / Остановлено пользователем.")
            self.set_busy(False)

    def _on_progress(self, done, total):
        if total > 0:
            self.progress.setMaximum(total)
            self.progress.setValue(done)

    def _on_finished(self, code, msg):
        self.log_view.appendPlainText(msg)
        self.progress.setValue(self.progress.maximum())
        self.set_busy(False)
        if code != 0:
            QMessageBox.warning(self, "Flash result / Результат прошивки", msg)
        else:
            QMessageBox.information(self, "Flash result / Результат прошивки", msg)

    def set_busy(self, busy):
        self.btn_flash.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()