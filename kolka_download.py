"""
Загрузка фото с фотоловушек на сервер (Windows).
Phase 3: BLE open → Wi-Fi connect → скачать JPG → удалить с камеры.

Использование:
    python kolka_download.py
"""
import asyncio
import aiohttp
import aiofiles
import logging
import json
import os
import subprocess
import time
import tempfile
from pathlib import Path
from datetime import datetime
from lxml import etree
from typing import Optional, List, Dict
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, and_
from bleak import BleakScanner, BleakClient

from models import Base, PhotoTrap, DownloadLog, SnapshotLog
from compress_images import compress_images
from calibration import run_calibration
from config_loader import load_config


# Настройка логирования
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
start_time = datetime.now()
log_filename = f"download_log_{start_time.strftime('%Y%m%d')}.log"
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

WIFI_PROFILE_TEMPLATE = """<?xml version="1.0"?>
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


class UnifiedCameraManager:
    SERVICE_UUID = "0000ff10-0000-1000-8000-00805f9b34fb"
    CHARACTERISTIC_UUID = "0000ff11-0000-1000-8000-00805f9b34fb"
    CAMERA_API_URL = "http://192.168.1.254/"
    
    def __init__(self, config_path: str = "appsettings.json"):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        
        db_url = self._convert_connection_string(self.config['ConnectionStrings']['DefaultConnection'])
        self.engine = create_async_engine(db_url, echo=False)
        self.async_session = sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        self.download_dir = Path(self.config.get('DownloadPath', './downloads'))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.cameras_count = int(self.config.get('CamerasCount', 1))
        
        # Таймауты из конфига
        self.wifi_password = self.config.get('WifiPassword', '12345678')
        self.ble_scan_timeout = float(self.config.get('BleScanTimeout', 10))
        self.ble_command_timeout = float(self.config.get('BleCommandTimeout', 10))
        self.wifi_wait_after_open = int(self.config.get('WifiWaitAfterOpen', 25))
        self.wifi_connect_timeout = int(self.config.get('WifiConnectTimeout', 45))
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
        self.wifi_connect_timeout = int(config.get('WifiConnectTimeout', 45))
        self.close_wait_seconds = int(config.get('CloseWaitSeconds', 25))
        self.retry_delay = int(config.get('RetryDelay', 15))
        self.max_retries_per_camera = int(config.get('MaxRetriesPerCamera', 3))
        self.max_scan_retries = int(config.get('MaxScanRetries', 10))
        self.camera_cooldown = int(config.get('CameraCooldown', 20))
        self.wifi_download_retries = int(config.get('WifiDownloadRetries', 3))
        self.delete_after_download = str(config.get('DeleteAfterDownload', 'true')).lower() in ('true', '1', 'yes')
        self.compress_after_download = str(config.get('CompressAfterDownload', 'true')).lower() in ('true', '1', 'yes')

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

    def _create_wifi_profile(self, ssid: str) -> bool:
        """Создание профиля Wi-Fi WPA2PSK для подключения к сети камеры"""
        profile_xml = WIFI_PROFILE_TEMPLATE.format(ssid=ssid, password=self.wifi_password)
        profile_path = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.xml', delete=False, encoding='utf-8') as f:
                f.write(profile_xml)
                profile_path = f.name
            
            subprocess.run(f'netsh wlan delete profile name="{ssid}"', 
                         shell=True, capture_output=True, text=True)
            
            result = subprocess.run(f'netsh wlan add profile filename="{profile_path}" user=all', 
                                  shell=True, capture_output=True, text=True)
            logger.info(f"Wi-Fi: Профиль '{ssid}' создан (WPA2PSK, user=all)")
            return True
        except Exception as e:
            logger.error(f"Wi-Fi: Ошибка создания профиля '{ssid}': {e}")
            return False
        finally:
            if profile_path and os.path.exists(profile_path):
                os.unlink(profile_path)

    def connect_to_wifi(self, ssid: str) -> tuple:
        """Подключение к Wi-Fi сети камеры (WPA2PSK). Возвращает (success, attempts)."""
        logger.info(f"Wi-Fi: Подключение к {ssid} (таймаут {self.wifi_connect_timeout} сек)...")

        subprocess.run('netsh wlan disconnect', shell=True, capture_output=True)
        time.sleep(2)

        # Создаём профиль WPA2PSK
        self._create_wifi_profile(ssid)
        time.sleep(2)

        # Подключаемся через профиль
        cmd = f'netsh wlan connect name="{ssid}"'
        start_time = time.time()
        attempt = 0

        while time.time() - start_time < self.wifi_connect_timeout:
            attempt += 1
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

            if attempt == 1:
                logger.debug(f"Wi-Fi connect: {result.stdout.strip()}")

            time.sleep(5)
            check = subprocess.run('netsh wlan show interfaces', shell=True, capture_output=True, text=True)

            # Проверяем подключение
            ssid_found = False
            state_line = ""
            for line in check.stdout.split('\n'):
                if ssid in line:
                    ssid_found = True
                if 'State' in line or 'Состояние' in line:
                    state_line = line.strip()
            
            if ssid_found and ('connected' in state_line.lower() or 'подключ' in state_line.lower()):
                logger.info(f"Wi-Fi: Подключено к {ssid} (попытка {attempt})")
                return True, attempt

            if attempt % 3 == 0:
                logger.info(f"Wi-Fi: Попытка {attempt}... state={state_line}")

        logger.warning(f"Wi-Fi: Не удалось подключиться к {ssid} за {self.wifi_connect_timeout} сек ({attempt} попыток)")
        return False, attempt

    async def _health_check(self, session) -> bool:
        """Проверка связи с камерой"""
        # cmd=3012 - версия
        try:
            async with session.get(f"{self.CAMERA_API_URL}?custom=1&cmd=3012", timeout=10) as resp:
                await resp.text()
        except Exception as e:
            logger.warning(f"HEALTH cmd=3012: ошибка - {e}")
            return False

        # cmd=3014 - статус
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
                        date_folder = start_time.strftime('%Y%m%d')
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
                            # Проверяем — это ошибка сети или ошибка камеры?
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

        # ── Перезагрузка конфигурации из БД ───────────────────────────────────
        async with self.async_session() as session:
            db_config = await load_config(session)
        self._apply_config(db_config)

        # ── Калибровка (фаза 1+2) ────────────────────────────────────────────
        if self.config.get("NeedCalibration", False):
            logger.info("Калибровка включена — запуск calibration.py")
            await run_calibration()
        else:
            logger.info("Калибровка отключена — пропуск фаз 1+2")

        # ── Читаем активные камеры из БД ──────────────────────────────────────
        async with self.async_session() as session:
            result = await session.execute(
                select(PhotoTrap).where(
                    PhotoTrap.MacAddress.isnot(None),
                    PhotoTrap.IsActive == True
                )
            )
            cameras = result.scalars().all()

        if not cameras:
            logger.error("Нет активных камер в БД. Завершение.")
            return

        logger.info("\nАктивные камеры в БД:")
        for cam in cameras:
            logger.info(f"  {cam.Name} | {cam.MacAddress} | SSID: {cam.WifiSSID or '---'}")

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

            # ── Запись начала опроса ──────────────────────────────────────────
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
                            subprocess.run('netsh wlan disconnect', shell=True)
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
                            date_folder = start_time.strftime('%Y%m%d')
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
                subprocess.run('netsh wlan disconnect', shell=True)

            # ── Сводка попыток ─────────────────────────────────────────────────
            summary = (f"BLE open: {ble_attempts}/{self.max_retries_per_camera}, "
                      f"Wi-Fi connect: {wifi_connect_attempts}, "
                      f"Wi-Fi reconnect: {wifi_download_reconnects}/{self.wifi_download_retries}")
            log_messages.insert(0, summary)
            logger.info(f"[{cam.MacAddress}] {summary}")
            
            # ── Запись окончания опроса ────────────────────────────────────────
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

if __name__ == "__main__":
    manager = UnifiedCameraManager()
    asyncio.run(manager.run())
