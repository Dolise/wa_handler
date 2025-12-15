import os

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
EMULATORS_COUNT = 10

# Emulator settings
EMULATOR_PORTS = [5554 + i * 2 for i in range(EMULATORS_COUNT)]  # 5554, 5556, 5558... (20 эмуляторов)
AVD_NAME = "Pixel_4_API_30"
EMULATOR_GPU_MODE = os.getenv("EMULATOR_GPU_MODE", "off")  # off|swiftshader_indirect|host
EMULATOR_CORES = int(os.getenv("EMULATOR_CORES", "1"))
EMULATOR_MEMORY_MB = int(os.getenv("EMULATOR_MEMORY_MB", "1024"))
EMULATOR_CACHE_MB = int(os.getenv("EMULATOR_CACHE_MB", "128"))

# Proxy settings (HTTP proxy для всех эмуляторов)
PROXY_HOST = "na.proxy.piaproxy.com"
PROXY_PORT = "5000"
PROXY_USER = "user-mtt33_A0xiF-region-ru"
PROXY_PASS = "nskjfdbnker4G"
PROXY_URL = f"{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"  # user:pass@host:port для -http-proxy

# Timeouts
REGISTRATION_TIMEOUT = 600  # 10 минут на регистрацию
CODE_WAIT_TIMEOUT = 120  # 2 минуты на ожидание кода
HEARTBEAT_INTERVAL = 10  # Обновлять heartbeat каждые 10 секунд
HEARTBEAT_TIMEOUT = 120  # Считать эмулятор мертвым если нет heartbeat 2 минуты

# Android SDK
ANDROID_HOME = os.getenv("ANDROID_HOME", os.path.expanduser("~/Library/Android/sdk"))

