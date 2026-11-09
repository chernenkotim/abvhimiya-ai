"""Best-effort minimization. Not a guarantee to detect every personal detail."""
import re

PHONE = re.compile(r'(?<!\w)(?:\+?38[\s().-]*)?0(?:[\s().-]*\d){9}(?!\d)')
EMAIL = re.compile(r'\b[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-яІіЇїЄє]{2,}\b')
CARD = re.compile(r'(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)')
ADDRESS = re.compile(r'\b(?:вул\.?|вулиця|просп\.?|проспект|провулок|адреса)\s*[:.]?\s+[^\n;]{1,160}', re.I)

def redact(text):
    text = CARD.sub('[платіжні дані приховано]', str(text))
    text = PHONE.sub('[телефон приховано]', text)
    text = EMAIL.sub('[email приховано]', text)
    return ADDRESS.sub('[адресу приховано]', text)

def normalize_phone(value):
    if re.search(r'[^\d\s+().-]', value):
        raise ValueError('Вкажіть український номер телефону')
    digits = re.sub(r'\D', '', value)
    if len(digits) == 12 and digits.startswith('380'):
        digits = digits[2:]
    if len(digits) != 10 or not digits.startswith('0'):
        raise ValueError('Вкажіть український номер: +380… або 0…')
    return '+38' + digits
