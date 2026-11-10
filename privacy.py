"""Best-effort minimization. Not a guarantee to detect every personal detail."""
import re

# Номер телефону (виключає збіги, які можуть бути довгими номерами накладних ТТН)
PHONE = re.compile(r'(?<!\d)(?:\+?38)?[\s().-]*0[\s().-]*\d(?:[\s().-]*\d){8}(?!\d)')

# Email
EMAIL = re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-яІіЇїЄє]{2,}\b')

# Платіжні картки (16 цифр, розділених пробілами або дефісами)
CARD = re.compile(r'(?<!\d)(?:\d[ -]?){15,16}(?!\d)')

# Відділення пошти (НП, Укрпошта, Міст)
POST_OFFICE = re.compile(r'\b(?:відділення|нп|нова пошта|укрпошта|склад)\s*[№#]?\s*\d{1,4}\b', re.I)

# Вулиці та адреси (захоплюємо лише 4-5 слів після ключового слова, щоб не з'їсти весь текст)
ADDRESS = re.compile(r'\b(?:вул\.?|вулиця|просп\.?|проспект|пров\.?|провулок|адреса)\s*[:.]?\s+(?:[А-ЯІЇЄҐа-яіїєґa-zA-Z0-9-]+\s*[,.]?\s*){1,4}(?:буд\.?|б\.?|д\.?)?\s*\d+[a-zA-Zа-яіїєґ]?', re.I)

def redact(text):
    """
    Маскує чутливі дані (телефон, email, картки, адреси),
    залишаючи корисний контекст для AI (наприклад, номери замовлень).
    """
    safe_text = str(text)
    
    # 1. Приховуємо номери карток
    safe_text = CARD.sub('[картка прихована]', safe_text)
    
    # 2. Приховуємо номери телефонів (але обходимо 11-14 значні номери ТТН Нової Пошти)
    # Зберігаємо ТТН (2045... 5900... 100...) для контексту, маскуючи лише телефони
    def phone_replacer(match):
        val = match.group(0).replace(' ', '').replace('-', '')
        if len(val) >= 11 and (val.startswith('204') or val.startswith('590') or val.startswith('100')):
            return match.group(0) # Це ТТН, залишаємо
        return '[телефон приховано]'
    
    safe_text = PHONE.sub(phone_replacer, safe_text)
    
    # 3. Приховуємо Email
    safe_text = EMAIL.sub('[email приховано]', safe_text)
    
    # 4. Приховуємо адреси та відділення пошти
    safe_text = POST_OFFICE.sub('[відділення приховано]', safe_text)
    safe_text = ADDRESS.sub('[адресу приховано]', safe_text)
    
    return safe_text

def normalize_phone(value):
    """
    Валідація та нормалізація українського номера телефону для бази даних CRM.
    Формат на виході: +380XXXXXXXXX
    """
    if re.search(r'[^\d\s+().-]', str(value)):
        raise ValueError('Вкажіть український номер телефону')
        
    digits = re.sub(r'\D', '', str(value))
    
    # Якщо клієнт ввів просто 9 цифр без нуля (напр. 671234567)
    if len(digits) == 9:
        digits = '0' + digits
        
    # Якщо номер починається з 380
    if len(digits) == 12 and digits.startswith('380'):
        digits = digits[2:]
        
    # Після всіх очисток номер має містити 10 цифр і починатися з 0
    if len(digits) != 10 or not digits.startswith('0'):
        raise ValueError('Вкажіть український номер: +380… або 0…')
        
    return '+38' + digits
