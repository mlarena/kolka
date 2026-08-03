"""
Калибровка фотоловушек (Linux/Windows).

Две фазы:
  Фаза 1 — обнаружение камер по BLE (запись Name + MacAddress в БД)
  Фаза 2 — привязка Wi-Fi SSID к каждой камере

При запуске проверяет: если все поля (Name, MacAddress, WifiSSID)
уже заполнены — сообщает об этом и выходит.

Использование:
    python calibration.py
"""
import asyncio
import json
import logging
import platform
import subprocess
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakScanner
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from models import Base, PhotoTrap, CalibrationLog
from config_loader import load_config

# ── Логирование ───────────────────────────────────────────────────────────────
logger = logging.getLogger("calibration")

# Определение операционной системы для выбора способа управления Wi-Fi
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"


# ── Управление Wi-Fi (платформенно-зависимое) ─────────────────────────────────

class WifiManager:
    """Абстракция над Wi-Fi подключениями для Windows (netsh) и Linux (nmcli)."""

    def __init__(self, password: str, interface: str = "", connect_timeout: int = 45):
        self.password = password
        self.interface = interface or self._detect_wifi_interface()
        self.connect_timeout = connect_timeout

    @staticmethod
    def _detect_wifi_interface() -> str:
        """Найти имя Wi-Fi интерфейса через nmcli (только Linux)."""
        if not IS_LINUX:
            return ""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "DEVICE,TYPE", "device", "status"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split("\n"):
                parts = line.split(":")
                if len(parts) >= 2 and parts[1] == "wifi":
                    return parts[0]
        except Exception:
            pass
        return "wlan0"

    # ── Сканирование видимых CAM_* сетей ──────────────────────────────────────

    def scan_cam_ssids(self) -> set:
        """Сканировать Wi-Fi сети и вернуть множество SSID, начинающихся с CAM_."""
        if IS_WINDOWS:
            return self._scan_windows()
        elif IS_LINUX:
            return self._scan_linux()
        return set()

    def _scan_windows(self) -> set:
        """Сканирование Wi-Fi сетей через netsh (Windows)."""
        try:
            subprocess.run("netsh wlan show networks mode=bssid", shell=True, capture_output=True)
            result = subprocess.run("netsh wlan show networks", shell=True, capture_output=True, text=True)
            ssids = set()
            for line in result.stdout.split("\n"):
                if "SSID" in line and ":" in line:
                    ssid = line.split(":", 1)[1].strip()
                    if ssid.startswith("CAM_"):
                        ssids.add(ssid)
            return ssids
        except Exception as e:
            logger.error("Wi-Fi scan (Windows): %s", e)
            return set()

    def _scan_linux(self) -> set:
        """Сканирование Wi-Fi сетей через nmcli (Linux)."""
        try:
            # Принудительное сканирование перед получением списка
            self._run_nmcli("device", "wifi", "rescan", "ifname", self.interface)
            time.sleep(2)

            stdout, _, rc = self._run_nmcli(
                "-t", "-f", "SSID", "device", "wifi", "list", "ifname", self.interface
            )
            ssids = set()
            for line in stdout.split("\n"):
                ssid = line.strip()
                if ssid.startswith("CAM_"):
                    ssids.add(ssid)
            return ssids
        except Exception as e:
            logger.error("Wi-Fi scan (Linux): %s", e)
            return set()

    # ── Подключение / отключение ──────────────────────────────────────────────

    def connect(self, ssid: str) -> bool:
        """Подключиться к Wi-Fi сети по SSID."""
        if IS_WINDOWS:
            return self._connect_windows(ssid)
        elif IS_LINUX:
            return self._connect_linux(ssid)
        return False

    def disconnect(self, ssid: str = ""):
        """Отключиться от текущего Wi-Fi соединения."""
        if IS_WINDOWS:
            subprocess.run("netsh wlan disconnect", shell=True, capture_output=True)
        elif IS_LINUX:
            # Ищем активное wifi-подключение на интерфейсе и отключаем его
            stdout, _, _ = self._run_nmcli("-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active")
            for line in stdout.strip().split("\n"):
                parts = line.split(":")
                if len(parts) >= 3 and parts[1] == "802-11-wireless" and parts[2] == self.interface:
                    con_name = parts[0]
                    logger.info("Wi-Fi: отключение от '%s'...", con_name)
                    self._run_nmcli("connection", "down", "id", con_name)
                    return
            logger.info("Wi-Fi: активное wifi-подключение не найдено")

    def _connect_windows(self, ssid: str) -> bool:
        """Подключение к Wi-Fi через netsh (Windows). Создаёт XML-профиль."""
        profile_xml = _WIFI_PROFILE_TEMPLATE.format(ssid=ssid, password=self.password)
        profile_path = None
        try:
            import tempfile, os
            with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-8") as f:
                f.write(profile_xml)
                profile_path = f.name

            subprocess.run(f'netsh wlan delete profile name="{ssid}"', shell=True, capture_output=True)
            subprocess.run(f'netsh wlan add profile filename="{profile_path}" user=all',
                           shell=True, capture_output=True)

            start = time.time()
            attempt = 0
            while time.time() - start < self.connect_timeout:
                attempt += 1
                subprocess.run(f'netsh wlan connect name="{ssid}"', shell=True, capture_output=True)
                time.sleep(5)
                check = subprocess.run("netsh wlan show interfaces", shell=True, capture_output=True, text=True)
                ssid_found = False
                state_line = ""
                for line in check.stdout.split("\n"):
                    if ssid in line:
                        ssid_found = True
                    if "State" in line or "Состояние" in line:
                        state_line = line.strip()
                if ssid_found and ("connected" in state_line.lower() or "подключ" in state_line.lower()):
                    logger.info("Wi-Fi: подключено к %s", ssid)
                    return True
                if attempt % 3 == 0:
                    logger.info("Wi-Fi: попытка %d... state=%s", attempt, state_line)
            logger.warning("Wi-Fi: не удалось подключиться к %s за %d сек", ssid, self.connect_timeout)
            return False
        except Exception as e:
            logger.error("Wi-Fi connect (Windows): %s", e)
            return False
        finally:
            import os
            if profile_path and os.path.exists(profile_path):
                os.unlink(profile_path)

    def _connect_linux(self, ssid: str) -> bool:
        """Подключение к Wi-Fi через nmcli (Linux). Удаляет старый профиль, создаёт новый."""
        self._run_nmcli("connection", "down", "id", ssid)
        time.sleep(2)
        self._run_nmcli("connection", "delete", "id", ssid)

        stdout, stderr, rc = self._run_nmcli(
            "connection", "add",
            "type", "wifi",
            "con-name", ssid,
            "ssid", ssid,
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", self.password,
            "connection.autoconnect", "no",
            "ifname", self.interface,
        )
        if rc != 0:
            logger.error("Wi-Fi: ошибка создания профиля '%s': %s", ssid, stderr)
            return False

        start = time.time()
        attempt = 0
        while time.time() - start < self.connect_timeout:
            attempt += 1
            self._run_nmcli("connection", "up", "id", ssid)
            time.sleep(5)
            # Проверяем: NAME:DEVICE:STATE — точное совпадение NAME + activated
            stdout, _, _ = self._run_nmcli("-t", "-f", "NAME,DEVICE,STATE", "connection", "show", "--active")
            for line in stdout.strip().split("\n"):
                parts = line.split(":")
                if len(parts) >= 3 and parts[0] == ssid and parts[2] == "activated":
                    logger.info("Wi-Fi: подключено к %s (STATE=activated)", ssid)
                    return True
            if attempt % 3 == 0:
                logger.info("Wi-Fi: попытка %d...", attempt)
        logger.warning("Wi-Fi: не удалось подключиться к %s за %d сек", ssid, self.connect_timeout)
        return False

    # ── Вспомогательная функция для вызова nmcli ──────────────────────────────

    def _run_nmcli(self, *args) -> tuple:
        """Выполнить команду nmcli. Возвращает (stdout, stderr, returncode)."""
        cmd = ["nmcli"] + list(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            logger.warning("nmcli timeout: %s", " ".join(cmd))
            return "", "timeout", 1
        except Exception as e:
            logger.error("nmcli error: %s", e)
            return "", str(e), 1


# Шаблон XML-профиля для подключения к Wi-Fi через netsh (Windows)
_WIFI_PROFILE_TEMPLATE = """\
<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
    <name>{ssid}</name>
    <SSIDConfig>
        <SSID>
            <name>{ssid}</name>
        </SSID>
    </SSIDConfig>
    <connectionType>ESS</connectionType>
    <connectionMode>auto</connectionMode>
    <MSM>
        <security>
            <authEncryption>
                <authentication>WPA2PSK</authentication>
                <encryption>AES</encryption>
                <useOneX>false</useOneX>
            </authEncryption>
            <sharedKey>
                <keyType>passPhrase</keyType>
                <protected>false</protected>
                <keyMaterial>{password}</keyMaterial>
            </sharedKey>
        </security>
    </MSM>
</WLANProfile>"""


# ── BLE команды ───────────────────────────────────────────────────────────────

# UUID характеристики для отправки BLE-команд на камеру
CHARACTERISTIC_UUID = "0000ff11-0000-1000-8000-00805f9b34fb"


async def ble_send_command(mac_address: str, command: str, scan_timeout: float = 10,
                           cmd_timeout: float = 10) -> bool:
    """Отправка BLE-команды (open/close) на камеру."""
    logger.info("BLE: поиск %s...", mac_address)
    try:
        device = await BleakScanner.find_device_by_address(mac_address, timeout=scan_timeout)
        if not device:
            logger.warning("BLE: %s не найден (таймаут %s сек)", mac_address, scan_timeout)
            return False

        from bleak import BleakClient
        logger.info("BLE: %s найден (%s). Отправка '%s'...", mac_address, device.name, command)
        try:
            async with BleakClient(device, timeout=cmd_timeout) as client:
                await client.write_gatt_char(CHARACTERISTIC_UUID, command.encode())
                logger.info("BLE: '%s' отправлена на %s", command, mac_address)
                return True
        except asyncio.CancelledError:
            logger.warning("BLE: таймаут подключения к %s", mac_address)
            return False
        except Exception as e:
            logger.error("BLE: ошибка отправки '%s' на %s: %s", command, mac_address, e)
            return False
    except asyncio.CancelledError:
        logger.warning("BLE: таймаут поиска %s", mac_address)
        return False
    except Exception as e:
        logger.error("BLE: ошибка поиска %s: %s", mac_address, e)
        return False


async def ble_scan_cameras(scan_timeout: float = 10) -> list:
    """Сканирование BLE — поиск камер GCBT40."""
    try:
        devices = await BleakScanner.discover(timeout=scan_timeout)
        return [d for d in devices if d.name and "GCBT40" in d.name]
    except Exception as e:
        logger.error("BLE scan error: %s", e)
        return []


# ── Поиск SSID новой камеры ──────────────────────────────────────────────────

async def find_new_cam_ssid(wifi: WifiManager, known_ssids: set,
                            max_attempts: int = 5, delay: int = 5) -> str | None:
    """
    Ищет НОВУЮ CAM_* сеть (которой не было в known_ssids).
    Сканирует Wi-Fi несколько раз с задержкой между попытками.
    """
    for attempt in range(max_attempts):
        current = wifi.scan_cam_ssids()
        new_ssids = current - known_ssids
        if new_ssids:
            ssid = next(iter(new_ssids))
            logger.info("Wi-Fi: найдена НОВАЯ сеть %s (попытка %d/%d)", ssid, attempt + 1, max_attempts)
            return ssid
        if current:
            logger.info("Wi-Fi: видны сети %s (все уже известны)", current)
        if attempt < max_attempts - 1:
            logger.info("Wi-Fi: CAM_* нет (видны: %s). Повтор %d/%d...", current, attempt + 1, max_attempts)
            await asyncio.sleep(delay)
    return None


# ── Фаза 1: обнаружение камер по BLE ─────────────────────────────────────────

async def phase1_discover_cameras(config: dict, db_session_factory) -> list:
    """
    Сканирует BLE-эфир, находит устройства GCBT40 (фотоловушки),
    записывает Name и MacAddress в таблицу PhotoTrap.
    Гарантирует уникальность MacAddress перед записью.
    Повторяет сканирование до достижения CamerasCount камер.
    """
    logger.info("=" * 50)
    logger.info("ФАЗА 1: обнаружение камер (BLE)")
    logger.info("=" * 50)

    cameras_count = int(config.get("CamerasCount", 1))
    scan_timeout = float(config.get("BleScanTimeout", 10))
    retry_delay = int(config.get("RetryDelay", 15))
    max_scan_retries = int(config.get("MaxScanRetries", 10))

    logger.info("Необходимо камер: %d", cameras_count)

    scan_attempts = 0
    while scan_attempts < max_scan_retries:
        async with db_session_factory() as session:
            result = await session.execute(
                select(PhotoTrap).where(PhotoTrap.MacAddress.isnot(None))
            )
            existing = result.scalars().all()
            existing_macs = {cam.MacAddress for cam in existing}

        logger.info("В БД: %d/%d", len(existing), cameras_count)
        if len(existing) >= cameras_count:
            logger.info("Таблица заполнена.")
            break

        scan_attempts += 1
        found = await ble_scan_cameras(scan_timeout)
        if not found:
            logger.info("Камеры не найдены. Ждём %d сек...", retry_delay)
            await asyncio.sleep(retry_delay)
            continue

        added = 0
        for device in found:
            if device.address not in existing_macs:
                # Проверка уникальности MacAddress в БД
                async with db_session_factory() as session:
                    result = await session.execute(
                        select(PhotoTrap).where(PhotoTrap.MacAddress == device.address)
                    )
                    existing_entry = result.scalar_one_or_none()
                if existing_entry:
                    logger.warning("MAC %s уже в БД (камера Id=%d). Пропуск.",
                                  device.address, existing_entry.Id)
                    existing_macs.add(device.address)
                    continue

                async with db_session_factory() as session:
                    trap = PhotoTrap(Name=device.name, MacAddress=device.address)
                    session.add(trap)
                    await session.commit()
                logger.info("+ %s (%s)", device.name, device.address)
                existing_macs.add(device.address)
                added += 1

        if len(existing_macs) >= cameras_count:
            logger.info("Таблица заполнена.")
            break
        await asyncio.sleep(retry_delay)

    async with db_session_factory() as session:
        result = await session.execute(
            select(PhotoTrap).where(PhotoTrap.MacAddress.isnot(None))
        )
        cameras = result.scalars().all()

    if not cameras:
        logger.error("Нет камер. Завершение фазы 1.")
        return []

    logger.info("\nКамеры в БД:")
    for cam in cameras:
        logger.info("  %s | %s | SSID: %s", cam.Name, cam.MacAddress, cam.WifiSSID or "---")
    return cameras


# ── Фаза 2: привязка Wi-Fi SSID ──────────────────────────────────────────────

async def phase2_bind_ssids(config: dict, cameras: list, db_session_factory) -> list:
    """
    Для каждой камеры без WifiSSID:
      1. Сканирует Wi-Fi до open (запоминает известные сети)
      2. Отправляет BLE 'open' — камера поднимает точку доступа
      3. Сканирует Wi-Fi после open — ищет НОВУЮ CAM_* сеть
      4. Проверяет уникальность SSID в БД
      5. Привязывает SSID к камере
    """
    logger.info("\n" + "=" * 50)
    logger.info("ФАЗА 2: привязка Wi-Fi SSID")
    logger.info("=" * 50)

    scan_timeout = float(config.get("BleScanTimeout", 10))
    cmd_timeout = float(config.get("BleCommandTimeout", 10))
    wifi_password = config.get("WifiPassword", "12345678")
    wifi_wait = int(config.get("WifiWaitAfterOpen", 25))
    close_wait = int(config.get("CloseWaitSeconds", 25))
    retry_delay = int(config.get("RetryDelay", 15))
    max_retries = int(config.get("MaxRetriesPerCamera", 3))
    connect_timeout = int(config.get("WifiConnectTimeout", 45))

    wifi = WifiManager(wifi_password, "", connect_timeout)

    # Сначала отправляем 'close' на все камеры, чтобы сбросить состояние
    logger.info("Отправка 'close' на все камеры...")
    for cam in cameras:
        await ble_send_command(cam.MacAddress, "close", scan_timeout, cmd_timeout)
    logger.info("Ожидание %d сек...", close_wait)
    await asyncio.sleep(close_wait)

    # Множество уже привязанных SSID (чтобы не привязать одну сеть дважды)
    known_cam_ssids = set()

    for cam in cameras:
        if cam.WifiSSID:
            logger.info("[%s] SSID уже есть: %s", cam.MacAddress, cam.WifiSSID)
            known_cam_ssids.add(cam.WifiSSID)
            continue

        logger.info("\n[%s] Ищу SSID...", cam.MacAddress)

        # Пробуем несколько раз найти SSID для этой камеры
        for retry in range(max_retries):
            # Сканируем Wi-Fi ДО open — запоминаем какие сети уже видны
            before_open = wifi.scan_cam_ssids()
            logger.info("[%s] До open видны CAM_*: %s", cam.MacAddress, before_open or "(нет)")

            # Отправляем BLE 'open' — камера включает Wi-Fi точку доступа
            if not await ble_send_command(cam.MacAddress, "open", scan_timeout, cmd_timeout):
                logger.warning("[%s] BLE open не удался. Повтор через %d сек...", cam.MacAddress, retry_delay)
                await asyncio.sleep(retry_delay)
                continue

            # Ждём пока камера поднимет Wi-Fi
            logger.info("[%s] Ожидание Wi-Fi (%d сек)...", cam.MacAddress, wifi_wait)
            await asyncio.sleep(wifi_wait)

            # Ищем новую CAM_* сеть, которой не было до open
            found_ssid = await find_new_cam_ssid(wifi, known_cam_ssids)

            # Если новая сеть не найдена — пробуем найти непривязанную
            if not found_ssid:
                all_visible = wifi.scan_cam_ssids()
                logger.warning("[%s] Новая CAM_* не найдена. Видны: %s", cam.MacAddress, all_visible)

                # Непривязанные = видимые минус уже привязанные
                unbound = all_visible - known_cam_ssids
                if len(unbound) == 1:
                    found_ssid = next(iter(unbound))
                    logger.info("[%s] Найдена непривязанная сеть: %s", cam.MacAddress, found_ssid)
                elif len(all_visible) == 1 and len(known_cam_ssids) == 0:
                    found_ssid = next(iter(all_visible))
                    logger.info("[%s] Единственная CAM_* сеть: %s", cam.MacAddress, found_ssid)

            if not found_ssid:
                logger.warning("[%s] CAM_* не найдена. Повтор %d/%d", cam.MacAddress, retry + 1, max_retries)
                await ble_send_command(cam.MacAddress, "close", scan_timeout, cmd_timeout)
                await asyncio.sleep(retry_delay)
                continue

            # Проверка уникальности WifiSSID перед привязкой
            # Привязываем SSID
            async with db_session_factory() as session:
                result = await session.execute(
                    select(PhotoTrap).where(PhotoTrap.WifiSSID == found_ssid)
                )
                existing_ssid = result.scalar_one_or_none()
            if existing_ssid:
                logger.warning("[%s] SSID %s уже привязан к камере %s (Id=%d). Пропуск.",
                              cam.MacAddress, found_ssid, existing_ssid.Name, existing_ssid.Id)
                await ble_send_command(cam.MacAddress, "close", scan_timeout, cmd_timeout)
                await asyncio.sleep(retry_delay)
                continue

            async with db_session_factory() as session:
                t_db = await session.get(PhotoTrap, cam.Id)
                t_db.WifiSSID = found_ssid
                await session.commit()
            known_cam_ssids.add(found_ssid)
            logger.info("[%s] SSID привязан: %s", cam.MacAddress, found_ssid)

            wifi.disconnect()
            break
        else:
            logger.error("[%s] Не удалось найти SSID после %d попыток", cam.MacAddress, max_retries)

    # Перечитываем камеры из БД после привязки
    async with db_session_factory() as session:
        result = await session.execute(
            select(PhotoTrap).where(PhotoTrap.MacAddress.isnot(None))
        )
        cameras = result.scalars().all()

    logger.info("\n" + "=" * 50)
    logger.info("Состояние после фазы 2:")
    logger.info("=" * 50)
    all_ok = True
    for cam in cameras:
        status = "OK" if cam.WifiSSID else "НУЖЕН SSID"
        logger.info("  %s | %s | %s | %s", cam.Name, cam.MacAddress, cam.WifiSSID or "---", status)
        if not cam.WifiSSID:
            all_ok = False

    if not all_ok:
        logger.warning("Не все камеры имеют SSID.")

    return cameras


# ── Точка входа ───────────────────────────────────────────────────────────────

async def run_calibration(config_path: str = "appsettings.json"):
    """
    Полная калибровка фотоловушек:
      1. Проверяет: если Name + MacAddress + WifiSSID заполнены — выходит
      2. Фаза 1: обнаружение камер по BLE
      3. Фаза 2: привязка Wi-Fi SSID
      4. Помечает все камеры IsActive = true
      5. Записывает результат в CalibrationLog
    """
    cal_start = datetime.now()
    log_messages = []
    error_messages = []

    logger.info("Калибровка запущена (%s)", "Windows" if IS_WINDOWS else "Linux")

    # Читаем конфиг из файла для получения строки подключения к БД
    with open(config_path, "r", encoding="utf-8") as f:
        file_config = json.load(f)

    db_url = _convert_connection_string(file_config["ConnectionStrings"]["DefaultConnection"])
    # Создаём асинхронный движок SQLAlchemy
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Записываем начало калибровки в CalibrationLog
    async with db_session_factory() as session:
        cal_log = CalibrationLog(StartTime=cal_start)
        session.add(cal_log)
        await session.commit()
        await session.refresh(cal_log)

    # Загружаем конфигурацию: значения из БД перекрывают файловые
    async with db_session_factory() as session:
        config = await load_config(session, config_path)

    # ── Проверка: если все поля заполнены — калибровка не нужна ─────────────────
    async with db_session_factory() as session:
        result = await session.execute(
            select(PhotoTrap).where(PhotoTrap.MacAddress.isnot(None))
        )
        all_cameras = result.scalars().all()

    cameras_count = int(config.get('CamerasCount', 1))

    if all_cameras:
        all_filled = True
        for cam in all_cameras:
            if not cam.Name or not cam.MacAddress or not cam.WifiSSID:
                all_filled = False
                break

        if all_filled and len(all_cameras) >= cameras_count:
            logger.info("Все камеры уже откалиброваны:")
            for cam in all_cameras:
                logger.info("  %s | MAC: %s | SSID: %s", cam.Name, cam.MacAddress, cam.WifiSSID)
            logger.info("Калибровка не требуется. Выход.")
            await engine.dispose()
            return
        elif all_filled and len(all_cameras) < cameras_count:
            logger.info("В БД %d камер, а нужно %d. Продолжаем калибровку.", len(all_cameras), cameras_count)

    cameras_found = 0
    ssids_bound = 0

    # ── Фаза 1: обнаружение камер по BLE ──────────────────────────────────────
    cameras = await phase1_discover_cameras(config, db_session_factory)
    if not cameras:
        logger.error("Фаза 1 не завершена — нет камер. Калибровка остановлена.")
        error_messages.append("Фаза 1 не завершена — нет камер")
    else:
        cameras_found = len(cameras)
        log_messages.append("Phase1: найдено камер %d" % cameras_found)

        # ── Фаза 2: привязка Wi-Fi SSID ──────────────────────────────────────
        cameras = await phase2_bind_ssids(config, cameras, db_session_factory)
        ssids_bound = sum(1 for cam in cameras if cam.WifiSSID)
        log_messages.append("Phase2: привязано SSID %d/%d" % (ssids_bound, cameras_found))

        # Помечаем все камеры как активные
        async with db_session_factory() as session:
            result = await session.execute(select(PhotoTrap))
            all_cams = result.scalars().all()
            for cam in all_cams:
                cam.IsActive = True
            await session.commit()
        logger.info("Все камеры помечены как активные (IsActive = true)")

    # Обновляем запись CalibrationLog результатами
    cal_end = datetime.now()
    async with db_session_factory() as session:
        entry = await session.get(CalibrationLog, cal_log.Id)
        entry.EndTime = cal_end
        entry.CamerasFound = cameras_found
        entry.SsidsBound = ssids_bound
        entry.LogMessage = "; ".join(log_messages) if log_messages else None
        entry.ErrorMessage = "; ".join(error_messages) if error_messages else None
        await session.commit()
    logger.info("CalibrationLog #%d обновлён (CamerasFound=%d, SsidsBound=%d)",
                cal_log.Id, cameras_found, ssids_bound)

    await engine.dispose()

    logger.info("\n" + "=" * 50)
    logger.info("КАЛИБРОВКА ЗАВЕРШЕНА")
    logger.info("=" * 50)


def _convert_connection_string(conn_string: str) -> str:
    """Конвертировать строку подключения из формата appsettings.json в формат SQLAlchemy."""
    params = {
        part.split("=")[0].strip(): part.split("=")[1].strip()
        for part in conn_string.split(";")
        if "=" in part
    }
    return (
        f"postgresql+asyncpg://{params.get('Username')}:{params.get('Password')}"
        f"@{params.get('Host')}/{params.get('Database')}"
    )


# ── Запуск из командной строки ─────────────────────────────────────────────────
if __name__ == "__main__":
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"calibration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logger.info("Лог-файл: %s", log_path)
    asyncio.run(run_calibration())
