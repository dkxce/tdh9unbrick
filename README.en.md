# Tidradio TD-H9 HID Unbricker

[RU](README.md) | [EN](README.en.md) | [RELEASES](https://github.com/dkxce/tdh9unbrick/releases)

Tools for debugging the main flash memory of the radio station**TIDRADIO TD-H9**
в **HID mode**(without switching the chip to UBOOT mode). The project consists of two parts:

* **`h9_flash.py`**Console Flasher (CLI) in Python.
* **`ui_app/app.py`**A window application (GUI) on PySide6 that repeats
  The same logic and adds buttons, progress and journal.

> **What the tool does.**Unpacking the bag`.fw`(extracts)`flash.bin`)
> compares the CRC sectors of the walkie-talkie to the target image and overwrites**only
> differing**sectors, then checks the record by CRC. Boot area`0x0000..0x4FFF`settings`0xDA000..0x100000` **hold off**.

---

## 1. Why should I?

Official firmware TD-H9 comes in file form`*.fw`(for example,`TD-H9-V1.0.38.fw`). It is now being used by a company program through**USB Update
(UBOOT)**regime.

However, the chip has**second** — **HID mode**(PTT+2 upon inclusion),
When the radio is defined as a HID device`VID 0x4C4A, PID 0x4155`.
Scripts work exactly on the HID channel.

---

## 2. What do you need?

| Component | Why? | Where to find it. |
| --- | --- | --- |
| **TIDRADIO TD-H9** | walkie-talkie | you |
| **USB-C cable with data transfer** | Physical connection (not "charging"!) | you |
| **Python 3. 11. +** | performance | python.org |
| **Poetry** | Dependency management and environment n | python-poetry.org |
| **Package`hid`**(pyhidapi) | access | `poetry add hid` |
| **`hidapi.dll`(x64)** | native library`hid` | github.com/libusb/hidapi/releases |
| **PySide6** | GUI only | `poetry add pyside6` |
| **PyInstaller** | exe | `poetry add pyinstaller` |
| **Firmware file`.fw`** | pour | firmware |

> **Important`hidapi.dll`.**Package`pyhidapi`It's just a Python wrapper. She needs her.
> native library**`hidapi.dll`**. Put her down.**near-script**
> `h9_flash.py`(at the root of the project).

---

## 3. Installation

The project uses**Poetry**for addiction management. Dependencies.
?`hid`, `pyside6`, `pyinstaller`declared in`pyproject.toml`.

```
# 1) Установить Poetry (один раз, если ещё нет)
pip install poetry

# 2) Перейти в папку проекта
cd <path>

# poetry env use "<path to python C:\...>"   # опционально

# 3) Установить зависимости и создать виртуальное окружение
poetry install

# 4) Положить hidapi.dll (x64) в корень проекта:
#    — скачать: https://github.com/libusb/hidapi/releases
#    — взять архив hidapi-win.zip, распаковать x64\hidapi.dll
#    — поместить рядом с h9_flash.py
```

All the teams are running through`poetry run python ...`(or activate)
Poetry's environment:`poetry shell`Then just`python ...`).

---

## 4. Radio preparation (entry into HID mode)

1. **Turn off the radio.**
2. **Press the PTT and the button simultaneously.`[2]`.**
3. **Holding the buttons, turn on the radio**It will enter HID mode (updater).
4. **Connect the USB-C cable**(requiredly with data) to the computer.
5. In the system, the radio should be defined as a device.**`Jieli Technology USB Composite Device`** с `VID 0x4C4A, PID 0x4155`.

You can check with the team:

```
poetry run python h9_flash.py --list
```

Expected conclusion:

```
Scanning for JieLi HID devices (VID 0x4C4A):
  VID:0x4C4A PID:0x4155  Jieli Technology USB Composite Device (TD-H9 HID flash mode)
```

Or use it.[USBTreeView](https://www.uwe-sieber.de/usbtreeview_e.html)

> If there is no record, check the cable (you need data, not just charging).
> button retention and order of inclusion.

---

## 5. Console flasher`h9 flash.py`

### 5.1. Reference

```
poetry run python h9_flash.py --help
```

| Flag. | Appointment |
| --- | --- |
| `--list` | Show the found JieLi HID devices |
| `--fw <файл.fw>` | path to the firmware package (required for filling) |
| `--backup <файл.bin>` | Save the current image in a file before recording |
| `--verify` | Slow full check on shifted CRC before recording |
| `--nocheck` | skip CRC reconciliation and force all sectors to be rewritten |
| `--yes` | ask for confirmation |

### 5.2. Basic launch

```
poetry run python h9_flash.py --fw "TD-H9-V1.0.38.fw" --backup before.bin
```

Script:
1. unpack and show SHA-256 packet and image;
2. open the radio and check the geometry;
3. make a CRC scan of sectors;
4. will show a list of sectors for rewriting and ask for confirmation;
5. to record sectors (from senior to junior) with verification of each;
6. perform the final inspection of all sectors.

Once successful, turn off the USB and restart the radio.

### 5.3. Automatic start (without confirmation)

```
poetry run python h9_flash.py --fw "TD-H9-V1.0.38.fw" --backup before.bin --yes
```

### 5.4. Full (slow) inspection`-verify.`

The script before recording additionally makes a check on**shifted**This proves that the real bytes in memory are the same.
with image, not just 16-bit CRC sectors (excludes collisions). This is a test.
It is slow (thousands of HID requests), so it is turned off by default.

```
poetry run python h9_flash.py --fw "TD-H9-V1.0.38.fw" --verify --yes
```

In case of discrepancy, the recording will be prohibited.

### 5.5. Forced rewriting`-Nocheck.`

If you need to re-write**everything**Application sectors regardless of the current CRC
(without stopping at coincidence) - use`--nocheck`:

```
poetry run python h9_flash.py --fw "TD-H9-V1.0.38.fw" --nocheck --yes
```

> `--nocheck`switch off`--verify`(if given together) Erasure/recording/checking
> Each sector is still executed - only preliminary is missed.
> reconciliation.

---

## 6. Window appendix`ui app/app. py`

GUI repeats console logic and adds convenience.

### 6.1. Launch

```
poetry run python ui_app\app.py
```

### 6.2. What in the window

* **Instructions for entering HID mode**at the top of the window (summary from section 4).
* **Firmware**- file selection`.fw`(Browse button)
* **Options**Optional path to backup, checkboxes`--verify` и `--nocheck`.
* **progress**- progress by sector.
* **Magazine (log)**all messages; copyable log (selection + Ctrl+C), or
  button**Copy log.**) and the button**Clear.**(clean)
* **Detection device**Search and output of connected JieLi HID devices.
* **Flash**Start firmware in the background stream (the window does not freeze).
* **Stop.**- early stop.

### 6.3. Working arrangements at GUI

1. Connect the radio in HID mode (PTT+2 when turned on).
2. Press.**Detection device**- Make sure it's visible.`4C4A:4155`.
3. Select the file`.fw`.
4. (Optional) indicate the path to the backup.
5. (Optional) Turn on`--verify` / `--nocheck`.
6. Press.**Flash**Keep an eye on progress and log.
7. Upon completion, turn off the USB and restart the radio.

---

## 7. Assembly of exe (PyInstaller)

The assembly is done through PyInstaller (announced in poetry). way
spec file (recommended - includes)`h9_flash.py` и `hidapi.dll`:

```
poetry run pyinstaller --noconfirm ui_app\flasher.spec
```

Result:`dist\TDH9Flasher.exe`a single-file GUI application in which
packed`app.py`module`h9_flash` и `hidapi.dll`.

---

## 8. File structure

```
Chat-20/
├── h9_flash.py          # консольный флешер (CLI)
├── hidapi.dll           # нативная библиотека (нужна для CLI и GUI)
├── pyproject.toml       # зависимости Poetry
├── ui_app/
│   ├── app.py           # оконное приложение (GUI, PySide6)
│   └── flasher.spec     # spec-файл PyInstaller для сборки exe
└── README.md            # этот файл
```

---

## 9. Security and warnings

* **Firmware is destructive.**Before the first launch, always make a backup if
  There's an opportunity.
* Tool.**doesn't**boot-field`0x0000..0x4FFF`and settings`0xDA000+`.
* Use it.**only**checked package intended for**yours**Radios.
  Incompatible firmware can disrupt the radio.
* Do not turn off the walkie-talkie and USB while recording - you can get an interrupt
  partially recorded state. Re-launch will recheck the sectors.
* `--nocheck`More dangerous than the usual regime: forced re-recording without reconciliation.
  Use it consciously.

---

## 10. Frequent problems

| Problem. | Decision |
| --- | --- |
| `ImportError: Unable to load ... hidapi.dll` | put it down`hidapi.dll`(x64) at the root of the project |
| The device`4C4A:4155`not visible`--list` | Check cable (with data), PTT+2 when turned on, work on behalf of admin if necessary |
| `Package unpack error` | The selected file is not a valid TD-H9 package`.fw` |
| `Radio already matches ... Nothing to write` | The radio already matches the image; use`--nocheck`If a forced re-recording is required |
| GUI window does not open (PySide6) | `poetry run python -c "import PySide6"`If there is a mistake, check it out.`poetry install` |

---

**Author**: [dkxcece](https://github.com/dkxce)   
**Android**: [dkxce/tdh9android](https://github.com/dkxce/tdh9android)   
**Desktop**: [dkxce/tdh9desktop](https://github.com/dkxce/tdh9desktop)   
**Manual**: [dkxce/Tidradio-TD-H9](https://github.com/dkxce/Tidradio-TD-H9)