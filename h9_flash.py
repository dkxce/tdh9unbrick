#!/usr/bin/env python3
"""
h9_flash.py -- Flasher for the TIDRADIO TD-H9.
h9_flash.py -- Флешера для TIDRADIO TD-H9.

Author:  dkxce (c)  https://github.com/dkxce/Tidradio-TD-H9
Version: v1.0.0

Flashes the radio's main flash in HID mode (hold PTT+2 at power-on, USB VID
0x4C4A, PID 0x4155) through a WebHID-like interface (hidapi). Only the console
output here is in English; the code comments are bilingual (EN + RU).

Прошивает основную flash-память рации в HID-режиме (PTT+2 при включении,
USB VID 0x4C4A, PID 0x4155) через WebHID-подобный интерфейс (hidapi).
Вывод в консоль — на английском; комментарии кода — на двух языках (англ+рус).

Does NOT touch the boot area 0x0000..0x4FFF and the settings area
0xDA000..0x100000. / Не пишет boot-область 0x0000..0x4FFF и настройки
0xDA000..0x100000.

Dependency: pip install hidapi + hidapi.dll near this script (Windows).
Зависимость: pip install hidapi и hidapi.dll рядом со скриптом (Windows).

Example / Пример:
  python h9_flash.py --fw TD-H9-V1.0.38.fw
  python h9_flash.py --list
"""

import argparse
import os
import os.path
import sys

# --- Windows DLL search ----------------------------------------------------
# pyhidapi loads hidapi.dll via ctypes.LoadLibrary, which does NOT reliably
# search the script's own directory on Windows. Expose the script folder as a
# DLL search path so a hidapi.dll placed next to this file is found.
# pyhidapi грузит hidapi.dll через ctypes.LoadLibrary, который на Windows не
# гарантирует поиск в папке скрипта. Добавляем папку скрипта в путь поиска
# DLL, чтобы hidapi.dll, лежащая рядом, нашлась.
if sys.platform.startswith("win"):
    try:
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        os.add_dll_directory(_script_dir)
    except Exception:
        pass

# --- Protocol parameters (from dist/firmware-update/firmware.mjs) ----------
# --- Параметры протокола (из dist/firmware-update/firmware.mjs) ------------
APP_START = 0x5000        # firmware image start offset / начало образа прошивки
IMAGE_SIZE = 0xDA000      # 892 928 -- main app image size / размер образа
FLASH_SIZE = 0x100000     # full chip size / полный размер чипа
SECTOR_SIZE = 4096        # 4 KiB sector / сектор 4 КБ

# Official stock v1.0.38 .fw package and image hashes.
# Хэши официального пакета и образа stock v1.0.38.
STOCK_PACKAGE_SHA = "c4d0beb5281f3a06d7a56083e1744b925f78ff9917c8364e006418ff0bbd0cd6"
STOCK_IMAGE_SHA = "74243eaed19fe0c3e5fbb3ef3c46442f71ee8f7c216f214d322894d26ce4e648"

CHIP_VID = 0x4C4A          # JieLi vendor ID / идентификатор производителя JieLi
CHIP_PID = 0x4155          # TD-H9 HID flashing mode (PTT+2) / HID-режим (PTT+2)
REPORT_ID = 0              # HID report ID used for the vendor interface

# Radar/HID protocol parameters / Параметры протокола (из readback.mjs)
# Shift offsets for the sliding-CRC verification pass. A plain 16-bit sector
# CRC can collide, so the web flasher re-checks 4 KiB windows shifted by these
# byte offsets to prove the live radio bytes really match the target image.
# Смещения для проверки по «скользящим» CRC-окнам. 16-битный CRC сектора может
# дать коллизию, поэтому web-флешер перепроверяет окна 4 КБ, сдвинутые на эти
# байты, чтобы доказать, что «живые» байты рации действительно равны образу.
SHIFTS = [1, 37, 1024, 2048, 3072, 4095]

# Command definitions / Команды HID-протокола
CMD_INIT = 0xC0            # app_dir_head -> returns update geometry / геометрия
CMD_ERASE = 0xC2           # erase a sector / стирание сектора
CMD_WRITE = 0xC3           # program bytes / запись байтов
CMD_CRC = 0xC4             # read sector CRCs / чтение CRC секторов

# TD-H9 .fw package layout constants / Константы раскладки пакета TD-H9 .fw
HDR_SIZE = 0x200           # 512-byte header / 512-байтный заголовок
ENTRY_SIZE = 0x50          # 80-byte file-list record / 80-байтная запись списка


def hashlib_sha256(data):
    """Return hex SHA-256 of bytes. / Возвращает SHA-256 (hex) данных."""
    import hashlib
    return hashlib.sha256(data).hexdigest()


def crc16(data):
    """CRC-16/XMODEM, matching crc16() in firmware.mjs.

    CRC-16/XMODEM, как в crc16() из firmware.mjs."""
    crc = 0
    # For each byte, XOR into the top and run 8 polynomial steps.
    # Для каждого байта делаем XOR в старшие биты и 8 шагов полинома.
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if (crc & 0x8000) else 0)) & 0xFFFF
    return crc


def jl_cipher(block, key=0xF181):
    """Apply the JieLi keystream (XOR with crc16 sequence), like decodePackage.

    Применяет потоковый ключ JieLi (XOR с последовательностью crc16),
    как в decodePackage."""
    out = bytearray(block)
    k = key
    # The key advances one crc16 step per byte / ключ сдвигается на шаг crc16 на байт
    for i in range(len(out)):
        out[i] ^= k & 0xFF
        k = ((k << 1) ^ (0x1021 if (k & 0x8000) else 0)) & 0xFFFF
    return bytes(out)


def decode_package(bytes_data):
    """Extract flash.bin from an official TD-H9 .fw package.

    Достаёт flash.bin из официального пакета TD-H9 .fw."""
    # Basic size guards / Базовая проверка размера
    if len(bytes_data) < HDR_SIZE:
        raise ValueError("Package too short / Слишком короткий пакет")
    if len(bytes_data) > 16 * 1024 * 1024:
        raise ValueError("Package too large / Пакет слишком большой")

    # In the original source only the first 0x40 header bytes are encrypted;
    # the file-list records (from 0x40) are each encrypted separately.
    # В исходнике шифруются ТОЛЬКО первые 0x40 байт заголовка; записи файлов
    # (с 0x40) зашифрованы отдельно, каждый блок своим ключом.
    raw = bytes(bytes_data[:HDR_SIZE])
    hdr_enc = bytearray(jl_cipher(raw[:0x40]))
    hdr_enc += raw[0x40:]   # file records stay encrypted / записи остаются зашифрованными
    dec = hdr_enc           # decrypted header view / расшифрованный заголовок

    # Header self-checks / Самопроверки заголовка
    if crc16(dec[2:0x40]) != int.from_bytes(dec[0:2], "little"):
        raise ValueError("Invalid package header CRC / Недействительный CRC заголовка")
    if int.from_bytes(dec[4:8], "little") != len(bytes_data):
        raise ValueError("Package length mismatch / Длина пакета не совпадает")
    if int.from_bytes(dec[12:16], "little") != HDR_SIZE:
        raise ValueError("Invalid file-area offset / Неверное смещение файловой области")
    if dec[16:22].decode("latin1", "ignore") != "AC695X":
        raise ValueError("Chip type is not AC695X (not a TD-H9) / Чип не AC695X")

    # File list validation / Проверка списка файлов
    count = int.from_bytes(dec[8:10], "little")
    if not (1 <= count <= 6):
        raise ValueError("Invalid package file count / Неверное число файлов")
    # The list CRC is computed over the encrypted records, as in JS
    # (header.slice(0x40...)). / CRC списка считается по зашифрованным записям.
    if crc16(dec[0x40:0x40 + ENTRY_SIZE * count]) != int.from_bytes(dec[2:4], "little"):
        raise ValueError("Invalid file-list CRC / Недействительный CRC списка файлов")

    # Locate flash.bin among the records / Ищем flash.bin среди записей
    image = None
    for entry in range(count):
        rec_off = 0x40 + entry * ENTRY_SIZE
        record = bytearray(bytes_data[rec_off:rec_off + ENTRY_SIZE])
        dec_rec = jl_cipher(record)
        # Name is 16 bytes at offset 64 / имя — 16 байт со смещения 64
        name = bytes(dec_rec[64:80]).split(b"\x00")[0].decode("latin1", "ignore")
        if int.from_bytes(dec_rec[0:2], "little") == 0 and name == "flash.bin":
            if image is not None:
                raise ValueError("Multiple flash.bin found / Найдено несколько flash.bin")
            offset = int.from_bytes(dec_rec[8:12], "little")
            size = int.from_bytes(dec_rec[12:16], "little")
            if offset < HDR_SIZE or offset + size > len(bytes_data):
                raise ValueError("flash.bin outside package / flash.bin вне пакета")
            img = bytes(bytes_data[offset:offset + size])
            if crc16(img) != int.from_bytes(dec_rec[4:6], "little"):
                raise ValueError("Invalid flash.bin CRC / Недействительный CRC flash.bin")
            image = img
    if image is None:
        raise ValueError("No flash.bin in package / В пакете нет flash.bin")
    if len(image) != IMAGE_SIZE:
        raise ValueError("flash.bin size %d, expected %d / Размер flash.bin, ожидалось"
                         % (len(image), IMAGE_SIZE))
    return image


# --- Low-level HID layer ----------------------------------------------------
# --- HID низкоуровневый слой ------------------------------------------------

def build_report(command, body=b""):
    """Assemble a 64-byte HID report, like makePacket() in firmware.mjs.

    Собирает 64-байтный HID report, как makePacket() из firmware.mjs."""
    if len(body) > 56:
        raise ValueError("HID payload too long / HID payload слишком длинный")
    report = bytearray(64)
    report[0] = 0xAA
    report[1] = 0x55
    report[2:4] = (len(body) + 2).to_bytes(2, "little")
    report[4] = command
    report[6:6 + len(body)] = body
    # XOR-scramble payload+command with the key 0x4E 0x61 0xBC 0x00.
    # XOR-шифрование payload+команды ключом 0x4E 0x61 0xBC 0x00.
    key = [0x4E, 0x61, 0xBC, 0x00]
    for i in range(4, 6 + len(body)):
        report[i] ^= key[(i - 4) % 4]
    # Append CRC after command+body / CRC в конец после command+body
    end = 6 + len(body)
    report[end:end + 2] = crc16(report[0:end]).to_bytes(2, "little")
    return report


def parse_reply(report, command):
    """Parse a 64-byte HID reply and return the decrypted payload.

    Разбирает 64-байтный HID-ответ и возвращает расшифрованный payload."""
    # Fixed framing checks / Проверка фиксированных маркеров
    if len(report) != 64 or report[0] != 0xAA or report[1] != 0x55:
        raise RuntimeError("Malformed HID reply / Малформативный HID-ответ")
    length = int.from_bytes(report[2:4], "little")
    end = 4 + length
    if length < 2 or length > 58:
        raise RuntimeError("Invalid HID reply length / Некорректная длина HID-ответа")
    # CRC over the whole framed payload / CRC по всей рамке
    if crc16(report[0:end]) != int.from_bytes(report[end:end + 2], "little"):
        raise RuntimeError("HID reply CRC mismatch / CRC HID-ответа не совпадает")
    # Decrypt / Расшифровка
    content = bytearray(report[4:end])
    for i in range(len(content)):
        content[i] ^= [0x4E, 0x61, 0xBC, 0x00][i % 4]
    # Command echo and status check / Проверка эха команды и статуса
    if content[0] != command:
        raise RuntimeError("Unexpected HID reply command / Неожиданная команда HID-ответа")
    if content[1] != 0:
        raise RuntimeError("Radio rejected command 0x%02X (status %d) / Рация отклонила команду (статус)"
                           % (command, content[1]))
    return bytes(content[2:])


def u32(*vals):
    """Pack values as little-endian uint32. / Упаковывает значения как uint32 LE."""
    out = bytearray()
    for v in vals:
        out += v.to_bytes(4, "little")
    return bytes(out)


class H9HidRadio:
    """Thin wrapper around a hidapi Device exposing the H9 update protocol.

    Тонкая обёртка вокруг hidapi-устройства с протоколом обновления H9."""

    def __init__(self, hid_device):
        self.dev = hid_device  # the pyhidapi Device / pyhidapi-устройство

    @staticmethod
    def _send(dev, data):
        """Send one HID report (report ID first). / Отправляет HID-отчёт."""
        # hidapi expects the report ID as the first byte / report ID первым байтом
        if dev is None:
            raise RuntimeError("HID device is not open (dev=None) / Устройство не открыто (dev=None)")
        buf = bytes([REPORT_ID]) + data
        n = dev.write(buf)
        if n != len(buf):
            raise RuntimeError("HID write returned %d, expected %d / HID write вернул неверное число"
                               % (n, len(buf)))

    @staticmethod
    def _recv(dev):
        """Read one HID reply with a 5 s timeout. / Читает HID-ответ (таймаут 5 с)."""
        r = dev.read(64, timeout=5000)
        if r is None:
            raise RuntimeError("HID read timeout / Таймаут HID-read")
        return bytes(r)

    def exchange(self, command, body=b""):
        """Send a command and return the decrypted payload. / Отправляет команду и возвращает payload."""
        self._send(self.dev, build_report(command, body))
        return parse_reply(self._recv(self.dev), command)

    def initialize(self):
        """Read and validate the update geometry via CMD_INIT.

        Читает и проверяет геометрию обновления через CMD_INIT."""
        # 17-byte 'app_dir_head' request / 17-байтный запрос 'app_dir_head'
        b = bytearray(17)
        b[:12] = b"app_dir_head"
        res = self.exchange(CMD_INIT, bytes(b))
        if len(res) != 16:
            raise RuntimeError("Invalid radio geometry / Некорректная геометрия радио")
        address = int.from_bytes(res[0:4], "little")
        erase_off = int.from_bytes(res[8:12], "little")
        block = int.from_bytes(res[12:16], "little")
        # The geometry must match the expected layout / Геометрия должна совпадать с ожидаемой
        if address != APP_START or erase_off != 0 or block != SECTOR_SIZE:
            raise RuntimeError("Unexpected radio geometry (0x%X, %d, %d) / Неожиданная геометрия"
                               % (address, erase_off, block))

    def crcs(self, relative, count):
        """Read `count` sector CRCs starting at a sector-aligned offset.

        Читает `count` CRC секторов со смещения, кратного сектору."""
        if count < 1 or count > 16 or relative % SECTOR_SIZE != 0:
            raise RuntimeError("Invalid CRC request / Некорректный CRC-запрос")
        data = self.exchange(CMD_CRC, u32(relative, count * SECTOR_SIZE, SECTOR_SIZE))
        if len(data) != count * 2:
            raise RuntimeError("Invalid CRC reply / Некорректный CRC-ответ")
        return [int.from_bytes(data[i * 2:i * 2 + 2], "little") for i in range(count)]

    def window_crcs(self, relative, count):
        """Read `count` sliding 4 KiB CRC windows (unaligned support).

        Читает `count` сдвинутых CRC-окон по 4 КБ (поддержка несмещённых адресов)."""
        if relative < 0 or count < 1 or count > 16:
            raise RuntimeError("Invalid sliding CRC request / Некорректный sliding-запрос")
        data = self.exchange(CMD_CRC, u32(relative, count, 1))
        if len(data) != count * 2:
            raise RuntimeError("Invalid sliding CRC reply / Некорректный sliding-ответ")
        return [int.from_bytes(data[i * 2:i * 2 + 2], "little") for i in range(count)]

    def sector_crc(self, physical):
        """CRC of a single physical sector. / CRC одного физического сектора."""
        return self.crcs(physical - APP_START, 1)[0]

    def erase(self, physical):
        """Erase one physical sector. / Стирает один физический сектор."""
        self.exchange(CMD_ERASE, u32(physical - APP_START, 2))

    def write(self, physical, offset, data):
        """Program `data` at offset within a sector. / Записывает `data` по смещению в секторе."""
        body = u32(physical - APP_START + offset, len(data)) + data
        self.exchange(CMD_WRITE, body)


# --- Images and planning -----------------------------------------------------
# --- Образы и планирование ---------------------------------------------------

def expected_crcs(image):
    """Compute expected per-sector CRCs for the whole flash region.

    Вычисляет ожидаемые CRC по секторам для всей зоны flash."""
    out = []
    # Iterate all sectors from APP_START; beyond IMAGE_SIZE the image has no bytes.
    # Идём по всем секторам от APP_START; за IMAGE_SIZE данных в образе нет.
    for physical in range(APP_START, FLASH_SIZE, SECTOR_SIZE):
        out.append(crc16(image[physical:physical + SECTOR_SIZE]) if physical < IMAGE_SIZE else None)
    return out


def scan_radio(dev, on_progress=None):
    """Read the CRC of every accessible sector (no writes). / Читает CRC всех секторов (без записи).

    `on_progress` is an optional callable(done, total) so GUI callers can update
    their own progress; when it is None, a small console progress line is shown
    (safely, ignoring a missing stdout in windowed builds).
    `on_progress` — опциональный callable(done, total) для обновления прогресса
    в GUI; если None, показывается строка прогресса в консоли (безопасно,
    игнорируя отсутствие stdout в windowed-сборках)."""
    result = []
    total = (FLASH_SIZE - APP_START) // SECTOR_SIZE
    # Read 16 sectors per command / По 16 секторов за одну команду
    for relative in range(0, FLASH_SIZE - APP_START, SECTOR_SIZE * 16):
        count = min(16, (FLASH_SIZE - APP_START - relative) // SECTOR_SIZE)
        result += dev.crcs(relative, count)
        # Progress line / строка прогресса (safe for console=False exe)
        if on_progress is not None:
            on_progress(len(result), total)
        else:
            _console_progress(len(result), total)
    _console_progress(total, total)
    return result


def _console_progress(done, total):
    """Write a console progress line, ignoring a missing stdout (windowed exe).

    Пишет строку прогресса в консоль, игнорируя отсутствие stdout
    (windowed exe)."""
    out = sys.stdout
    if out is None:
        return
    try:
        out.write("\r  Scanning sector CRCs: %d/%d" % (done, total))
        out.flush()
    except Exception:
        pass


# Number of app sectors (firmware region) and the CRC of an erased sector.
# Число секторов приложения (зона прошивки) и CRC стёртого сектора.
APP_SECTORS = (IMAGE_SIZE - APP_START) // SECTOR_SIZE
BLANK = bytes([0xFF]) * SECTOR_SIZE
BLANK_CRC = crc16(BLANK)


def known_app_matches(hid, crcs, target):
    """Return True if the radio's live CRC scan exactly matches `target`.

    Checks both the plain per-sector CRCs and the shifted 4 KiB windows
    (SHIFTS) to rule out 16-bit CRC collisions, exactly like knownAppMatches()
    in readback.mjs. Returns False on any mismatch instead of raising.

    Возвращает True, если «живой» CRC-скан рации точно совпадает с `target».
    Проверяет и обычные CRC секторов, и сдвинутые окна 4 КБ (SHIFTS), чтобы
    исключить коллизии 16-битного CRC, как knownAppMatches() в readback.mjs."""
    if len(target) != IMAGE_SIZE:
        return False
    # 1) Plain per-sector CRCs / Обычные CRC по секторам
    for index in range(APP_SECTORS):
        start = APP_START + index * SECTOR_SIZE
        if crc16(target[start:start + SECTOR_SIZE]) != crcs[index]:
            return False
    # 2) Shifted-window CRCs / Сдвинутые CRC-окна
    for shift in SHIFTS:
        for start_index in range(0, APP_SECTORS - 1, 16):
            count = min(16, APP_SECTORS - 1 - start_index)
            try:
                observed = hid.window_crcs(start_index * SECTOR_SIZE + shift, count)
            except Exception:
                return False
            for index in range(count):
                physical = APP_START + (start_index + index) * SECTOR_SIZE + shift
                if observed[index] != crc16(target[physical:physical + SECTOR_SIZE]):
                    return False
    return True


def write_sector(dev, physical, target):
    """Erase then program one sector and verify it by CRC.

    Стирает, программирует и проверяет по CRC один сектор."""
    sector = target[physical:physical + SECTOR_SIZE]
    dev.erase(physical)
    # Erased sector must match 0xFF CRC (0xE03E) / Стирание даёт CRC 0xE03E
    if dev.sector_crc(physical) != crc16(bytes([0xFF]) * SECTOR_SIZE):
        raise RuntimeError("Erase verify failed at 0x%X / Ошибка проверки стирания 0x%X" % physical)
    # For the first sector write bytes 32+ first, then the 32-byte header last.
    # Для первого сектора сначала пишем байты 32+, затем 32-байтный заголовок последним.
    ranges = [[32, SECTOR_SIZE], [0, 32]] if physical == APP_START else [[0, SECTOR_SIZE]]
    for start, end in ranges:
        offset = start
        while offset < end:
            part = sector[offset:min(offset + 40, end)]
            # Skip already-erased bytes / Пропускаем уже стёртые байты
            if any(v != 0xFF for v in part):
                dev.write(physical, offset, part)
            offset += 40
    # Final verification / Итоговая проверка
    if dev.sector_crc(physical) != crc16(sector):
        raise RuntimeError("Write verify failed at 0x%X / Ошибка проверки записи 0x%X" % physical)


# --- Main / main --------------------------------------------------------------

def list_devices():
    """Print found JieLi HID devices and exit. / Печатает найденные HID-устройства JieLi."""
    import hid
    print("Scanning for JieLi HID devices (VID 0x%04X):" % CHIP_VID)
    found = False
    for d in hid.enumerate():
        vid = d['vendor_id']
        pid = d['product_id']
        if vid == CHIP_VID:
            found = True
            match = "(TD-H9 HID flash mode)" if pid == CHIP_PID else ""
            print("  VID:0x%04X PID:0x%04X  %s %s %s"
                  % (vid, pid, d.get('manufacturer_string', ''), d.get('product_string', ''), match))
    if not found:
        print("  None found. Check: radio off, hold PTT+2, power on, connect USB.")
    sys.exit(1 if not found else 0)


def choose_device():
    """Open the TD-H9 HID device (open by path, reliable on composite devices).

    Открывает HID-устройство TD-H9 (по path — надёжно на composite-устройствах)."""
    import hid
    errors = []
    for d in hid.enumerate():
        if d['vendor_id'] == CHIP_VID and d['product_id'] == CHIP_PID:
            try:
                return hid.Device(path=d['path'])
            except Exception as e:
                errors.append("%s" % e)
    if errors:
        raise RuntimeError("Failed to open TD-H9 HID (4C4A:4155): %s / Не удалось открыть HID: %s"
                           % ("; ".join(errors), "; ".join(errors)))
    raise RuntimeError("TD-H9 not found in HID flash mode (4C4A:4155). Check PTT+2 at power-on."
                       " / TD-H9 не найден в HID-режиме. Проверьте PTT+2 при включении.")


def main():
    """CLI entry point. / Точка входа CLI."""
    # Argument parser / Парсер аргументов
    ap = argparse.ArgumentParser(description="TIDRADIO TD-H9 flasher (HID mode) / Прошивка TD-H9 (HID-режим)")
    ap.add_argument("--list", action="store_true", help="list found JieLi HID devices / показать найденные HID-устройства")
    ap.add_argument("--fw", help="path to .fw firmware package / путь к пакету прошивки .fw")
    ap.add_argument("--backup", help="save current firmware to file before flashing (optional) / сохранить текущую прошивку в файл (опционально)")
    ap.add_argument("--verify", action="store_true",
                    help="run the slow full shifted-CRC pre-flash verification (like the web flasher) "
                         "/ выполнить медленную полную проверку по сдвинутым CRC перед записью (как web-флешер)")
    ap.add_argument("--nocheck", action="store_true",
                    help="skip CRC comparison before flashing and write anyway "
                         "/ пропустить сверку CRC перед записью и заливать без остановки")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation / не спрашивать подтверждения")
    args = ap.parse_args()

    if args.list:
        list_devices()
        return

    if not args.fw:
        print("Specify --fw <file.fw> (or --list). Укажите --fw <файл.fw> (или --list).")
        return 1

    if not os.path.isfile(args.fw):
        print("Firmware file not found: %s. Файл прошивки не найден: %s" % (args.fw, args.fw))
        return 1

    import hid

    print("Reading package: %s  Читаю пакет: %s" % (args.fw, args.fw))
    with open(args.fw, "rb") as f:
        pkg = f.read()

    # Unpack the .fw package / Распаковка пакета .fw
    try:
        image = decode_package(pkg)
    except ValueError as e:
        print("Package unpack error: %s  Ошибка распаковки пакета: %s" % (e, e))
        return 1
    # Summarize what we loaded / Краткая сводка о загруженном
    pkg_kind = "official v1.0.38" if hashlib_sha256(pkg) == STOCK_PACKAGE_SHA else "custom build"
    img_kind = "stock v1.0.38" if hashlib_sha256(image) == STOCK_IMAGE_SHA else "build"
    print("  Package SHA-256: %s (%s)  Пакет: (%s)"
          % (hashlib_sha256(pkg), pkg_kind, "официальный v1.0.38" if pkg_kind == "official v1.0.38" else "кастомная сборка"))
    print("  flash.bin SHA-256: %s (%s)  Образ: (%s)"
          % (hashlib_sha256(image), img_kind, img_kind))
    if len(image) != IMAGE_SIZE:
        print("Package produced a %d-byte image, expected %d - refused."
              " Пакет дал образ %d байт, ожидалось %d - отказ." % (len(image), IMAGE_SIZE, len(image), IMAGE_SIZE))
        return 1

    print("Opening TD-H9 HID (PTT+2)...  Открываю TD-H9 HID (PTT+2)...")
    dev = choose_device()
    radio = H9HidRadio(dev)
    radio.initialize()
    print("  Radio connected, geometry valid.  Рация подключена, геометрия валидна.")

    target = image  # the image to install / образ, который ставим

    # Scan the current on-radio CRCs (no writes) / Скан текущих CRC рации (без записи)
    cur = scan_radio(radio)
    tgt_crcs = expected_crcs(target)

    # Does the radio match the target over the firmware region? / Совпадает ли радио с целью?
    matches = all(cur[i] == tgt_crcs[i]
                  for i in range((IMAGE_SIZE - APP_START) // SECTOR_SIZE))

    # --nocheck: skip CRC comparison and flash regardless. Useful to force a
    # rewrite even when the radio CRC already matches (e.g. after a resume, or
    # to re-write sectors that verified equal but are suspected corrupt).
    # --nocheck: пропускаем сверку CRC и заливаем при любом состоянии. Удобно,
    # чтобы принудительно перезаписать даже когда CRC радио совпадает
    # (например, после resume или для перезаписи секторов с подозрением на порчу).
    if args.nocheck:
        print("--nocheck set: skipping CRC comparison, will flash all target sectors."
              "  Задан --nocheck: пропускаю сверку CRC, перезапишу все целевые секторы.")

    # Optional full pre-flash verification (--verify). The web flasher ALWAYS
    # runs knownAppMatches() before writing: it re-checks shifted 4 KiB CRC
    # windows to prove the live radio bytes equal the target, catching 16-bit
    # CRC collisions. That pass is slow (thousands of 0xC4 requests), so here
    # it is opt-in with --verify; the default stays fast.
    # Опциональная полная проверка перед записью (--verify). Web-флешер ВСЕГДА
    # запускает knownAppMatches() перед записью: перепроверяет сдвинутые окна
    # CRC, доказывая, что «живые» байты рации равны образу, и ловит коллизии
    # 16-битного CRC. Этот проход медленный (тысячи запросов 0xC4), поэтому
    # здесь он включается флагом --verify; по умолчанию — быстрый режим.
    if args.verify and not args.nocheck:
        print("Running full shifted-CRC verification (slow)..."
              "  Выполняю полную проверку по сдвинутым CRC (медленно)...")
        if not known_app_matches(radio, cur, target):
            print("ERROR: radio does NOT match the target image on shifted-CRC windows. "
                  "No writes allowed."
                  "  ОШИБКА: рация не совпадает с целевым образом по сдвинутым CRC. "
                  "Запись запрещена.")
            return 3
        print("  Shifted-CRC verification passed.  Проверка по сдвинутым CRC пройдена.")

    # Optional backup: save the matched image (this is NOT a full HID recovery).
    # Full HID reconstruction 0x5000..0xFFFFF from sliding CRCs needs ~12-65k
    # requests and is implemented in the web flasher; here we save the known
    # image only when the radio matches it, otherwise we just warn.
    # Опциональный бэкап: сохраняем совпавший образ (это НЕ полное HID-восстановление).
    # Полная HID-реконструкция 0x5000..0xFFFFF по sliding-CRC требует ~12-65k
    # запросов и есть в web-флешере; здесь сохраняем известный образ только если
    # рация ему соответствует, иначе предупреждаем.
    if args.backup:
        if matches:
            with open(args.backup, "wb") as f:
                f.write(target)
            print("  Backup of the matched image saved: %s  Бэкап сохранён: %s" % (args.backup, args.backup))
            print("  (firmware area 0x5000..0xDA000; boot 0x0000-0x4FFF not saved)"
                  "  (область прошивки 0x5000..0xDA000; boot не сохранялся)")
        else:
            print("  WARNING: radio does NOT match the target image 100%."
                  "  ВНИМАНИЕ: рация НЕ соответствует целевому образу на 100%.")
            print("  Full HID backup requires sliding-CRC reconstruction; use the web flasher"
                  "  Continuing without saving a backup."
                  "  Полный HID-бэкап требует реконструкции по sliding-CRC; используйте web-флешер"
                  "  Продолжаю без сохранения бэкапа.")

    # Determine which sectors differ. With --nocheck, take ALL app sectors
    # (force a full rewrite regardless of current CRC).
    # Определяем отличающиеся секторы. С --nocheck берём ВСЕ секторы
    # приложения (принудительная полная перезапись независимо от текущего CRC).
    if args.nocheck:
        changed = [APP_START + i * SECTOR_SIZE
                   for i in range((IMAGE_SIZE - APP_START) // SECTOR_SIZE)]
    else:
        changed = []
        for i in range(len(cur)):
            physical = APP_START + i * SECTOR_SIZE
            if physical >= IMAGE_SIZE:
                continue
            if cur[i] != tgt_crcs[i]:
                changed.append(physical)

    if not changed:
        print("Radio already fully matches the target image. Nothing to write."
              " Рация уже полностью совпадает. Нечего писать.")
        return 0

    print("Sectors to rewrite (%d):  Будут перезаписаны секторы (%d):" % (len(changed), len(changed)))
    for c in changed:
        print("  0x%05X" % c)

    print("\nNOTE / ОБРАТИТЕ ВНИМАНИЕ:")
    print("  * HID mode does not write the boot area 0x0000-0x4FFF or settings 0xDA000+."
          "  / HID-режим не пишет boot-область и настройки 0xDA000+.")
    print("  * Before the first flash, strongly recommend a full backup"
          "  / Перед первым флешем рекомендуется полный бэкап web-флешером.")

    if not args.yes:
        inp = input("Proceed with flashing? [y/N]  Продолжить заливку? [y/N] ").strip().lower()
        if inp not in ("y", "yes"):
            print("Aborted.  Отменено.")
            return 0

    # Write sectors high-to-low, as in the original plan / Пишем секторы от старших к младшим
    for idx, physical in enumerate(changed, 1):
        print("  [%d/%d] sector 0x%05X...  сектор 0x%05X..." % (idx, len(changed), physical, physical),
              end=" ", flush=True)
        try:
            write_sector(radio, physical, target)
            print("OK")
        except Exception as e:
            print("\nERROR at sector 0x%X: %s  ОШИБКА на секторе 0x%X: %s" % (physical, e, physical, e))
            print("Radio is now partially flashed. Re-run to re-check sectors."
                  "  Рация частично записана. Повторите запуск — секторы перепроверяются.")
            return 2

    # Final full check / Финальная проверка всех секторов
    print("Final verification of all sectors...  Финальная проверка всех секторов...")
    after = scan_radio(radio)
    tgt_crcs = expected_crcs(target)
    bad = 0
    for i in range(len(after)):
        physical = APP_START + i * SECTOR_SIZE
        if physical >= IMAGE_SIZE:
            continue
        if after[i] != tgt_crcs[i]:
            bad += 1
            print("  MISMATCH / НЕ СОВПАДАЕТ: 0x%X" % physical)
    if bad:
        print("Verification failed: %d sector(s) mismatch.  Проверка не прошла: %d секторов."
              % (bad, bad))
        return 2

    print("Done: all target sectors written and CRC-verified."
          "  Готово: все целевые секторы записаны и подтверждены по CRC.")
    print("Disconnect USB and power-cycle the radio.  Отключите USB и перезапустите рацию.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.  Прервано пользователем.")
        sys.exit(130)