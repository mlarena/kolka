# Анализ скриптов для автономной работы как systemd сервисы

## Контекст

Скрипты работают **автономно**, без человека. Конфигурация должна работать **годами**.
Приоритеты: стабильность, экономия памяти, нет утечек соединений с БД.

---

## Текущее состояние

- Калибровка удалена из скриптов, выполняется вручную: `python3 calibration.py`
- На старте `_validate_cameras()` проверяет что камеры настроены
- Если проверка не прошла — скрипт завершается

---

## Архитектура запуска

### kolka_take_photo — долгоживущий сервис

```
systemd service (Type=simple, Restart=on-failure)
  └──kolka_take_photo.py
        └── while True: цикл снимков каждые N минут
```

### kolka_download — по расписанию (timer)

```
systemd timer (OnCalendar=*-*-* *:00)
  └──kolka_download.service (Type=oneshot)
        └──kolka_download.py — один проход, завершение
```

---

## Проблемы и рекомендации

### 1. kolka_download: запуск по timer с проверкой блокировки

**Проблема:** Timer запускает процесс каждый час. Если предыдущий запуск ещё не
завершился (камеры медленные, BLE завис), будет два процесса одновременно.

**Решение:** Файл-блокировка при старте:

```python
import fcntl, sys, os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOCK_FILE = SCRIPT_DIR / "service.lock"

def main():
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
        os.unlink(LOCK_FILE)
```

**systemd timer:**

```ini
# /etc/systemd/system/kolka_download.timer
[Unit]
Description=Таймер запуска скачивания фото (каждый час)

[Timer]
OnCalendar=*-*-* *:00
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/kolka_download.service
[Unit]
Description=Скачивание фото с фотоловушек
After=network.target bluetooth.target postgresql.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/kolka_service_download
ExecStart=/opt/kolka_service_download/venv/bin/python /opt/kolka_service_download/kolka_download.py
TimeoutStartSec=1800
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kolka-download
```

---

### 2. kolka_take_photo: обновление списка камер в каждом цикле

**Проблема:** Камеры загружаются один раз при старте. Если камеру сделали
неактивной (`IsActive = false`) или добавили новую — скрипт не увидит.

**Решение:** Перечитывать камеры и конфиг в каждом цикле. Но без создания
новых engine/session — использовать одну сессию:

```python
async def run(self):
    await self.init_db()

    # Одна сессия на весь цикл жизни сервиса
    async with self.async_session() as session:
        db_config = await load_config(session)
        self._apply_config(db_config)

        if not await self._validate_cameras(session):
            return

        cycle = 0
        while not shutdown_event.is_set():
            cycle += 1

            # Перечитываем конфиг и камеры в каждом цикле
            db_config = await load_config(session)
            self._apply_config(db_config)

            cameras = await self._load_cameras(session)
            if not cameras:
                logger.warning("Нет активных камер, ждём...")
                await self._wait_with_shutdown(session, 300)
                continue

            # ... выполнение цикла снимков

            # Ожидание до следующего цикла с проверкой сигнала
            await self._wait_with_shutdown(session, wait)
```

Метод загрузки камер:

```python
async def _load_cameras(self, session) -> list:
    result = await session.execute(
        select(PhotoTrap).where(
            PhotoTrap.MacAddress.isnot(None),
            PhotoTrap.WifiSSID.isnot(None),
            PhotoTrap.IsActive == True
        )
    )
    return result.scalars().all()
```

Метод ожидания с обработкой SIGTERM:

```python
async def _wait_with_shutdown(self, session, seconds: int):
    """Ожидание с проверкой SIGTERM каждые 60 сек"""
    waited = 0
    while waited < seconds and not shutdown_event.is_set():
        chunk = min(60, seconds - waited)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=chunk)
            break
        except asyncio.TimeoutError:
            waited += chunk
```

---

### 3. Утечки соединений с БД

**Проблема:** SQLAlchemy engine создаёт пул соединений. При долгой работе (годы)
могут накапливаться утечки, особенно если сессии закрываются не полностью.

**Решения:**

**A. Один engine на жизнь процесса:**
```python
def __init__(self, config_path):
    # engine создаётся ОДИН раз
    self.engine = create_async_engine(
        db_url,
        echo=False,
        pool_size=2,           # Максимум 2 соединения в пуле
        max_overflow=1,        # Максимум 1 дополнительное
        pool_timeout=30,
        pool_recycle=3600,     # Пересоздавать соединения каждый час
        pool_pre_ping=True     # Проверять соединение перед использованием
    )
```

**B. Не создавать сессии в циклах ожидания:**
```python
# ПЛОХО — новая сессия каждые 60 сек:
while waited < wait:
    await asyncio.sleep(60)
    async with self.async_session() as session:  # ← утечка
        db_config = await load_config(session)

# ХОРОШО — одна сессия:
async with self.async_session() as session:
    while waited < wait:
        await asyncio.sleep(60)
        db_config = await load_config(session)  # ← та же сессия
```

**C. pool_pre_ping для обрыва соединений:**
```python
engine = create_async_engine(
    db_url,
    pool_pre_ping=True  # SELECT 1 перед использованием
)
```

---

### 4. Обработка памяти

**Проблема:** При работе годами могут накапливаться объекты в памяти.

**Решения:**

**A. Ограничивать размер XML-ответов:**
```python
async with session.get(url, timeout=15) as resp:
    data = await resp.read()
    # Не держать data в памяти дольше чем нужно
    files = self._parse_xml(data)
    del data  # Явно освободить
```

**B. Не хранить списки файлов в атрибутах класса:**
```python
# ПЛОХО:
self.all_files = []  # Растёт бесконечно

# ХОРОШО:
files = await self._get_file_list()  # Локальная переменная
```

**C. Периодический мониторинг:**
```python
import resource

def log_memory():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    logger.info(f"Память: RSS={usage.ru_maxrss} KB")
```

---

### 5. Обработка ошибок для автономной работы

**Проблема:** Без человека скрипт должен восстанавливаться сам.

**Принцип:** Ловить исключения на максимальном уровне, логировать, продолжать:

```python
async def run(self):
    await self.init_db()

    async with self.async_session() as session:
        while not shutdown_event.is_set():
            try:
                await self._process_cycle(session)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле: {e}", exc_info=True)
                # НЕ выходим — ждём и пробуем снова
                await self._wait_with_shutdown(session, 300)

            await self._wait_with_shutdown(session, self.snapshot_interval * 60)
```

**Критические ошибки (выход):**
- БД недоступна после 5 попыток
- Bluetooth адаптер не найден

**Некритичные (продолжаем):**
- Камера не отвечает → пропускаем
- Wi-Fi не подключился → пропускаем
- Файл не скачался → помечаем ошибку, следующий файл

---

### 6. Graceful shutdown для kolka_take_photo

```python
import signal

shutdown_event = asyncio.Event()

def _handle_signal(sig, frame):
    logger.info("Получен сигнал %s, завершение...", sig)
    shutdown_event.set()

# Регистрируем ДО asyncio.run()
signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)
```

В цикле:
```python
while not shutdown_event.is_set():
    # ... работа
    await self._wait_with_shutdown(session, wait)
```

---

### 7. Логирование для автономной работы

**Требования:**
- Логи в systemd journal (stdin/stdout)
- Ротация через logrotate (14 дней)
- Не дублировать StreamHandler + journal

**Рекомендация:** Убрать `StreamHandler()`, оставить только journal:

```python
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_path, encoding='utf-8'),
        # StreamHandler убран — journal перехватит stderr
    ]
)
```

**logrotate:**
```
/opt/kolka_service_take_photo/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    create 0644 root root
}
```

---

## Рекомендуемые systemd unit файлы

### kolka_take_photo (долгоживущий сервис)

```ini
[Unit]
Description=Kolka — снимки по расписанию
After=network.target bluetooth.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/kolka_service_take_photo
ExecStart=/opt/kolka_service_take_photo/venv/bin/python /opt/kolka_service_take_photo/kolka_take_photo.py

Restart=on-failure
RestartSec=120
TimeoutStartSec=60
TimeoutStopSec=60

KillSignal=SIGTERM
SendSIGKILL=no

MemoryMax=512M
CPUQuota=50%

Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kolka-take-photo
```

### kolka_download (timer + oneshot)

```ini
# /etc/systemd/system/kolka_download.timer
[Unit]
Description=Таймер скачивания фото (каждый час)

[Timer]
OnCalendar=*-*-* *:00
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/kolka_download.service
[Unit]
Description=Скачивание фото с фотоловушек
After=network.target bluetooth.target postgresql.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/kolka_service_download
ExecStart=/opt/kolka_service_download/venv/bin/python /opt/kolka_service_download/kolka_download.py
TimeoutStartSec=1800

Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kolka-download
```

---

## Итоговая таблица

| # | Что | Приоритет | Статус |
|---|-----|-----------|--------|
| 1 | Файл-блокировка от повторного запуска download | Высокий | Исправить |
| 2 | Обновление списка камер в каждом цикле take_photo | Высокий | Исправить |
| 3 | Один engine, pool_size=2, pool_recycle=3600, pool_pre_ping | Высокий | Исправить |
| 4 | Одна сессия в цикле ожидания (не новая каждые 60 сек) | Высокий | Исправить |
| 5 | Graceful shutdown (SIGTERM → shutdown_event) | Высокий | Исправить |
| 6 | try/except в основном цикле (не падать) | Высокий | Исправить |
| 7 | Убрать StreamHandler, оставить journal | Средний | Исправить |
| 8 | pool_pre_ping для обрыва соединений | Средний | Исправить |
| 9 | Логирование памяти (RSS) | Низкий | Опционально |
| 10 | Ограничение размера XML-ответов | Низний | Опционально |
