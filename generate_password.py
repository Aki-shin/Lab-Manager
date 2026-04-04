#!/usr/bin/env python3
"""Генерирует хеш пароля для .env файла."""
import getpass
from werkzeug.security import generate_password_hash

password = getpass.getpass("Введите пароль: ")
password_hash = generate_password_hash(password)
print(f"\nДобавьте в .env файл:")
print(f"ADMIN_PASSWORD={password_hash}")
