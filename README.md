# Safe Telegram PC Bot

Прозрачный проект из двух частей:

1. `server_bot.py` ставится на VPS и поднимает:
   - `Telegram`-бота
   - `HTTP API` для агентов
2. `pc_agent.py` ставится на каждый свой Windows-ПК вручную.

Тут нет обфускации, упаковщиков и скрытой установки. Код специально оставлен простым, чтобы потом было легко править под себя.

## Что умеет бот

- список ПК со статусом `ONLINE/OFFLINE`
- автоматические уведомления `онлайн/оффлайн`
- выбор активного ПК
- стабильная работа с несколькими устройствами
- `/help`
- `/status`
- `/top`
- `/info`
- `/uptime`
- `/net`
- `/drives`
- `/apps`
- `/services`
- `/screenshot`
- `/cmd <powershell command>`
- `/run <alias>`
- `/closeapp <alias>`
- `/restartapp <alias>`
- `/lock`
- `/restart`
- `/shutdown`
- удаление установленного `SchoolPro` с подтверждением

## Быстрый старт для сервера

### 1. Установка Python и зависимостей

```bash
# dnf or yum-based systems:
dnf install -y python3 python3-pip

# apt-based systems:
# apt update
# apt install -y python3 python3-pip python3-venv

mkdir -p /root/pc-telebot
cd /root/pc-telebot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn python-telegram-bot
```

### 2. Скопировать файл

Положи `server_bot.py` в `/root/pc-telebot/server_bot.py`.

### 3. Задать переменные

```bash
export PCBOT_TOKEN="TOKEN_IZ_BOTFATHER"
export PCBOT_REGISTRATION_KEY="SVOI_SEKRET_DLYA_AGENTOV"
export PCBOT_HOST="0.0.0.0"
export PCBOT_PORT="8090"
```

### 4. Запуск

```bash
cd /root/pc-telebot
source .venv/bin/activate
python3 server_bot.py
```

После запуска напиши своему боту `/start`.

## Быстрый старт для Windows-агента

### 1. Установка библиотек

```powershell
py -m pip install --upgrade pip
py -m pip install requests psutil pillow
```

### 2. Измени конфиг вверху `pc_agent.py`

Нужно поправить:

- `SERVER_URL`
- `REGISTRATION_KEY`
- `APP_ALIASES`
- `WATCHED_SERVICES`

Минимум:

```python
SERVER_URL = "http://217.60.245.42:8090"
REGISTRATION_KEY = "change-this-key"
```

### 3. Первый запуск

```powershell
py C:\path\to\pc_agent.py --set-name "My Gaming PC"
py C:\path\to\pc_agent.py
```

Рядом с файлом появится `agent_state.json`. В нем хранится токен агента и имя устройства.

### 4. Автозапуск

Автозапуск делается для текущего пользователя без всплывающего окна терминала:

```powershell
py C:\path\to\pc_agent.py --install-startup
```

## Где что менять

### В `server_bot.py`

- `PCBOT_TOKEN` - токен Telegram-бота
- `PCBOT_REGISTRATION_KEY` - ключ регистрации
- `PCBOT_PORT` - порт API
- `ONLINE_TIMEOUT_SECONDS` - через сколько секунд считать ПК оффлайн
- `PRESENCE_SWEEP_INTERVAL_SECONDS` - как часто перепроверять статус устройств

### В `pc_agent.py`

- `APP_ALIASES` - какие приложения разрешено запускать и закрывать
- `WATCHED_SERVICES` - какие службы показывать в `/services`
- `HEARTBEAT_INTERVAL_SECONDS`
- `COMMAND_POLL_SECONDS`

## Почему такой вариант меньше раздражает AV

Гарантировать, что антивирус вообще никогда не среагирует, нельзя. Но здесь уже сделано нормальное базовое:

- обычный читаемый `Python`
- без упаковщиков
- без обфускации
- без скрытия файлов и без системной установки в `Program Files`
- `/cmd` теперь есть, но выполняет произвольные PowerShell-команды на выбранном ПК
- только явные команды и whitelist приложений

## `/cmd`

Команда `/cmd` отправляет всё, что идет после нее, в `powershell.exe` на выбранном Windows-ПК.

Примеры:

```text
/cmd Get-Location
/cmd Get-Process | Sort-Object CPU -Descending | Select-Object -First 5
/cmd dir C:\Users\14\Desktop
```

Что важно:

- выполнение идет через `powershell.exe -NoProfile -NonInteractive -Command`
- рабочая папка по умолчанию: домашняя папка пользователя агента
- таймаут одной команды: `25` секунд
- в Telegram бот красиво показывает команду, код выхода, `stdout` и `stderr`
- слишком длинный вывод обрезается для Telegram

## Установка агента через GitHub

Без прав администратора удобнее ставить агент в `%LocalAppData%\SchoolPro`.

Для GitHub достаточно загрузить:

- `pc_agent.py`
- `install.ps1`
- `.gitignore`

Пример запуска установщика на своем ПК:

```powershell
powershell -ExecutionPolicy Bypass -Command "$tmp = Join-Path $env:TEMP 'schoolpro-install.ps1'; Invoke-WebRequest 'https://raw.githubusercontent.com/USERNAME/REPO/main/install.ps1' -OutFile $tmp; powershell -ExecutionPolicy Bypass -File $tmp -AgentUrl 'https://raw.githubusercontent.com/USERNAME/REPO/main/pc_agent.py' -DisplayName 'My PC'"
```

Что делает `install.ps1`:

- создает папку `%LocalAppData%\SchoolPro`
- скачивает `pc_agent.py`
- создает `.venv`
- ставит `requests`, `psutil`, `pillow`
- включает `TLS 1.2`, чтобы загрузка с GitHub проходила стабильнее на старых Windows/PowerShell
- создает скрытый автозапуск для текущего пользователя через `HKCU\...\Run`, а если это недоступно - через `Startup` + `.vbs`
- сразу запускает агент

По совместимости: логика установки и автозапуска теперь рассчитана на разные версии Windows/PowerShell, но основной реальный прогон проверен на Windows 10 и Windows 11.

## Что можно добавить потом

1. `systemd`-сервис для сервера
2. уведомления о высокой загрузке CPU/RAM
3. экспорт логов в файл
4. дополнительные кнопки в inline-меню
