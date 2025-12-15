import builtins
import os
import re
import subprocess
import threading
import time
import requests
import sys
import tempfile

# Сервисные импорты (оставляем как было)
from service.redis_client import RedisClient
from service.emulator_pool import EmulatorPool
from service.models import RegistrationStatus

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
# Путь к ADB (для Windows с MEMU)
# Пытаемся найти автоматически или берем из ENV
ADB_PATH = os.getenv("ADB_PATH") or r"C:\Program Files\Microvirt\MEmu\adb.exe"

# ==========================================
# ВСПОМОГАТЕЛЬНЫЙ КЛАСС (ADB CONTROLLER)
# ==========================================
class ADBController:
    """
    Легковесная замена Appium для управления Android через ADB.
    """
    def __init__(self, device_name, adb_path):
        self.device_name = device_name
        self.adb = adb_path

    def run_shell(self, cmd, timeout=10):
        """Выполнить shell команду"""
        full_cmd = [self.adb, "-s", self.device_name, "shell"] + cmd.split()
        try:
            return subprocess.run(full_cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=timeout)
        except subprocess.TimeoutExpired:
            # Не принтим тут, чтобы не засорять логи сервиса, если не критично
            return None

    def tap(self, x, y):
        """Клик по координатам"""
        self.run_shell(f"input tap {x} {y}")

    def text(self, text):
        """Ввод текста"""
        escaped_text = text.replace(" ", "%s").replace("'", r"\'").replace('"', r'\"')
        self.run_shell(f"input text {escaped_text}")

    def keyevent(self, keycode):
        """Нажатие кнопки (66=ENTER, 67=BACKSPACE, 3=HOME, 4=BACK)"""
        self.run_shell(f"input keyevent {keycode}")

    def get_ui_dump(self):
        """Получить XML текущего экрана через uiautomator"""
        remote_dump = "/data/local/tmp/window_dump.xml"
        
        # 1. Создаем дамп
        for _ in range(2):
            res = self.run_shell(f"uiautomator dump {remote_dump}", timeout=15)
            if res and "UI hierchary dumped to" in res.stdout:
                break
            time.sleep(0.5)

        # 2. Читаем файл
        res = self.run_shell(f"cat {remote_dump}", timeout=5)
        if res and res.stdout:
            return res.stdout
        return ""

    def find_element(self, text=None, resource_id=None, class_name=None, index=0):
        """Ищет элемент в XML дампе. Возвращает {x, y, raw} или None"""
        xml = self.get_ui_dump()
        if not xml:
            return None

        # Простой парсинг регулярками
        nodes = re.findall(r'<node [^>]*>', xml)
        
        matches = []
        for node in nodes:
            if text and text.lower() not in node.lower():
                continue
            if resource_id and resource_id not in node:
                continue
            if class_name and class_name not in node:
                continue
            
            # Достаем координаты
            bounds_match = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if bounds_match:
                x1, y1, x2, y2 = map(int, bounds_match.groups())
                matches.append({
                    'x': (x1 + x2) // 2,
                    'y': (y1 + y2) // 2,
                    'raw': node
                })

        if len(matches) > index:
            return matches[index]
        return None

# ==========================================
# ГЛАВНЫЙ КЛАСС ИСПОЛНИТЕЛЯ
# ==========================================

class RegistrationExecutor:
    """
    Выполнение регистрации WhatsApp (версия Pure ADB / MEmu).
    """
    
    def __init__(self, phone: str, emulator_id: str, port: int, proxy: str = None):
        self.phone = phone
        self.emulator_id = emulator_id
        self.port = port
        self.proxy = proxy
        # Для MEmu порт ADB обычно вычисляется или передается. 
        # Если port пришел правильный (например 21503), используем его.
        # Если пришел 5554 (стандарт), пробуем адаптироваться, но лучше доверять входным данным.
        self.device_name = f"127.0.0.1:{port}" if ":" not in str(port) else str(port)
        
        self.redis_client = RedisClient()
        self.emulator_pool = EmulatorPool()
        
        # Инициализируем наш контроллер
        self.adb = ADBController(self.device_name, ADB_PATH)

    # --- Вспомогательные методы (перенесены из main.py) ---

    def _setup_proxydroid(self):
        """Настройка ProxyDroid (с генерацией конфига)"""
        print("🌍 Настраиваю ProxyDroid...")
        
        # Генерируем конфиг XML во временный файл, чтобы не зависеть от файловой системы
        config_content = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <boolean name="isConnecting" value="false" />
    <string name="host">na.proxy.piaproxy.com</string>
    <string name="port">5000</string>
    <string name="user">user-mtt33_A0xiF-region-ru</string>
    <string name="proxyType">socks5</string>
    <boolean name="isAuth" value="true" />
    <string name="password">nskjfdbnker4G</string>
    <boolean name="isAutoConnect" value="true" />
    <boolean name="isProfile" value="true" />
    <string name="proxyApps">com.whatsapp</string>
    <string name="bypassAddrs">127.0.0.1,localhost,::1,10.0.2.2</string>
</map>"""
        
        try:
            # Создаем временный файл
            with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as tmp:
                tmp.write(config_content)
                tmp_path = tmp.name

            # Останавливаем приложение
            self.adb.run_shell("am force-stop org.proxydroid")
            
            # Заливаем конфиг
            remote_path = "/data/data/org.proxydroid/shared_prefs/org.proxydroid_preferences.xml"
            subprocess.run([ADB_PATH, "-s", self.device_name, "push", tmp_path, remote_path], capture_output=True)
            self.adb.run_shell(f"chmod 777 {remote_path}")
            
            # Удаляем временный файл
            os.unlink(tmp_path)

            # Запускаем GUI (триггер прав) и сервис
            self.adb.run_shell("am start -n org.proxydroid/.MainActivity")
            time.sleep(3)
            self.adb.run_shell("am startservice -n org.proxydroid/.ProxyDroidService")
            self.adb.run_shell("am broadcast -a org.proxydroid.intent.action.START")
            time.sleep(2)

            # Обработка диалогов (Хорошо -> Grant)
            print("🕵️ Проверяю диалоги прав ProxyDroid...")
            if self._click_element(text="Хорошо", timeout=5) or self._click_element(text="OK", timeout=1):
                time.sleep(1)
            
            for txt in ["Grant", "Allow", "Разрешить", "Предоставить"]:
                if self._click_element(text=txt, timeout=2):
                    break
            
            print("✓ ProxyDroid настроен")
        except Exception as e:
            print(f"⚠️ Ошибка настройки ProxyDroid: {e}")

    def _redirect_calls_to_sip(self):
        """Перенаправить входящие звонки на SIP через MTT API"""
        print(f"📞 Настраиваю перенаправление звонков для {self.phone}...")
        
        MTT_USERNAME = "ip_ivanchin"
        MTT_PASSWORD = "s13jgSxHpQ"
        ASTERISK_SIP_ID = "883140005582687"
        
        mtt_phone = self.phone.lstrip('+')
        
        data = {
            "id": "1",
            "jsonrpc": "2.0",
            "method": "SetReserveStruct",
            "params": {
                "sip_id": mtt_phone,
                "redirect_type": 1,
                "masking": "N",
                "controlCallStruct": [
                    {
                        "I_FOLLOW_ORDER": 1,
                        "PERIOD": "Always",
                        "PERIOD_DESCRIPTION": "Always",
                        "TIMEOUT": 40,
                        "ACTIVE": "Y",
                        "NAME": ASTERISK_SIP_ID,
                        "REDIRECT_NUMBER": ASTERISK_SIP_ID,
                    }
                ],
            },
        }
        
        try:
            response = requests.post(
                "https://api.mtt.ru/ipcr/",
                json=data,
                auth=(MTT_USERNAME, MTT_PASSWORD),
                timeout=10
            )
            response.raise_for_status()
            print(f"✓ Звонки перенаправлены на {ASTERISK_SIP_ID}")
        except Exception as e:
            print(f"✗ Ошибка MTT API: {e}")

    def _wait_for_voice_call_code(self, timeout=120):
        """Ожидание звонка через API"""
        print(f"⏳ Жду звонок на {self.phone} ({timeout} сек)...")
        phone = self.phone.lstrip('+')
        try:
            response = requests.post(
                "http://92.51.23.204:8000/api/wait-call",
                json={"phone_number": phone, "timeout": timeout},
                timeout=timeout + 10
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"✗ Ошибка wait-call API: {e}")
            return None

    def _click_element(self, text=None, resource_id=None, timeout=10):
        """Обертка над adb.click_element с поддержкой проверки остановки сервиса"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            # Важно: Проверка флага остановки
            if self.redis_client.is_stop_requested(self.phone):
                raise Exception("Registration cancelled by user")
            
            # Важно: Heartbeat
            if int(time.time()) % 5 == 0:
                self.emulator_pool.update_heartbeat(self.emulator_id)

            el = self.adb.find_element(text=text, resource_id=resource_id)
            if el:
                print(f"✓ Клик по '{text or resource_id}' ({el['x']}, {el['y']})")
                self.adb.tap(el['x'], el['y'])
                return True
            time.sleep(1)
        return False

    def _wait_for_element(self, text=None, resource_id=None, class_name=None, timeout=20):
        """Обертка над adb.wait_for_element"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.redis_client.is_stop_requested(self.phone):
                raise Exception("Registration cancelled by user")
            
            el = self.adb.find_element(text=text, resource_id=resource_id, class_name=class_name)
            if el:
                return True
            time.sleep(1)
        return False

    def execute(self) -> dict:
        """
        ОСНОВНОЙ МЕТОД ВЫПОЛНЕНИЯ
        """
        # Переопределяем print для логирования с ID эмулятора
        log_prefix = f"[{self.emulator_id}]"
        base_print = builtins.print
        def print(*args, **kwargs):
            if args:
                args = (f"{log_prefix} {args[0]}",) + args[1:]
            return base_print(*args, **kwargs)

        try:
            # 1. ОБНОВЛЕНИЕ СТАТУСА: STARTING
            self.redis_client.update_registration_status(
                self.phone, RegistrationStatus.STARTING, emulator=self.emulator_id
            )
            self.emulator_pool.update_heartbeat(self.emulator_id)
            
            print(f"🚀 НАЧАЛО РЕГИСТРАЦИИ: {self.phone} на {self.device_name}")

            # 2. ОЧИСТКА И ПОДГОТОВКА
            print("🧹 Очистка данных...")
            self.adb.run_shell("pm clear com.whatsapp")
            
            # # 3. НАСТРОЙКА ПРОКСИ
            # self._setup_proxydroid()
            
            # 4. НАСТРОЙКА ПЕРЕАДРЕСАЦИИ (MTT)
            self._redirect_calls_to_sip()
            
            # 5. ЗАПУСК WHATSAPP
            print("📱 Запускаю WhatsApp...")
            self.adb.run_shell("am start -n com.whatsapp/.Main")
            time.sleep(3)
            
            # 6. КНОПКА СОГЛАСИЯ
            print("⏳ Жду кнопку согласия...")
            if not self._click_element(resource_id="com.whatsapp:id/eula_accept", timeout=10):
                # Фолбэк по тексту
                if not self._click_element(text="AGREE", timeout=2):
                     print("⚠️ Кнопка согласия не найдена! Пробую тапнуть в низ экрана.")
                     self.adb.tap(360, 1150)

            # 7. ВВОД НОМЕРА
            print("⏳ Ввожу номер...")
            if not self._wait_for_element(class_name="android.widget.EditText", timeout=15):
                raise Exception("Поля ввода номера не найдены")
            
            # Логика ввода (как в main.py)
            cc_field = self.adb.find_element(class_name="android.widget.EditText", index=0)
            phone_field = self.adb.find_element(class_name="android.widget.EditText", index=1)
            
            if cc_field and phone_field:
                # Код страны
                self.adb.tap(cc_field['x'], cc_field['y'])
                time.sleep(0.5)
                for _ in range(5): self.adb.keyevent(67) # Backspace
                self.adb.text("7")
                
                # Телефон
                self.adb.tap(phone_field['x'], phone_field['y'])
                time.sleep(0.5)
                phone_clean = self.phone.replace("+7", "").replace("7", "", 1) if self.phone.startswith("7") or self.phone.startswith("+7") else self.phone
                self.adb.text(phone_clean)
                time.sleep(1)
            else:
                raise Exception("Не удалось определить координаты полей ввода")

            # 8. NEXT -> OK
            print("⏳ Жму 'Next'...")
            if not self._click_element(text="Далее", timeout=5):
                if not self._click_element(text="Next", timeout=2):
                    self._click_element(resource_id="com.whatsapp:id/registration_submit", timeout=2)
            
            print("⏳ Жду 'Connecting' и подтверждение...")
            confirmed = False
            for _ in range(20):
                # Проверка Stop
                if self.redis_client.is_stop_requested(self.phone): raise Exception("Cancelled")
                
                if self._click_element(text="Yes", timeout=1) or \
                   self._click_element(text="Да", timeout=0.5) or \
                   self._click_element(text="OK", timeout=0.5) or \
                   self._click_element(resource_id="android:id/button1", timeout=0.5):
                    confirmed = True
                    print("✓ Подтвердил номер")
                    break
                time.sleep(1)
            
            if not confirmed:
                print("⚠️ Диалог подтверждения мог быть пропущен")

            # 9. VERIFY ANOTHER WAY -> CALL ME
            print("⏳ Ищу 'Verify another way'...")
            time.sleep(2)
            self._click_element(text="Not now", timeout=1)
            self._click_element(text="Не сейчас", timeout=0.5)

            if self._click_element(text="Подтвердить другим способом", timeout=10) or \
               self._click_element(text="Verify another way", timeout=2) or \
               self._click_element(text="другим способом", timeout=1):
                
                print("✓ Выбрал другой способ")
                time.sleep(1)
                
                print("⏳ Выбираем 'Call Me'...")
                if self._click_element(text="Аудиозвонок", timeout=5) or \
                   self._click_element(text="Позвонить", timeout=1) or \
                   self._click_element(text="Call me", timeout=1):
                    print("✓ Запрошен звонок")
                    time.sleep(1)
                    # Кнопка "Продолжить" после выбора радио-кнопки
                    if self._click_element(text="Continue", timeout=2) or \
                       self._click_element(text="Продолжить", timeout=1) or \
                       self._click_element(resource_id="com.whatsapp:id/continue_button", timeout=1):
                        print("✓ Нажата кнопка 'Продолжить'")
                else:
                    print("⚠️ Опция звонка не найдена")
            else:
                print("⚠️ Кнопка 'Verify another way' не найдена")

            # 10. ОЖИДАНИЕ КОДА (ЗВОНОК)
            # ОБНОВЛЕНИЕ СТАТУСА: READY_FOR_CODE
            self.redis_client.update_registration_status(
                self.phone, RegistrationStatus.READY_FOR_CODE, emulator=self.emulator_id
            )
            
            print("📞 Ожидание звонка и ввод кода...")
            call_result = self._wait_for_voice_call_code(timeout=120)
            
            if call_result and call_result.get('status') == 'success':
                code = str(call_result.get('code'))
                print(f"✅ Код получен: {code}")
                self.adb.text(code)
                print("⌨️ Код введен")
            else:
                raise Exception("Звонок не прошел или код не получен")

            # 11. ВВОД ИМЕНИ И ФИНАЛИЗАЦИЯ
            print("⏳ Жду экран ввода имени...")
            if self._wait_for_element(resource_id="com.whatsapp:id/registration_name", timeout=40) or \
               self._wait_for_element(text="Type your name here", timeout=1) or \
               self._wait_for_element(text="Введите ваше имя", timeout=1):
                
                print("✓ Экран ввода имени найден")
                time.sleep(1)
                self._click_element(resource_id="com.whatsapp:id/registration_name", timeout=2)
                self.adb.text("Alex")
                self.adb.keyevent(66) # Enter
                time.sleep(1)
                
                print("⏳ Жму 'Далее'...")
                if self._click_element(text="Next", timeout=5) or \
                   self._click_element(text="Далее", timeout=1) or \
                   self._click_element(resource_id="com.whatsapp:id/register_name_accept", timeout=1):
                    print("✓ Нажато 'Далее'")
                    
                    # 12. ФИНАЛЬНЫЙ БОСС (ПРОПУСК ОКОН)
                    print("⏳ Пропуск лишних окон (Email, Passkey)...")
                    success_reg = False
                    for _ in range(60): # 60 * 1.5s = 90 sec
                        # Check stop
                        if self.redis_client.is_stop_requested(self.phone): raise Exception("Cancelled")
                        self.emulator_pool.update_heartbeat(self.emulator_id)

                        # Check Success
                        if self.adb.find_element(text="Чаты") or \
                           self.adb.find_element(text="Chats") or \
                           self.adb.find_element(text="Звонки") or \
                           self.adb.find_element(text="Calls"):
                            print("🎉 ГЛАВНЫЙ ЭКРАН НАЙДЕН!")
                            success_reg = True
                            break
                        
                        # Click Skips
                        for skip_txt in ["Пропустить", "Skip", "Не сейчас", "Not now", "Отмена", "Cancel"]:
                            if self._click_element(text=skip_txt, timeout=0.5):
                                print(f"✓ Пропущено ({skip_txt})")
                                time.sleep(1)
                                break # Break inner loop to re-check success
                        
                        time.sleep(1)
                    
                    if not success_reg:
                        raise Exception("Не удалось попасть на главный экран за 90 сек")
                    
                    # 13. ПОИСК КОДА ТЕЛЕГРАМА (ВНУТРИ ЧАТОВ)
                    print("📩 Жду сообщение с кодом Телеграма (120 сек)...")
                    
                    # ОБНОВЛЕНИЕ СТАТУСА: COMPLETED
                    self.redis_client.update_registration_status(
                        self.phone, RegistrationStatus.COMPLETED, emulator=self.emulator_id
                    )
                    
                    tg_code = None
                    start_wait = time.time()
                    while time.time() - start_wait < 120:
                        if self.redis_client.is_stop_requested(self.phone): break
                        
                        xml = self.adb.get_ui_dump()
                        if xml:
                            # Ищем 5 цифр
                            match = re.search(r'(?:code|код|login)[:\s-]*(\d{5})', xml, re.IGNORECASE)
                            if match:
                                tg_code = match.group(1)
                                print(f"🚀🚀🚀 НАЙДЕН КОД ТЕЛЕГРАМА: {tg_code}")
                                # Можно сохранить его куда-то, если нужно
                                break
                        time.sleep(2)
                    
                    if not tg_code:
                        print("⚠️ Код Телеграма не найден")

                else:
                    raise Exception("Не удалось нажать Далее после ввода имени")
            else:
                raise Exception("Экран ввода имени не появился")

            return {
                "success": True,
                "phone": self.phone,
                "emulator": self.emulator_id
            }

        except Exception as e:
            error_msg = str(e)
            print(f"❌ ОШИБКА: {error_msg}")
            
            # Обработка статусов ошибки
            status = RegistrationStatus.FAILED
            if "Cancelled" in error_msg:
                error_msg = "Cancelled by user"
            elif "blocked" in error_msg:
                error_msg = "WhatsApp blocked login"
            
            self.redis_client.update_registration_status(
                self.phone, status, emulator=self.emulator_id, error=error_msg
            )
            
            return {
                "success": False,
                "phone": self.phone,
                "emulator": self.emulator_id,
                "error": error_msg
            }
