"""Best-effort minimization. Not a guarantee to detect every personal detail."""
import re

# Номер телефону (строгі межі, щоб не чіпляти 14-значні ТТН)
PHONE = re.compile(r'(?<!\d)(?:\+?38)?[\s().-]*0[\s().-]*\d(?:[\s().-]*\d){8}(?!\d)')

# Email
EMAIL = re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-яІіЇїЄє]{2,}\b')

# Платіжні картки (16 цифр) та рахунки IBAN (UA + 27 цифр)
CARD = re.compile(r'(?<!\d)(?:\d[ -]?){15,16}(?!\d)')
IBAN = re.compile(r'\bUA\d{27}\b', re.I)

# Відділення пошти (НП, Укрпошта, Міст)
POST_OFFICE = re.compile(r'\b(?:відділення|нп|нова пошта|укрпошта|склад|поштомат)\s*[№#]?\s*\d{1,5}\b', re.I)

# Вулиці та адреси (захоплюємо лише 3-5 слів після ключового слова, щоб не видалити питання клієнта)
ADDRESS = re.compile(r'\b(?:вул\.?|вулиця|просп\.?|проспект|пров\.?|провулок|адреса)\s*[:.]?\s+(?:[А-ЯІЇЄҐа-яіїєґa-zA-Z0-9-]+\s*[,.]?\s*){1,4}(?:буд\.?|б\.?|д\.?)?\s*\d+[a-zA-Zа-яіїєґ]?', re.I)


def redact(text):
    """
    Маскує чутливі дані (телефон, email, картки, адреси),
    залишаючи корисний контекст для AI (наприклад, номери замовлень та ТТН).
    """
    safe_text = str(text)
    
    safe_text = CARD.sub('[картка прихована]', safe_text)
    safe_text = IBAN.sub('[IBAN приховано]', safe_text)
    
    # Маскуємо телефони, але залишаємо номери ТТН (2045..., 5900..., 100...) для перевірки статусу
    def phone_replacer(match):
        val = match.group(0).replace(' ', '').replace('-', '')
        if len(val) >= 11 and (val.startswith('204') or val.startswith('590') or val.startswith('100')):
            return match.group(0)
        return '[телефон приховано]'
    
    safe_text = PHONE.sub(phone_replacer, safe_text)
    safe_text = EMAIL.sub('[email приховано]', safe_text)
    safe_text = POST_OFFICE.sub('[відділення приховано]', safe_text)
    safe_text = ADDRESS.sub('[адресу приховано]', safe_text)
    
    return safe_text


def normalize_phone(value):
    """
    Валідація та нормалізація українського номера телефону.
    Формат на виході: +380XXXXXXXXX
    """
    if re.search(r'[^\d\s+().-]', str(value)):
        raise ValueError('Вкажіть український номер телефону')
        
    digits = re.sub(r'\D', '', str(value))
    
    # Якщо клієнт ввів 9 цифр без нуля (напр. 671234567), додаємо його автоматично
    if len(digits) == 9:
        digits = '0' + digits
        
    if len(digits) == 12 and digits.startswith('380'):
        digits = digits[2:]
        
    if len(digits) != 10 or not digits.startswith('0'):
        raise ValueError('Вкажіть український номер: +380… або 0…')
        
    return '+38' + digits
