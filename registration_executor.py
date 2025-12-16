import builtins
import os
import re
import subprocess
import tempfile
import time
import requests
import sys

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
ADB_PATH = os.getenv("ADB_PATH") or r"C:\Program Files\Microvirt\MEmu\adb.exe"
HANDLER_URL = os.getenv("HANDLER_URL", "http://5.129.204.230:8000")

# ==========================================
# ADB CONTROLLER
# ==========================================
class ADBController:
    def __init__(self, device_name, adb_path):
        self.device_name = device_name
        self.adb = adb_path

    def run_shell(self, cmd, timeout=10):
        full_cmd = [self.adb, "-s", self.device_name, "shell"] + cmd.split()
        try:
            return subprocess.run(full_cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def tap(self, x, y):
        self.run_shell(f"input tap {x} {y}")

    def text(self, text):
        escaped_text = text.replace(" ", "%s").replace("'", r"\'").replace('"', r'\"')
        self.run_shell(f"input text {escaped_text}")

    def keyevent(self, keycode):
        self.run_shell(f"input keyevent {keycode}")

    def get_ui_dump(self):
        remote_dump = "/data/local/tmp/window_dump.xml"
        for _ in range(2):
            res = self.run_shell(f"uiautomator dump {remote_dump}", timeout=15)
            if res and "UI hierchary dumped to" in res.stdout:
                break
            time.sleep(0.5)
        res = self.run_shell(f"cat {remote_dump}", timeout=5)
        if res and res.stdout:
            return res.stdout
        return ""

    def find_element(self, text=None, resource_id=None, class_name=None, index=0):
        xml = self.get_ui_dump()
        if not xml: return None
        nodes = re.findall(r'<node [^>]*>', xml)
        matches = []
        for node in nodes:
            if text and text.lower() not in node.lower(): continue
            if resource_id and resource_id not in node: continue
            if class_name and class_name not in node: continue
            bounds_match = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if bounds_match:
                x1, y1, x2, y2 = map(int, bounds_match.groups())
                matches.append({'x': (x1 + x2) // 2, 'y': (y1 + y2) // 2, 'raw': node})
        if len(matches) > index: return matches[index]
        return None

# ==========================================
# REGISTRATION EXECUTOR
# ==========================================
class RegistrationExecutor:
    def __init__(self, phone: str, emulator_id: str, port: int, proxy: str = None):
        self.phone = phone
        self.emulator_id = emulator_id
        self.port = port
        self.proxy = proxy
        self.device_name = f"127.0.0.1:{port}" if ":" not in str(port) else str(port)
        self.adb = ADBController(self.device_name, ADB_PATH)

    def _send_status(self, status, code="", error=""):
        """Отправка статуса через HTTP API"""
        try:
            payload = {
                "phone": self.phone,
                "status": status,
                "emulator": self.emulator_id
            }
            if code: payload["code"] = code
            if error: payload["error"] = error
            
            requests.post(f"{HANDLER_URL}/agent/status", json=payload, timeout=10)
        except Exception as e:
            print(f"⚠️ Failed to send status {status}: {e}")

    def _get_status(self):
        """Получение статуса через HTTP API"""
        try:
            response = requests.get(f"{HANDLER_URL}/api/status/{self.phone}", timeout=10)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            print(f"⚠️ Failed to get status: {e}")
        return {}

    # --- Вспомогательные методы ---
    def _setup_proxydroid(self):
        print("🌍 Настраиваю ProxyDroid...")
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
            with tempfile.NamedTemporaryFile(mode='w', delete=False, encoding='utf-8') as tmp:
                tmp.write(config_content)
                tmp_path = tmp.name
            self.adb.run_shell("am force-stop org.proxydroid")
            remote_path = "/data/data/org.proxydroid/shared_prefs/org.proxydroid_preferences.xml"
            subprocess.run([ADB_PATH, "-s", self.device_name, "push", tmp_path, remote_path], capture_output=True)
            self.adb.run_shell(f"chmod 777 {remote_path}")
            os.unlink(tmp_path)
            self.adb.run_shell("am start -n org.proxydroid/.MainActivity")
            time.sleep(3)
            self.adb.run_shell("am startservice -n org.proxydroid/.ProxyDroidService")
            self.adb.run_shell("am broadcast -a org.proxydroid.intent.action.START")
            time.sleep(2)
            if self._click_element(text="Хорошо", timeout=5) or self._click_element(text="OK", timeout=1): time.sleep(1)
            for txt in ["Grant", "Allow", "Разрешить", "Предоставить"]:
                if self._click_element(text=txt, timeout=2): break
            print("✓ ProxyDroid настроен")
        except Exception as e:
            print(f"⚠️ Ошибка настройки ProxyDroid: {e}")

    def _redirect_calls_to_sip(self):
        print(f"📞 Настраиваю перенаправление звонков для {self.phone}...")
        try:
            mtt_phone = self.phone.lstrip('+')
            data = {
                "id": "1", "jsonrpc": "2.0", "method": "SetReserveStruct",
                "params": {
                    "sip_id": mtt_phone, "redirect_type": 1, "masking": "N",
                    "controlCallStruct": [{"I_FOLLOW_ORDER": 1, "PERIOD": "Always", "TIMEOUT": 40, "ACTIVE": "Y", "NAME": "883140005582687", "REDIRECT_NUMBER": "883140005582687"}]
                }
            }
            requests.post("https://api.mtt.ru/ipcr/", json=data, auth=("ip_ivanchin", "s13jgSxHpQ"), timeout=10)
            print(f"✓ Звонки перенаправлены")
        except Exception as e:
            print(f"✗ Ошибка MTT API: {e}")

    def _wait_for_voice_call_code(self, timeout=120):
        print(f"⏳ Жду звонок на {self.phone} ({timeout} сек)...")
        phone = self.phone.lstrip('+')
        try:
            response = requests.post("http://92.51.23.204:8000/api/wait-call", json={"phone_number": phone, "timeout": timeout}, timeout=timeout + 10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"✗ Ошибка wait-call API: {e}")
            return None

    def _click_element(self, text=None, resource_id=None, timeout=10):
        start_time = time.time()
        while time.time() - start_time < timeout:
            el = self.adb.find_element(text=text, resource_id=resource_id)
            if el:
                print(f"✓ Клик по '{text or resource_id}'")
                self.adb.tap(el['x'], el['y'])
                return True
            time.sleep(1)
        return False

    def _wait_for_element(self, text=None, resource_id=None, class_name=None, timeout=20):
        start_time = time.time()
        while time.time() - start_time < timeout:
            el = self.adb.find_element(text=text, resource_id=resource_id, class_name=class_name)
            if el: return True
            time.sleep(1)
        return False

    def execute(self) -> dict:
        log_prefix = f"[{self.emulator_id}]"
        base_print = builtins.print
        def print(*args, **kwargs):
            if args: args = (f"{log_prefix} {args[0]}",) + args[1:]
            return base_print(*args, **kwargs)

        try:
            # self._send_status("starting") # Main.py уже отправляет этот статус
            print(f"🚀 НАЧАЛО РЕГИСТРАЦИИ: {self.phone} на {self.device_name}")

            print("🧹 Очистка данных...")
            self.adb.run_shell("pm clear com.whatsapp")
            
            # self._setup_proxydroid()
            self._redirect_calls_to_sip()
            
            print("📱 Запускаю WhatsApp...")
            self.adb.run_shell("am start -n com.whatsapp/.Main")
            time.sleep(3)
            
            print("⏳ Жду кнопку согласия...")
            if not self._click_element(resource_id="com.whatsapp:id/eula_accept", timeout=10):
                if not self._click_element(text="AGREE", timeout=2):
                     self.adb.tap(360, 1150)

            print("⏳ Ввожу номер...")
            if not self._wait_for_element(class_name="android.widget.EditText", timeout=15):
                raise Exception("Поля ввода номера не найдены")
            
            cc_field = self.adb.find_element(class_name="android.widget.EditText", index=0)
            phone_field = self.adb.find_element(class_name="android.widget.EditText", index=1)
            
            if cc_field and phone_field:
                self.adb.tap(cc_field['x'], cc_field['y'])
                time.sleep(0.5)
                for _ in range(5): self.adb.keyevent(67)
                self.adb.text("7")
                self.adb.tap(phone_field['x'], phone_field['y'])
                time.sleep(0.5)
                phone_clean = self.phone.replace("+7", "").replace("7", "", 1) if self.phone.startswith("7") or self.phone.startswith("+7") else self.phone
                self.adb.text(phone_clean)
                time.sleep(1)
            else:
                raise Exception("Не удалось определить координаты полей ввода")

            print("⏳ Жму 'Next'...")
            if not self._click_element(text="Далее", timeout=5):
                if not self._click_element(text="Next", timeout=2):
                    self._click_element(resource_id="com.whatsapp:id/registration_submit", timeout=2)
            
            print("⏳ Жду 'Connecting' и подтверждение...")
            confirmed = False
            for _ in range(20):
                if self._click_element(text="Yes", timeout=1) or \
                   self._click_element(text="Да", timeout=0.5) or \
                   self._click_element(text="OK", timeout=0.5) or \
                   self._click_element(resource_id="android:id/button1", timeout=0.5):
                    confirmed = True
                    break
                time.sleep(1)
            
            if not confirmed: print("⚠️ Диалог подтверждения мог быть пропущен")

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
                    if self._click_element(text="Continue", timeout=2) or \
                       self._click_element(text="Продолжить", timeout=1) or \
                       self._click_element(resource_id="com.whatsapp:id/continue_button", timeout=1):
                        print("✓ Нажата кнопка 'Продолжить'")
                else:
                    print("⚠️ Опция звонка не найдена")
            else:
                print("⚠️ Кнопка 'Verify another way' не найдена")

            
            print("📞 Ожидание звонка и ввод кода...")
            call_result = self._wait_for_voice_call_code(timeout=120)
            
            if call_result and call_result.get('status') == 'success':
                code = str(call_result.get('code'))
                print(f"✅ Код получен: {code}")
                self.adb.text(code)
                print("⌨️ Код введен")
            else:
                raise Exception("Звонок не прошел или код не получен")

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
                    
                    print("⏳ Пропуск лишних окон...")
                    success_reg = False
                    for _ in range(60):
                        if self.adb.find_element(text="Чаты") or \
                           self.adb.find_element(text="Chats") or \
                           self.adb.find_element(text="Звонки") or \
                           self.adb.find_element(text="Calls"):
                            print("🎉 ГЛАВНЫЙ ЭКРАН НАЙДЕН!")
                            success_reg = True
                            break
                        
                        for skip_txt in ["Пропустить", "Skip", "Не сейчас", "Not now", "Отмена", "Cancel"]:
                            if self._click_element(text=skip_txt, timeout=0.5):
                                time.sleep(1)
                                break 
                        time.sleep(1)
                    
                    if not success_reg: raise Exception("Не удалось попасть на главный экран за 90 сек")

                    self._send_status("ready_for_code")
                    
                    print("📩 Жду сообщение с кодом Телеграма (120 сек)...")
                    
                    tg_code = None
                    last_code = None
                    start_wait = time.time()
                    
                    # Этап 1: Поиск первого кода
                    while time.time() - start_wait < 120:
                        xml = self.adb.get_ui_dump()
                        if xml:
                            match = re.search(r'(?:code|код|login)[:\s-]*(\d{5})', xml, re.IGNORECASE)
                            if match:
                                tg_code = match.group(1)
                                last_code = tg_code
                                print(f"🚀🚀🚀 НАЙДЕН КОД ТЕЛЕГРАМА: {tg_code}")
                                self._send_status("completed", code=tg_code)
                                break
                        time.sleep(2)
                    
                    if not tg_code: 
                        print("⚠️ Код Телеграма не найден за 120 сек")
                    else:
                        # Этап 2: Ожидание второго кода или стоп-сигнала
                        print("🔄 Перехожу в режим ожидания второго кода или стоп-сигнала (240 сек)...")
                        monitor_start = time.time()
                        monitor_timeout = 240
                        
                        while time.time() - monitor_start < monitor_timeout:
                            # 1. Проверка стоп-сигнала
                            status_data = self._get_status()
                            if status_data.get("stop_requested"):
                                print("🛑 Получен сигнал остановки. Завершаю работу.")
                                break
                                
                            if status_data.get("second_code_requested"):
                                print("ℹ️ API запрашивает поиск второго кода...")
                            
                            # 2. Поиск нового кода
                            xml = self.adb.get_ui_dump()
                            if xml:
                                match = re.search(r'(?:code|код|login)[:\s-]*(\d{5})', xml, re.IGNORECASE)
                                if match:
                                    current_code = match.group(1)
                                    if current_code != last_code:
                                        print(f"🚀🚀🚀 НАЙДЕН НОВЫЙ КОД ТЕЛЕГРАМА: {current_code}")
                                        self._send_status("completed", code=current_code)
                                        last_code = current_code
                                        # Сбрасываем таймер, чтобы дать время на обработку
                                        monitor_start = time.time() 
                            
                            time.sleep(2)
                    
                    return {"success": True, "phone": self.phone, "emulator": self.emulator_id, "code": last_code}

                else:
                    raise Exception("Не удалось нажать Далее")
            else:
                raise Exception("Экран ввода имени не появился")

        except Exception as e:
            error_msg = str(e)
            print(f"❌ ОШИБКА: {error_msg}")
            return {"success": False, "phone": self.phone, "emulator": self.emulator_id, "error": error_msg}
