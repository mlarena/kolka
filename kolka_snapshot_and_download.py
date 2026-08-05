"""
Создание одного снимка и загрузка именно этого файла (Linux).
BLE open → Wi-Fi connect → cmd=1001 → скачать полученный файл → close.

Использование:
    python kolka_snapshot_and_download.py
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
import sys
from pathlib import Path
from datetime import datetime
from lxml import etree
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from bleak import BleakScanner, BleakClient

from models import Base, PhotoTrap, SnapshotLog, DownloadLog, PhotoTrapConfig
from config_loader import load_config

# ── Логирование ──────────────────────────────────────────────────────────────
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
log_filename = f"snapshot_download_log_{datetime.now().strftime('%Y-%m-%d_%H')}.log"
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


class SnapshotDownloadManager:
    CAMERA_API_URL = "http://192.168.1.254/"
    CHARACTERISTIC_UUID = "0000ff11-0000-1000-8000-00805f9b34fb"

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
        self.camera_cooldown = int(self.config.get('CameraCooldown', 20))
        self.compress_after_download = str(self.config.get('CompressAfterDownload', 'true')).lower() in ('true', '1', 'yes')
        self.compress_quality = int(self.config.get('CompressQuality', 12))

    def _apply_config(self, config: dict):
        """Применить загруженную конфигурацию к атрибутам класса."""
        self.config = config
        self.download_dir = Path(config.get('DownloadPath', './downloads'))
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.wifi_password = config.get('WifiPassword', '12345678')
        self.ble_scan_timeout = float(config.get('BleScanTimeout', 10))
        self.ble_command_timeout = float(config.get('BleCommandTimeout', 10))
        self.wifi_wait_after_open = int(config.get('WifiWaitAfterOpen', 25))
        self.wifi_connect_timeout = int(config.get('WifiConnectTimeout', 90))
        self.wifi_max_retries = int(config.get('WifiMaxRetries', 5))
        self.close_wait_seconds = int(config.get('CloseWaitSeconds', 25))
        self.retry_delay = int(config.get('RetryDelay', 15))
        self.max_retries_per_camera = int(config.get('MaxRetriesPerCamera', 3))
        self.camera_cooldown = int(config.get('CameraCooldown', 20))
        self.compress_after_download = str(config.get('CompressAfterDownload', 'true')).lower() in ('true', '1', 'yes')
        self.compress_quality = int(config.get('CompressQuality', 12))

    def _detect_wifi_interface(self) -> str:
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

    def _convert_connection_string(self, conn_string: str) -> str:
        params = {part.split('=')[0].strip(): part.split('=')[1].strip()
                  for part in conn_string.split(';') if '=' in part}
        return (f"postgresql+asyncpg://{params.get('Username')}:{params.get('Password')}"
                f"@{params.get('Host')}/{params.get('Database')}")

    async def init_db(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("База данных инициализирована")

    # ── BLE ──────────────────────────────────────────────────────────────────
    def _reset_ble_adapter(self):
        """Сбросить BLE-адаптер, чтобы снять состояние 'Operation already in progress'."""
        try:
            subprocess.run(['hciconfig', 'hci0', 'down'], capture_output=True, timeout=5)
            time.sleep(1)
            subprocess.run(['hciconfig', 'hci0', 'up'], capture_output=True, timeout=5)
            time.sleep(2)
            logger.info("BLE: адаптер сброшен (hci0 down/up)")
        except Exception as e:
            logger.warning(f"BLE: не удалось сбросить адаптер: {e}")

    async def send_ble_command(self, mac_address: str, command: str) -> bool:
        logger.info(f"BLE: Поиск {mac_address}...")
        for attempt in range(2):
            try:
                device = await BleakScanner.find_device_by_address(mac_address, timeout=self.ble_scan_timeout)
                if not device:
                    logger.warning(f"BLE: {mac_address} не найден")
                    return False
                try:
                    async with BleakClient(device, timeout=self.ble_command_timeout) as client:
                        await client.write_gatt_char(self.CHARACTERISTIC_UUID, command.encode())
                        logger.info(f"BLE: '{command}' → {mac_address}")
                        return True
                except Exception as e:
                    logger.error(f"BLE: ошибка {mac_address}: {e}")
                    return False
            except Exception as e:
                if 'InProgress' in str(e) and attempt == 0:
                    logger.warning(f"BLE: адаптер занят, сброс... ({e})")
                    self._reset_ble_adapter()
                    continue
                logger.error(f"BLE: ошибка поиска {mac_address}: {e}")
                return False
        return False

    def _ble_open_retry(self, mac_address: str):
        """Вернуть async-коллбэк для повторной отправки BLE open."""
        async def _do_open():
            logger.info(f"BLE: повторная отправка open → {mac_address}")
            await self.send_ble_command(mac_address, "open")
            logger.info(f"Ожидание Wi-Fi ({self.wifi_wait_after_open} сек)...")
            await asyncio.sleep(self.wifi_wait_after_open)
        return _do_open

    # ── Wi-Fi ────────────────────────────────────────────────────────────────
    def _run_nmcli(self, *args) -> tuple:
        cmd = ['nmcli'] + list(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", "timeout", 1
        except Exception as e:
            return "", str(e), 1

    async def connect_to_wifi(self, ssid: str, on_retry=None) -> tuple:
        """Подключиться к Wi-Fi. on_retry — async-вызов перед каждой повторной попыткой."""
        logger.info(f"Wi-Fi: Подключение к {ssid}...")
        self._run_nmcli('connection', 'down', 'id', ssid)
        await asyncio.sleep(2)
        self._run_nmcli('connection', 'delete', 'id', ssid)

        stdout, stderr, rc = self._run_nmcli(
            'connection', 'add', 'type', 'wifi', 'con-name', ssid, 'ssid', ssid,
            'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', self.wifi_password,
            'connection.autoconnect', 'no', 'ifname', self.wifi_interface
        )
        if rc != 0:
            logger.error(f"Wi-Fi: Ошибка профиля '{ssid}': {stderr}")
            return False, 0

        for attempt in range(1, self.wifi_max_retries + 1):
            logger.info(f"Wi-Fi: Попытка {attempt}/{self.wifi_max_retries} (таймаут {self.wifi_connect_timeout} сек)...")
            start = time.time()
            while time.time() - start < self.wifi_connect_timeout:
                self._run_nmcli('connection', 'up', 'id', ssid)
                await asyncio.sleep(5)
                stdout, _, _ = self._run_nmcli('-t', '-f', 'NAME,DEVICE,STATE', 'connection', 'show', '--active')
                for line in stdout.strip().split('\n'):
                    parts = line.split(':')
                    if len(parts) >= 3 and parts[0] == ssid and parts[2] == 'activated':
                        logger.info(f"Wi-Fi: Подключено к {ssid}")
                        return True, attempt
            logger.warning(f"Wi-Fi: Попытка {attempt} не удалась")
            if attempt < self.wifi_max_retries:
                # Повторная отправка BLE open перед следующей попыткой
                if on_retry is not None:
                    await on_retry()
                await asyncio.sleep(5)

        return False, self.wifi_max_retries

    def disconnect_wifi(self):
        stdout, _, _ = self._run_nmcli('-t', '-f', 'NAME,TYPE,DEVICE', 'connection', 'show', '--active')
        for line in stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) >= 3 and parts[1] == '802-11-wireless' and parts[2] == self.wifi_interface:
                con_name = parts[0]
                logger.info(f"Wi-Fi: Отключение от '{con_name}'...")
                self._run_nmcli('connection', 'down', 'id', con_name)
                return
        logger.info("Wi-Fi: активное подключение не найдено")

    def _compress_downloaded_file(self, file_path: Path) -> bool:
        if not self.compress_after_download:
            return False
        if file_path.suffix.lower() not in ('.jpg', '.jpeg'):
            logger.info("Сжатие пропущено: не JPG (%s)", file_path.name)
            return False

        compress_script = Path(__file__).resolve().parent / "compress_images.py"
        if not compress_script.exists():
            logger.warning("Сжатие невозможно: %s не найден", compress_script)
            return False

        cmd = [sys.executable, str(compress_script), str(file_path.parent)]
        try:
            logger.info("Сжатие: %s", cmd)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            logger.info("Сжатие stdout: %s", result.stdout.strip())
            if result.stderr.strip():
                logger.warning("Сжатие stderr: %s", result.stderr.strip())
            return result.returncode == 0
        except Exception as e:
            logger.error("Ошибка при запуске compress_images.py: %s", e)
            return False

    # ── Camera API ───────────────────────────────────────────────────────────
    async def _health_check(self, session) -> bool:
        try:
            async with session.get(f"{self.CAMERA_API_URL}?custom=1&cmd=3012", timeout=10) as resp:
                await resp.text()
            async with session.get(f"{self.CAMERA_API_URL}?custom=1&cmd=3014", timeout=10) as resp:
                await resp.text()
            logger.info("HEALTH: камера доступна")
            return True
        except Exception as e:
            logger.warning(f"HEALTH: {e}")
            return False

    async def _api_call(self, session, cmd: int, par=None, timeout=3) -> dict:
        params = {"custom": 1, "cmd": cmd}
        if par is not None:
            params["par"] = par
        async with session.get(self.CAMERA_API_URL, params=params, timeout=timeout) as resp:
            data = await resp.text()
            logger.info(f"API cmd={cmd}: ответ: {data}")
            root = etree.fromstring(data.encode('utf-8'))
            return {el.tag: el.text for el in root.iter() if el.text}

    async def _take_snapshot(self, session) -> tuple:
        """Сделать снимок cmd=1001. Возвращает (success, status, filename, filepath)."""
        ERROR_CODES = {
            -26: "Нет SD карты", -11: "SD карта заполнена",
            -36: "Ошибка SD карты", -37: "SD карта требует форматирования",
        }

        try:
            await self._api_call(session, 3036, par=0, timeout=2)
        except Exception:
            pass

        try:
            record_status = await self._api_call(session, 2024, timeout=3)
            is_recording = record_status.get("Value") == "2"
        except Exception as e:
            logger.error(f"API cmd=2024: {e}")
            return False, -1, None, None

        if is_recording:
            logger.info("Камера записывает видео — cmd=2017&par=1")
            try:
                result = await self._api_call(session, 2017, par=1, timeout=10)
                status = int(result.get("Status", "-1"))
                filename = result.get("NAME")
                filepath = result.get("FPATH")
                if status == 0:
                    logger.info(f"Снимок во время записи (File={filename})")
                    return True, 0, filename, filepath
                else:
                    logger.warning(f"Ошибка Status={status}: {ERROR_CODES.get(status, 'неизвестно')}")
                    return False, status, None, None
            except Exception as e:
                logger.error(f"API cmd=2017: {e}")
                return False, -1, None, None
        else:
            logger.info("Переключение в режим фото (cmd=3001&par=0)")
            try:
                mode_result = await self._api_call(session, 3001, par=0, timeout=3)
                mode_status = int(mode_result.get("Status", "-1"))
                if mode_status in ERROR_CODES:
                    logger.warning(f"Ошибка: {ERROR_CODES[mode_status]} (Status={mode_status})")
                    return False, mode_status, None, None
            except Exception as e:
                logger.error(f"API cmd=3001: {e}")
                return False, -1, None, None

            logger.info("Создание снимка (cmd=1001, таймаут 10 сек)...")
            try:
                result = await self._api_call(session, 1001, timeout=10)
                status = int(result.get("Status", "-1"))
                filename = result.get("NAME")
                filepath = result.get("FPATH")
                if status == 0:
                    logger.info(f"Снимок выполнен (Status=0, File={filename}, Path={filepath})")
                else:
                    logger.warning(f"Ошибка Status={status}: {ERROR_CODES.get(status, 'неизвестно')}")
            except Exception as e:
                logger.error(f"API cmd=1001: {e}")
                try:
                    await self._api_call(session, 3001, par=0, timeout=3)
                except Exception:
                    pass
                return False, -1, None, None

            try:
                await self._api_call(session, 3001, par=0, timeout=3)
            except Exception:
                pass

            return status == 0, status, filename, filepath

    async def _download_file(self, session, url, path) -> tuple:
        """Скачать файл. Возвращает (success, error_msg)."""
        try:
            async with session.get(url, timeout=120) as resp:
                if resp.status == 200:
                    async with aiofiles.open(path, 'wb') as f:
                        await f.write(await resp.read())
                    return True, None
                return False, f"HTTP {resp.status}"
        except Exception as e:
            return False, str(e)

    # ── Основная логика: снимок + загрузка ───────────────────────────────────
    async def snapshot_and_download(self, cam) -> dict:
        """Сделать один снимок и скачать именно этот файл."""
        snap_start = datetime.now()
        log_messages = []
        error_messages = []
        ble_attempts = 0
        wifi_connect_attempts = 0
        snap_file = None
        snap_fpath = None

        # SnapshotLog
        async with self.async_session() as session:
            snap_log = SnapshotLog(
                PhotoTrapId=cam.Id, CycleNumber=1,
                StartTime=snap_start, Status='PENDING', ActivityType='snapshot_download'
            )
            session.add(snap_log)
            await session.commit()
            await session.refresh(snap_log)

        # BLE open
        ble_ok = False
        for attempt in range(self.max_retries_per_camera):
            ble_attempts += 1
            if await self.send_ble_command(cam.MacAddress, "open"):
                ble_ok = True
                break
            if attempt < self.max_retries_per_camera - 1:
                await asyncio.sleep(self.retry_delay)

        if not ble_ok:
            error_messages.append(f"BLE open не удался ({ble_attempts} попыток)")
        else:
            logger.info(f"Ожидание Wi-Fi ({self.wifi_wait_after_open} сек)...")
            await asyncio.sleep(self.wifi_wait_after_open)

            # Wi-Fi
            wifi_ok, wifi_connect_attempts = await self.connect_to_wifi(
                cam.WifiSSID,
                on_retry=self._ble_open_retry(cam.MacAddress)
            )
            if not wifi_ok:
                error_messages.append(f"Wi-Fi не подключился ({wifi_connect_attempts} попыток)")
            else:
                async with aiohttp.ClientSession() as http_session:
                    if not await self._health_check(http_session):
                        error_messages.append("Health-check не прошёл")
                    else:
                        # ── Снимок ────────────────────────────────────────
                        snap_ok, snap_status, snap_file, snap_fpath = await self._take_snapshot(http_session)
                        if snap_ok and snap_file:
                            log_messages.append(f"Снимок создан: {snap_file}")

                            # ── Загрузка именно этого файла ───────────────
                            clean_path = snap_fpath.replace('A:\\', '').replace('\\', '/')
                            url = f"{self.CAMERA_API_URL}{clean_path}"
                            snap_time = datetime.now()
                            ext = Path(snap_file).suffix  # .JPG
                            local_name = f"{cam.Id}_{snap_time.strftime('%Y-%m-%d-%H-%M-%S')}{ext}"
                            local_path = self.download_dir / local_name
                            local_path.parent.mkdir(parents=True, exist_ok=True)

                            logger.info(f"Загрузка файла: {snap_file} -> {local_path}")
                            dl_ok, dl_err = await self._download_file(http_session, url, local_path)

                            if dl_ok:
                                file_size = local_path.stat().st_size if local_path.exists() else 0
                                log_messages.append(f"Файл загружен: {local_name} ({file_size} байт)")
                                logger.info(f"Файл загружен: {local_name} ({file_size} байт)")

                                # Сжатие (если включено)
                                self._compress_downloaded_file(local_path)

                                # Запись в DownloadLog (успех)
                                async with self.async_session() as dl_session:
                                    dl_entry = DownloadLog(
                                        PhotoTrapId=cam.Id,
                                        FileName=local_name,
                                        FilePath=str(local_path),
                                        FileSize=file_size,
                                        IsSuccess=True,
                                        LocalPath=str(local_path),
                                        DownloadedAt=datetime.now()
                                    )
                                    dl_session.add(dl_entry)
                                    await dl_session.commit()
                                    logger.info(f"DownloadLog: записано (успех, {local_name})")
                            else:
                                error_messages.append(f"Ошибка загрузки {snap_file}: {dl_err}")
                                logger.error(f"Ошибка загрузки {snap_file}: {dl_err}")

                                # Запись в DownloadLog (ошибка)
                                async with self.async_session() as dl_session:
                                    dl_entry = DownloadLog(
                                        PhotoTrapId=cam.Id,
                                        FileName=snap_file,
                                        IsSuccess=False,
                                        ErrorMessage=str(dl_err),
                                        DownloadedAt=datetime.now()
                                    )
                                    dl_session.add(dl_entry)
                                    await dl_session.commit()
                                    logger.info(f"DownloadLog: записано (ошибка, {snap_file})")
                        else:
                            error_messages.append(f"Ошибка снимка (Status={snap_status})")

                # Закрываем соединения
                await self.send_ble_command(cam.MacAddress, "close")
                self.disconnect_wifi()

        # Сводка
        summary = (f"BLE open: {ble_attempts}/{self.max_retries_per_camera}, "
                   f"Wi-Fi connect: {wifi_connect_attempts}")
        log_messages.insert(0, summary)

        # Обновляем SnapshotLog
        snap_end = datetime.now()
        status = 'OK' if not error_messages else 'ERROR'
        async with self.async_session() as session:
            entry = await session.get(SnapshotLog, snap_log.Id)
            entry.EndTime = snap_end
            entry.FileName = snap_file
            entry.Status = status
            entry.LogMessage = "; ".join(log_messages)
            entry.ErrorMessage = "; ".join(error_messages) if error_messages else None
            await session.commit()

        logger.info(f"[{cam.MacAddress}] {summary} | {status}")
        return {"success": not error_messages, "log": summary, "file": snap_file}

    async def run(self):
        await self.init_db()

        try:
            async with self.async_session() as session:
                db_config = await load_config(session)
                self._apply_config(db_config)

                if not await self._validate_cameras(session):
                    logger.error("Камеры не настроены. Выполните калибровку: python calibration.py")
                    return

                # Загружаем активные камеры
                result = await session.execute(
                    select(PhotoTrap).where(
                        PhotoTrap.MacAddress.isnot(None),
                        PhotoTrap.WifiSSID.isnot(None),
                        PhotoTrap.IsActive == True
                    )
                )
                cameras = result.scalars().all()

                if not cameras:
                    logger.warning("Нет активных камер")
                    return

                logger.info(f"\n{'='*50}")
                logger.info(f"СНИМОК + ЗАГРУЗКА | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"Камер: {len(cameras)}")
                logger.info(f"{'='*50}")

                for cam in cameras:
                    if not cam.WifiSSID:
                        logger.info(f"[{cam.MacAddress}] Пропуск — нет SSID")
                        continue

                    await self.snapshot_and_download(cam)

                    if cam != cameras[-1]:
                        logger.info(f"Пауза {self.camera_cooldown} сек...")
                        await asyncio.sleep(self.camera_cooldown)

                logger.info(f"\n{'='*50}")
                logger.info("ЗАВЕРШЕНО")
                logger.info(f"{'='*50}")

        except Exception as e:
            logger.error(f"Ошибка в run(): {e}", exc_info=True)
        finally:
            await self.engine.dispose()
            logger.info("Ресурсы освобождены")

    async def _validate_cameras(self, session) -> bool:
        try:
            result = await session.execute(
                select(PhotoTrapConfig.Value).where(PhotoTrapConfig.Key == 'CamerasCount')
            )
            cameras_count_str = result.scalar_one_or_none()
            expected_count = int(cameras_count_str) if cameras_count_str else 1
        except Exception as e:
            logger.warning(f"Не удалось прочитать CamerasCount из БД: {e}, ожидаем 1")
            expected_count = 1

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

        for cam in cameras:
            logger.info(f"  OK: {cam.Name} | {cam.MacAddress} | SSID: {cam.WifiSSID}")

        return True


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
        manager = SnapshotDownloadManager()
        asyncio.run(manager.run())
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass
