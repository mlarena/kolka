# Kolka Snapshot Download — Техническое задание

## 1. Общее описание

### 1.1. Назначение

Система автоматического создания снимков и загрузки фотографий с фотоловушек (камер-ловушек)
через BLE + Wi-Fi. Работает как systemd-сервис на Linux (Raspberry Pi / ПК).

### 1.2. Компоненты

| Компонент | Файл | Назначение |
|-----------|------|------------|
| Основной скрипт | `kolka_snapshot_and_download.py` | Снимок + загрузка + сжатие |
| Калибровка | `calibration.py` | Обнаружение камер и привязка Wi-Fi |
| Конфигурация | `config_loader.py` | Загрузка параметров (БД → файл → дефолты) |
| Установка | `install_snapshot_download.sh` | Деплой systemd-сервиса |
| Удаление | `uninstall_snapshot_download.sh` | Удаление systemd-сервиса |

## 2. Требования

### 2.1. Функциональные требования

#### FR-01: Автоматический снимок

Скрипт должен автоматически создавать снимок на камере по команде BLE `cmd=1001`.

#### FR-02: Загрузка файла

После создания снимка скрипт должен скачать именно этот файл (не все файлы с камеры).

#### FR-03: Именование файлов

Файлы сохраняются в формате `{CamId}_{YYYY-MM-DD-HH-MM-SS}.{ext}`.

**Пример:** `1_2026-07-29-13-52-27.JPG`

#### FR-04: Директория загрузки

Файлы сохраняются в директорию из конфига `DownloadPath` (по умолчанию `/outgoing/cameratrap/`).
Без создания подпапок с датой.

#### FR-05: Сжатие изображений

После загрузки JPG/JPEG файл автоматически сжимается через `compress_images.py`
(ffmpeg, параметр `-q:v`). Сжатие управляется конфигом `CompressAfterDownload`.

#### FR-06: Логирование в БД

Каждый снимок записывается в `SnapshotLog` (ActivityType=`snapshot_download`).
Каждая загрузка записывается в `DownloadLog`.

#### FR-07: Логирование в файл

Логи пишутся в `logs/snapshot_download_log_{YYYY-MM-DD}_{HH}.log`.
Ротация через logrotate: ежедневно, 14 дней, gzip.

#### FR-08: Блокировка запуска

Одновременно может работать только один экземпляр скрипта (файловая блокировка `fcntl`).

#### FR-09: Расписание запуска

Скрипт запускается по расписанию systemd timer.
По умолчанию — каждый час. Расписание настраивается в файле таймера.

#### FR-10: Калибровка камер

Перед первым запуском обязательна калибровка (`calibration.py`):
- Фаза 1: обнаружение камер по BLE (Name + MacAddress)
- Фаза 2: привязка Wi-Fi SSID к каждой камере

#### FR-11: Автопропуск калибровки

Если все поля (Name, MacAddress, WifiSSID) заполнены **И** количество камер ≥ `CamerasCount` —
калибровка пропускается автоматически.

#### FR-12: Уникальность камер

- `MacAddress`: UNIQUE constraint на уровне БД
- `WifiSSID`: проверка уникальности на уровне приложения

### 2.2. Нефункциональные требования

#### NFR-01: Отказоустойчивость BLE

При ошибке `InProgress` (BLE-адаптер занят) — автоматический сброс адаптера
(`hciconfig hci0 down/up`) с одной повторной попыткой.

#### NFR-02: Отказоустойчивость Wi-Fi

При неудачном подключении — повторная отправка BLE `open` + ожидание `WifiWaitAfterOpen`
перед следующей попыткой. Количество попыток: `WifiMaxRetries`.

#### NFR-03: Отказоустойчивость загрузки

При обрыве загрузки — повторные попытки: `WifiDownloadRetries`.

#### NFR-04: Конфигурация через БД

Все параметры можно переопределить через таблицу `PhotoTrapConfig`
без перезапуска сервиса (загружаются при каждом запуске скрипта).

#### NFR-05: Автономность

Скрипт не зависит от внешних сервисов (кроме PostgreSQL и Bluetooth).
Камеры общаются напрямую по BLE и Wi-Fi.

## 3. Архитектура

### 3.1. Схема взаимодействия

```
┌─────────────────────────────────────────────────────┐
│                   Скрипт (Python)                    │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ BLE      │  │ Wi-Fi    │  │ HTTP (камера API) │  │
│  │ (bleak)  │  │ (nmcli)  │  │ (aiohttp)         │  │
│  └────┬─────┘  └────┬─────┘  └────────┬──────────┘  │
│       │              │                 │             │
└───────┼──────────────┼─────────────────┼─────────────┘
        │              │                 │
   ┌────▼────┐    ┌────▼────┐     ┌─────▼─────┐
   │ BLE     │    │ Wi-Fi   │     │ HTTP API  │
   │ Адаптер │    │ Интерфейс│     │ Камера    │
   │ (hci0)  │    │ (wlan0) │     │ 192.168.1.254│
   └────┬────┘    └────┬────┘     └─────┬─────┘
        │              │                 │
   ┌────▼──────────────▼─────────────────▼─────┐
   │              Фотоловушка (GCBT40)          │
   │  BLE: 0000ff11-...  │  Wi-Fi: CAM_xxx     │
   └───────────────────────────────────────────┘
```

### 3.2. Жизненный цикл обработки одной камеры

```
Начало
  │
  ├─► BLE scan → найти камеру по MacAddress
  │     └─ Ошибка InProgress → сброс адаптера → повтор
  │
  ├─► BLE write "open" → камера включает Wi-Fi
  │     └─ Повтор: MaxRetriesPerCamera раз, задержка RetryDelay
  │
  ├─► Ожидание WifiWaitAfterOpen сек
  │
  ├─► nmcli connect к CAM_xxx
  │     └─ Повтор: WifiMaxRetries раз
  │     └─ Перед каждой попыткой: BLE "open" + ожидание
  │
  ├─► HTTP health check (http://192.168.1.254/)
  │
  ├─► HTTP cmd=1001 → получить имя и путь файла
  │     └─ Повтор: WifiDownloadRetries раз
  │
  ├─► HTTP GET → скачать файл в /outgoing/cameratrap/
  │     └─ Имя: {CamId}_{YYYY-MM-DD-HH-MM-SS}.{ext}
  │
  ├─► Сжатие (compress_images.py) если JPG и CompressAfterDownload=true
  │
  ├─► Запись в SnapshotLog + DownloadLog
  │
  ├─► BLE write "close" → камера выключает Wi-Fi
  │
  └─► CameraCooldown пауза → следующая камера
```

### 3.3. БД (таблицы)

#### PhotoTrap

| Поле | Тип | Описание |
|------|-----|----------|
| Id | INTEGER PK | Идентификатор камеры |
| Name | TEXT | BLE-имя устройства (GCBT40-xxxx) |
| MacAddress | TEXT UNIQUE | MAC-адрес BLE |
| WifiSSID | TEXT | Имя Wi-Fi сети (CAM_xxx) |

#### SnapshotLog

| Поле | Тип | Описание |
|------|-----|----------|
| Id | INTEGER PK | Идентификатор записи |
| PhotoTrapId | INTEGER FK | Ссылка на камеру |
| CycleNumber | INTEGER | Номер цикла |
| StartTime | TIMESTAMP | Время начала |
| EndTime | TIMESTAMP | Время завершения |
| Status | TEXT | PENDING / SUCCESS / ERROR |
| ActivityType | TEXT | `snapshot_download` |
| Attempts | INTEGER | Количество попыток BLE |
| ErrorMessage | TEXT | Текст ошибки |

#### DownloadLog

| Поле | Тип | Описание |
|------|-----|----------|
| Id | INTEGER PK | Идентификатор записи |
| PhotoTrapId | INTEGER FK | Ссылка на камеру |
| FileName | TEXT | Имя файла |
| FilePath | TEXT | Полный путь |
| FileSize | INTEGER | Размер в байтах |
| IsSuccess | BOOLEAN | Успех загрузки |
| ErrorMessage | TEXT | Текст ошибки |
| LocalPath | TEXT | Локальный путь |
| DownloadedAt | TIMESTAMP | Время загрузки |

#### PhotoTrapConfig

| Поле | Тип | Описание |
|------|-----|----------|
| Key | TEXT PK | Имя параметра |
| Value | TEXT | Значение |
| Description | TEXT | Описание |

## 4. Сценарии использования

### 4.1. Первый запуск (с нуля)

1. Установить систему: Python, PostgreSQL, bluez, NetworkManager, ffmpeg
2. Запустить калибровку: `python3 calibration.py`
3. Проверить камеры в БД
4. Установить сервис: `sudo bash install_snapshot_download.sh`
5. Дождаться первого запуска по таймеру или запустить вручную

### 4.2. Штатная работа

1. Таймер запускает скрипт по расписанию
2. Скрипт последовательно обрабатывает все камеры
3. Фото сохраняются в `/outgoing/cameratrap/`
4. Логи пишутся в файлы и БД

### 4.3. Добавление новой камеры

1. Включить новую камеру
2. Запустить калибровку: `python3 calibration.py`
3. Калибровка обнаружит новую камеру (проверка CamerasCount)
4. Привяжет Wi-Fi SSID
5. Следующий запуск сервиса обработает новую камеру

### 4.4. Изменение расписания

1. Отредактировать `/etc/systemd/system/kolka_snapshot_download.timer`
2. Выполнить: `sudo systemctl daemon-reload && sudo systemctl restart kolka_snapshot_download.timer`
3. Проверить: `sudo systemctl list-timers kolka_snapshot_download.timer`

### 4.5. Обновление скрипта

1. Отредактировать файлы в `/scanner/`
2. Переустановить сервис: `sudo bash /scanner/scripts/install_snapshot_download.sh`
3. Скрипт обновится автоматически (файлы копируются при установке)

### 4.6. Удаление сервиса

1. `sudo bash /scanner/scripts/uninstall_snapshot_download.sh`
2. Все файлы сервиса удаляются
3. БД и исходники в `/scanner/` не затрагиваются

## 5. Конфигурация (все параметры)

| Параметр | Тип | По умолчанию | Описание |
|----------|-----|-------------|----------|
| `DownloadPath` | string | `/outgoing/cameratrap` | Директория для загрузки |
| `CamerasCount` | int | `1` | Ожидаемое количество камер |
| `WifiPassword` | string | `12345678` | Пароль Wi-Fi камер |
| `WifiConnectTimeout` | int | `90` | Таймаут подключения к Wi-Fi (сек) |
| `WifiMaxRetries` | int | `5` | Попыток Wi-Fi подключения |
| `WifiWaitAfterOpen` | int | `25` | Ожидание после BLE open (сек) |
| `WifiDownloadRetries` | int | `3` | Попыток загрузки файла |
| `MaxRetriesPerCamera` | int | `3` | Попыток BLE open |
| `BleScanTimeout` | float | `10` | Таймаут BLE сканирования (сек) |
| `BleCommandTimeout` | float | `10` | Таймаут BLE подключения (сек) |
| `RetryDelay` | int | `15` | Задержка между попытками (сек) |
| `CameraCooldown` | int | `20` | Пауза между камерами (сек) |
| `CloseWaitSeconds` | int | `25` | Ожидание после BLE close (сек) |
| `MaxScanRetries` | int | `10` | Попыток BLE-сканирования (калибровка) |
| `CompressAfterDownload` | bool | `true` | Сжимать JPG после загрузки |
| `CompressQuality` | int | `12` | Качество ffmpeg (-q:v, 1–31) |
| `NeedCalibration` | bool | `false` | Требовать калибровку |

## 6. Зависимости

### 6.1. Системные

| Пакет | Назначение |
|-------|------------|
| Python 3.10+ | Интерпретатор |
| PostgreSQL | База данных |
| bluez, libbluetooth-dev | Bluetooth-стек |
| NetworkManager (nmcli) | Управление Wi-Fi |
| ffmpeg | Сжатие изображений |

### 6.2. Python (requirements.txt)

| Пакет | Назначение |
|-------|------------|
| bleak | BLE-сканирование и подключение |
| aiohttp | HTTP-клиент (загрузка файлов) |
| aiofiles | Асинхронная запись файлов |
| lxml | Парсинг XML-ответов камеры |
| sqlalchemy[asyncio] | ORM и управление БД |
| asyncpg | PostgreSQL-драйвер |
