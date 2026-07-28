"""
Создание снимков на фотоловушках (Linux).
Отправляет команду cmd=1001 на каждую камеру с заданной частотой.

Использование:
    python kolka_take_photo_linux.py
"""
import asyncio
import aiohttp
import logging
import signal
import sys
import os
import fcntl
import json
import subprocess
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime
from lxml import etree
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, and_
from bleak import BleakScanner, BleakClient

from models import Base, PhotoTrap, SnapshotLog, PhotoTrapConfig
from config_loader import load_config

# ── Graceful shutdown ─────────────────────────────────────────────────────────
shutdown_event = asyncio.Event()

def _handle_signal(sig, frame):
    logger.info("Получен сигнал %s, завершение...", sig)
    shutdown_event.set()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

# ── Логирование ───────────────────────────────────────────────────────────────
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
log_filename = f"take_photo_log_{datetime.now().strftime('%Y-%m-%d')}.log"
log_path = log_dir / log_filename
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        TimedRotatingFileHandler(log_path, when='midnight', interval=1, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logger.info(f"Лог-файл: {log_path}")


class SnapshotCameraManager:
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

        self.wifi_password = self.config.get('WifiPassword', '12345678')
        self.ble_scan_timeout = float(self.config.get('BleScanTimeout', 10))
        self.ble_command_timeout = float(self.config.get('BleCommandTimeout', 10))
        self.wifi_wait_after_open = int(self.config.get('WifiWaitAfterOpen', 25))
        self.wifi_connect_timeout = int(self.config.get('WifiConnectTimeout', 90))
        self.wifi_max_retries = int(self.config.get('WifiMaxRetries', 5))
        self.retry_delay = int(self.config.get('RetryDelay', 15))
        self.max_retries_per_camera = int(self.config.get('MaxRetriesPerCamera', 3))
        self.camera_cooldown = int(self.config.get('CameraCooldown', 20))

    def _apply_config(self, config: dict):
        self.config = config
        self.wifi_password = config.get('WifiPassword', '12345678')
        self.ble_scan_timeout = float(config.get('BleScanTimeout', 10))
        self.ble_command_timeout = float(config.get('BleCommandTimeout', 10))
        self.wifi_wait_after_open = int(config.get('WifiWaitAfterOpen', 25))
        self.wifi_connect_timeout = int(config.get('WifiConnectTimeout', 90))
        self.wifi_max_retries = int(config.get('WifiMaxRetries', 5))
        self.retry_delay = int(config.get('RetryDelay', 15))
        self.max_retries_per_camera = int(config.get('MaxRetriesPerCamera', 3))
        self.camera_cooldown = int(config.get('CameraCooldown', 20))

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

    async def _load_cameras(self, session) -> list:
        """Загрузить активные камеры с заполненными MacAddress и WifiSSID"""
        result = await session.execute(
            select(PhotoTrap).where(
                PhotoTrap.MacAddress.isnot(None),
                PhotoTrap.WifiSSID.isnot(None),
                PhotoTrap.IsActive == True
            )
        )
        return result.scalars().all()

    def _convert_connection_string(self, conn_string: str) -> str:
        params = {part.split('=')[0].strip(): part.split('=')[1].strip()
                  for part in conn_string.split(';') if '=' in part}
        return f"postgresql+asyncpg://{params.get('Username')}:{params.get('Password')}@{params.get('Host')}/{params.get('Database')}"

    async def init_db(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("База данных инициализирована")

    async def send_ble_command(self, mac_address: str, command: str) -> bool:
        logger.info(f"BLE: Поиск {mac_address}...")
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
            logger.error(f"BLE: ошибка поиска {mac_address}: {e}")
            return False

    def _run_nmcli(self, *args) -> tuple:
        cmd = ['nmcli'] + list(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", "timeout", 1
        except Exception as e:
            return "", str(e), 1

    def connect_to_wifi(self, ssid: str) -> tuple:
        logger.info(f"Wi-Fi: Подключение к {ssid}...")
        self._run_nmcli('connection', 'down', 'id', ssid)
        time.sleep(2)
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
            logger.info(f"Wi-Fi: Подключение к {ssid} (таймаут {self.wifi_connect_timeout} сек, попытка {attempt}/{self.wifi_max_retries})...")
            start = time.time()
            while time.time() - start < self.wifi_connect_timeout:
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
            return False, -1, None

        if is_recording:
            logger.info("Камера записывает видео — cmd=2017&par=1")
            try:
                result = await self._api_call(session, 2017, par=1, timeout=10)
                status = int(result.get("Status", "-1"))
                filename = result.get("NAME")
                if status == 0:
                    logger.info(f"Снимок во время записи выполнен (Status=0, File={filename})")
                    return True, 0, filename
                else:
                    logger.warning(f"Ошибка Status={status}: {ERROR_CODES.get(status, 'неизвестно')}")
                    return False, status, None
            except Exception as e:
                logger.error(f"API cmd=2017: {e}")
                return False, -1, None
        else:
            logger.info("Переключение в режим фото (cmd=3001&par=0)")
            try:
                mode_result = await self._api_call(session, 3001, par=0, timeout=3)
                mode_status = int(mode_result.get("Status", "-1"))
                if mode_status in ERROR_CODES:
                    logger.warning(f"Ошибка: {ERROR_CODES[mode_status]} (Status={mode_status})")
                    return False, mode_status, None
            except Exception as e:
                logger.error(f"API cmd=3001: {e}")
                return False, -1, None

            logger.info("Создание снимка (cmd=1001, таймаут 10 сек)...")
            try:
                result = await self._api_call(session, 1001, timeout=10)
                status = int(result.get("Status", "-1"))
                filename = result.get("NAME")
                if status == 0:
                    logger.info(f"Снимок выполнен (Status=0, File={filename})")
                else:
                    logger.warning(f"Ошибка Status={status}: {ERROR_CODES.get(status, 'неизвестно')}")
            except Exception as e:
                logger.error(f"API cmd=1001: {e}")
                try:
                    await self._api_call(session, 3001, par=0, timeout=3)
                except Exception:
                    pass
                return False, -1, None

            logger.info("Возврат в режим фото (cmd=3001&par=0)")
            try:
                await self._api_call(session, 3001, par=0, timeout=3)
            except Exception:
                pass

            return status == 0, status, filename

    async def make_snapshot(self, cam, cycle_number: int = 0) -> dict:
        snap_start = datetime.now()
        log_messages = []
        error_messages = []
        ble_attempts = 0
        wifi_connect_attempts = 0
        snap_file = None

        async with self.async_session() as session:
            snap_log = SnapshotLog(PhotoTrapId=cam.Id, CycleNumber=cycle_number, StartTime=snap_start, Status='PENDING', ActivityType='photo')
            session.add(snap_log)
            await session.commit()
            await session.refresh(snap_log)

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
            await asyncio.sleep(self.wifi_wait_after_open)

            wifi_ok, wifi_connect_attempts = self.connect_to_wifi(cam.WifiSSID)
            if not wifi_ok:
                error_messages.append(f"Wi-Fi не подключился ({wifi_connect_attempts} попыток)")
            else:
                async with aiohttp.ClientSession() as http_session:
                    if await self._health_check(http_session):
                        snap_ok, snap_status, snap_file = await self._take_snapshot(http_session)
                        if snap_ok:
                            log_messages.append(f"Снимок создан (файл: {snap_file})")
                        else:
                            error_messages.append(f"Ошибка cmd=1001 (Status={snap_status})")
                    else:
                        error_messages.append("Health-check не прошёл")

            await self.send_ble_command(cam.MacAddress, "close")
            self.disconnect_wifi()

        summary = f"BLE open: {ble_attempts}/{self.max_retries_per_camera}, Wi-Fi connect: {wifi_connect_attempts}"
        log_messages.insert(0, summary)

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
        return {"success": not error_messages, "log": summary}

    async def run(self):
        await self.init_db()

        try:
            async with self.async_session() as session:
                db_config = await load_config(session)
                self._apply_config(db_config)

                # ── Проверка что камеры настроены ──────────────────────────────
                if not await self._validate_cameras(session):
                    logger.error("Камеры не настроены. Выполните калибровку: python calibration.py")
                    return

                # ── Один цикл снимков (oneshot) ────────────────────────────────
                cameras = await self._load_cameras(session)
                if not cameras:
                    logger.warning("Нет активных камер")
                    return

                cycle_start = datetime.now()
                logger.info(f"\n{'='*50}")
                logger.info(f"ЦИКЛ | {cycle_start.strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"Камер: {len(cameras)}")
                logger.info(f"{'='*50}")

                for cam in cameras:
                    if shutdown_event.is_set():
                        break
                    if not cam.WifiSSID:
                        logger.info(f"[{cam.MacAddress}] Пропуск — нет SSID")
                        continue
                    await self.make_snapshot(cam, 1)
                    if cam != cameras[-1]:
                        await asyncio.sleep(self.camera_cooldown)

                elapsed = (datetime.now() - cycle_start).total_seconds()
                logger.info(f"\nЦикл завершён за {elapsed:.0f} сек")

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
        manager = SnapshotCameraManager()
        asyncio.run(manager.run())
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass
