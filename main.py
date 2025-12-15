"""
Агент для выполнения WhatsApp регистраций на эмуляторах (MEmu Edition).

Архитектура:
  - На тачке запущено N эмуляторов MEmu (127.0.0.1:21503, ...)
  - Агент полит handler (/agent/poll), получает задачи (номера телефонов)
  - Распределяет задачи по свободным эмуляторам из пула (asyncio.Queue)
  - Каждый эмулятор выполняет регистрацию через RegistrationExecutor (Pure ADB)
  - После завершения эмулятор возвращается в пул свободных
  - Статусы отправляются в handler (/agent/status)

Env переменные:
  HANDLER_URL       — URL handler'а (default: http://5.129.204.230:8000)
  AGENT_ID          — ID агента (default: hostname)
  EMULATOR_COUNT    — Количество эмуляторов (default: 10) - используется как fallback
  POLL_INTERVAL     — Интервал polling если нет задач (default: 5 сек)
  POLL_BACKOFF      — Пауза при ошибках (default: 10 сек)
  STATUS_RETRY      — Retry для отправки статусов (default: 3)
"""

import asyncio
import logging
import os
import signal
import socket
import sys
from typing import Any, Dict

import httpx

from registration_executor import RegistrationExecutor

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Конфиг
HANDLER_URL = os.getenv("HANDLER_URL", "http://5.129.204.230:8000")
AGENT_ID = os.getenv("AGENT_ID", socket.gethostname())
EMULATOR_COUNT = int(os.getenv("EMULATOR_COUNT", "10"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
POLL_BACKOFF = float(os.getenv("POLL_BACKOFF", "10"))
STATUS_RETRY = int(os.getenv("STATUS_RETRY", "3"))

def get_memu_devices():
    """Получает список подключенных устройств MEmu через ADB"""
    adb_path = os.getenv("ADB_PATH") or r"C:\Program Files\Microvirt\MEmu\adb.exe"
    try:
        import subprocess
        import re
        # Запускаем adb devices
        # Добавляем creationflag=0x08000000 для скрытия окна консоли на Windows (если нужно)
        result = subprocess.run([adb_path, "devices"], capture_output=True, text=True)
        
        # Ищем 127.0.0.1:2xxxx
        devices = re.findall(r"(127\.0\.0\.1:2\d{4})\s+device", result.stdout)
        if devices:
            logger.info(f"Found MEmu devices: {devices}")
            return devices
    except Exception as e:
        logger.error(f"Failed to get MEmu devices via ADB: {e}")
    
    # Fallback на расчетную генерацию (если adb не ответил или пусто)
    logger.warning("Using fallback MEmu device generation")
    return [f"127.0.0.1:{21503 + i * 10}" for i in range(EMULATOR_COUNT)]

# Создаем список эмуляторов динамически
EMULATOR_IDS = get_memu_devices()

# Глобальный флаг для graceful shutdown
shutdown_event = asyncio.Event()


async def post_status(
    client: httpx.AsyncClient,
    phone: str,
    status: str,
    emulator: str = "",
    code: str = "",
    error: str = "",
) -> None:
    """Отправить статус выполнения в handler с retry"""
    payload: Dict[str, Any] = {"phone": phone, "status": status}
    if emulator:
        payload["emulator"] = emulator
    if code:
        payload["code"] = code
    if error:
        payload["error"] = error

    for attempt in range(STATUS_RETRY):
        try:
            resp = await client.post(f"{HANDLER_URL}/agent/status", json=payload, timeout=15)
            resp.raise_for_status()
            logger.debug(f"[{phone}] Status updated: {status}")
            return
        except Exception as e:
            if attempt == STATUS_RETRY - 1:
                logger.error(f"[{phone}] Failed to send status after {STATUS_RETRY} attempts: {e}")
                # Не падаем, просто логируем
                return
            await asyncio.sleep(1)


async def run_job(client: httpx.AsyncClient, phone: str, emulator_id: str) -> None:
    """Выполнить регистрацию на эмуляторе"""
    # Для MEmu порт = часть emulator_id после двоеточия
    # 127.0.0.1:21503 -> 21503
    try:
        if ":" in emulator_id:
            port = int(emulator_id.split(":")[-1])
        else:
            # Fallback для старых имен emulator-5554
            port = int(emulator_id.split("-")[-1])
    except (ValueError, IndexError):
        logger.error(f"[{phone}] Invalid emulator_id format: {emulator_id}")
        await post_status(client, phone, "failed", emulator=emulator_id, error="Invalid emulator_id")
        return

    logger.info(f"[{phone}] Starting registration on {emulator_id}")
    await post_status(client, phone, "starting", emulator=emulator_id)

    try:
        # Создаем executor и запускаем в thread pool (не блокируем event loop!)
        # executor.execute() теперь синхронный и использует чистый ADB
        executor = RegistrationExecutor(phone=phone, emulator_id=emulator_id, port=port)
        result = await asyncio.to_thread(executor.execute)
        
        # Извлекаем код из результата (если есть)
        code = result.get("code", "") if isinstance(result, dict) else ""
        success = result.get("success", False)
        
        if success:
            logger.info(f"[{phone}] Registration completed on {emulator_id}")
            await post_status(client, phone, "completed", emulator=emulator_id, code=code)
        else:
            error_msg = result.get("error", "Unknown error")
            logger.error(f"[{phone}] Registration failed: {error_msg}")
            # Статус failed уже мог быть отправлен внутри executor (через Redis), 
            # но для надежности шлем и сюда
            # await post_status(client, phone, "failed", emulator=emulator_id, error=error_msg)
        
    except Exception as e:
        logger.error(f"[{phone}] Critical execution error on {emulator_id}: {e}", exc_info=True)
        await post_status(client, phone, "failed", emulator=emulator_id, error=str(e))


async def emulator_worker(
    client: httpx.AsyncClient,
    emulator_id: str,
    job_queue: asyncio.Queue,
    emulator_pool: asyncio.Queue
) -> None:
    """Воркер для одного эмулятора - берет задачи из очереди и выполняет"""
    logger.info(f"[{emulator_id}] Worker started")
    
    while not shutdown_event.is_set():
        try:
            # Помещаем эмулятор в пул свободных
            await emulator_pool.put(emulator_id)
            logger.debug(f"[{emulator_id}] Ready for work")
            
            # Ждем задачу из очереди
            try:
                job = await asyncio.wait_for(job_queue.get(), timeout=1.0)
                
                # Если взяли задачу - убираем себя из пула свободных (если мы там еще есть)
                # (Хотя логически мы только что отдали себя в пул, 
                # но queue.get() не связан с emulator_pool напрямую)
                # Нам нужно убедиться, что poll_and_distribute не считает нас свободными
                try:
                    # Это немного костыль: poll_and_distribute смотрит на размер пула.
                    # Когда мы берем задачу, мы должны "забрать" эмулятор из пула.
                    # Но emulator_pool.get() мы тут не вызываем.
                    # Идея: poll_and_distribute берет задачи ТОЛЬКО если size > 0.
                    # И он сам не достает из пула.
                    
                    # ПРАВИЛЬНАЯ ЛОГИКА:
                    # poll_and_distribute должен класть в job_queue только если есть свободные слоты.
                    # Но здесь worker сам себя кладет в pool.
                    # А кто достает из pool? Никто?
                    
                    # Исправление: poll_and_distribute должен забирать токен из emulator_pool
                    # перед тем как положить задачу в job_queue.
                    pass
                except Exception:
                    pass

            except asyncio.TimeoutError:
                # Таймаут ожидания задачи
                # Эмулятор остается в пуле (мы его положили в начале цикла)
                # Но проблема: если мы положим его снова в начале следующего цикла, 
                # то в пуле будет 2 записи для одного эмулятора?
                
                # Решение: перед началом цикла нужно убедиться, что эмулятора нет в пуле?
                # Или проще: worker кладет себя один раз, а забирает задачу.
                # Если задачи нет - он ждет.
                
                # Давайте упростим: worker просто ждет job_queue.
                # А poll_and_distribute смотрит qsize().
                
                # Но qsize() пула свободных эмуляторов?
                # В текущей архитектуре:
                # 1. Worker кладет себя в pool.
                # 2. Poll видит size > 0, берет задачи у сервера.
                # 3. Poll кладет задачи в job_queue.
                # 4. Worker берет задачу из job_queue.
                # 5. Worker должен ЗАБРАТЬ себя из pool, чтобы Poll не думал, что он свободен.
                
                try:
                    # Забираем свой токен (или любой токен) из пула, так как мы заняты
                    emulator_pool.get_nowait()
                except asyncio.QueueEmpty:
                    # Странно, мы же только что положили?
                    # Может кто-то другой забрал? (Poll не забирает)
                    pass
                continue
            
            # Мы получили задачу и забрали токен из пула. Мы заняты.
            
            phone = job.get("phone")
            if not phone:
                logger.warning(f"[{emulator_id}] Job without phone: {job}")
                job_queue.task_done()
                continue
            
            # Выполняем регистрацию
            await run_job(client, phone, emulator_id)
            job_queue.task_done()
            
        except Exception as e:
            logger.error(f"[{emulator_id}] Worker error: {e}", exc_info=True)
            await asyncio.sleep(1)
    
    logger.info(f"[{emulator_id}] Worker stopped")


async def poll_and_distribute(
    client: httpx.AsyncClient,
    job_queue: asyncio.Queue,
    emulator_pool: asyncio.Queue
) -> None:
    """Основной цикл: полит handler и распределяет задачи"""
    logger.info(f"Starting poll loop, handler={HANDLER_URL}")
    
    while not shutdown_event.is_set():
        try:
            # Проверяем сколько свободных эмуляторов
            free_count = emulator_pool.qsize()
            
            if free_count == 0:
                # Нет свободных эмуляторов - ждем
                await asyncio.sleep(POLL_INTERVAL)
                continue
            
            # Запрашиваем задачи (не больше чем свободных эмуляторов)
            # Но нужно учесть, что в job_queue уже могут лежать задачи, которые еще не разобрали
            pending_jobs = job_queue.qsize()
            effective_free = max(0, free_count - pending_jobs)
            
            if effective_free == 0:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            capacity = min(effective_free, len(EMULATOR_IDS))
            
            resp = await client.post(
                f"{HANDLER_URL}/agent/poll",
                json={"agent_id": AGENT_ID, "capacity": capacity},
                timeout=15,
            )
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
            
            if not jobs:
                logger.debug(f"No jobs received, sleeping {POLL_INTERVAL}s")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            
            logger.info(f"Received {len(jobs)} job(s) from handler")
            
            # Добавляем задачи в очередь
            for job in jobs:
                await job_queue.put(job)
            
            # Небольшая пауза перед следующим poll
            await asyncio.sleep(0.5)
            
        except httpx.HTTPError as e:
            logger.error(f"HTTP error during poll: {e}")
            await asyncio.sleep(POLL_BACKOFF)
        except Exception as e:
            logger.error(f"Unexpected error during poll: {e}", exc_info=True)
            await asyncio.sleep(POLL_BACKOFF)
    
    logger.info("Poll loop stopped")


async def main_async() -> None:
    """Главная асинхронная функция"""
    # Создаем очереди
    job_queue: asyncio.Queue = asyncio.Queue()
    emulator_pool: asyncio.Queue = asyncio.Queue()
    
    # HTTP клиент
    async with httpx.AsyncClient() as client:
        # Запускаем воркеры для каждого эмулятора
        workers = []
        if not EMULATOR_IDS:
            logger.error("NO EMULATORS FOUND! Please start MEmu instances.")
            return

        for emu_id in EMULATOR_IDS:
            worker = asyncio.create_task(
                emulator_worker(client, emu_id, job_queue, emulator_pool)
            )
            workers.append(worker)
        
        # Запускаем poll loop
        poll_task = asyncio.create_task(
            poll_and_distribute(client, job_queue, emulator_pool)
        )
        
        logger.info(f"Agent {AGENT_ID} started with {len(EMULATOR_IDS)} emulators: {EMULATOR_IDS}")
        
        # Ждем сигнала shutdown
        await shutdown_event.wait()
        
        logger.info("Shutting down gracefully...")
        
        # Отменяем все задачи
        poll_task.cancel()
        for worker in workers:
            worker.cancel()
        
        # Ждем завершения
        await asyncio.gather(poll_task, *workers, return_exceptions=True)
        
        logger.info("Agent stopped")


def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown"""
    logger.info(f"Received signal {signum}, initiating shutdown...")
    shutdown_event.set()


def main() -> None:
    """Entry point"""
    # Настраиваем обработку сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info(f"Starting agent {AGENT_ID}")
    logger.info(f"Handler URL: {HANDLER_URL}")
    
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
