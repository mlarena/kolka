"""
Загрузка фото с фотоловушек на сервер (Linux).
Phase 3: BLE open → Wi-Fi connect → скачать JPG → удалить с камеры.

Использование:
    python kolka_download_linux.py
"""
import asyncio
import aiohttp
import aiofiles
import logging
import json
import os
import fcntl
import subprocess
import time
import tempfile
import sys
from pathlib import Path
from datetime import datetime
from lxml import etree
from typing import Optional, List, Dict
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, and_
from bleak import BleakScanner, BleakClient

from models import Base, PhotoTrap, DownloadLog, SnapshotLog, PhotoTrapConfig
from compress_images import compress_images
from config_loader import load_config

# Настройка логирования
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
start_time = datetime.now()
log_filename = f"download_log_{start_time.strftime('%Y-%m-%d_%H')}.log"
log_path = log_dir / log_filename
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_path, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logger.info(f"Лог-файл: {log_path}")


class UnifiedCameraManager:
    SERVICE_UUID = "0000ff10-0000-1000-8000-00805f9b34fb"
    CHARACTERISTIC_UUID = "0000ff11-0000-1000-8000-00805f9b34fb"
    CAMERA_API_URL = "http://192.168.1.254/"

    def __init__(self, config_path: str = "appsettings.json"):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)

        self.wifi_interface = self._detect_wifi_interface()
        logger.info(f"Wi-Fi интерфейс: {self.wifi_interface}")
        
        db_url = self._convert_connection_string(self.config['ConnectionStrings']['DefaultConnection'])
        self.engine = create_async_engine(
            db_url, echo=False,
            pool_size=2, max_overflow=1,
            pool_recycle=3600, pool_pre_ping=True
        )
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.download_dir = Path(self.config.get('DownloadPath', './downloads'))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.cameras_count = int(self.config.get('CamerasCount', 1))
        
        # Таймауты из конфига
        self.wifi_password = self.config.get('WifiPassword', '12345678')
        self.ble_scan_timeout = float(self.config.get('BleScanTimeout', 10))
        self.ble_command_timeout = float(self.config.get('BleCommandTimeout', 10))
        self.wifi_wait_after_open = int(self.config.get('WifiWaitAfterOpen', 25))
        self.wifi_connect_timeout = int(self.config.get('WifiConnectTimeout', 90))
        self.wifi_max_retries = int(self.config.get('WifiMaxRetries', 5))
        self.close_wait_seconds = int(self.config.get('CloseWaitSeconds', 25))
        self.retry_delay = int(self.config.get('RetryDelay', 15))
        self.max_retries_per_camera = int(self.config.get('MaxRetriesPerCamera', 3))
        self.max_scan_retries = int(self.config.get('MaxScanRetries', 10))
        self.camera_cooldown = int(self.config.get('CameraCooldown', 20))
        self.wifi_download_retries = int(self.config.get('WifiDownloadRetries', 3))
        self.delete_after_download = str(self.config.get('DeleteAfterDownload', 'true')).lower() in ('true', '1', 'yes')
        self.compress_after_download = str(self.config.get('CompressAfterDownload', 'true')).lower() in ('true', '1', 'yes')

    def _apply_config(self, config: dict):
        """Применить загруженную конфигурацию к атрибутам класса."""
        self.config = config
        self.download_dir = Path(config.get('DownloadPath', './downloads'))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.cameras_count = int(config.get('CamerasCount', 1))
        self.wifi_password = config.get('WifiPassword', '12345678')
        self.ble_scan_timeout = float(config.get('BleScanTimeout', 10))
        self.ble_command_timeout = float(config.get('BleCommandTimeout', 10))
        self.wifi_wait_after_open = int(config.get('WifiWaitAfterOpen', 25))
        self.wifi_connect_timeout = int(config.get('WifiConnectTimeout', 90))
        self.wifi_max_retries = int(config.get('WifiMaxRetries', 5))
        self.close_wait_seconds = int(config.get('CloseWaitSeconds', 25))
        self.retry_delay = int(config.get('RetryDelay', 15))
        self.max_retries_per_camera = int(config.get('MaxRetriesPerCamera', 3))
        self.max_scan_retries = int(config.get('MaxScanRetries', 10))
        self.camera_cooldown = int(config.get('CameraCooldown', 20))
        self.wifi_download_retries = int(config.get('WifiDownloadRetries', 3))
        self.delete_after_download = str(config.get('DeleteAfterDownload', 'true')).lower() in ('true', '1', 'yes')
        self.compress_after_download = str(config.get('CompressAfterDownload', 'true')).lower() in ('true', '1', 'yes')

    def _detect_wifi_interface(self) -> str:
        """Найти имя WiFi интерфейса через nmcli"""
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'DEVICE,TYPE', 'device', 'status'],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.strip().split('\n'):
                parts = line.split(':')
                if len(parts) >= 2 and parts[1] == 'wifi':
                    return parts[0]
        except Exception:
            pass
        return "wlan0"

    async def _validate_cameras(self, session) -> bool:
        """Проверить что камеры настроены: MacAddress + WifiSSID заполнены, количество = CamerasCount"""
        # Получаем ожидаемое количество из конфига
        try:
            from sqlalchemy import func
            result = await session.execute(
                select(PhotoTrapConfig.Value).where(PhotoTrapConfig.Key == 'CamerasCount')
            )
            cameras_count_str = result.scalar_one_or_none()
            expected_count = int(cameras_count_str) if cameras_count_str else 1
        except Exception as e:
            logger.warning(f"Не удалось прочитать CamerasCount из БД: {e}, ожидаем 1")
            expected_count = 1

        # Считаем камеры с заполненными MacAddress и WifiSSID
        result = await session.execute(
            select(PhotoTrap).where(
                PhotoTrap.MacAddress.isnot(None),
                PhotoTrap.WifiSSID.isnot(None)
            )
        )
        cameras = result.scalars().all()
        valid_count = len(cameras)

        logger.info(f"Проверка камер: в БД с SSID={valid_count}, ожидается={expected_count}")

        if valid_count == 0:
            logger.error("В таблице PhotoTrap нет камер с заполненными MacAddress и WifiSSID")
            return False

        if valid_count < expected_count:
            logger.error(f"Камер с SSID: {valid_count}, а нужно: {expected_count}. "
                        f"Выполните калибровку: python calibration.py")
            return False

        if valid_count > expected_count:
            logger.warning(f"Камер с SSID: {valid_count}, а в конфиге CamerasCount={expected_count}. "
                          f"Будут обработаны все {valid_count} камер.")

        # Логируем список камер
        for cam in cameras:
            logger.info(f"  OK: {cam.Name} | {cam.MacAddress} | SSID: {cam.WifiSSID}")

        return True

    def _convert_connection_string(self, conn_string: str) -> str:
        params = {part.split('=')[0].strip(): part.split('=')[1].strip() 
                 for part in conn_string.split(';') if '=' in part}
        return f"postgresql+asyncpg://{params.get('Username')}:{params.get('Password')}@{params.get('Host')}/{params.get('Database')}"

    async def init_db(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("База данных инициализирована")

    async def send_ble_command(self, mac_address: str, command: str) -> bool:
        """Отправка команды (open/close) на камеру через Bluetooth"""
        logger.info(f"BLE: Поиск устройства {mac_address}...")
        try:
            device = await BleakScanner.find_device_by_address(mac_address, timeout=self.ble_scan_timeout)
            if not device:
                logger.warning(f"BLE: Устройство {mac_address} не найдено в эфире (таймаут {self.ble_scan_timeout} сек)")
                return False
            
            logger.info(f"BLE: Устройство {mac_address} найдено ({device.name}). Отправка '{command}'...")
            try:
                async with BleakClient(device, timeout=self.ble_command_timeout) as client:
                    await client.write_gatt_char(self.CHARACTERISTIC_UUID, command.encode())
                    logger.info(f"BLE: Команда '{command}' успешно отправлена на {mac_address}")
                    return True
            except asyncio.CancelledError:
                logger.warning(f"BLE: Таймаут подключения к {mac_address}")
                return False
            except Exception as e:
                logger.error(f"BLE: Ошибка отправки '{command}' на {mac_address}: {e}")
                return False
        except asyncio.CancelledError:
            logger.warning(f"BLE: Таймаут поиска {mac_address}")
            return False
        except Exception as e:
            logger.error(f"BLE: Ошибка поиска {mac_address}: {e}")
            return False

    def _run_nmcli(self, *args) -> tuple:
        """Выполнить команду nmcli"""
        cmd = ['nmcli'] + list(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            logger.warning(f"nmcli команда превысила таймаут: {' '.join(cmd)}")
            return "", "timeout", 1
        except Exception as e:
            logger.error(f"Ошибка выполнения nmcli: {e}")
            return "", str(e), 1

    def connect_to_wifi(self, ssid: str) -> tuple:
        """Подключение к Wi-Fi сети камеры через nmcli (WPA2PSK). Возвращает (success, attempts)."""
        logger.info(f"Wi-Fi: Подключение к {ssid}...")

        # Отключаемся от текущей сети
        self._run_nmcli('connection', 'down', 'id', ssid)
        time.sleep(2)

        # Удаляем старый профиль если есть
        self._run_nmcli('connection', 'delete', 'id', ssid)

        # Создаём новый профиль
        stdout, stderr, rc = self._run_nmcli(
            'connection', 'add',
            'type', 'wifi',
            'con-name', ssid,
            'ssid', ssid,
            'wifi-sec.key-mgmt', 'wpa-psk',
            'wifi-sec.psk', self.wifi_password,
            'connection.autoconnect', 'no',
            'ifname', self.wifi_interface
        )

        if rc != 0:
            logger.error(f"Wi-Fi: Ошибка создания профиля '{ssid}': {stderr}")
            return False, 0
        logger.info(f"Wi-Fi: Профиль '{ssid}' создан")

        for attempt in range(1, self.wifi_max_retries + 1):
            logger.info(f"Wi-Fi: Подключение к {ssid} (таймаут {self.wifi_connect_timeout} сек, попытка {attempt}/{self.wifi_max_retries})...")
            start_time = time.time()

            while time.time() - start_time < self.wifi_connect_timeout:
                self._run_nmcli('connection', 'up', 'id', ssid)
                time.sleep(5)

                stdout, _, _ = self._run_nmcli('-t', '-f', 'NAME,DEVICE,STATE', 'connection', 'show', '--active')
                for line in stdout.strip().split('\n'):
                    parts = line.split(':')
                    if len(parts) >= 3 and parts[0] == ssid and parts[2] == 'activated':
                        logger.info(f"Wi-Fi: Подключено к {ssid} (попытка {attempt})")
                        return True, attempt

            logger.warning(f"Wi-Fi: Не удалось подключиться к {ssid} за {self.wifi_connect_timeout} сек (попытка {attempt}/{self.wifi_max_retries})")
            if attempt < self.wifi_max_retries:
                time.sleep(5)

        return False, self.wifi_max_retries

    def disconnect_wifi(self):
        """Отключение от Wi-Fi: ищем активное wifi-подключение и отключаем его"""
        stdout, _, _ = self._run_nmcli('-t', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show', '--active')
        for line in stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) >= 3 and parts[1] == '802-11-wireless' and parts[2] == self.wifi_interface:
                con_name = parts[0]
                logger.info(f"Wi-Fi: Отключение от '{con_name}'...")
                self._run_nmcli('connection', 'down', 'id', con_name)
                return
        logger.info("Wi-Fi: активное wifi-подключение не найдено")

    async def _health_check(self, session) -> bool:
        """Проверка связи с камерой"""
        try:
            async with session.get(f"{self.CAMERA_API_URL}?custom=1&cmd=3012", timeout=10) as resp:
                await resp.text()
        except Exception as e:
            logger.warning(f"HEALTH cmd=3012: ошибка - {e}")
            return False

        try:
            async with session.get(f"{self.CAMERA_API_URL}?custom=1&cmd=3014", timeout=10) as resp:
                await resp.text()
        except Exception as e:
            logger.warning(f"HEALTH cmd=3014: ошибка - {e}")
            return False

        logger.info("HEALTH: камера доступна")
        return True

    async def process_camera_files(self, trap_id: int, mac_address: str, wifi_ssid: str) -> tuple:
        """Загрузка файлов с докачкой. Возвращает (success, wifi_reconnects)."""

        wifi_reconnects = 0
        for wifi_attempt in range(self.wifi_download_retries + 1):
            if wifi_attempt > 0:
                wifi_reconnects += 1
                logger.info(f"[{mac_address}] Реконнект Wi-Fi (попытка {wifi_attempt}/{self.wifi_download_retries})...")
                ok, _ = self.connect_to_wifi(wifi_ssid)
                if not ok:
                    logger.warning(f"[{mac_address}] Wi-Fi не подключился. Повтор через {self.retry_delay} сек...")
                    await asyncio.sleep(self.retry_delay)
                    continue
                await asyncio.sleep(5)

            try:
                async with aiohttp.ClientSession() as http_session:
                    # Health-check
                    if not await self._health_check(http_session):
                        logger.error(f"[{mac_address}] Health-check не прошёл")
                        if wifi_attempt < self.wifi_download_retries:
                            logger.info(f"[{mac_address}] Повтор через {self.retry_delay} сек...")
                            await asyncio.sleep(self.retry_delay)
                            continue
                        return False, wifi_reconnects

                    # Запрашиваем список файлов
                    xml_data = await self._get_file_list(http_session)
                    if not xml_data:
                        logger.error(f"[{mac_address}] cmd=3015 не вернул данные")
                        if wifi_attempt < self.wifi_download_retries:
                            logger.info(f"[{mac_address}] Повтор через {self.retry_delay} сек...")
                            await asyncio.sleep(self.retry_delay)
                            continue
                        return False, wifi_reconnects

                    files = self._parse_xml(xml_data)
                    logger.info(f"[{mac_address}] Файлов на камере: {len(files)}")

                    # Добавляем новые файлы в БД
                    async with self.async_session() as db_session:
                        stmt = select(DownloadLog).where(
                            and_(DownloadLog.PhotoTrapId == trap_id)
                        )
                        existing_count = len((await db_session.execute(stmt)).scalars().all())

                    added = 0
                    async with self.async_session() as db_session:
                        for f_data in files:
                            stmt = select(DownloadLog).where(
                                and_(DownloadLog.PhotoTrapId == trap_id,
                                     DownloadLog.FileName == f_data['Name'])
                            )
                            if not (await db_session.execute(stmt)).scalar_one_or_none():
                                db_session.add(DownloadLog(
                                    PhotoTrapId=trap_id, FileName=f_data['Name'],
                                    FilePath=f_data['Path'], FileSize=f_data['Size'],
                                    TimeCode=f_data.get('TimeCode'), IsSuccess=False
                                ))
                                added += 1
                        await db_session.commit()

                    if existing_count > 0:
                        logger.info(f"[{mac_address}] В БД уже было {existing_count} записей. Добавлено новых: {added}")
                    else:
                        logger.info(f"[{mac_address}] Добавлено {added} файлов в БД")

                    # Качаем файлы со статусом IsSuccess=False
                    wifi_dropped = False
                    processed = 0
                    skipped = 0
                    failed = 0

                    while True:
                        async with self.async_session() as db_session:
                            stmt = select(DownloadLog).where(
                                and_(DownloadLog.PhotoTrapId == trap_id,
                                     DownloadLog.IsSuccess == False)
                            ).limit(1)
                            entry = (await db_session.execute(stmt)).scalar_one_or_none()

                            if not entry:
                                break

                        processed += 1
                        date_folder = datetime.now().strftime('%Y%m%d')
                        prefixed_name = f"{trap_id}_{entry.FileName}"
                        local_path = self.download_dir / str(trap_id) / date_folder / prefixed_name
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        clean_path = entry.FilePath.replace('A:\\', '').replace('\\', '/')
                        url = f"{self.CAMERA_API_URL}{clean_path}"

                        # Файл уже скачан — помечаем и удаляем с камеры
                        if local_path.exists() and (entry.FileSize == 0 or local_path.stat().st_size == entry.FileSize):
                            logger.info(f"[{mac_address}] Уже есть на диске: {entry.FileName} ({entry.FileSize} байт)")
                            if self.delete_after_download:
                                deleted = await self._delete_file_from_camera(http_session, entry.FilePath)
                                if deleted:
                                    logger.info(f"[{mac_address}] Удалён с камеры: {entry.FileName}")
                                else:
                                    logger.warning(f"[{mac_address}] Не удалось удалить с камеры: {entry.FileName}")
                            async with self.async_session() as db_session:
                                upd = await db_session.get(DownloadLog, entry.Id)
                                upd.IsSuccess = True
                                upd.IsDeleted = deleted if self.delete_after_download else False
                                upd.ErrorMessage = None
                                upd.LocalPath = str(local_path)
                                upd.DownloadedAt = datetime.now()
                                await db_session.commit()
                            skipped += 1
                            continue

                        # Скачиваем
                        logger.info(f"[{mac_address}] Начало загрузки: {entry.FileName}")
                        success, error_msg = await self._download_file(http_session, url, local_path)

                        if success:
                            if local_path.exists() and (entry.FileSize == 0 or local_path.stat().st_size == entry.FileSize):
                                deleted = False
                                if self.delete_after_download:
                                    deleted = await self._delete_file_from_camera(http_session, entry.FilePath)
                                    if deleted:
                                        logger.info(f"[{mac_address}] Удалён с камеры: {entry.FileName}")
                                    else:
                                        logger.warning(f"[{mac_address}] Не удалось удалить с камеры: {entry.FileName}")
                                async with self.async_session() as db_session:
                                    upd = await db_session.get(DownloadLog, entry.Id)
                                    upd.IsSuccess = True
                                    upd.IsDeleted = deleted
                                    upd.ErrorMessage = None
                                    upd.LocalPath = str(local_path)
                                    upd.DownloadedAt = datetime.now()
                                    await db_session.commit()
                            else:
                                actual_size = local_path.stat().st_size if local_path.exists() else 0
                                async with self.async_session() as db_session:
                                    upd = await db_session.get(DownloadLog, entry.Id)
                                    upd.ErrorMessage = f"Размер не совпадает: {actual_size} != {entry.FileSize}"
                                    await db_session.commit()
                                logger.warning(f"[{mac_address}] Размер не совпадает: {entry.FileName} "
                                             f"({actual_size} != {entry.FileSize})")
                                failed += 1
                        else:
                            if self._is_network_error(error_msg):
                                logger.warning(f"[{mac_address}] Обрыв сети при загрузке {entry.FileName}: {error_msg}")
                                wifi_dropped = True
                                break
                            else:
                                logger.warning(f"[{mac_address}] Ошибка загрузки: {entry.FileName}: {error_msg}")
                                async with self.async_session() as db_session:
                                    upd = await db_session.get(DownloadLog, entry.Id)
                                    upd.ErrorMessage = error_msg
                                    await db_session.commit()
                                failed += 1

                    if wifi_dropped:
                        logger.info(f"[{mac_address}] Wi-Fi обнаружен. Попытка реконнекта...")
                        await asyncio.sleep(self.retry_delay)
                        continue

                    success_count = max(0, processed - skipped - failed)
                    logger.info(f"[{mac_address}] Итого: скачано={success_count}, "
                               f"пропущено={skipped}, ошибок={failed}")
                    return True, wifi_reconnects

            except Exception as e:
                logger.error(f"[{mac_address}] Ошибка сессии: {e}")
                if wifi_attempt < self.wifi_download_retries:
                    logger.info(f"[{mac_address}] Повтор через {self.retry_delay} сек...")
                    await asyncio.sleep(self.retry_delay)
                    continue
                return False, wifi_reconnects

        logger.error(f"[{mac_address}] Не удалось загрузить файлы после {self.wifi_download_retries + 1} попыток")
        return False, wifi_reconnects

    def _is_network_error(self, error_msg: str) -> bool:
        """Определяет является ли ошибка сетевой (обрыв Wi-Fi)"""
        if not error_msg:
            return False
        network_keywords = [
            'connection', 'connect', 'network', 'timed out', 'timeout',
            'errno', 'reset', 'refused', 'unreachable', 'broken pipe',
            'connectionreset', 'connectionerror', 'clienterror',
            'aiohttp', 'serverdisconnectederror', 'connectionclosederror',
        ]
        msg_lower = error_msg.lower()
        return any(kw in msg_lower for kw in network_keywords)

    async def _get_file_list(self, session):
        """Получение списка файлов (cmd=3015)"""
        url = f"{self.CAMERA_API_URL}?custom=1&cmd=3015"
        logger.info(f"API cmd=3015: запрос списка файлов -> {url}")
        try:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    logger.info(f"API cmd=3015: HTTP {resp.status}, {len(data)} байт")
                    logger.info(f"API cmd=3015 ответ:\n{data.decode('utf-8', errors='replace')}")
                    return data
                logger.warning(f"API cmd=3015: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"API cmd=3015: {e}")
        return None

    def _parse_xml(self, xml_content):
        try:
            root = etree.fromstring(xml_content)
            return [{'Name': f.findtext('NAME'), 'Path': f.findtext('FPATH'), 
                     'Size': int(f.findtext('SIZE') or 0),
                     'TimeCode': int(f.findtext('TIMECODE') or 0) or None}
                    for f in root.xpath('//File')]
        except Exception as e:
            logger.error(f"Ошибка парсинга XML: {e}")
            return []

    async def _download_file(self, session, url, path):
        try:
            async with session.get(url, timeout=60) as resp:
                if resp.status == 200:
                    async with aiofiles.open(path, 'wb') as f:
                        await f.write(await resp.read())
                    return True, None
                return False, f"HTTP {resp.status}"
        except Exception as e: return False, str(e)

    async def _delete_file_from_camera(self, session, file_path):
        """Удаление файла с камеры (cmd=4003) - по полному пути как есть из БД"""
        url = f"{self.CAMERA_API_URL}?custom=1&cmd=4003&str={file_path}"
        logger.info(f"API cmd=4003: удаление -> {url}")
        try:
            async with session.get(url, timeout=15) as resp:
                data = await resp.text()
                logger.info(f"API cmd=4003: HTTP {resp.status}, ответ: {data}")
                return resp.status == 200
        except Exception as e:
            logger.error(f"API cmd=4003: ошибка - {e}")
            return False

    async def run(self):
        await self.init_db()

        try:
            # ── Перезагрузка конфигурации из БД ───────────────────────────────
            async with self.async_session() as session:
                db_config = await load_config(session)
            self._apply_config(db_config)

            # ── Проверка что камеры настроены ──────────────────────────────────
            async with self.async_session() as session:
                cameras_ok = await self._validate_cameras(session)
            if not cameras_ok:
                logger.error("Камеры не настроены. Выполните калибровку вручную: python calibration.py")
                return

            # ── Читаем активные камеры из БД ──────────────────────────────────
            async with self.async_session() as session:
                result = await session.execute(
                    select(PhotoTrap).where(
                        PhotoTrap.MacAddress.isnot(None),
                        PhotoTrap.WifiSSID.isnot(None),
                        PhotoTrap.IsActive == True
                    )
                )
                cameras = result.scalars().all()

            if not cameras:
                logger.error("Нет активных камер в БД. Завершение.")
                return

            logger.info("\nАктивные камеры в БД:")
            for cam in cameras:
                logger.info(f"  {cam.Name} | {cam.MacAddress} | SSID: {cam.WifiSSID}")

            # ── Установка прав на папку загрузки ──────────────────────────────
            try:
                os.chmod(self.download_dir, 0o777)
                logger.info(f"chmod 777 {self.download_dir}")
            except Exception as e:
                logger.warning(f"Не удалось установить права на {self.download_dir}: {e}")

            # ============================
            # ФАЗА 3: Скачивание файлов
            # ============================
            logger.info("\n" + "=" * 50)
            logger.info("ФАЗА 3: Скачивание файлов")
            logger.info("=" * 50)

            processed = 0
            for cam in cameras:
                if not cam.WifiSSID:
                    logger.info(f"\n[{cam.MacAddress}] Пропуск — нет SSID")
                    continue

                # ── Запись начала опроса ──────────────────────────────────────
                daily_start = datetime.now()
                daily_log_entry = None
                log_messages = []
                error_messages = []

                async with self.async_session() as session:
                    daily_log_entry = SnapshotLog(
                        PhotoTrapId=cam.Id,
                        CycleNumber=1,
                        StartTime=daily_start,
                        Status='PENDING',
                        ActivityType='download',
                    )
                    session.add(daily_log_entry)
                    await session.commit()
                    await session.refresh(daily_log_entry)
                logger.info(f"[{cam.MacAddress}] SnapshotLog #{daily_log_entry.Id} создан (StartTime={daily_start})")

                processed += 1
                logger.info(f"\n=== КАМЕРА {processed}/{len(cameras)}: {cam.Name} ({cam.MacAddress}) ===")

                camera_success = True
                ble_attempts = 0
                wifi_connect_attempts = 0
                wifi_download_reconnects = 0

                # Отправляем open с повторами
                ble_ok = False
                for attempt in range(self.max_retries_per_camera):
                    ble_attempts += 1
                    if await self.send_ble_command(cam.MacAddress, "open"):
                        ble_ok = True
                        break
                    logger.warning(f"[{cam.MacAddress}] BLE open попытка {ble_attempts}/{self.max_retries_per_camera} не удалась")
                    if attempt < self.max_retries_per_camera - 1:
                        await asyncio.sleep(self.retry_delay)

                if not ble_ok:
                    msg = f"BLE open не удался после {ble_attempts} попыток"
                    logger.warning(f"[{cam.MacAddress}] {msg}. Пропуск.")
                    error_messages.append(msg)
                    camera_success = False
                else:
                    logger.info(f"Ожидание Wi-Fi ({self.wifi_wait_after_open} сек)...")
                    await asyncio.sleep(self.wifi_wait_after_open)

                    # Подключаемся к Wi-Fi
                    wifi_ok, wifi_connect_attempts = self.connect_to_wifi(cam.WifiSSID)
                    if not wifi_ok:
                        msg = f"Wi-Fi не подключился ({wifi_connect_attempts} попыток)"
                        logger.warning(f"[{cam.MacAddress}] {msg}. Пропуск.")
                        error_messages.append(msg)
                        camera_success = False
                    else:
                        # Health-check
                        async with aiohttp.ClientSession() as http_session:
                            if not await self._health_check(http_session):
                                msg = "Health-check не прошёл"
                                logger.warning(f"[{cam.MacAddress}] {msg}. Пропуск.")
                                error_messages.append(msg)
                                camera_success = False
                                self.disconnect_wifi()
                                await self.send_ble_command(cam.MacAddress, "close")

                        if camera_success:
                            # Считаем файлы до загрузки
                            async with self.async_session() as db_session:
                                stmt = select(DownloadLog).where(
                                    and_(DownloadLog.PhotoTrapId == cam.Id,
                                         DownloadLog.IsSuccess == True)
                                )
                                before_count = len((await db_session.execute(stmt)).scalars().all())

                            # Загружаем файлы
                            _, wifi_download_reconnects = await self.process_camera_files(cam.Id, cam.MacAddress, cam.WifiSSID)

                            # Считаем файлы после загрузки
                            async with self.async_session() as db_session:
                                stmt = select(DownloadLog).where(
                                    and_(DownloadLog.PhotoTrapId == cam.Id,
                                         DownloadLog.IsSuccess == True)
                                )
                                after_count = len((await db_session.execute(stmt)).scalars().all())

                            session_downloaded = after_count - before_count
                            if session_downloaded > 0:
                                log_messages.append(f"Загружено файлов: {session_downloaded}")
                            else:
                                log_messages.append("Нет файлов для загрузки")

                            # Сжимаем загруженные файлы
                            if self.compress_after_download:
                                date_folder = datetime.now().strftime('%Y%m%d')
                                compress_folder = self.download_dir / str(cam.Id) / date_folder
                                if compress_folder.exists():
                                    logger.info(f"[{cam.MacAddress}] Сжатие изображений в {compress_folder}")
                                    quality = int(self.config.get('CompressQuality', 12))
                                    compressed, errors = compress_images(str(compress_folder), quality)
                                    logger.info(f"[{cam.MacAddress}] Сжатие: сжато={compressed}, ошибок={errors}")
                                    log_messages.append(f"Сжато: {compressed}, ошибок: {errors}")

                    # Завершаем
                    logger.info(f"[{cam.MacAddress}] Завершено. Закрываю Wi-Fi...")
                    await self.send_ble_command(cam.MacAddress, "close")
                    self.disconnect_wifi()

                # ── Сводка попыток ─────────────────────────────────────────────
                summary = (f"BLE open: {ble_attempts}/{self.max_retries_per_camera}, "
                          f"Wi-Fi connect: {wifi_connect_attempts}, "
                          f"Wi-Fi reconnect: {wifi_download_reconnects}/{self.wifi_download_retries}")
                log_messages.insert(0, summary)
                logger.info(f"[{cam.MacAddress}] {summary}")

                # ── Запись окончания опроса ────────────────────────────────────
                daily_end = datetime.now()
                snap_status = 'OK' if not error_messages else 'ERROR'
                async with self.async_session() as session:
                    entry = await session.get(SnapshotLog, daily_log_entry.Id)
                    entry.EndTime = daily_end
                    entry.Status = snap_status
                    entry.LogMessage = "; ".join(log_messages) if log_messages else None
                    entry.ErrorMessage = "; ".join(error_messages) if error_messages else None
                    await session.commit()
                logger.info(f"[{cam.MacAddress}] SnapshotLog #{daily_log_entry.Id} обновлён "
                           f"(EndTime={daily_end}, duration={daily_end - daily_start})")

                if processed < len(cameras):
                    logger.info(f"Пауза {self.camera_cooldown} сек...")
                    await asyncio.sleep(self.camera_cooldown)

            logger.info("\n" + "=" * 50)
            logger.info(f"ЗАВЕРШЕНО: {processed}/{len(cameras)} камер обработано")
            logger.info("=" * 50)

        except Exception as e:
            logger.error(f"Ошибка в run(): {e}", exc_info=True)
        finally:
            await self.engine.dispose()
            logger.info("Ресурсы освобождены")

if __name__ == "__main__":
    SCRIPT_DIR = Path(__file__).resolve().parent
    LOCK_FILE = SCRIPT_DIR / "service.lock"

    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
    except IOError:
        print("Процесс уже запущен, пропуск.")
        sys.exit(0)

    try:
        manager = UnifiedCameraManager()
        asyncio.run(manager.run())
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass
