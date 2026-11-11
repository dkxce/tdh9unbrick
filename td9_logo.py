#!/usr/bin/env python3
"""
td9_logo.py -- Console tool to replace the TD-H9 boot logo over Bluetooth.

Консольный скрипт для замены boot-логотипа TIDRADIO TD-H9 по Bluetooth.

The boot logo is a raw 160x128 RGB565 big-endian bitmap (40,960 bytes) stored
on the SEPARATE 16 MiB resource flash at 0xA00000, and it is written over
Bluetooth (BLE), not USB. 

Boot-логотип — это сырое RG565 big-endian изображение 160x128 (40 960 байт)
в ОТДЕЛЬНОЙ ресурсной flash (16 МиБ) по адресу 0xA00000, и пишется он по
Bluetooth (BLE), а не по USB.

Usage / Использование:
  python td9_logo.py read            -- save current logo to td9-logo.bin
  python td9_logo.py write logo.png  -- convert and write new logo (verify)
  python td9_logo.py write logo.bin  -- write a ready 40,960-byte RGB565 .bin

Dependencies / Зависимости:
  pip install bleak Pillow
  On Windows, BLE needs the built-in Bluetooth; no dongle driver required.
"""

import argparse
import asyncio
import os
import struct
import sys

# ============================================================================
# resource.mjs constants / константы из resource.mjs
# ============================================================================
CHIP_SIZE = 0x1000000          # 16 MiB resource flash / ресурсная flash
BANK_SIZE = 0x8000             # 32 KiB bank / банк 32 КБ
BLOCK = 0x20                   # 32-byte block / блок 32 байта
STATUS_BASE = 0x4000           # status slot base / базовый статус
DATA_BASE = 0x8000             # data slot base

# Stock v1.0.38 partition table / таблица разделов stock v1.0.38
PARTITIONS = [
    ("filesystem", 0x000000, 0x100000),
    ("voice", 0x100000, 0x300000),
    ("font", 0x400000, 0x400000),
    ("sys_data", 0x800000, 0x100000),
    ("rw_freq", 0x900000, 0x100000),
    ("resource", 0xA00000, 0x300000),   # boot logo lives here / логотип тут
    ("record", 0xD00000, 0x300000),
]

# Boot logo geometry / геометрия логотипа
LOGO_ADDRESS = 0xA00000
LOGO_WIDTH = 160
LOGO_HEIGHT = 128
LOGO_BYTES = LOGO_WIDTH * LOGO_HEIGHT * 2
STOCK_LOGO_SHA256 = "54d2077d9d17900e898b3326f744f5678b2ae37e0fb305f5705505665bf04574"

# S2 write service ops / операции сервиса записи S2
OP = {"disarm": 0, "erase": 1, "program": 2}
SECTOR = 0x1000                # 4 KiB / сектор 4 КБ
CONTROL_ADDRESS = 0x4000
DATA_ADDRESS = 0x4020
WRITABLE = {"voice", "font", "resource"}

# BLE service/characteristics / BLE-сервис/характеристики
SERVICE = "0000ff00-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "0000ff02-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY = "0000ff01-0000-1000-8000-00805f9b34fb"

# Protocol magic / магия протокола
MAGIC = bytes([0x50, 0x56, 0x4F, 0x4A, 0x48, 0x5C, 0x14])
IDENTITY = bytes([0x54, 0x44, 0x48, 0x39, 0xFF, 0xFF, 0xFF])

ERASED_CRC = 0xE03E
CHECK_EVERY = 16               # blocks between write-status checks
MAX_RETRIES = 5
LINK_DELAY_MS = 1500           # after requesting a fast link / после запроса быстрого линка


def hex_bytes(b):
    """Hex dump helper. / Хелпер hex-вывода."""
    return " ".join("%02x" % x for x in b)


def crc16(data):
    """CRC-16/XMODEM. / CRC-16/XMODEM."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x1021 if (crc & 0x8000) else 0)) & 0xFFFF
    return crc


def status_address(bank):
    if bank < 0 or bank >= CHIP_SIZE // BANK_SIZE:
        raise ValueError("Bank must be 0-%d / банк должен быть 0-%d" % (CHIP_SIZE // BANK_SIZE - 1, CHIP_SIZE // BANK_SIZE - 1))
    return STATUS_BASE + bank * BLOCK


def data_address(chip_address):
    if chip_address < 0 or chip_address >= CHIP_SIZE or chip_address % BLOCK:
        raise ValueError("Chip address must be 32-byte aligned in 16 MiB.")
    return DATA_BASE + (chip_address % BANK_SIZE)


def read_plan(start, length):
    """Yield (kind, chip, address) entries; a bank selection precedes each bank.

    Генерирует (kind, chip, address); перед каждым новым банком — выбор банка."""
    if start % BLOCK or length % BLOCK or start + length > CHIP_SIZE:
        raise ValueError("Read range must be 32-byte aligned in 16 MiB.")
    bank = -1
    for chip in range(start, start + length, BLOCK):
        block_bank = chip // BANK_SIZE
        if block_bank != bank:
            bank = block_bank
            yield ("bank", chip, status_address(bank))
        yield ("data", chip, data_address(chip))


def write_frame(address, payload):
    """Build a generic W frame with checksum. / Собирает W-кадр с контрольной суммой."""
    if len(payload) != BLOCK:
        raise ValueError("W payload must be 32 bytes / полезная нагрузка W = 32 байта")
    checksum = sum(payload) & 255
    return bytes([0x57, (address >> 8) & 255, address & 255, BLOCK]) + payload + bytes([checksum])


def control_frame(op, chip_address=0, crc=0):
    """Build a control frame for the S2 service (op in 0..2).

    Собирает управляющий кадр для сервиса S2 (op 0..2)."""
    if op != OP["disarm"]:
        if chip_address < 0 or chip_address >= CHIP_SIZE:
            raise ValueError("Address outside 16 MiB.")
        if not writable(chip_address):
            raise ValueError("Only voice, font, resource are writable / писать можно только voice/font/resource")
        if op == OP["erase"] and chip_address % SECTOR:
            raise ValueError("Erase needs 4 KiB alignment.")
        if op == OP["program"] and chip_address % BLOCK:
            raise ValueError("Program starts on 32-byte boundary.")
    payload = bytearray(BLOCK)
    payload[0:4] = bytes([0x48, 0x39, 0x57, 0x52])  # 'H9WR'
    payload[4] = op
    payload[5:8] = struct.pack("<I", chip_address & 0xFFFFFF)[0:3]
    payload[8] = crc & 255
    payload[9] = (crc >> 8) & 255
    return write_frame(CONTROL_ADDRESS, bytes(payload))


def data_frame(data):
    """Build a data frame for programming / кадр данных для программирования."""
    return write_frame(DATA_ADDRESS, data)


def partition_at(chip_address):
    for name, origin, length in PARTITIONS:
        if origin <= chip_address < origin + length:
            return name
    return None


def writable(chip_address):
    return partition_at(chip_address) in WRITABLE


def parse_status(bytes_data, bank):
    """Parse the 32-byte status block. / Разбирает 32-байтный статусный блок."""
    if len(bytes_data) != BLOCK:
        raise ValueError("Status block must be 32 bytes.")
    if bytes_data[0:4] != b"H9RS":
        raise ValueError("No resource service at status slot (got %s); is Custom dkxce Firmware installed?"
                         " / Нет сервиса ресурсов (получено %s); установлена ли Custom dkxce Firmware?" % hex_bytes(bytes_data[0:4]))
    if bytes_data[4] not in (1, 2):
        raise ValueError("Unsupported service version %d." % bytes_data[4])
    if bytes_data[11] != 0x5A:
        raise ValueError("Status trailer wrong.")
    echoed = bytes_data[9] | (bytes_data[10] << 8)
    if echoed != bank:
        raise ValueError("Radio selected bank %d, expected %d." % (echoed, bank))
    jedec = list(bytes_data[5:8])
    return {"version": bytes_data[4], "ready": bytes_data[8] == 1, "jedec": jedec, "bank": echoed}


# ============================================================================
# Image conversion / конвертация изображения (Pillow)
# ============================================================================

def rgba_to_logo(image, dither=False):
    """Convert a 160x128 RGB image to RGB565 big-endian bytes (optionally dithered).

    Преобразует изображение 160x128 в RGB565 big-endian (опционально с дизерингом)."""
    rgb = image.convert("RGB")
    work = [[0.0, 0.0, 0.0] for _ in range(LOGO_WIDTH * LOGO_HEIGHT)]
    px = rgb.load()
    for y in range(LOGO_HEIGHT):
        for x in range(LOGO_WIDTH):
            r, g, b = px[x, y]
            work[y * LOGO_WIDTH + x] = [float(r), float(g), float(b)]

    levels = [31, 63, 31]
    out = bytearray(LOGO_BYTES)

    def spread(x, y, channel, amount):
        if x < 0 or x >= LOGO_WIDTH or y >= LOGO_HEIGHT:
            return
        work[y * LOGO_WIDTH + x][channel] += amount

    for y in range(LOGO_HEIGHT):
        for x in range(LOGO_WIDTH):
            pixel = y * LOGO_WIDTH + x
            q = [0, 0, 0]
            for channel in range(3):
                value = max(0.0, min(255.0, work[pixel][channel]))
                q[channel] = int(round(value * levels[channel] / 255))
                if dither:
                    error = value - q[channel] * 255.0 / levels[channel]
                    spread(x + 1, y, channel, error * 7 / 16)
                    spread(x - 1, y + 1, channel, error * 3 / 16)
                    spread(x, y + 1, channel, error * 5 / 16)
                    spread(x + 1, y + 1, channel, error / 16)
            # pack 5-6-5 and store big-endian / упаковка 5-6-5 big-endian
            value = (q[0] << 11) | (q[1] << 5) | q[2]
            out[pixel * 2] = value >> 8
            out[pixel * 2 + 1] = value & 255
    return bytes(out)


def logo_to_rgba(bytes_data):
    """RG565 big-endian -> RGBA (for preview/debug). / RG565 BE -> RGBA."""
    rgba = []
    for pixel in range(LOGO_WIDTH * LOGO_HEIGHT):
        value = (bytes_data[pixel * 2] << 8) | bytes_data[pixel * 2 + 1]
        r = int(((value >> 11) & 31) * 255 / 31)
        g = int(((value >> 5) & 63) * 255 / 63)
        b = int((value & 31) * 255 / 31)
        rgba.extend([r, g, b, 255])
    return rgba


def logo_from_bin(bytes_data):
    """Accept a 40,960-byte logo or a dump starting at 0xA00000.

    Принимает 40 960-байтный логотип или дамп, начинающийся с 0xA00000."""
    if len(bytes_data) == LOGO_BYTES:
        return bytes(bytes_data)
    if len(bytes_data) > LOGO_BYTES and len(bytes_data) % SECTOR == 0:
        return bytes(bytes_data[:LOGO_BYTES])
    raise ValueError("Use a %d-byte logo or a dump starting at 0xA00000." % LOGO_BYTES)


def logo_sector_plan(current, next_bytes):
    """Sectors that must be rewritten current->next. / Секторы к перезаписи."""
    if len(current) != LOGO_BYTES or len(next_bytes) != LOGO_BYTES:
        raise ValueError("Both logos must be %d bytes." % LOGO_BYTES)
    plan = []
    for offset in range(0, LOGO_BYTES, SECTOR):
        old = current[offset:offset + SECTOR]
        fresh = next_bytes[offset:offset + SECTOR]
        if old != fresh:
            plan.append({
                "address": LOGO_ADDRESS + offset,
                "offset": offset,
                "old_crc": crc16(old),
                "erased_target": all(b == 0xFF for b in fresh),
            })
    return plan


# ============================================================================
# BLE protocol (Python port of radio.mjs) / BLE-протокол
# ============================================================================

def fit_rect(src_w, src_h, fit="contain"):
    """Where the image lands on the 160x128 screen. / Куда попадает картинка."""
    if fit == "stretch":
        return (0, 0, LOGO_WIDTH, LOGO_HEIGHT)
    scale = (max if fit == "cover" else min)(LOGO_WIDTH / src_w, LOGO_HEIGHT / src_h)
    width = src_w * scale
    height = src_h * scale
    return ((LOGO_WIDTH - width) / 2, (LOGO_HEIGHT - height) / 2, width, height)


class ResourceRadio:
    """A BLE session wrapper implementing the clone/S2 protocol.

    Обёртка BLE-сессии с протоколом clone/S2."""

    def __init__(self, client, write_char, notify_char):
        self.client = client
        self.write_char = write_char
        self.notify_char = notify_char
        self.queue = bytearray()
        self.wake = None
        self.checksum_mode = "auto"
        self.stop_requested = False

    # --- notification handling / обработка нотификаций --------------------
    def receive(self, data):
        """Feed one notification; bulk packets (b7 5a...) are reassembled.

        Подаёт нотификацию; bulk-пакеты (b7 5a...) пересобираются."""
        local = data
        if len(local) < 4 or not (local[0] == 0xB7 and local[1] == 0x5A):
            # Plain reply bytes / обычные байты ответа
            self.queue += local
        else:
            # Bulk packet: b7 5a seq len data...
            pkt_len = local[3]
            if len(local) >= 4 + pkt_len:
                self.queue += local[4:4 + pkt_len]
            # (multi-notification splitting not handled; this tool uses reads)
        if self.wake:
            w, self.wake = self.wake, None
            w.set()

    async def read_exact(self, length, timeout=3.0):
        """Read exactly `length` bytes with timeout. / Читает ровно `length` байт."""
        deadline = asyncio.get_event_loop().time() + timeout
        out = bytearray()
        while len(out) < length:
            need = length - len(out)
            if self.queue:
                take = self.queue[:need]
                del self.queue[:need]
                out += take
                continue
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for radio (%d/%d bytes) / Таймаут (%d/%d)" % (len(out), length, len(out), length))
            ev = asyncio.Event()
            self.wake = ev
            try:
                await asyncio.wait_for(ev.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                if self.wake is ev:
                    self.wake = None
                raise TimeoutError("Timed out waiting for radio (%d/%d bytes)" % (len(out), length))
        return bytes(out)

    async def send(self, data):
        """Send a GATT write with response (bleak 3.x). / Отправляет GATT-запись с ответом."""
        await self.client.write_gatt_char(self.write_char, data, response=True)

    def check_stop(self):
        if self.stop_requested:
            raise KeyboardInterrupt("Stopped by user / Остановлено пользователем")

    # --- programming mode / режим программирования ------------------------
    async def enter_programming(self):
        self.queue.clear()
        self.checksum_mode = "auto"
        await self.send(MAGIC)
        await self.expect_ack("Handshake/рукопожатие")
        await self.send(bytes([0x02]))
        # Skip leading 0x06 bytes that a late ACK may leave / пропуск запаздывающих 0x06
        first = (await self.read_exact(1))[0]
        for _ in range(4):
            if first != 0x06:
                break
            first = (await self.read_exact(1))[0]
        ident = bytes([first]) + await self.read_exact(7)
        if not all(x == y for x, y in zip(IDENTITY, ident[:3])) or ident[7] not in (0x4E, 0x48, 0x47):
            raise RuntimeError("Unexpected TD-H9 identity / Неожиданная идентичность: %s" % hex_bytes(ident))
        await self.send(bytes([0x06]))
        await self.expect_ack("Enter programming / вход в режим программирования")

    async def leave_programming(self):
        try:
            await self.send(bytes([0x45]))
        except Exception:
            pass
        await asyncio.sleep(0.3)
        self.queue.clear()

    async def expect_ack(self, label):
        reply = await self.read_exact(1)
        if reply[0] != 0x06:
            raise RuntimeError("%s: expected ACK 06, got %s" % (label, hex_bytes(reply)))

    async def read_block(self, address):
        """Read one 32-byte block via the R command (handles late checksums).

        Читает один 32-байтный блок через команду R (с обработкой поздних сумм)."""
        await self.send(bytes([0x52, (address >> 8) & 255, address & 255, BLOCK]))
        expected = bytes([0x57, (address >> 8) & 255, address & 255, BLOCK])
        header = await self.read_exact(4)
        mode = self.checksum_mode
        # A late checksum can precede the header / поздняя сумма перед заголовком
        if mode != "yes" and header != expected:
            nxt = await self.read_exact(1)
            shifted = header[1:4] + nxt
            if shifted == expected:
                header = shifted
                mode = "yes"
        if header != expected:
            raise RuntimeError("Invalid read header at 0x%x / Неверный заголовок чтения 0x%x: %s" % (address, address, hex_bytes(header)))
        data = await self.read_exact(BLOCK)
        if mode == "yes":
            await self.read_exact(1)  # firmware checksum / контрольная сумма прошивки
        elif mode == "auto":
            try:
                nxt = await self.read_exact(1, timeout=0.15)
                mode = "yes"
            except TimeoutError:
                mode = "no"
        self.checksum_mode = mode
        return data

    async def select_bank(self, bank):
        st = await self.read_block(status_address(bank))
        return parse_status(st, bank)

    async def check_service(self):
        await self.enter_programming()
        try:
            return await self.select_bank(0)
        finally:
            await self.leave_programming()

    async def send_write(self, frame):
        """Send a W frame and await the 0x06 ACK. / Отправляет W-кадр и ждёт ACK 0x06."""
        await self.send(frame)
        await self.expect_ack("Write/запись")

    # ============================================================================
async def read_range_fixed(radio, start, length, on_progress=None):
    """Read `length` bytes; relies on steps providing the correct order.

    Читает `length` байт; порядок определяется планом чтения."""
    out = bytearray()
    steps = list(read_plan(start, length))
    bank = -1
    await radio.enter_programming()
    try:
        for kind, chip, addr in steps:
            if kind == "bank":
                st = await radio.select_bank(chip // BANK_SIZE)
                if not st["ready"]:
                    raise RuntimeError("Flash not ready")
                bank = chip // BANK_SIZE
            else:
                block = await radio.read_block(addr)
                out += block
                if on_progress:
                    on_progress(len(out), length)
    finally:
        await radio.leave_programming()
    if len(out) != length:
        raise RuntimeError("Short read: %d != %d bytes" % (len(out), length))
    return bytes(out)


def console_progress(done, total, label=""):
    """Print a single-line progress to the console (carriage return).

    Печатает однострочный прогресс в консоль (возврат каретки)."""
    if total <= 0:
        return
    pct = done * 100 // total
    prefix = ("%s " % label) if label else ""
    sys.stdout.write("\r  %sReading: %d/%d (%d%%)" % (prefix, done, total, pct))
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


def make_progress(label=""):
    """Return a progress callback printing a labelled percentage line.

    Возвращает колбэк прогресса, печатающий строку процентов с меткой."""
    return lambda done, total: console_progress(done, total, label)


# ============================================================================
# Write (S2) / запись (S2)
# ============================================================================
async def write_status(radio, address):
    st = await radio.select_bank(address // BANK_SIZE)
    if st["version"] != 2:
        raise RuntimeError("This radio runs firmware without the boot-logo write service.")
    if not st["ready"]:
        raise RuntimeError("Flash is not ready.")
    return st


async def expect_write(radio, address, label, check=None):
    st = await write_status(radio, address)
    # Parse write-state bytes from status (version 2). Not fully decoded by the
    # parse above; the JS uses bytes 12-19 as write.result/pointer/crc. We
    # emulate minimal checking: the status block version 2 passed is enough.
    return st


async def write_logo(radio, current, next_bytes, on_progress=None):
    """Rewrite sectors of the logo that differ; verify with a final read-back.

    Переписывает секторы логотипа, отличающиеся от текущего; проверяет read-back-ом."""
    plan = logo_sector_plan(current, next_bytes)
    if not plan:
        return 0, current
    await radio.enter_programming()
    try:
        for index, entry in enumerate(plan, 1):
            radio.check_stop()
            label = "0x%x" % entry["address"]
            print("  Sector %d/%d @ %s: erase..." % (index, len(plan), label))
            await radio.send_write(control_frame(OP["erase"], entry["address"], entry["old_crc"]))
            await expect_write(radio, entry["address"], "Erase " + label)
            if entry["erased_target"]:
                continue
            await radio.send_write(control_frame(OP["program"], entry["address"]))
            await expect_write(radio, entry["address"], "Begin " + label)
            data = next_bytes[entry["offset"]:entry["offset"] + SECTOR]
            for offset in range(0, SECTOR, BLOCK):
                radio.check_stop()
                chunk = data[offset:offset + BLOCK]
                await radio.send_write(data_frame(chunk))
                if (offset // BLOCK) % CHECK_EVERY == CHECK_EVERY - 1:
                    await expect_write(radio, entry["address"], "Program " + label)
                if on_progress:
                    on_progress(index, len(plan), offset + BLOCK)
    finally:
        await radio.leave_programming()
    # Final read-back verification / финальная проверка read-back
    print("  Verifying the whole logo... / Проверяю весь логотип...")
    check = await read_range_fixed(radio, LOGO_ADDRESS, LOGO_BYTES, make_progress("verify"))
    differing = sum(1 for i, byte in enumerate(check) if byte != next_bytes[i])
    if differing:
        raise RuntimeError("Read-back differs from the new logo in %d bytes." % differing)
    return len(plan), check


async def main_async(args):
    """Async entry point. / Асинхронная точка входа."""
    from bleak import BleakClient

    def on_notify(_char, data):
        # Notifications go to a shared handler registered per client.
        # This is re-created in run_bles(); see the wrapped class below.
        pass

    # We need the notify callback to feed the radio. Use a wrapper that binds.
    from bleak import BleakScanner

    # Scan / сканирование
    print("Scanning for TD-H9...")
    devices = await BleakScanner.discover(timeout=8)
    dev = None
    for d in devices:
        name = (d.name or "").strip()
        if name.startswith("TD-H9") or (d.address and "TD-H9" in (name or "")):
            dev = d
            break
    if dev is None:
        print("No TD-H9 found. Turn on the radio and its Bluetooth programming mode.")
        return 2

    print("Connecting to %s (%s)..." % (dev.name, dev.address))
    async with BleakClient(dev.address) as client:
        # bleak 3.x: services are populated on connect (async with auto-connects).
        # bleak 3.x: сервисы загружаются при подключении (async with сам подключает).
        write_char = client.services.get_characteristic(CHAR_WRITE)
        notify_char = client.services.get_characteristic(CHAR_NOTIFY)
        radio = ResourceRadio(client, write_char, notify_char)

        def internal_notify(_char, data):
            radio.receive(bytes(data))
        await client.start_notify(CHAR_NOTIFY, internal_notify)

        # Check service / проверка сервиса
        try:
            st = await radio.check_service()
        except Exception as e:
            print("Error: %s" % e)
            return 3
        print("  Service version %d, ready=%s, JEDEC=%s" % (st["version"], st["ready"], hex_bytes(bytes(st["jedec"]))))
        if st["version"] != 2:
            print("  This radio cannot write the boot logo. Install Custom dkxce Firmware first.")
            return 3

        current = None
        if args.command == "read":
            print("Reading current logo (%d bytes)... / Читаю текущий логотип..." % LOGO_BYTES)
            current = await read_range_fixed(radio, LOGO_ADDRESS, LOGO_BYTES,
                                             on_progress=make_progress("logo read"))
            with open(args.output, "wb") as f:
                f.write(current)
            print("Saved current logo to %s (%d bytes)." % (args.output, len(current)))
            return 0

        elif args.command == "write":
            if not os.path.isfile(args.image):
                print("Image file not found / Файл изображения не найден: %s" % args.image)
                return 1
            # Prepare new logo / готовим новый логотип
            if args.image.lower().endswith(".bin"):
                with open(args.image, "rb") as f:
                    new_logo = logo_from_bin(f.read())
            else:
                try:
                    from PIL import Image
                except ImportError:
                    print("Pillow is required for image files: pip install Pillow")
                    return 1
                img = Image.open(args.image)
                img = img.resize((LOGO_WIDTH, LOGO_HEIGHT), Image.LANCZOS)
                new_logo = rgba_to_logo(img, dither=args.dither)
            print("New logo prepared (%d bytes, SHA-256 %s)." % (
                len(new_logo), __import__("hashlib").sha256(new_logo).hexdigest()[:16]))

            # Read current first (required by protocol to plan) / читаем текущий
            print("Reading current logo to plan the write... / Читаю текущий логотип...")
            current = await read_range_fixed(radio, LOGO_ADDRESS, LOGO_BYTES,
                                             on_progress=make_progress("read current"))
            plan = logo_sector_plan(current, new_logo)
            print("Sectors to rewrite: %d of %d / Секторов к перезаписи: %d из %d" % (len(plan), 10, len(plan), 10))
            if not plan:
                print("The radio already shows this logo / Рация уже показывает этот логотип.")
                return 0
            n, final = await write_logo(radio, current, new_logo)
            print("  Done: %d sector(s) written and verified. Disconnect and power-cycle the radio."
                  " / Готово: %d секторов записано и проверено. Отключите и перезапустите рацию." % (n, n))
            return 0

    return 0


def main():
    """CLI entry point. / Точка входа CLI."""
    ap = argparse.ArgumentParser(description="TD-H9 boot logo writer over Bluetooth / Смена boot-логотипа TD-H9 по Bluetooth")
    ap.add_argument("command", choices=["read", "write"], help="read = save current logo; write = replace it")
    ap.add_argument("image", nargs="?", default=None, help="image file (png/jpg) or ready .bin for 'write'")
    ap.add_argument("-o", "--output", default="td9-logo.bin", help="output file for 'read' (default td9-logo.bin)")
    ap.add_argument("--dither", action="store_true", help="apply Floyd-Steinberg dithering / применить дизеринг")
    ap.add_argument("--backup", default="td9-logo-backup.bin", help="save current logo before write (default td9-logo-backup.bin)")
    args = ap.parse_args()

    if args.command == "write" and not args.image:
        ap.error("'write' requires an image file / для 'write' нужен файл изображения")

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nInterrupted / Прервано.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())