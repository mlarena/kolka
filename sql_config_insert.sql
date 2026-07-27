-- Заполнение таблицы конфигурации начальными значениями
INSERT INTO "PhotoTrapConfig" ("Key", "Value", "Description") VALUES
('NeedCalibration', 'false', 'Запускать калибровку (фаза 1+2) перед загрузкой файлов'),
('DownloadPath', 'E:\\TestFoto\\images', 'Папка для сохранения файлов с камер'),
('CamerasCount', '1', 'Сколько камер должно быть в таблице PhotoTrap'),
('WifiPassword', '12345678', 'Пароль Wi-Fi сети камеры (WPA2PSK)'),
('BleScanTimeout', '10', 'Время BLE-сканирования (сек)'),
('BleCommandTimeout', '10', 'Таймаут BLE-подключения (сек)'),
('WifiWaitAfterOpen', '25', 'Ожидание после BLE open перед подключением к Wi-Fi (сек)'),
('WifiConnectTimeout', '45', 'Таймаут подключения к Wi-Fi сети камеры (сек)'),
('CloseWaitSeconds', '25', 'Пауза после close на все камеры (сек)'),
('RetryDelay', '15', 'Задержка между повторными попытками (сек)'),
('MaxRetriesPerCamera', '3', 'Попыток найти SSID / подключиться к одной камере'),
('MaxScanRetries', '10', 'Попыток BLE-сканирования для фазы 1'),
('CameraCooldown', '20', 'Пауза между разными камерами (сек)'),
('CompressQuality', '12', 'Качество сжатия ffmpeg -q:v (1-31, чем меньше тем лучше)'),
('WifiDownloadRetries', '3', 'Попыток реконнекта Wi-Fi при обрыве во время загрузки файлов'),
('DeleteAfterDownload', 'true', 'Удалять фото с камеры после загрузки (true/false)'),
('CompressAfterDownload', 'true', 'Сжимать изображения после загрузки (true/false)');
